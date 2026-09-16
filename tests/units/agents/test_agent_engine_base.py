"""Tests for agents.engine_base – shared agent infrastructure."""

import json
from dataclasses import asdict
from unittest.mock import MagicMock, patch

import requests

from agents.engine_base import (
    AgentStep,
    accumulate_usage,
    call_serving_endpoint,
    dispatch_tool,
    extract_message_content,
)


class TestAgentStep:
    def test_defaults(self):
        step = AgentStep(step_type="output", content="hello")
        assert step.step_type == "output"
        assert step.content == "hello"
        assert step.tool_name == ""
        assert step.duration_ms == 0

    def test_tool_call_step(self):
        step = AgentStep(
            step_type="tool_call", content="result", tool_name="get_ontology", duration_ms=42
        )
        assert step.tool_name == "get_ontology"
        assert step.duration_ms == 42

    def test_is_dataclass(self):
        step = AgentStep(step_type="output", content="x")
        d = asdict(step)
        assert d == {"step_type": "output", "content": "x", "tool_name": "", "duration_ms": 0}


class TestCallServingEndpoint:
    @patch("agents.engine_base.call_llm_with_retry")
    def test_builds_url_and_calls(self, mock_retry):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"choices": [{"message": {"content": "hi"}}]}
        mock_retry.return_value = mock_resp

        result = call_serving_endpoint(
            "https://host.databricks.com",
            "tok",
            "my-endpoint",
            [{"role": "user", "content": "hello"}],
        )

        mock_retry.assert_called_once()
        call_args = mock_retry.call_args
        assert "my-endpoint/invocations" in call_args[0][0]
        assert call_args[0][1]["Authorization"] == "Bearer tok"
        assert result == {"choices": [{"message": {"content": "hi"}}]}

    @patch("agents.engine_base.call_llm_with_retry")
    def test_includes_tools_when_provided(self, mock_retry):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        mock_retry.return_value = mock_resp

        tools = [{"type": "function", "function": {"name": "get_data"}}]
        call_serving_endpoint(
            "https://host.databricks.com/",
            "tok",
            "ep",
            [],
            tools=tools,
        )

        payload = mock_retry.call_args[0][2]
        assert payload["tools"] == tools

    @patch("agents.engine_base.call_llm_with_retry")
    def test_no_tools_key_when_none(self, mock_retry):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        mock_retry.return_value = mock_resp

        call_serving_endpoint("https://h", "t", "ep", [])
        payload = mock_retry.call_args[0][2]
        assert "tools" not in payload

    @patch("agents.engine_base.call_llm_with_retry")
    def test_astra_direct_call_uses_supported_chat_parameters(self, mock_retry):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        mock_retry.return_value = mock_resp

        call_serving_endpoint(
            "https://host.databricks.com",
            "tok",
            "databricks-gpt-6-astra",
            [],
            temperature=0.1,
        )

        payload = mock_retry.call_args[0][2]
        assert payload["reasoning_effort"] == "low"
        assert "temperature" not in payload
        assert "tools" not in payload

    @patch("agents.engine_base.call_llm_with_retry")
    def test_astra_tool_call_uses_responses_api(self, mock_retry):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "completed", "output": []}
        mock_retry.return_value = mock_resp

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "submit_mapping",
                    "description": "Submit a mapping.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        call_serving_endpoint(
            "https://host.databricks.com",
            "tok",
            "databricks-gpt-6-astra",
            [
                {"role": "system", "content": "Map the entity."},
                {"role": "user", "content": "Map Address."},
            ],
            tools=tools,
        )

        url, _, payload = mock_retry.call_args[0]
        assert url == "https://host.databricks.com/serving-endpoints/responses"
        assert payload["model"] == "databricks-gpt-6-astra"
        assert payload["instructions"] == "Map the entity."
        assert payload["input"] == [{"role": "user", "content": "Map Address."}]
        assert payload["tools"][0]["name"] == "submit_mapping"
        assert payload["reasoning"] == {"effort": "low"}

    @patch("agents.engine_base.call_llm_with_retry")
    def test_astra_function_call_is_adapted_to_chat_completion(self, mock_retry):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "completed",
            "output": [
                {
                    "type": "reasoning",
                    "id": "rs_123",
                    "summary": [],
                },
                {
                    "type": "function_call",
                    "call_id": "call_123",
                    "name": "submit_mapping",
                    "arguments": '{"class_name":"Address"}',
                },
            ],
            "usage": {
                "input_tokens": 20,
                "output_tokens": 10,
                "total_tokens": 30,
            },
        }
        mock_retry.return_value = mock_resp

        result = call_serving_endpoint(
            "https://host.databricks.com",
            "tok",
            "databricks-gpt-6-astra",
            [{"role": "user", "content": "Map Address."}],
            tools=[{"type": "function", "function": {"name": "submit_mapping"}}],
        )

        choice = result["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        assert choice["message"]["tool_calls"] == [
            {
                "id": "call_123",
                "type": "function",
                "function": {
                    "name": "submit_mapping",
                    "arguments": '{"class_name":"Address"}',
                },
            }
        ]
        assert choice["message"]["_responses_output_items"][0] == {
            "type": "reasoning",
            "id": "rs_123",
            "summary": [],
        }
        assert result["usage"] == {
            "prompt_tokens": 20,
            "completion_tokens": 10,
            "total_tokens": 30,
        }

    @patch("agents.engine_base.call_llm_with_retry")
    def test_astra_responses_history_stays_on_responses_api_without_tools(self, mock_retry):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "completed", "output": []}
        mock_retry.return_value = mock_resp

        call_serving_endpoint(
            "https://host.databricks.com",
            "tok",
            "databricks-gpt-6-astra",
            [
                {"role": "user", "content": "Map Address."},
                {
                    "role": "assistant",
                    "content": None,
                    "_responses_output_items": [
                        {
                            "type": "reasoning",
                            "id": "rs_123",
                            "summary": [],
                        },
                        {
                            "type": "function_call",
                            "call_id": "call_123",
                            "name": "submit_mapping",
                            "arguments": '{"class_name":"Address"}',
                        },
                    ],
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": "submit_mapping",
                                "arguments": '{"class_name":"Address"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_123",
                    "content": '{"success":true}',
                },
            ],
        )

        url, _, payload = mock_retry.call_args[0]
        assert url == "https://host.databricks.com/serving-endpoints/responses"
        assert "tools" not in payload
        assert payload["input"][1] == {
            "type": "reasoning",
            "id": "rs_123",
            "summary": [],
        }
        assert payload["input"][2] == {
            "type": "function_call",
            "call_id": "call_123",
            "name": "submit_mapping",
            "arguments": '{"class_name":"Address"}',
        }
        assert payload["input"][3] == {
            "type": "function_call_output",
            "call_id": "call_123",
            "output": '{"success":true}',
        }

    @patch("agents.engine_base.call_llm_with_retry")
    def test_astra_retries_without_reasoning_when_endpoint_rejects_it(self, mock_retry):
        response = requests.Response()
        response.status_code = 400
        response._content = b'{"error":{"message":"Unsupported reasoning_effort parameter"}}'
        error = requests.exceptions.HTTPError(response=response)
        success = MagicMock()
        success.json.return_value = {"choices": []}
        payloads = []

        def respond(_url, _headers, payload, timeout):
            payloads.append(payload.copy())
            if len(payloads) == 1:
                raise error
            return success

        mock_retry.side_effect = respond

        call_serving_endpoint(
            "https://host.databricks.com",
            "tok",
            "databricks-gpt-6-astra",
            [],
        )

        assert ["reasoning_effort" in payload for payload in payloads] == [
            True,
            False,
        ]

    @patch("agents.engine_base.call_llm_with_retry")
    def test_strips_trailing_slash(self, mock_retry):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        mock_retry.return_value = mock_resp

        call_serving_endpoint("https://host.com/", "t", "ep", [])
        url = mock_retry.call_args[0][0]
        assert "//serving" not in url


