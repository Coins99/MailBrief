"""Safe Gmail authentication diagnostic; no message content or database writes."""

import argparse
import asyncio
import logging
from collections.abc import Sequence

from pydantic import ValidationError

from mailbrief.config import Settings
from mailbrief.errors import ConfigurationError
from mailbrief.ports.errors import AuthenticationRequiredError, ProviderError
from mailbrief.providers.gmail.cache import GmailCredentialStore
from mailbrief.providers.gmail.errors import GmailSetupError
from mailbrief.providers.gmail.factory import gmail_auth


async def run(command: str, *, silent_only: bool) -> int:
    """Verify a profile or forget credentials; output contains no mailbox identity."""
    if command == "disconnect":
        await asyncio.to_thread(GmailCredentialStore().clear)
        print("Local Gmail credentials removed. Google access and local app data are unchanged.")
        return 0
    async with gmail_auth(Settings()) as auth:
        await auth.connect(silent_only=silent_only)
        print("Gmail read-only connection verified. No messages downloaded (M1).")
        return 0


def main(arguments: Sequence[str] | None = None) -> int:
    """Return predictable exit codes without dumping provider payloads or secrets."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="Verify Gmail identity; M1 does not fetch mail.")
    fetch.add_argument("--silent-only", action="store_true", help="Never open a browser.")
    commands.add_parser("disconnect", help="Remove only locally saved Gmail credentials.")
    args = parser.parse_args(arguments)
    # Wire/debug logging can expose authorization headers and loopback URLs.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        return asyncio.run(run(args.command, silent_only=getattr(args, "silent_only", False)))
    except AuthenticationRequiredError as exc:
        print(str(exc))
        return 2
    except GmailSetupError as exc:
        print(str(exc))
        return 3
    except (ConfigurationError, ValidationError):
        print("Gmail configuration or secure storage is unavailable. See docs/gmail-setup.md.")
        return 3
    except ProviderError as exc:
        print(str(exc))
        return 4
    except KeyboardInterrupt:
        print("Gmail diagnostic cancelled.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
