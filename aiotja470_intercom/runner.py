import aiohttp
from typing import Any, Dict, Optional, Protocol, Union

from .exceptions import TJA470ConnectionError, TJA470AuthError, TJA470ResponseError


class Runner(Protocol):
    """Protocol for executing HTTP requests."""

    async def request(
        self,
        method: str,
        url: str,
        auth: Optional[aiohttp.BasicAuth] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> Union[Dict[str, Any], str, bytes, None]:
        """Execute the HTTP request and return the parsed JSON, text, or bytes."""
        ...

    def get_cookies(self, url: str) -> Dict[str, str]:
        """Get the cookies currently stored for a specific URL."""
        ...

    def set_cookies(self, url: str, cookies: Dict[str, str]) -> None:
        """Set cookies for a specific URL."""
        ...

    async def close(self) -> None:
        """Close the underlying session/resources."""
        ...


class AiohttpRunner(Runner):
    """Runner implementation using aiohttp.ClientSession."""

    def __init__(self, session: Optional[aiohttp.ClientSession] = None) -> None:
        self._session = session
        self._close_session = False

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._close_session = True
        return self._session

    async def request(
        self,
        method: str,
        url: str,
        auth: Optional[aiohttp.BasicAuth] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> Union[Dict[str, Any], str, bytes, None]:
        session = await self._get_session()
        
        try:
            async with session.request(method, url, auth=auth, json=json) as response:
                if response.status == 401 or response.status == 403:
                    raise TJA470AuthError("Authentication failed")
                
                response.raise_for_status()
                
                content_type = response.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    return await response.json()
                elif "text/" in content_type:
                    return await response.text()
                else:
                    return await response.read()

        except aiohttp.ClientConnectorError as e:
            raise TJA470ConnectionError(f"Connection failed: {e}") from e
        except aiohttp.ClientResponseError as e:
            raise TJA470ResponseError(f"HTTP Error {e.status}: {e.message}") from e
        except Exception as e:
            raise TJA470Error(f"An unexpected error occurred: {e}") from e

    def get_cookies(self, url: str) -> Dict[str, str]:
        if not self._session:
            return {}
        cookies = self._session.cookie_jar.filter_cookies(url)
        return {name: cookie.value for name, cookie in cookies.items()}

    def set_cookies(self, url: str, cookies: Dict[str, str]) -> None:
        if not self._session:
            # We must instantiate the session first to have a cookie jar
            # A synchronous instantiation is possible but since we rely on _get_session
            # being async, we can just create it if missing (though passing loop may be needed)
            # A cleaner way is to ensure session exists, but for sync set_cookies, 
            # we can create it directly:
            self._session = aiohttp.ClientSession()
            self._close_session = True
        self._session.cookie_jar.update_cookies(cookies, response_url=url)

    async def close(self) -> None:
        if self._session and self._close_session:
            await self._session.close()