class TestDispatchTool:
    def test_known_tool(self):
        ctx = MagicMock()
        handlers = {"my_tool": lambda c, **kw: json.dumps({"ok": True})}
        result = dispatch_tool(handlers, ctx, "my_tool", {})
        assert json.loads(result) == {"ok": True}

    def test_unknown_tool(self):
        result = dispatch_tool({}, MagicMock(), "missing_tool", {})
        parsed = json.loads(result)
        assert "error" in parsed
        assert "Unknown tool" in parsed["error"]

    def test_exception_in_handler(self):
        def bad_handler(ctx, **kw):
            raise RuntimeError("boom")

        result = dispatch_tool({"bad": bad_handler}, MagicMock(), "bad", {})
        parsed = json.loads(result)
        assert "error" in parsed
        assert "boom" in parsed["error"]

    def test_passes_kwargs(self):
        def echo_handler(ctx, **kwargs):
            return json.dumps(kwargs)

        result = dispatch_tool({"echo": echo_handler}, MagicMock(), "echo", {"a": 1, "b": "two"})
        assert json.loads(result) == {"a": 1, "b": "two"}


class TestExtractMessageContent:
    def test_openai_format(self):
        resp = {"choices": [{"message": {"content": "Hello world"}}]}
        assert extract_message_content(resp) == "Hello world"

    def test_predictions_format_string(self):
        resp = {"predictions": ["predicted text"]}
        assert extract_message_content(resp) == "predicted text"

    def test_predictions_format_non_string(self):
        resp = {"predictions": [42]}
        assert extract_message_content(resp) == "42"

    def test_empty_choices(self):
        assert extract_message_content({"choices": []}) == ""

    def test_no_content_key(self):
        resp = {"choices": [{"message": {}}]}
        assert extract_message_content(resp) == ""

    def test_unknown_format(self):
        assert extract_message_content({"unknown": 1}) == ""

    def test_none_content(self):
        resp = {"choices": [{"message": {"content": None}}]}
        assert extract_message_content(resp) == ""


class TestAccumulateUsage:
    def test_from_empty(self):
        total = {}
        accumulate_usage(total, {"prompt_tokens": 10, "completion_tokens": 5})
        assert total == {"prompt_tokens": 10, "completion_tokens": 5}

    def test_accumulates(self):
        total = {"prompt_tokens": 10, "completion_tokens": 5}
        accumulate_usage(total, {"prompt_tokens": 20, "completion_tokens": 15})
        assert total == {"prompt_tokens": 30, "completion_tokens": 20}

    def test_missing_keys(self):
        total = {"prompt_tokens": 10}
        accumulate_usage(total, {})
        assert total["prompt_tokens"] == 10
        assert total["completion_tokens"] == 0

    def test_empty_usage_block(self):
        total = {}
        accumulate_usage(total, {})
        assert total == {"prompt_tokens": 0, "completion_tokens": 0}
