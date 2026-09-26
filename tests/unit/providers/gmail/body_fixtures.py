"""Synthetic Gmail format=full payload builders; never real mail."""

import base64


def encode(content: str | bytes) -> str:
    raw = content.encode() if isinstance(content, str) else content
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def part(
    mime: str,
    content: str | bytes | None = None,
    *,
    filename: str = "",
    headers: dict[str, str] | None = None,
    attachment_id: str | None = None,
    size: int | None = None,
    parts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    raw = content.encode() if isinstance(content, str) else content
    body: dict[str, object] = {"size": size if size is not None else len(raw or b"")}
    if raw is not None:
        body["data"] = encode(raw)
    if attachment_id is not None:
        body["attachmentId"] = attachment_id
    result: dict[str, object] = {
        "mimeType": mime,
        "filename": filename,
        "headers": [{"name": name, "value": value} for name, value in (headers or {}).items()],
        "body": body,
    }
    if parts is not None:
        result["parts"] = parts
    return result


def message(payload: dict[str, object], identifier: str = "m1") -> dict[str, object]:
    return {"id": identifier, "payload": payload}
