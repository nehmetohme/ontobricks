# Fix Astra mapping tool calls

## Purpose

Keep the workspace-configured `databricks-gpt-6-astra` model while allowing the Mapping UI to execute its required structured function tools.

## Contract

- Input: the existing Chat Completions-style history and function definitions accepted by `call_serving_endpoint`.
- Output: the existing Chat Completions-style response consumed by mapping agents, with Astra Responses output items retained for subsequent tool iterations.
- Transport: Astra requests with tools and their remaining conversation history use the workspace Responses API; unrelated requests retain their current transport.

## Success criteria

1. An initial Astra mapping request returns a function call instead of HTTP 400.
2. Tool results, call IDs, and Astra reasoning output survive subsequent iterations.
3. Non-Astra endpoints and Astra requests without tools remain unchanged.

## Failure modes

- Passing nested Chat Completions tool definitions unchanged causes Responses API validation errors.
- Losing a function call ID or its reasoning item breaks the following tool-result request.
- Applying the adapter to other endpoints changes established behavior unnecessarily.

## Plan

1. Add three transport regression cases and deterministic unit tests.
2. Add a narrow Astra-with-tools Responses API adapter in `agents.engine_base`.
3. Run the focused mapping suite and the complete non-scenario suite.
4. Record the implementation and verification in the v0.8.0 changelog.

## Evaluation note

The repository has no runnable `agent_auto_assignment` MLflow eval harness. This production hotfix therefore uses the required three regression cases plus deterministic unit tests; no PR is being created.

## Incremental attribute reassignment hotfix

### Purpose

Allow Mapping users to reassign every still-missing ontology attribute without losing valid entity mappings that already exist.

### Contract

- `get_ontology` returns every requested attribute that is neither mapped nor excluded; it does not silently cap wide entities.
- For partially mapped entities, the tool also returns the existing SQL and column assignments so the agent can extend the projection.
- `submit_entity_mapping` merges new attribute assignments into the existing mapping only when the submitted SQL exposes every retained output column.
- Batch completion reports attributes requested, mapped, and remaining in addition to entity and relationship counts.

### Success criteria

1. An entity with more than 30 pending attributes exposes the complete pending set to the agent.
2. Reassignment extends an existing mapping and preserves its ID, label, exclusions, and mapped attributes.
3. SQL that drops an existing mapped output column is rejected without mutating the valid mapping.
4. A run cannot report complete attribute coverage when requested attributes remain unresolved.

### Failure modes

- Reapplying the historical 30-attribute slice makes wide entities permanently incomplete.
- Replacing a partial mapping discards attributes from earlier runs.
- Blindly unioning attribute maps with a narrower SQL projection creates mappings that fail during Knowledge Graph sync.
- Counting pre-existing entity mappings as work completed makes a no-op reassignment look successful.

### Implementation plan

1. Add wide-entity, incremental-merge, projection-safety, and coverage regression tests.
2. Return all pending attributes and existing mapping context from `get_ontology`.
3. Make entity submission incremental and projection-safe, and track mappings submitted in the current run.
4. Send only missing attributes from the UI and surface attribute-level task statistics.
5. Run focused tests, Ruff, and the complete non-scenario suite; record the results in the v0.8.0 changelog.
