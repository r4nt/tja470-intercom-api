import json

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from aiotja470_intercom.client import TJA470IntercomClient
from aiotja470_intercom.exceptions import TJA470AuthError
from aiotja470_intercom.runner import AiohttpRunner

SESSION_ID = "session-1"
TOPIC = "com/hager/doorphone/runtime/rest/*"


def event(name, value):
    topic = f"com/hager/doorphone/runtime/rest/{name}"
    return json.dumps({
        "topic": topic,
        "properties": {"subscription.id": "s-1", "event.topics": topic, "value": value},
    })


class FakeDevice:
    """Device with a session-cookie login and an event bus WebSocket."""

    def __init__(self):
        self.logins = 0
        self.subscribed_topics = []
        self.events = [
            event("currentDevice/UPDATED", {"order": 1}),
            "not json",
            event("INCOMINGCALL/1", None),
        ]

    async def manifest(self, request):
        if request.cookies.get("SID") == SESSION_ID:
            return web.json_response({"ref": "TJA470"})
        if request.headers.get("Authorization") == "Basic dXNlcjpwYXNz":
            self.logins += 1
            response = web.json_response({"ref": "TJA470"})
            response.set_cookie("SID", SESSION_ID)
            return response
        return web.Response(status=401)

    async def events_ws(self, request):
        if request.cookies.get("SID") != SESSION_ID:
            return web.Response(status=401)
        self.subscribed_topics.append(request.query.get("topics"))
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        for message in self.events:
            await ws.send_str(message)
        await ws.close()
        return ws


@pytest_asyncio.fixture
async def device():
    fake = FakeDevice()
    app = web.Application()
    app.router.add_get("/API/manifest", fake.manifest)
    app.router.add_get("/remote/events/", fake.events_ws)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    fake.host = f"127.0.0.1:{server.port}"
    yield fake
    await server.close()


async def collect(client):
    return [e async for e in client.events()]


@pytest.mark.asyncio
async def test_events_logs_in_and_yields_parsed_events(device):
    async with aiohttp.ClientSession() as session:
        client = TJA470IntercomClient(device.host, "user", "pass", AiohttpRunner(session))

        events = await collect(client)

    assert [e.name for e in events] == ["currentDevice/UPDATED", "INCOMINGCALL/1"]
    assert events[0].value == {"order": 1}
    assert events[0].topic == "com/hager/doorphone/runtime/rest/currentDevice/UPDATED"
    assert events[1].value is None
    assert device.logins == 1
    assert device.subscribed_topics == [f"[{TOPIC}]"]


@pytest.mark.asyncio
async def test_events_reuses_existing_session(device):
    async with aiohttp.ClientSession() as session:
        client = TJA470IntercomClient(device.host, "user", "pass", AiohttpRunner(session))
        await client.get_manifest()

        await collect(client)

    assert device.logins == 1


@pytest.mark.asyncio
async def test_events_auth_failure(device):
    async with aiohttp.ClientSession() as session:
        client = TJA470IntercomClient(device.host, "user", "wrong", AiohttpRunner(session))

        with pytest.raises(TJA470AuthError):
            await collect(client)
