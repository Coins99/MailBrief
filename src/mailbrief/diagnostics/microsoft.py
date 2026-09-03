"""Narrow Microsoft authentication and first-page retrieval diagnostic."""

import argparse
import asyncio
import sys
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from typing import cast

from pydantic import ValidationError

from mailbrief.config import Settings
from mailbrief.domain.messages import MessagePage
from mailbrief.errors import ConfigurationError
from mailbrief.paths import AppPaths, configure_qt_identity
from mailbrief.ports.errors import (
    AuthenticationCancelledError,
    AuthenticationRequiredError,
    ProviderError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderResponseError,
)
from mailbrief.providers.microsoft.cache import clear_microsoft_session
from mailbrief.providers.microsoft.factory import microsoft_provider_context


class _ExitCode(IntEnum):
    SUCCESS = 0
    CONFIGURATION = 2
    AUTHENTICATION = 3
    PERMISSION = 4
    PROVIDER = 5
    RESPONSE = 6
    CANCELLED = 130


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mailbrief-ms-diagnostic",
        description="Validate Microsoft authentication and one Inbox metadata page.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="Connect and retrieve one Inbox metadata page.")
    fetch.add_argument(
        "--silent-only",
        action="store_true",
        help="Require an existing session and never launch a browser.",
    )
    commands.add_parser("disconnect", help="Remove locally cached Microsoft credentials.")
    return parser


async def _first_page(iterator: AsyncGenerator[MessagePage]) -> MessagePage | None:
    try:
        return await anext(iterator, None)
    finally:
        await iterator.aclose()


async def _run_fetch(settings: Settings, paths: AppPaths, *, silent_only: bool) -> None:
    async with microsoft_provider_context(settings, paths) as provider:
        account = await provider.current_account() if silent_only else await provider.connect()
        if account is None:
            raise AuthenticationRequiredError("Microsoft authentication is required.")

        now = datetime.now(UTC)
        range_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        range_end = range_start + timedelta(days=1)
        iterator = cast(
            AsyncGenerator[MessagePage],
            provider.iter_message_pages(
                range_start_utc=range_start,
                range_end_utc=range_end,
            ),
        )
        page = await _first_page(iterator)
        count = len(page.messages) if page is not None else 0
        print(f"Connected: {account.email_address}")
        print(f"Messages in first page: {count}")


async def _run_disconnect(paths: AppPaths) -> None:
    await clear_microsoft_session(paths.microsoft_token_cache_path)
    print("Microsoft session removed.")


def _failure(message: str, code: _ExitCode) -> int:
    print(message, file=sys.stderr)
    return int(code)


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the diagnostic and translate typed failures to stable process exits."""
    configure_qt_identity()
    parsed = _parser().parse_args(arguments)
    try:
        paths = AppPaths.from_qt()
        if parsed.command == "disconnect":
            asyncio.run(_run_disconnect(paths))
        else:
            settings = Settings()
            asyncio.run(_run_fetch(settings, paths, silent_only=bool(parsed.silent_only)))
    except (ValidationError, ConfigurationError):
        return _failure("MailBrief configuration is invalid.", _ExitCode.CONFIGURATION)
    except (AuthenticationCancelledError, AuthenticationRequiredError):
        return _failure("Microsoft authentication is required.", _ExitCode.AUTHENTICATION)
    except ProviderPermissionError:
        return _failure("Microsoft permissions are insufficient.", _ExitCode.PERMISSION)
    except ProviderResponseError:
        return _failure("Microsoft Graph returned an invalid response.", _ExitCode.RESPONSE)
    except (ProviderRateLimitError, ProviderError):
        return _failure("Microsoft services are currently unavailable.", _ExitCode.PROVIDER)
    except (asyncio.CancelledError, KeyboardInterrupt):
        return _failure("Microsoft diagnostic was cancelled.", _ExitCode.CANCELLED)
    except Exception:
        return _failure("Microsoft diagnostic failed unexpectedly.", _ExitCode.PROVIDER)
    return int(_ExitCode.SUCCESS)


if __name__ == "__main__":
    raise SystemExit(main())
