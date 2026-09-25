"""Exercise the actual loopback listener using synthetic browser callbacks."""

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from mailbrief.errors import ConfigurationError
from mailbrief.ports.errors import AuthenticationCancelledError, AuthenticationRequiredError
from mailbrief.providers.gmail.oauth import DesktopClient, LoopbackAuthorization

CLIENT = DesktopClient("test.apps.googleusercontent.com", "fake-client-secret")


def test_client_file(tmp_path: Path) -> None:
    path = tmp_path / "client.json"
    path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": CLIENT.client_id,
                    "client_secret": CLIENT.client_secret,
                    "token_uri": "https://untrusted.example/token",
                }
            }
        )
    )
    assert DesktopClient.load(path) == CLIENT
    assert "fake-client-secret" not in repr(CLIENT)


@pytest.mark.parametrize("payload", ["bad-json", "[]", '{"web":{}}', '{"installed":{}}'])
def test_invalid_client_file(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "client.json"
    path.write_text(payload)
    with pytest.raises(ConfigurationError, match="Desktop"):
        DesktopClient.load(path)


def test_missing_and_oversized_file(tmp_path: Path) -> None:
    path = tmp_path / "client.json"
    with pytest.raises(ConfigurationError):
        DesktopClient.load(path)
    path.write_text("x" * 64_001)
    with pytest.raises(ConfigurationError):
        DesktopClient.load(path)


async def callback(url: str, fields: dict[str, str]) -> httpx.Response:
    async with httpx.AsyncClient(trust_env=False) as http:
        return await http.get(url, params=fields)


async def test_real_callback_pkce_and_state_validation() -> None:
    query: dict[str, list[str]] = {}

    async def browser(url: str) -> bool:
        query.update(parse_qs(urlsplit(url).query))
        redirect = query["redirect_uri"][0]
        assert urlsplit(redirect).hostname == "127.0.0.1"
        bad = await callback(redirect, {"state": "wrong", "code": "secret-code"})
        assert bad.status_code == 400
        assert "secret-code" not in bad.text
        valid = await callback(redirect, {"state": query["state"][0], "code": "valid-code"})
        assert valid.status_code == 200
        assert "valid-code" not in valid.text
        return True

    grant = await LoopbackAuthorization(browser=browser, timeout_seconds=5).authorize(CLIENT)
    assert grant.code == "valid-code"
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(grant.verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert query["code_challenge"] == [expected]
    assert query["code_challenge_method"] == ["S256"]
    assert query["access_type"] == ["offline"]
    assert "valid-code" not in repr(grant)
    with pytest.raises(httpx.ConnectError):
        await callback(grant.redirect_uri, {})


async def test_denied_authorization() -> None:
    async def browser(url: str) -> bool:
        query = parse_qs(urlsplit(url).query)
        await callback(
            query["redirect_uri"][0], {"state": query["state"][0], "error": "access_denied"}
        )
        return True

    with pytest.raises(AuthenticationCancelledError):
        await LoopbackAuthorization(browser=browser).authorize(CLIENT)


async def test_duplicate_state_and_wrong_path_do_not_complete_flow() -> None:
    async def browser(url: str) -> bool:
        query = parse_qs(urlsplit(url).query)
        redirect = query["redirect_uri"][0]
        fields = {"state": query["state"][0], "code": "fake"}
        async with httpx.AsyncClient(trust_env=False) as http:
            duplicate = await http.get(redirect + "?" + urlencode(fields) + "&state=extra")
            assert duplicate.status_code == 400
        wrong = await callback(redirect.replace("/oauth2/callback", "/favicon.ico"), fields)
        assert wrong.status_code == 400
        valid = await callback(redirect, fields)
        assert valid.status_code == 200
        return True

    assert (await LoopbackAuthorization(browser=browser).authorize(CLIENT)).code == "fake"


@pytest.mark.parametrize("raises", [True, False])
async def test_browser_failure(raises: bool) -> None:
    async def browser(url: str) -> bool:
        if raises:
            raise RuntimeError("secret-url")
        return False

    with pytest.raises(AuthenticationRequiredError, match="browser") as error:
        await LoopbackAuthorization(browser=browser).authorize(CLIENT)
    assert "secret-url" not in str(error.value)


async def test_timeout_closes_listener() -> None:
    redirect = ""

    async def browser(url: str) -> bool:
        nonlocal redirect
        redirect = parse_qs(urlsplit(url).query)["redirect_uri"][0]
        return True

    with pytest.raises(AuthenticationRequiredError, match="timed out"):
        await LoopbackAuthorization(browser=browser, timeout_seconds=0.05).authorize(CLIENT)
    with pytest.raises(httpx.ConnectError):
        await callback(redirect, {})


async def test_task_cancellation_closes_listener() -> None:
    opened = asyncio.Event()
    redirect = ""

    async def browser(url: str) -> bool:
        nonlocal redirect
        redirect = parse_qs(urlsplit(url).query)["redirect_uri"][0]
        opened.set()
        return True

    task = asyncio.create_task(LoopbackAuthorization(browser=browser).authorize(CLIENT))
    await asyncio.wait_for(opened.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(httpx.ConnectError):
        await callback(redirect, {})
