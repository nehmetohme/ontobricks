# SPEC: agent_owl_generator

> **Status:** Active. Hotfix regression coverage is maintained in
> `tests/eval/datasets/agent_owl_generator/regression.jsonl`.
> Required by `.cursor/12-ai-feature-lifecycle.mdc`.

## 1. Purpose

`agent_owl_generator` auto-designs an OWL ontology from UC metadata. Given a catalog/schema/table set, it proposes classes, properties, and relationships in a single LLM-driven step, returning a structure that conforms to the OntoBricks ontology JSON format consumed by `back/objects/ontology/OntologyService`.

## 2. Identity

| Field | Value |
|---|---|
| `agent_name` | `agent_owl_generator` |
| `module_path` | `src/agents/agent_owl_generator/` |
| `model_endpoint` | Configured per domain; Ecommerce v1 uses `databricks-gpt-6-astra` |
| `temperature` | `0.0` (for eval) |
| `max_tokens` | Endpoint-specific: `128000` for `databricks-gpt-6-astra`; `8192` fallback |
| `astra_transport` | Chat Completions direct-context mode; `reasoning_effort=low`, no function tools |
| `max_owl_eval_rounds` | `2` (`MAX_OWL_EVAL_ROUNDS`; Stage-1 PGE evaluator retry cap) |
| `max_classes` | `40` (`_DEFAULT_MAX_CLASSES`; over-generation guard — accepted ontology is asked to consolidate above this. Overridable via `options["max_classes"]`, `<=0` disables) |
| `mlflow_experiment` | `/Shared/ontobricks/agents/owl_generator` |

## 3. Tool surface

| Tool name | Input schema | Output type | Purpose |
|---|---|---|---|
| `list_documents` | `{}` | JSON file list | Discover domain documents available in the registry volume. |
| `read_document` | `{"filename": "string"}` | JSON document text | Read a selected text or parsed binary document. |
| `get_metadata` | `{}` | JSON table summaries | Inspect the bounded set of loaded tables and columns. |
| `get_table_detail` | `{"table_name": "string"}` | JSON full table schema | Inspect every column for one selected table. |

These tools are used by compatible Chat Completions endpoints. Astra does not
support function tools through this transport, so the engine embeds the bounded
`get_metadata` result directly and asks Astra to generate Turtle in one step.

## 4. Success criteria

1. Given `customers` and `orders` with a customer foreign key, output valid
   Turtle containing `Customer`, `Order`, their meaningful datatype properties,
   and an object property connecting them.
2. Given several source-system tables representing the same business entity,
   consolidate them into one class and union their meaningful attributes without
   creating source-specific duplicate properties.
3. Given a large selected schema on Astra, complete without a Chat Completions
   tool request, ground the ontology in embedded metadata, and return at least one
   `owl:Class` declaration rather than prose or an empty result.

## 5. Eval dimensions

_To fill in M2.P4. Below is the proposed table; calibrate after baseline run._

| Dimension | Metric | Threshold | Weight | Judge |
|---|---|---|---|---|
| `schema_validity` | RDFLib `parse(serialize())` succeeds | `0.95` | `0.30` | rule-based |
| `class_coverage` | proportion of input tables mapped to a class | `0.80` | `0.20` | rule-based |
| `property_quality` | LLM-judge on property naming + domain/range correctness | `0.80` | `0.25` | `tests/eval/judges/owl_property_judge.py` (to build) |
| `latency_p95` | seconds | `<= 30.0` | `0.10` | wall-clock |
| `cost_per_call` | USD | `<= 0.05` | `0.15` | MLflow usage |

**Aggregate threshold:** ≥ `0.82` to pass G2 (proposed).

## 6. Failure modes

