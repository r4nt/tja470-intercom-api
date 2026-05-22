import aiohttp
from typing import Any, Dict, List, Optional

from .exceptions import TJA470ResponseError, TJA470AuthError
from .models import FreeDevice, Manifest, ProvisioningInfo
from .runner import Runner

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
        self._auth = aiohttp.BasicAuth(username, password)
        self._runner = runner

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
            return await self._runner.request(method, url, auth=self._auth, json=json)

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

        return ProvisioningInfo.from_dict(response)

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

    async def switch_to_camera_position(self, uid: str, position: int, max_attempts: int = 10) -> int:
        """Switch the camera repeatedly until it reaches the specified position.

        Args:
            uid: The registered client UUID.
            position: The target camera position index to switch to.
            max_attempts: Maximum number of switches to perform before giving up.

        Returns:
            int: The matched camera position index.

        Raises:
            TJA470ResponseError: If the target position is not found in the cycle or the attempt limit is reached.
        """
        seen_positions = set()
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

    async def open_door_at_position(self, uid: str, position: int, door_id: int = 1, max_attempts: int = 10) -> None:
        """Switch the camera feed to the target position first, and then open the door.

        This ensures the correct door is released since the Hager TJA-470 releases
        the door corresponding to the currently active camera feed.

        Args:
            uid: The registered client UUID.
            position: The camera position index corresponding to the door.
            door_id: The door release ID (default: 1).
            max_attempts: Maximum number of camera switches to attempt.

        Raises:
            TJA470ResponseError: If the camera position cannot be matched.
        """
        await self.switch_to_camera_position(uid, position, max_attempts=max_attempts)
        await self.open_door(door_id)

    async def open_door(self, door_id: int = 1) -> None:
        """Trigger the door release command for the currently active camera feed.

        Args:
            door_id: The door release ID (default: 1).
        """
        url = f"{self.base_url}/runtime/command/doorrelease/{door_id}"
        await self._request("POST", url, json={})
