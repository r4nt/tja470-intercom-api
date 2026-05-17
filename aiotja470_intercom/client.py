import aiohttp
from typing import Any, Dict, List, Optional

from .exceptions import TJA470ResponseError, TJA470AuthError
from .models import FreeDevice, Manifest, ProvisioningInfo
from .runner import Runner

class TJA470IntercomClient:
    """Client for the Hager TJA470 Intercom API."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        runner: Runner,
    ) -> None:
        self.host = host
        self._auth = aiohttp.BasicAuth(username, password)
        self._runner = runner

    @property
    def base_url(self) -> str:
        return f"http://{self.host}/API"

    def get_cookies(self) -> dict[str, str]:
        """Get the current cookies for the API base URL."""
        return self._runner.get_cookies(self.base_url)

    def set_cookies(self, cookies: dict[str, str]) -> None:
        """Set cookies for the API base URL."""
        self._runner.set_cookies(self.base_url, cookies)

    async def _request(self, method: str, url: str, json: Optional[Dict[str, Any]] = None) -> Any:
        try:
            return await self._runner.request(method, url, json=json)
        except TJA470AuthError:
            return await self._runner.request(method, url, auth=self._auth, json=json)

    async def get_manifest(self) -> Manifest:
        """Verify authentication and retrieve the API manifest."""
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
        """List devices available for pairing."""
        url = f"{self.base_url}/runtime/provisioning/freedevices"
        response = await self._request("GET", url)
        
        if not isinstance(response, list):
            raise TJA470ResponseError("Expected a list of free devices")

        return [FreeDevice.from_dict(item) for item in response]

    async def set_uid(self, device_id: int, uid: str) -> None:
        """Register the UUID as client to the device."""
        url = f"{self.base_url}/runtime/pairing/setuid"
        payload = {
            "id": device_id,
            "uid": uid,
            "description": ""
        }
        await self._request("POST", url, json=payload)

    async def get_provisioning(self, uid: str) -> ProvisioningInfo:
        """Retrieve the configuration details for the client."""
        url = f"{self.base_url}/runtime/provisioning"
        payload = {"uid": uid}
        response = await self._request("POST", url, json=payload)
        
        if not isinstance(response, dict):
            raise TJA470ResponseError("Expected a dictionary for provisioning info")

        return ProvisioningInfo.from_dict(response)

    async def switch_camera(self, uid: str) -> None:
        """Switch the camera between different intercom views."""
        url = f"{self.base_url}/runtime/command/camera/switch/{uid}"
        await self._request("POST", url, json={})

    async def open_door(self, door_id: int = 1) -> None:
        """Trigger the door release."""
        url = f"{self.base_url}/runtime/command/doorrelease/{door_id}"
        await self._request("POST", url, json={})
