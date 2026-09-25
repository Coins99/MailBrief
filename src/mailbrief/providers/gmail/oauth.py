"""Bounded system-browser OAuth authorization with an IPv4 loopback callback."""

import asyncio
import base64
import hashlib
import json
import secrets
import webbrowser
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from mailbrief.errors import ConfigurationError
from mailbrief.ports.errors import AuthenticationCancelledError, AuthenticationRequiredError

AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
BrowserOpener = Callable[[str], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class DesktopClient:
    """Installed-app client; endpoints from the downloaded file are not trusted."""

    client_id: str
    client_secret: str = field(repr=False)

    @classmethod
    def load(cls, path: Path) -> "DesktopClient":
        try:
            if path.stat().st_size > 64_000:
                raise ValueError
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            installed = data["installed"]
            client_id = installed["client_id"]
            client_secret = installed["client_secret"]
            if (
                not isinstance(client_id, str)
                or not client_id.endswith(".apps.googleusercontent.com")
                or not isinstance(client_secret, str)
                or not client_secret.strip()
            ):
                raise ValueError
            return cls(client_id=client_id, client_secret=client_secret)
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            raise ConfigurationError(
                "Gmail requires a valid Google Desktop OAuth client JSON file."
            ) from None


@dataclass(frozen=True, slots=True, repr=False)
class AuthorizationGrant:
    """Ephemeral secrets exchanged immediately for credentials."""

    code: str
    verifier: str
    redirect_uri: str


async def open_browser(url: str) -> bool:
    """Keep browser-launch work off the event loop."""
    return await asyncio.to_thread(webbrowser.open, url, new=1)


class LoopbackAuthorization:
    """Listen only on loopback; never log or reflect callback parameters."""

    def __init__(
        self, *, browser: BrowserOpener = open_browser, timeout_seconds: float = 180
    ) -> None:
        self._browser = browser
        self._timeout = timeout_seconds

    async def authorize(self, client: DesktopClient) -> AuthorizationGrant:
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        result: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        handlers: set[asyncio.Task[None]] = set()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                async with asyncio.timeout(5):
                    header = await reader.readuntil(b"\r\n\r\n")
                    first = header.split(b"\r\n", 1)[0].decode("ascii")
                    method, target, _ = first.split(" ", 2)
                    parsed = urlsplit(target)
                    query = parse_qs(parsed.query, max_num_fields=20)
                    valid = (
                        method == "GET"
                        and parsed.path == "/oauth2/callback"
                        and not parsed.scheme
                        and not parsed.netloc
                        and len(query.get("state", [])) == 1
                        and secrets.compare_digest(query["state"][0], state)
                    )
                    if not valid:
                        status, message = "400 Bad Request", "Invalid authorization callback."
                    elif result.done():
                        status, message = "409 Conflict", "Authorization already completed."
                    elif "error" in query:
                        result.set_exception(
                            AuthenticationCancelledError("Google authorization was declined.")
                        )
                        status, message = "200 OK", "Authorization declined. Return to MailBrief."
                    elif len(query.get("code", [])) == 1:
                        result.set_result(query["code"][0])
                        status, message = "200 OK", "Authorization received. Return to MailBrief."
                    else:
                        status, message = "400 Bad Request", "Missing authorization code."
                    body = message.encode("ascii")
                    writer.write(
                        (
                            f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\n"
                            f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n"
                            "Connection: close\r\n\r\n"
                        ).encode("ascii")
                        + body
                    )
                    await writer.drain()
            except (
                TimeoutError,
                ValueError,
                TypeError,
                UnicodeError,
                OSError,
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
            ):
                pass  # Untrusted callbacks must not leak into logs or abort the genuine flow.
            finally:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()

        def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            if len(handlers) >= 8:
                writer.close()
                return
            task = asyncio.create_task(handle(reader, writer))
            handlers.add(task)
            task.add_done_callback(handlers.discard)

        try:
            server = await asyncio.start_server(connected, "127.0.0.1", 0, limit=8192)
        except OSError:
            raise AuthenticationRequiredError("Unable to start the local OAuth callback.") from None
        port = server.sockets[0].getsockname()[1]
        redirect = f"http://127.0.0.1:{port}/oauth2/callback"
        url = (
            AUTHORIZATION_URL
            + "?"
            + urlencode(
                {
                    "client_id": client.client_id,
                    "redirect_uri": redirect,
                    "response_type": "code",
                    "scope": GMAIL_SCOPE,
                    "state": state,
                    "code_challenge": challenge.rstrip(b"=").decode("ascii"),
                    "code_challenge_method": "S256",
                    "access_type": "offline",
                    "prompt": "consent select_account",
                }
            )
        )
        try:
            async with asyncio.timeout(self._timeout):
                try:
                    opened = await self._browser(url)
                except Exception:
                    raise AuthenticationRequiredError(
                        "Unable to open the system browser."
                    ) from None
                if not opened:
                    raise AuthenticationRequiredError("Unable to open the system browser.")
                code = await result
                return AuthorizationGrant(code=code, verifier=verifier, redirect_uri=redirect)
        except TimeoutError:
            raise AuthenticationRequiredError(
                "Google authorization timed out; try again."
            ) from None
        finally:
            server.close()
            await server.wait_closed()
            pending = tuple(handlers)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if not result.done():
                result.cancel()
            elif not result.cancelled():
                result.exception()  # Observe a callback denial even if browser launch failed.
