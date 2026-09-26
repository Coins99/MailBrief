"""Synthetic Groq Chat Completions payloads and an in-memory credential vault."""

import json
from typing import Any

import httpx

CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
TEST_KEY = "gsk_test_" + "a" * 32


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
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def output_text(text: str) -> dict[str, Any]:
    return {"text": text}


def response_body(
    content: list[dict[str, Any]], *, status: str = "completed", with_usage: bool = True
) -> dict[str, Any]:
    return {
        "id": "chatcmpl_test",
        "object": "chat.completion",
        "created": 1_790_000_000,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop" if status == "completed" else "length",
                "message": {
                    "role": "assistant",
                    "content": content[0].get("text") if content else None,
                    "refusal": content[0].get("refusal") if content else None,
                },
            }
        ],
        "usage": usage() if with_usage else None,
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
    """The messages a captured chat request carried in its user message."""
    messages: list[dict[str, Any]] = json.loads(
        json.loads(request.content)["messages"][1]["content"]
    )["messages"]
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
