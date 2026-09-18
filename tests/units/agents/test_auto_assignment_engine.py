"""Regression tests for incremental auto-assignment run accounting."""

from unittest.mock import MagicMock

import pytest

from agents.agent_auto_assignment import engine


def _text_response(content: str = "Done") -> dict:
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


@pytest.mark.unit
def test_existing_entity_is_not_counted_without_current_run_submission(monkeypatch):
    uri = "http://test.org/Customer"
    monkeypatch.setattr(
        engine,
        "call_serving_endpoint",
        lambda *_args, **_kwargs: _text_response(),
    )

    result = engine.run_agent(
        host="https://test.databricks.com",
        token="token",
        endpoint_name="databricks-gpt-6-astra",
        client=MagicMock(),
        metadata={},
        ontology={
            "entities": [
                {
                    "uri": uri,
                    "name": "Customer",
                    "attributes": ["firstName", "lastName"],
                }
            ],
            "relationships": [],
        },
        entity_mappings=[
            {
                "ontology_class": uri,
                "sql_query": "SELECT id AS ID, name AS Label, first_name FROM source",
                "attribute_mappings": {"firstName": "first_name"},
            }
        ],
        max_iterations=1,
    )

    assert result.success is False
    assert result.stats == {
        "total": 1,
        "entities": 0,
        "relationships": 0,
        "attributes_requested": 1,
        "attributes_mapped": 0,
        "attributes_remaining": 1,
    }


@pytest.mark.unit
def test_agent_uses_expanded_output_budget(monkeypatch):
    captured = {}

    def respond(*_args, **kwargs):
        captured["max_tokens"] = kwargs["max_tokens"]
        return _text_response()

    monkeypatch.setattr(engine, "call_serving_endpoint", respond)

    engine.run_agent(
        host="https://test.databricks.com",
        token="token",
        endpoint_name="databricks-gpt-6-astra",
        client=MagicMock(),
        metadata={},
        ontology={"entities": [], "relationships": []},
        max_iterations=1,
    )

    assert captured["max_tokens"] == 8192
