"""Bounded desktop diagnostics containing only types and known application codes."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from mailbrief.errors import ConfigurationError

ERROR_MESSAGES = {
    "AUTH_REQUIRED": "Connect Gmail, then retry.",
    "PERMISSION_DENIED": "Gmail permission denied. Disconnect and reconnect with read-only access.",
    "RATE_LIMITED": "Gmail rate limit reached; retry later.",
    "PROVIDER_ERROR": "Gmail is offline or unavailable. Check your connection and retry.",
    "AI_KEY_MISSING": "Add a Groq API key in Settings.",
    "AI_AUTH_FAILED": "Groq rejected the API key. Replace it in Settings.",
    "AI_NETWORK_BLOCKED": (
        "Groq refused this network. Try your home or mobile connection without a VPN."
    ),
    "AI_USAGE_LIMIT": "The AI request limit was reached. Select fewer messages for the next run.",
    "AI_PERMISSION_DENIED": "Groq denied access. Check your account permissions, region and quota.",
    "AI_RATE_LIMITED": "Groq rate limit reached; retry later.",
    "AI_TIMEOUT": "Groq did not respond in time; retry later.",
    "AI_SERVER_ERROR": "Groq had a server problem or sent an unreadable reply; retry later.",
    "AI_REQUEST_REJECTED": (
        "Groq rejected messages. Check the Structured Outputs model in Settings."
    ),
    "AI_NETWORK_ERROR": "Could not reach Groq. Check your connection and retry.",
    "AI_PROVIDER_ERROR": "Groq request failed. Check the model in Settings.",
    "AI_OUTPUT_INCOMPLETE": "Groq couldn't finish some answers within its output limit. Try again.",
    "AI_INVALID_OUTPUT": "Groq's answer couldn't be used. Your text is unchanged; try again.",
    "DRAFT_CHANGED": (
        "The draft changed before Groq's text could be used. Nothing was lost; try again."
    ),
    "ANALYSIS_FAILED": "No message could be analyzed. Review your selection and AI settings.",
}

logger = logging.getLogger("mailbrief.desktop")
logger.propagate = False
logger.addHandler(logging.NullHandler())


def configure_logging(directory: Path) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        directory / "desktop.log", maxBytes=128 * 1024, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.disabled = False
    logger.setLevel(logging.WARNING)
    logger.addHandler(handler)
    return handler


def log_failure(exc: Exception) -> None:
    # Never log str(exc), tracebacks, request IDs, provider data or mail-derived text.
    logger.warning("exception=%s", type(exc).__name__)


def error_guidance(code: str) -> str:
    safe_code = code if code in ERROR_MESSAGES else "UNKNOWN"
    logger.warning("code=%s", safe_code)
    return ERROR_MESSAGES.get(code, "Some messages could not be processed. Try another selection.")


def configuration_guidance(exc: ConfigurationError) -> str:
    message = str(exc)  # ConfigurationError messages are static and nonsecret by contract.
    if "MAILBRIEF_GMAIL_OAUTH_CLIENT_PATH" in message:
        return "Choose the downloaded Google Desktop OAuth client JSON file in Settings."
    if "MAILBRIEF_GROQ_MODEL" in message:
        return "Choose a Groq model that supports Structured Outputs in Settings."
    return message
