"""Desktop diagnostics never persist exception payloads or unrecognized codes."""

from pathlib import Path

from mailbrief.ui.diagnostics import configure_logging, error_guidance, log_failure, logger


def test_rotating_log_contains_only_types_and_known_codes(tmp_path: Path) -> None:
    handler = configure_logging(tmp_path)
    try:
        log_failure(RuntimeError("SECRET email body token"))
        error_guidance("AI_AUTH_FAILED")
        error_guidance("SECRET provider payload")
        handler.flush()
        content = (tmp_path / "desktop.log").read_text()
        assert "RuntimeError" in content
        assert "AI_AUTH_FAILED" in content
        assert "UNKNOWN" in content
        assert "SECRET" not in content
        assert "Traceback" not in content
        assert handler.maxBytes == 128 * 1024
        assert handler.backupCount == 2
    finally:
        logger.removeHandler(handler)
        handler.close()
