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
