# SPEC: agent_auto_assignment

> **Scaffold status:** Partially filled. Sections 4/6/7 still carry `_TBD_` rows inherited from the
> scaffold. Section 3.1 (observability contract) and the two `trace_*` / `audit_*` dimensions in
> section 5 are authoritative and were filled for the v0.8.0 live-trace change.

## 1. Purpose

`agent_auto_assignment` is the auto-mapping agent: given ontology entities/relationships plus Unity
Catalog schema context, it generates and validates the SQL query and column assignments for each
item, then records them through `submit_entity_mapping` / `submit_relationship_mapping`. It runs as
one serial agentic loop over chunks of items, driven by
`Mapping.run_auto_assign_task`.

> The icon/layout wording previously in this section described `agent_auto_icon_assign`, not this
> agent. Sections 5–7 still carry the icon/layout dimensions from that scaffold; they are left in
> place (with `thresholds.yaml`) so this change does not silently re-baseline an unrelated gate.

## 2. Identity

| Field | Value |
|---|---|
| `agent_name` | `agent_auto_assignment` |
| `module_path` | `src/agents/agent_auto_assignment/` |
| `model_endpoint` | Configured per workspace; this transport regression targets `databricks-gpt-6-astra` |
| `temperature` | `0.0` (deterministic ground truth) |
| `mlflow_experiment` | `/Shared/ontobricks/agents/auto_assignment` |

## 3. Tool surface

| Tool name | Input schema | Output type | Purpose |
|---|---|---|---|
| `get_ontology` | `{}` | JSON ontology scope plus existing mapping context | Return every pending, non-excluded attribute and relationship in the current chunk. |
| `get_metadata` | `{}` | JSON table schemas | Return source tables and columns available to mapping SQL. |
| `get_documents_context` | `{}` | JSON document excerpts | Supply additional domain semantics. |
| `execute_sql` | `{sql}` | JSON columns and sample rows | Validate candidate row-returning SQL. |
| `submit_entity_mapping` | class URI/name, SQL, ID/label columns, attribute maps, optional unmapped attributes | JSON result | Record a projection-safe entity mapping while preserving existing assignments. |
| `submit_relationship_mapping` | property URI/name, SQL, source/target columns, domain/range/direction | JSON result | Record a validated relationship mapping. |

The v0.8.0 live-trace change adds **no tool**. It only makes the existing loop observable.

### 3.1 Observability contract (v0.8.0 — authoritative)

Every emitted step is an `AgentStep` (`src/agents/engine_base.py`) serialized by
`serialize_agent_steps` (`src/agents/serialization.py`) into exactly four keys:

```json
{"type": "tool_call | tool_result | output", "tool": "<tool name or empty>", "content": "<text>", "ms": 0}
```

**Live surface.** `Mapping.run_auto_assign_task` republishes the cumulative serialized list on
`task.result["agent_steps"]` after each chunk. `Task.to_dict()` already serializes `result` while
the task is `running`, so the existing 1.5 s poll in `mapping-autoassign.js` renders it with no new
route. Steps are append-only within a run: a step already published never changes index or content.

**Cancellation.** `run_auto_assign_task` checks `TaskManager.is_cancelled(task.id)` at the top of
each chunk. On cancel it stops issuing new LLM calls, persists whatever mappings completed, writes
the audit report with `status="cancelled"`, and leaves no partially written `assignment`.

**Durable surface (audit report).** On every terminal outcome the run appends one change-audit event
to the session buffer (`domain_data.change_log`), flushed to `domain_change_events` by the existing
save-to-registry path:

| Field | Value |
|---|---|
| `action` | `agent_auto_map_run` |
| `source` | `agent` |
| `entity_type` | `agent_run` |
| `entity_ref` | task id |
| `summary` | human-readable counts + status |
| `meta.status` | `completed` \| `failed` \| `cancelled` |
| `meta.task_id` | task id |
| `meta.duration_ms` | wall-clock run duration |
| `meta.stats` | `{entities, relationships, failed, chunk_errors}` |
| `meta.steps` | the same list published on `task.result["agent_steps"]` |

`meta.steps` must be byte-identical to the last live payload for the same run: one report, two
surfaces. Audit writes are best-effort — a failed audit append must never fail the mapping save.

## 4. Success criteria

