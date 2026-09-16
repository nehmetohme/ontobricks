# Hotfix: preserve ontology generation checkpoints

## Purpose

Prevent a valid Turtle response from being lost when a later consolidation,
quality-repair, evaluator, or transport step fails.

## Target users

Ontology designers generating a domain ontology from the OntoBricks UI,
especially large schemas using `databricks-gpt-6-astra`.

## Input and output

- Input: successive LLM responses produced during one ontology-generation task.
- Output: the newest parseable Turtle candidate, automatically used as a
  recovery result when a later refinement fails.
- Persistence: one bounded checkpoint in the current server-side session,
  retrievable through the ontology wizard API.

## Success criteria

1. A parseable 102-class candidate followed by a zero-class consolidation
   response returns the 102-class checkpoint instead of failing.
2. A parseable candidate followed by a consolidation timeout returns the
   checkpoint instead of requiring a new generation.
3. A failed or interrupted UI task can retrieve and apply the session checkpoint.

## Failure modes

- Invalid candidates must never replace a valid checkpoint.
- Checkpoint persistence failures must not fail the model run.
- Session checkpoint writes must validate the session identifier and retain
  only the newest candidate.
- Recovery must be clearly identified to the UI as a fallback result.

## Implementation plan

1. Add regression cases and failing unit/UI contract tests.
2. Validate and checkpoint parseable Turtle candidates inside the OWL agent.
3. Persist the newest checkpoint in the current server-side session and expose
   a read endpoint.
4. Recover from the checkpoint in the task worker and wizard UI.
5. Run targeted tests, the routine non-scenario suite, and update the changelog.
