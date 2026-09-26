"""Synthetic OpenAI Responses payloads and an in-memory vault for adapter and CLI tests.

This folder deliberately has no __init__.py: as a package named "openai" it could shadow
the OpenAI SDK on sys.path. Import these helpers by their full dotted path.
"""

import json
from typing import Any

import httpx

RESPONSES_URL = "https://api.openai.com/v1/responses"
TEST_KEY = "sk-test-" + "a" * 32


class MemoryVault:
    """VaultBackend fake keeping secrets in a dict; deleting a missing entry raises."""

    def __init__(self, entries: dict[tuple[str, str], str] | None = None) -> None:
        self.entries = dict(entries or {})

    def get_password(self, service: str, username: str) -> str | None:
        return self.entries.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.entries[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.entries[(service, username)]


def usage(input_tokens: int = 1_200, output_tokens: int = 300) -> dict[str, Any]:
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": input_tokens + output_tokens,
    }


def output_text(text: str) -> dict[str, Any]:
    return {"type": "output_text", "text": text, "annotations": []}


def response_body(
    content: list[dict[str, Any]], *, status: str = "completed", with_usage: bool = True
) -> dict[str, Any]:
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 1_790_000_000,
        "model": "test-model",
        "status": status,
        "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
        "error": None,
        "output": [
            {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": content,
            }
        ],
        "usage": usage() if with_usage else None,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }


def wire_result(message_key: str, evidence: str, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "message_key": message_key,
        "category": "information",
        "summary": "A short summary.",
        "action_required": False,
        "action_text": None,
        "deadline_text": None,
        "deadline_date": None,
        "deadline_time": None,
        "stated_timezone": None,
        "confidence": 0.8,
        "evidence": evidence,
    }
    values.update(overrides)
    return values


def results_body(results: list[dict[str, Any]], **options: Any) -> dict[str, Any]:
    return response_body([output_text(json.dumps({"results": results}))], **options)


def sent_messages(request: httpx.Request) -> list[dict[str, Any]]:
    """The messages a captured Responses request carried in its input."""
    messages: list[dict[str, Any]] = json.loads(json.loads(request.content)["input"])["messages"]
    return messages


def answer_every_message(request: httpx.Request) -> httpx.Response:
    """Answer each message in the request with a valid result quoting its body's start."""
    results = [
        wire_result(message["message_key"], message["body"][:40])
        for message in sent_messages(request)
    ]
    return httpx.Response(200, json=results_body(results))


def error_body(code: str | None, message: str = "Request failed.") -> dict[str, Any]:
    return {"error": {"message": message, "type": "invalid_request_error", "code": code}}
