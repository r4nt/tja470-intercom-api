import pytest
import aiohttp
from typing import Any, Dict, Optional, Union

from aiotja470_intercom.client import TJA470IntercomClient
from aiotja470_intercom.exceptions import TJA470ResponseError
from aiotja470_intercom.models import FreeDevice, Manifest, ProvisioningInfo

class MockRunner:
    def __init__(self):
        self.requests = []
        self.next_response: Union[Dict[str, Any], str, list, Exception] = {}
        self.responses_queue = []
        self.cookies = {}
        
    async def request(
        self,
        method: str,
        url: str,
        auth: Optional[aiohttp.BasicAuth] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> Union[Dict[str, Any], str, bytes, list, None]:
        self.requests.append({"method": method, "url": url, "auth": auth, "json": json})
        if self.responses_queue:
            resp = self.responses_queue.pop(0)
        else:
            resp = self.next_response
        if isinstance(resp, Exception):
            raise resp
        return resp

    def get_cookies(self, url: str) -> Dict[str, str]:
        return self.cookies.get(url, {})

    def set_cookies(self, url: str, cookies: Dict[str, str]) -> None:
        if url not in self.cookies:
            self.cookies[url] = {}
        self.cookies[url].update(cookies)

    async def close(self) -> None:
        pass


@pytest.fixture
def runner():
    return MockRunner()

@pytest.fixture
def client(runner):
    return TJA470IntercomClient("127.0.0.1", "testuser", "testpass", runner)

@pytest.mark.asyncio
async def test_get_manifest(client, runner):
    runner.next_response = {"status": "ok", "fw": "1.2.3"}
    manifest = await client.get_manifest()
    
    assert isinstance(manifest, Manifest)
    assert manifest.raw_data == {"status": "ok", "fw": "1.2.3"}
    assert manifest.fw == "1.2.3"
    assert len(runner.requests) == 1
    assert runner.requests[0]["method"] == "GET"
    assert "manifest" in runner.requests[0]["url"]

@pytest.mark.asyncio
async def test_get_free_devices(client, runner):
    runner.next_response = [
        {"id": 42, "name": "Test Device", "mac": "00:11:22:33:44:55"}
    ]
    devices = await client.get_free_devices()
    
    assert len(devices) == 1
    assert isinstance(devices[0], FreeDevice)
    assert devices[0].id == 42
    assert devices[0].name == "Test Device"
    assert devices[0].mac == "00:11:22:33:44:55"

@pytest.mark.asyncio
async def test_get_free_devices_error(client, runner):
    runner.next_response = {"error": "not a list"}
    with pytest.raises(TJA470ResponseError):
        await client.get_free_devices()

@pytest.mark.asyncio
async def test_set_uid(client, runner):
    runner.next_response = ""
    await client.set_uid(42, "test-uuid")
    
    assert len(runner.requests) == 1
    req = runner.requests[0]
    assert req["method"] == "POST"
    assert "setuid" in req["url"]
    assert req["json"] == {"id": 42, "uid": "test-uuid", "description": ""}

@pytest.mark.asyncio
async def test_get_provisioning(client, runner):
    runner.next_response = {
        "sipId": "1001",
        "sipPassword": "secretpassword",
        "rtspVideoUrl": "rtsp://127.0.0.1/stream",
        "httpVideoUrl": "http://127.0.0.1:8021/mjpg/high",
        "localIpAddress": "192.168.1.100",
        "doorReleaseAllowed": True,
        "calledElements": [
            {"sipId": "1002", "name": "Station 1", "order": 0}
        ],
        "remoteAccess": {
            "sipId": "remote1001",
            "sipPassword": "remotesecret",
            "ngrokUrl": "https://foo.ngrok.io",
            "rtspUrl": "rtsp://foo.ngrok.io/live",
            "rtspPort": 554,
            "sipTcpUrl": "tcp://foo.ngrok.io",
            "sipTcpPort": 5060,
            "wsPort": 8080,
            "stunTurnPrefix": "turn:",
            "stunTurnUser": "stunuser",
            "stunTurnPassword": "stunpassword",
            "stunTurnHostname": "stun.hager.com",
            "stunTurnPort": 3478
        }
    }
    config = await client.get_provisioning("test-uuid")
    
    assert isinstance(config, ProvisioningInfo)
    assert config.sip_info.sip_id == "1001"
    assert config.sip_info.sip_password == "secretpassword"
    assert config.rtsp_video_url == "rtsp://127.0.0.1/stream"
    assert config.http_video_url == "http://127.0.0.1:8021/mjpg/high"
    assert config.local_ip_address == "192.168.1.100"
    assert config.door_release_allowed is True
    assert len(config.called_elements) == 1
    assert config.called_elements[0].sip_id == "1002"
    assert config.called_elements[0].name == "Station 1"
    assert config.called_elements[0].order == 0

    assert config.remote_access is not None
    assert config.remote_access.sip_id == "remote1001"
    assert config.remote_access.sip_password == "remotesecret"
    assert config.remote_access.ngrok_url == "https://foo.ngrok.io"
    assert config.remote_access.rtsp_url == "rtsp://foo.ngrok.io/live"
    assert config.remote_access.rtsp_port == 554
    assert config.remote_access.sip_tcp_url == "tcp://foo.ngrok.io"
    assert config.remote_access.sip_tcp_port == 5060
    assert config.remote_access.ws_port == 8080
    assert config.remote_access.stun_turn_prefix == "turn:"
    assert config.remote_access.stun_turn_user == "stunuser"
    assert config.remote_access.stun_turn_password == "stunpassword"
    assert config.remote_access.stun_turn_hostname == "stun.hager.com"
    assert config.remote_access.stun_turn_port == 3478

@pytest.mark.asyncio
async def test_get_provisioning_integer_fields(client, runner):
    runner.next_response = {
        "sipId": 6004,
        "sipPassword": "secretpassword",
        "rtspVideoUrl": "rtsp://127.0.0.1/stream",
        "httpVideoUrl": "http://127.0.0.1:8021/mjpg/high",
        "localIpAddress": "192.168.1.100",
        "doorReleaseAllowed": True,
        "calledElements": [
            {"sipId": 6000, "name": "Station 1", "order": 0}
        ],
        "remoteAccess": {
            "sipId": 6005,
            "sipPassword": "remotesecret"
        }
    }
    config = await client.get_provisioning("test-uuid")
    
    assert isinstance(config, ProvisioningInfo)
    assert config.sip_info.sip_id == "6004"
    assert config.called_elements[0].sip_id == "6000"
    assert config.remote_access is not None
    assert config.remote_access.sip_id == "6005"


@pytest.mark.asyncio
async def test_get_provisioning_error(client, runner):
    runner.next_response = ["not", "a", "dict"]
    with pytest.raises(TJA470ResponseError):
        await client.get_provisioning("test-uuid")

@pytest.mark.asyncio
async def test_switch_camera(client, runner):
    runner.next_response = {"order": 1}
    pos = await client.switch_camera("test-uuid")
    
    assert pos == 1
    assert len(runner.requests) == 1
    assert "camera/switch/test-uuid" in runner.requests[0]["url"]

@pytest.mark.asyncio
async def test_switch_to_camera_position_success(client, runner):
    runner.responses_queue = [{"order": 0}, {"order": 1}]
    pos = await client.switch_to_camera_position("test-uuid", 1)
    
    assert pos == 1
    assert len(runner.requests) == 2

@pytest.mark.asyncio
async def test_switch_to_camera_position_not_found(client, runner):
    runner.responses_queue = [{"order": 0}, {"order": 1}, {"order": 0}]
    with pytest.raises(TJA470ResponseError) as exc_info:
        await client.switch_to_camera_position("test-uuid", 2)
    
    assert "Target position 2 not found" in str(exc_info.value)
    assert len(runner.requests) == 3

@pytest.mark.asyncio
async def test_open_door_at_position(client, runner):
    runner.responses_queue = [{"order": 0}, {"order": 1}, ""]
    await client.open_door_at_position("test-uuid", 1, door_id=1)
    
    assert len(runner.requests) == 3
    assert "camera/switch/test-uuid" in runner.requests[0]["url"]
    assert "camera/switch/test-uuid" in runner.requests[1]["url"]
    assert "doorrelease/1" in runner.requests[2]["url"]

@pytest.mark.asyncio
async def test_open_door(client, runner):
    runner.next_response = ""
    await client.open_door(1)
    
    assert len(runner.requests) == 1
    assert "doorrelease/1" in runner.requests[0]["url"]

def test_cookies(client, runner):
    client.set_cookies({"session_id": "12345"})
    cookies = client.get_cookies()
    assert cookies == {"session_id": "12345"}

def test_redact():
    from aiotja470_intercom.runner import _redact
    data = {
        "sipId": "1001",
        "sipPassword": "secretpassword",
        "nested": {
            "token": "sensitive_token",
            "safe": "hello"
        },
        "list": [
            {"password": "pw1"},
            {"safe": 42}
        ]
    }
    redacted = _redact(data)
    assert redacted["sipId"] == "1001"
    assert redacted["sipPassword"] == "********"
    assert redacted["nested"]["token"] == "********"
    assert redacted["nested"]["safe"] == "hello"
    assert redacted["list"][0]["password"] == "********"
    assert redacted["list"][1]["safe"] == 42
    
    import json
    json_str = json.dumps(data)
    redacted_str = _redact(json_str)
    parsed = json.loads(redacted_str)
    assert parsed["sipPassword"] == "********"
    
    assert _redact("just a string") == "just a string"