1. Every requested ontology entity and relationship is either submitted or reported unresolved.
2. Every pending, non-excluded entity attribute is visible to the agent, including attributes after position 30.
3. Reassignment preserves prior attribute mappings and exclusions while adding new assignments.
4. A merged attribute map is persisted only when the final SQL projection exposes every mapped output alias.
5. Task and audit statistics report requested, mapped, and remaining attributes without counting pre-existing mappings as new work.

## 5. Eval dimensions

| Dimension | Metric | Threshold | Weight | Judge |
|---|---|---|---|---|
| `icon_exact_match` | exact icon ID match against gold-standard | `0.92` | `0.40` | rule-based |
| `layout_no_overlap` | proportion of pairwise non-overlapping bounding boxes | `0.98` | `0.20` | rule-based |
| `f1_class_coverage` | F1 over assignments vs gold | `0.95` | `0.20` | rule-based |
| `latency_p95` | seconds | `<= 4.0` | `0.10` | wall-clock |
| `cost_per_call` | USD | `<= 0.01` | `0.10` | MLflow usage |
| `trace_step_order` | streamed step sequence matches the golden order, append-only | `1.00` | n/a | rule-based |
| `audit_report_parity` | `meta.steps` equals the last published `task.result["agent_steps"]` | `1.00` | n/a | rule-based |
| `pending_attribute_visibility` | pending attributes returned / pending attributes requested | `1.00` | n/a | rule-based |
| `incremental_mapping_safety` | retained mapped aliases present in final SQL projection | `1.00` | n/a | rule-based |
| `attribute_coverage_reporting` | reported requested/mapped/remaining counts equal ground truth | `1.00` | n/a | rule-based |

**Aggregate threshold:** ≥ `0.90`.

The two `trace_*` / `audit_*` dimensions are contract checks on the observability change, not model
quality. They are pass/fail (`1.00`) and carry no aggregate weight, so they cannot mask a regression
on the mapping-quality dimensions above.

## 6. Failure modes

| Symptom | Detection | Mitigation |
|---|---|---|
| Assigns same icon to two semantically different classes | `icon_exact_match` drops below 0.85 on a tag-specific subset | tighter system prompt; add tag-stratified examples |
| Overlapping bounding boxes | `layout_no_overlap` < 0.95 | post-hoc layout adjustment in code, not in the prompt |
| Live overlay stays empty during a long run | `trace_step_order` finds no `tool_call` before the terminal event | republish `task.result["agent_steps"]` per chunk, not only at completion |
| Audit trail shows mapping chips but no agent report | `audit_report_parity` = 0 (no `agent_auto_map_run` row) | append the event on all three terminal paths, not just success |
| Cancel leaves the agent running and the report missing | cancel case in `observability.jsonl` ends without a `cancelled` report | `is_cancelled` check at the top of each chunk |
| Astra rejects mapping tools on Chat Completions | HTTP 400 mentions function tools with `reasoning_effort`; the mapping task fails before inference | Route Astra tool calls and their remaining conversation history through `/serving-endpoints/responses`, preserve reasoning output items across iterations, and adapt the result to the agent's existing Chat Completions contract |
| Wide entities remain at low attribute coverage after a successful retry | `pending_attribute_visibility` is below 1.00 or an ontology response contains a truncation note | Remove the positional cap and return only pending, non-excluded attributes. |
| Reassignment reduces or corrupts prior coverage | `incremental_mapping_safety` fails or the saved mapping loses existing keys | Merge mappings and reject submissions whose SQL drops retained output aliases. |
| A no-op retry reports all entities mapped | `attribute_coverage_reporting` differs from the requested attribute set | Track current-run submissions and report attribute-level coverage. |

## 7. Eval dataset

- **Baseline:** `tests/eval/datasets/agent_auto_assignment/baseline.jsonl` — ≥ 20 examples covering small, medium, and large ontologies.
- **Observability:** `tests/eval/datasets/agent_auto_assignment/observability.jsonl` — golden step/report sequences for the v0.8.0 live-trace change (happy, error, cancel). Mirrored at `.planning/agents/agent_auto_assignment/eval/dataset.jsonl`.
- **Regression:** `tests/eval/datasets/agent_auto_assignment/regression.jsonl`.

## 8. MLflow tracing

`@trace_agent`, `@trace_tool`.

## 9. Plan reference

`.planning/agent_auto_assignment-spec/PLAN.md` (to create at M2.P4).

## 10. Sign-off

- [ ] Author has filled sections 4, 5, 6, 7.
- [ ] Baseline eval run URI pasted into PR body.
- [ ] Aggregate threshold ≥ declared value in §5.
- [ ] Reviewer waiver (if applicable): _____