| Symptom | Detection | Mitigation |
|---|---|---|
| **Truncated ontology → empty result.** The final Turtle answer is cut off at the output-token cap (`finish_reason == "length"`); the salvaged remainder fails to parse in every RDF syntax, so `/ontology/parse-owl` lands 0 classes and the Generate wizard polls until timeout. | `OntologyParser` logs `Content appeared truncated`; `rdf_utils.parse_rdf_flexible` fails all formats; session saved with 0 classes. In tests: `finish_reason == "length"` on the text answer. | Use endpoint-specific request limits (`128000` tokens and 600 seconds for `databricks-gpt-6-astra`, validated against the live endpoint) plus the existing truncation guard. Models without a validated override retain the conservative `8192`/180-second fallback. Regression: `tests/eval/datasets/agent_owl_generator/regression.jsonl` + `tests/units/agents/test_agent_owl_generator_truncation.py`. |
| **Over-generation / class explosion.** The model over-decomposes — one class per column or per attribute value (e.g. `VatAmount`, `MeterReading`, `Payment`, `Call`) — emitting ~110 classes for a ~5-entity guideline. The ontology parses fine but downstream **auto-mapping** chunks ~5 classes/chunk with cool-downs, so ~22 chunks overrun the scenario `AUTOMAP_TIMEOUT` (600s) → "Auto-Map produced no entity SQL". | Auto-assign log shows `Chunk N/22` (vs the healthy `N/4`); accepted ontology `owl:Class` count ≫ input entity count. In tests: `_count_owl_classes(content) > max_classes`. | Prompt: replaced the "30–60 classes" size limit with "prefer 8–25, one class per real-world entity, never a class per column/value, hard limit 40". Guard: a class-count check in `engine.run_agent` asks the model (bounded by `_MAX_CONSOLIDATE_ROUNDS=2`) to consolidate above `max_classes` (default 40) before accepting. Regression: `tests/eval/datasets/agent_owl_generator/regression.jsonl` + `tests/units/agents/test_agent_owl_generator_class_cap.py`. |
| **Structural defects survive generation.** Orphan classes, dangling `rdfs:domain`/`rdfs:range`, naming violations, or duplicate classes pass the pitfall-tool loop but break registry import or downstream mapping. | `evaluate_ontology()` reports Tier-1 issues; `_evaluate_ontology_stage()` returns a retry hint. In tests: `tests/units/pge_eval/test_owl_evaluator_stage.py`. | Stage-1 PGE Evaluator after the pitfall loop: deterministic `agents.pge_eval.ontology_metrics.evaluate_ontology` feeds concrete retry hints back to the generator, bounded by `MAX_OWL_EVAL_ROUNDS=2`. Fails open on parse errors. Regression: `tests/eval/datasets/agent_owl_generator/regression.jsonl` + `tests/units/pge_eval/`. |
| **Astra rejects Chat Completions tools before inference.** A request with function tools and `reasoning_effort=none` returns HTTP 400; the generic handler previously mislabeled it as a tools rejection, retried without schema context, and surfaced a secondary “no valid Turtle” error. | Serving response identifies `reasoning_effort`; no `finish_reason` exists because inference never started. Unit coverage verifies Astra direct mode and parameter-specific HTTP 400 classification. | Use `reasoning_effort=low` without function tools, embed the bounded metadata result in the user context, and only disable tools when the error specifically rejects the `tools` parameter. Regression: cases 004–006 plus `test_agent_engine_base.py` and `test_agent_owl_generator_truncation.py`. |

## 7. Eval dataset

- **Baseline:** `tests/eval/datasets/agent_owl_generator/baseline.jsonl` (3 seed examples; expansion and a live judge harness remain planned).
- **Synthetic:** Use `databricks-synthetic-data-generation` against UC sample data.
- **Regression:** `tests/eval/datasets/agent_owl_generator/regression.jsonl` (6 production-derived cases, including 3 Astra transport/error-routing hotfix cases).

## 8. MLflow tracing

Existing: `@trace_agent` on the entry point in `src/agents/agent_owl_generator/`. Verify `@trace_tool` is on each tool handler.

## 9. Plan reference

`.planning/agent_owl_generator-spec/PLAN.md` (to create when the team picks this up — M2.P4).

## 10. Sign-off

- [ ] Author has filled sections 4, 5, 6, 7.
- [ ] Baseline eval run URI pasted into PR body.
- [ ] Aggregate threshold ≥ declared value in §5.
- [ ] Reviewer waiver (if applicable): _____
