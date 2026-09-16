"""
Shared infrastructure for OntoBricks agent engines.

Provides the common ``AgentStep`` dataclass and reusable helpers for LLM
serving-endpoint calls, tool dispatch, response content extraction, and
token usage accumulation.  Each concrete agent engine imports what it needs
and focuses exclusively on its own ``AgentResult``, system prompt, and
``run_agent`` loop.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests

from agents.llm_utils import call_llm_with_retry
from agents.tracing import trace_llm
from back.core.logging import get_logger

logger = get_logger(__name__)

# Endpoints (e.g. databricks-claude-opus-4-7) sometimes reject optional
# OpenAI-style parameters with a 400 message like:
#   "Model ... does not support the temperature parameter."
# We cache such bans per (endpoint, param) pair so subsequent calls skip the
# offending field proactively instead of re-discovering the 400 every time.
_UNSUPPORTED_PARAMS: dict[str, set] = {}

# Astra's Chat Completions compatibility constraints differ from the generic
# OpenAI-style payload: direct requests support reasoning_effort="low", while
# function tools require the Responses API and cannot use this transport.
# Temperature must be omitted so the endpoint can use its supported default.
_ASTRA_CHAT_ENDPOINTS = {"databricks-gpt-6-astra"}


def _unsupported_params(endpoint_name: str) -> set:
    return _UNSUPPORTED_PARAMS.setdefault(endpoint_name, set())


def _looks_unsupported(body_text: str, param: str) -> bool:
    low = (body_text or "").lower()
    return f"does not support the {param} parameter" in low or (
        "unsupported" in low and param in low
    )


def supports_chat_completion_tools(endpoint_name: str) -> bool:
    """Return whether *endpoint_name* supports tools on Chat Completions."""
    return endpoint_name not in _ASTRA_CHAT_ENDPOINTS


def is_unsupported_parameter_error(exc: requests.exceptions.HTTPError, param: str) -> bool:
    """Return whether an HTTP error specifically rejects *param*."""
    response = exc.response
    if response is None or response.status_code not in (400, 422):
        return False
    return _looks_unsupported(response.text, param)


def _responses_tool_definitions(tools: list[dict]) -> list[dict]:
    """Convert Chat Completions function definitions to Responses tools."""
    converted: list[dict] = []
    for tool in tools:
        function = tool.get("function", {})
        response_tool: dict[str, Any] = {
            "type": "function",
            "name": function.get("name", ""),
            "parameters": function.get("parameters", {"type": "object"}),
        }
        for optional_field in ("description", "strict"):
            if optional_field in function:
                response_tool[optional_field] = function[optional_field]
        converted.append(response_tool)
    return converted


def _responses_input(messages: list[dict]) -> tuple[str, list[dict]]:
    """Convert Chat Completions history to Responses API input items."""
    instructions: list[str] = []
    input_items: list[dict] = []

    for message in messages:
        role = message.get("role", "user")
        content = message.get("content")
        if role == "system":
            if content:
                instructions.append(str(content))
            continue
        if role == "tool":
            tool_output = content if isinstance(content, str) else json.dumps(content)
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id", ""),
                    "output": tool_output,
                }
            )
            continue

        response_output_items = message.get("_responses_output_items")
        if role == "assistant" and isinstance(response_output_items, list):
            input_items.extend(response_output_items)
            continue

        if content:
            input_items.append({"role": role, "content": content})
        if role == "assistant":
            for tool_call in message.get("tool_calls", []):
                function = tool_call.get("function", {})
                input_items.append(
                    {
                        "type": "function_call",
                        "call_id": tool_call.get("id", ""),
                        "name": function.get("name", ""),
                        "arguments": function.get("arguments", "{}"),
                    }
                )

    return "\n\n".join(instructions), input_items


def _chat_completion_from_response(response: dict[str, Any]) -> dict[str, Any]:
    """Adapt a Responses API result to the agent engines' chat contract."""
    content_parts: list[str] = []
    tool_calls: list[dict] = []

    for item in response.get("output", []):
        item_type = item.get("type")
        if item_type == "function_call":
            call_id = item.get("call_id") or item.get("id", "")
            tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "{}"),
                    },
                }
            )
        elif item_type == "message":
            item_content = item.get("content", [])
            if isinstance(item_content, str):
                content_parts.append(item_content)
                continue
            for part in item_content:
                if part.get("type") in ("output_text", "text") and part.get("text"):
                    content_parts.append(part["text"])

    incomplete_reason = (response.get("incomplete_details") or {}).get("reason")
    finish_reason = (
        "length"
        if incomplete_reason == "max_output_tokens"
        else ("tool_calls" if tool_calls else "stop")
    )
    usage = response.get("usage", {})
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "\n".join(content_parts) or None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    message["_responses_output_items"] = response.get("output", [])

    return {
        "id": response.get("id"),
        "model": response.get("model"),
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": message,
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


# =====================================================
# Shared data class
# =====================================================


@dataclass
class AgentStep:
    """One observable step of the agent's execution."""

    step_type: str  # tool_call | tool_result | output
    content: str
    tool_name: str = ""
    duration_ms: int = 0


# =====================================================
# LLM call helper
# =====================================================


@trace_llm("agent:llm")
def call_serving_endpoint(
    host: str,
    token: str,
    endpoint_name: str,
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.1,
    timeout: int = 180,
    trace_name: str = "agent:llm",
) -> dict:
    """Call a Databricks serving endpoint (OpenAI-compatible chat completions).

    Builds the URL, headers, and payload, then delegates to
    :func:`call_llm_with_retry` for retry/backoff logic.

    Args:
        trace_name: Used for MLflow span naming via ``@trace_llm``.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    banned = _unsupported_params(endpoint_name)
    is_astra_chat = endpoint_name in _ASTRA_CHAT_ENDPOINTS
    has_responses_history = any("_responses_output_items" in message for message in messages)
    if is_astra_chat and (tools or has_responses_history):
        instructions, response_input = _responses_input(messages)
        responses_payload: dict[str, Any] = {
            "model": endpoint_name,
            "input": response_input,
            "reasoning": {"effort": "low"},
            "max_output_tokens": max_tokens,
        }
        if tools:
            responses_payload["tools"] = _responses_tool_definitions(tools)
        if instructions:
            responses_payload["instructions"] = instructions
        responses_url = f"{host.rstrip('/')}/serving-endpoints/responses"
        logger.info(
            "%s: POST %s via Responses API — %d input items, %d tool defs, max_output_tokens=%d",
            trace_name,
            endpoint_name,
            len(response_input),
            len(tools) if tools else 0,
            max_tokens,
        )
        response = call_llm_with_retry(
            responses_url,
            headers,
            responses_payload,
            timeout=timeout,
        )
        return _chat_completion_from_response(response.json())

    url = f"{host.rstrip('/')}/serving-endpoints/{endpoint_name}/invocations"
    payload: dict[str, Any] = {
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if not is_astra_chat and "temperature" not in banned and temperature is not None:
        payload["temperature"] = temperature
    if tools:
        payload["tools"] = tools
    elif is_astra_chat and "reasoning_effort" not in banned:
        payload["reasoning_effort"] = "low"

    logger.info(
        "%s: POST %s — %d messages, %d tool defs, max_tokens=%d, temperature=%s",
        trace_name,
        endpoint_name,
        len(messages),
        len(tools) if tools else 0,
        max_tokens,
        payload.get("temperature", "<skipped>"),
    )

    try:
        resp = call_llm_with_retry(url, headers, payload, timeout=timeout)
        return resp.json()
    except requests.exceptions.HTTPError as exc:
        response = exc.response
        status = response.status_code if response is not None else None
        if status != 400:
            raise
        body_text = response.text if response is not None else ""
        # Detect and strip parameters the model rejects, then retry once.
        dropped: list[str] = []
        for param in ("temperature", "reasoning_effort"):
            if param in payload and _looks_unsupported(body_text, param):
                banned.add(param)
                payload.pop(param, None)
                dropped.append(param)
        if not dropped:
            raise
        logger.warning(
            "%s: endpoint rejected unsupported param(s) %s — retrying without them",
            trace_name,
            dropped,
        )
        resp = call_llm_with_retry(url, headers, payload, timeout=timeout)
        return resp.json()


# =====================================================
# Tool dispatch helper
# =====================================================


def dispatch_tool(
    handlers: dict[str, Callable],
    ctx: Any,
    tool_name: str,
    arguments: dict,
    *,
    trace_name: str = "agent:tool",
) -> str:
    """Dispatch a tool call and return the JSON result string.

    Handles unknown tools and exceptions uniformly across agents.
    """
    handler = handlers.get(tool_name)
    if not handler:
        logger.warning(
            "%s: unknown tool '%s' — available: %s",
            trace_name,
            tool_name,
            list(handlers.keys()),
        )
        return json.dumps({"error": f"Unknown tool: {tool_name}"})
    try:
        t0 = time.time()
        result = handler(ctx, **arguments)
        elapsed = int((time.time() - t0) * 1000)
        logger.info(
            "%s: '%s' completed in %dms, returned %d chars",
            trace_name,
            tool_name,
            elapsed,
            len(result),
        )
        return result
    except Exception as exc:
        logger.exception("%s: '%s' raised exception: %s", trace_name, tool_name, exc)
        return json.dumps({"error": f"Tool execution failed: {exc}"})


# =====================================================
# Response content extraction
# =====================================================


def extract_message_content(llm_response: dict) -> str:
    """Extract text content from an OpenAI-style or predictions-style LLM response."""
    choices = llm_response.get("choices", [])
    if choices:
        content = choices[0].get("message", {}).get("content") or ""
        # Claude endpoints return content as a list of blocks, not a string
        if isinstance(content, list):
            content = "".join(b if isinstance(b, str) else b.get("text", "") for b in content)
        return content
    preds = llm_response.get("predictions", [])
    if preds:
        return preds[0] if isinstance(preds[0], str) else str(preds[0])
    logger.warning(
        "extract_message_content: no choices or predictions, keys=%s",
        list(llm_response.keys()),
    )
    return ""


# =====================================================
# Token usage accumulation
# =====================================================


def accumulate_usage(total: dict[str, int], usage_block: dict) -> None:
    """Add prompt/completion token counts from *usage_block* into *total* in-place."""
    total["prompt_tokens"] = total.get("prompt_tokens", 0) + usage_block.get("prompt_tokens", 0)
    total["completion_tokens"] = total.get("completion_tokens", 0) + usage_block.get(
        "completion_tokens", 0
    )
