import aiohttp
import base64
import json
import logging
from urllib.parse import quote
from typing import Any, AsyncIterator, Dict, List, Optional, Union

from .exceptions import TJA470ResponseError, TJA470AuthError
from .models import DOORPHONE_EVENT_TOPIC_PREFIX, DoorphoneEvent, FreeDevice, Manifest, ProvisioningInfo
from .runner import Runner

_LOGGER = logging.getLogger(__name__)

# All doorphone events: current camera changes, incoming calls, call history.
DOORPHONE_EVENTS_TOPIC = DOORPHONE_EVENT_TOPIC_PREFIX + "*"


def basic_auth_header(username: str, password: str) -> str:
    """Return the Authorization header value for HTTP Basic Auth.

    Encoded as latin-1, like aiohttp.BasicAuth, which aiohttp deprecates.
    """
    credentials = f"{username}:{password}".encode("latin1")
    return "Basic " + base64.b64encode(credentials).decode("ascii")

class TJA470IntercomClient:
    """Client for the Hager TJA470 Intercom API.

    This client communicates with the TJA470 local API to manage client pairing,
    provisioning, camera streams, switching feeds, and door releases.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        runner: Runner,
    ) -> None:
        """Initialize the TJA470 Intercom client.

        Args:
            host: The IP address or hostname of the TJA470.
            username: The login username.
            password: The login password.
            runner: The HTTP runner implementation used to execute requests.
        """
        self.host = host
        self._authorization = basic_auth_header(username, password)
        self._runner = runner
        # The client's own SIP id, remembered from the last provisioning
        # response; used as the default door release id.
        self._sip_id: Optional[str] = None

    @property
    def base_url(self) -> str:
        """Get the base URL for the API endpoints."""
        return f"http://{self.host}/API"

    def get_cookies(self) -> dict[str, str]:
        """Get the current cookies for the API base URL.

        Returns:
            dict[str, str]: A dictionary of cookie keys and values.
        """
        return self._runner.get_cookies(self.base_url)

    def set_cookies(self, cookies: dict[str, str]) -> None:
        """Set cookies for the API base URL.

        Args:
            cookies: A dictionary of cookie keys and values.
        """
        self._runner.set_cookies(self.base_url, cookies)

    async def _request(self, method: str, url: str, json: Optional[Dict[str, Any]] = None) -> Any:
        try:
            return await self._runner.request(method, url, json=json)
        except TJA470AuthError:
            return await self._runner.request(method, url, authorization=self._authorization, json=json)

    async def get_manifest(self) -> Manifest:
        """Verify authentication and retrieve the API manifest.

        Returns:
            Manifest: The manifest containing system details like firmware version.
        """
        url = f"{self.base_url}/manifest"
        response = await self._request("GET", url)
        if isinstance(response, dict):
            return Manifest(raw_data=response)
        elif isinstance(response, str):
            # Sometimes manifest might just return empty string or non-json if successful
            return Manifest()
        else:
            return Manifest()

    async def get_free_devices(self) -> List[FreeDevice]:
        """List devices available for pairing.

        Returns:
            List[FreeDevice]: A list of unassigned devices ready for pairing.

        Raises:
            TJA470ResponseError: If the server response structure is invalid.
        """
        url = f"{self.base_url}/runtime/provisioning/freedevices"
        response = await self._request("GET", url)
        
        if not isinstance(response, list):
            raise TJA470ResponseError("Expected a list of free devices")

        return [FreeDevice.from_dict(item) for item in response]

    async def set_uid(self, device_id: int, uid: str) -> None:
        """Register a client UUID to a free device slot.

        Args:
            device_id: The ID of the free device to register the client to.
            uid: The UUID string to register.
        """
        url = f"{self.base_url}/runtime/pairing/setuid"
        payload = {
            "id": device_id,
            "uid": uid,
            "description": ""
        }
        await self._request("POST", url, json=payload)

    async def get_provisioning(self, uid: str) -> ProvisioningInfo:
        """Retrieve the configuration details (SIP credentials and streams) for the paired client.

        Args:
            uid: The registered client UUID.

        Returns:
            ProvisioningInfo: The SIP credentials and camera stream URLs.

        Raises:
            TJA470ResponseError: If the server response structure is invalid.
        """
        url = f"{self.base_url}/runtime/provisioning"
        payload = {"uid": uid}
        response = await self._request("POST", url, json=payload)
        
        if not isinstance(response, dict):
            raise TJA470ResponseError("Expected a dictionary for provisioning info")

        return self._parse_provisioning(response)

    async def get_provisioning_if_changed(self, uid: str, version: Optional[str]) -> Optional[ProvisioningInfo]:
        """Retrieve the provisioning info only if it changed since the given version.

        The device answers with 304 Not Modified when the configuration version is
        unchanged, which saves transferring and parsing the full response.

        Args:
            uid: The registered client UUID.
            version: The `version` of the last known provisioning info, or None to
                always fetch it.

        Returns:
            Optional[ProvisioningInfo]: The new provisioning info, or None if it is
            unchanged.

        Raises:
            TJA470ResponseError: If the server response structure is invalid.
        """
        url = f"{self.base_url}/runtime/provisioning"
        payload = {"uid": uid}
        if version is not None:
            payload["version"] = version
        response = await self._request("POST", url, json=payload)

        if isinstance(response, dict):
            return self._parse_provisioning(response)
        if not response:
            # 304 Not Modified has no body.
            return None
        raise TJA470ResponseError("Expected a dictionary for provisioning info")

    def _parse_provisioning(self, data: Dict[str, Any]) -> ProvisioningInfo:
        info = ProvisioningInfo.from_dict(data)
        if info.sip_info.sip_id:
            self._sip_id = info.sip_info.sip_id
        return info

    async def get_software_version(self) -> str:
        """Get the version of the doorphone software.

        This differs from the firmware version in the manifest (`Manifest.fw`).

        Returns:
            str: The doorphone software version, e.g. "4.0.2".

        Raises:
            TJA470ResponseError: If the response is invalid.
        """
        url = f"{self.base_url}/runtime/platform/softwareversion"
        response = await self._request("GET", url)
        if isinstance(response, dict) and "softwareVersion" in response:
            return str(response["softwareVersion"])
        raise TJA470ResponseError("Expected a dict with 'softwareVersion' from get_software_version")

    async def is_alive(self, serial_number: str) -> bool:
        """Check that the device is reachable and has the given serial number.

        Args:
            serial_number: The expected serial number (see `Manifest.serial_number`).

        Returns:
            bool: True if the device reports that the serial number matches.

        Raises:
            TJA470ResponseError: If the response is invalid.
        """
        url = f"{self.base_url}/runtime/platform/isalive?serialNumber={quote(serial_number)}"
        response = await self._request("GET", url)
        if isinstance(response, dict) and "match" in response:
            return bool(response["match"])
        raise TJA470ResponseError("Expected a dict with 'match' from is_alive")

    async def switch_camera(self, uid: str) -> int:
        """Switch the active camera feed to the next position.

        Args:
            uid: The registered client UUID.

        Returns:
            int: The new camera position index (e.g. 0, 1, ...).

        Raises:
            TJA470ResponseError: If the camera switch fails or returns an invalid response.
        """
        url = f"{self.base_url}/runtime/command/camera/switch/{uid}"
        response = await self._request("POST", url, json={})
        if isinstance(response, dict) and "order" in response:
            return int(response["order"])
        raise TJA470ResponseError("Expected a dict with 'order' from switch_camera")

    async def get_current_camera(self, uid: str) -> int:
        """Get the current camera position.

        This also activates the video of the current outdoor station on the RTSP
        stream, which otherwise shows an idle placeholder.

        Args:
            uid: The registered client UUID.

        Returns:
            int: The current camera position index (e.g. 0, 1, ...).

        Raises:
            TJA470ResponseError: If the response is invalid.
        """
        url = f"{self.base_url}/runtime/command/camera/current/{uid}"
        response = await self._request("GET", url)
        if isinstance(response, dict) and "order" in response:
            return int(response["order"])
        raise TJA470ResponseError("Expected a dict with 'order' from get_current_camera")

    async def switch_to_camera_position(self, uid: str, position: int, max_attempts: int = 10) -> int:
        """Switch the camera repeatedly until it reaches the specified position.

        The current position is checked first, so no switch is made if the camera
        is already at the target position.

        Args:
            uid: The registered client UUID.
            position: The target camera position index to switch to.
            max_attempts: Maximum number of switches to perform before giving up.

        Returns:
            int: The matched camera position index.

        Raises:
            TJA470ResponseError: If the target position is not found in the cycle or the attempt limit is reached.
        """
        current_pos = await self.get_current_camera(uid)
        if current_pos == position:
            return current_pos
        seen_positions = {current_pos}
        for attempt in range(max_attempts):
            current_pos = await self.switch_camera(uid)
            if current_pos == position:
                return current_pos
            if current_pos in seen_positions and len(seen_positions) > 1:
                raise TJA470ResponseError(
                    f"Target position {position} not found in the camera cycle (seen positions: {seen_positions})"
                )
            seen_positions.add(current_pos)
        raise TJA470ResponseError(
            f"Failed to switch to camera position {position} after {max_attempts} attempts"
        )

    async def open_door_at_position(
        self, uid: str, position: int, door_id: Optional[Union[int, str]] = None, max_attempts: int = 10
    ) -> None:
        """Switch the camera feed to the target position first, and then open the door.

        This ensures the correct door is released since the Hager TJA-470 releases
        the door corresponding to the currently active camera feed.

        Args:
            uid: The registered client UUID.
            position: The camera position index corresponding to the door.
            door_id: The door release ID (default: the client's own SIP id, see open_door).
            max_attempts: Maximum number of camera switches to attempt.

        Raises:
            TJA470ResponseError: If the camera position cannot be matched.
        """
        await self.switch_to_camera_position(uid, position, max_attempts=max_attempts)
        await self.open_door(door_id)

    async def open_door(self, door_id: Optional[Union[int, str]] = None) -> None:
        """Trigger the door release command for the currently active camera feed.

        The official app sends its own SIP id as the door release ID, so that is
        the default here. The device does not appear to validate the ID.

        Args:
            door_id: The door release ID. Defaults to the client's own SIP id from
                the last get_provisioning() call, or 1 if provisioning has not
                been fetched yet.
        """
        if door_id is None:
            door_id = self._sip_id if self._sip_id is not None else 1
        url = f"{self.base_url}/runtime/command/doorrelease/{door_id}"
        await self._request("POST", url, json={})

    async def events(self, topic: str = DOORPHONE_EVENTS_TOPIC) -> AsyncIterator[DoorphoneEvent]:
        """Subscribe to the device's event bus and yield events as they arrive.

        Events include `currentDevice/UPDATED` (camera position changed),
        `INCOMINGCALL/{id}` and `callhistory/CREATED/{id}` / `callhistory/UPDATED/{id}`.
        The iterator ends when the device closes the connection; reconnecting is
        up to the caller.

        Args:
            topic: The event topic pattern to subscribe to.

        Yields:
            DoorphoneEvent: The parsed events.

        Raises:
            TJA470AuthError: If authentication fails.
            TJA470ConnectionError: If the device cannot be reached.
        """
        url = f"ws://{self.host}/remote/events/?topics=[{topic}]"
        try:
            ws = await self._runner.ws_connect(url)
        except TJA470AuthError:
            # No valid session yet: log in with a regular request, then retry.
            await self.get_manifest()
            ws = await self._runner.ws_connect(url, authorization=self._authorization)

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except ValueError:
                        _LOGGER.debug("Ignoring non-JSON event: %s", msg.data)
                        continue
                    if isinstance(data, dict):
                        yield DoorphoneEvent.from_dict(data)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        finally:
            await ws.close()
