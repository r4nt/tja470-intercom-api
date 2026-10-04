import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from yarl import URL

from aiotja470_intercom.client import TJA470IntercomClient
from aiotja470_intercom.runner import AiohttpRunner

SESSION_ID = "session-1"


class FakeDevice:
    """Minimal device that hands out a session cookie on Basic Auth login."""

    def __init__(self):
        self.logins = 0
        self.unauthorized = 0
        self.received_cookies = []

    async def manifest(self, request: web.Request) -> web.Response:
        self.received_cookies.append(dict(request.cookies))
        if request.cookies.get("SID") == SESSION_ID:
            return web.json_response({"ref": "TJA470"})
        if request.headers.get("Authorization") == aiohttp.BasicAuth("user", "pass").encode():
            self.logins += 1
            response = web.json_response({"ref": "TJA470"})
            response.set_cookie("SID", SESSION_ID)
            return response
        self.unauthorized += 1
        return web.Response(status=401)


@pytest_asyncio.fixture
async def device():
    fake = FakeDevice()
    app = web.Application()
    app.router.add_get("/API/manifest", fake.manifest)
    # Bind to an IP address: aiohttp's default cookie jar rejects cookies
    # from IP hosts, which is how the real device is usually addressed.
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    fake.host = f"127.0.0.1:{server.port}"
    yield fake
    await server.close()


@pytest.mark.asyncio
async def test_session_cookie_reused_with_default_cookie_jar_session(device):
    async with aiohttp.ClientSession() as session:
        client = TJA470IntercomClient(device.host, "user", "pass", AiohttpRunner(session))

        await client.get_manifest()
        await client.get_manifest()
        await client.get_manifest()

    assert device.logins == 1
    assert device.unauthorized == 1
    assert device.received_cookies[-1] == {"SID": SESSION_ID}


@pytest.mark.asyncio
async def test_session_cookie_reused_with_own_session(device):
    runner = AiohttpRunner()
    client = TJA470IntercomClient(device.host, "user", "pass", runner)
    try:
        await client.get_manifest()
        await client.get_manifest()
    finally:
        await runner.close()

    assert device.logins == 1
    assert device.received_cookies[-1] == {"SID": SESSION_ID}


@pytest.mark.asyncio
async def test_get_cookies_only_returns_device_cookies(device):
    async with aiohttp.ClientSession() as session:
        session.cookie_jar.update_cookies(
            {"other": "secret"}, response_url=URL("http://example.com/")
        )
        client = TJA470IntercomClient(device.host, "user", "pass", AiohttpRunner(session))

        await client.get_manifest()

        assert client.get_cookies() == {"SID": SESSION_ID}


@pytest.mark.asyncio
async def test_set_cookies_restores_session(device):
    async with aiohttp.ClientSession() as session:
        client = TJA470IntercomClient(device.host, "user", "pass", AiohttpRunner(session))
        client.set_cookies({"SID": SESSION_ID})

        await client.get_manifest()

    assert device.logins == 0
    assert device.unauthorized == 0
