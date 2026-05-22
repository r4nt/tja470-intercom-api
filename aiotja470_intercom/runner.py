import aiohttp
import logging
from typing import Any, Dict, Optional, Protocol, Union

from .exceptions import TJA470Error, TJA470ConnectionError, TJA470AuthError, TJA470ResponseError

_LOGGER = logging.getLogger(__name__)


def _redact(data: Any) -> Any:
    """Redact sensitive fields from data for safe logging."""
    if isinstance(data, dict):
        return {
            k: "********" if any(secret_key in k.lower() for secret_key in ("password", "secret", "token", "cookie", "auth"))
            else _redact(v)
            for k, v in data.items()
        }
    elif isinstance(data, list):
        return [_redact(item) for item in data]
    elif isinstance(data, str):
        import json
        try:
            parsed = json.loads(data)
            return json.dumps(_redact(parsed))
        except (ValueError, TypeError):
            pass
    return data


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
            jar = aiohttp.CookieJar(unsafe=True)
            self._session = aiohttp.ClientSession(cookie_jar=jar)
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
        
        _LOGGER.debug(f"Request: {method} {url}")
        if json is not None:
            _LOGGER.debug(f"Request JSON: {_redact(json)}")

        from yarl import URL
        req_cookies = session.cookie_jar.filter_cookies(URL(url))
        if req_cookies:
            logged_cookies = {k: "********" for k in req_cookies.keys()}
            _LOGGER.debug(f"Sending Cookies: {logged_cookies}")

        try:
            async with session.request(method, url, auth=auth, json=json) as response:
                _LOGGER.debug(f"Response Status: {response.status}")
                logged_headers = dict(response.headers)
                if "Set-Cookie" in logged_headers:
                    logged_headers["Set-Cookie"] = "********"
                _LOGGER.debug(f"Response Headers: {logged_headers}")

                if response.status == 401 or response.status == 403:
                    raise TJA470AuthError("Authentication failed")
                
                response.raise_for_status()
                
                content_type = response.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    data = await response.json()
                elif "text/" in content_type:
                    data = await response.text()
                else:
                    data = await response.read()

                _LOGGER.debug(f"Response Content: {_redact(data)}")
                return data

        except aiohttp.ClientConnectorError as e:
            raise TJA470ConnectionError(f"Connection failed: {e}") from e
        except aiohttp.ClientResponseError as e:
            raise TJA470ResponseError(f"HTTP Error {e.status}: {e.message}") from e
        except TJA470Error:
            # Re-raise our own exceptions so they aren't wrapped by the catch-all
            raise
        except Exception as e:
            raise TJA470Error(f"An unexpected error occurred: {e}") from e

    def get_cookies(self, url: str) -> Dict[str, str]:
        if not self._session:
            return {}
        # We iterate over all cookies in the jar to ensure we don't miss any due to path/domain mismatches
        cookies = {}
        for cookie in self._session.cookie_jar:
            cookies[cookie.key] = cookie.value
        return cookies

    def set_cookies(self, url: str, cookies: Dict[str, str]) -> None:
        if not self._session:
            # We must instantiate the session first to have a cookie jar
            jar = aiohttp.CookieJar(unsafe=True)
            self._session = aiohttp.ClientSession(cookie_jar=jar)
            self._close_session = True
        
        from yarl import URL
        self._session.cookie_jar.update_cookies(cookies, response_url=URL(url))

    async def close(self) -> None:
        if self._session and self._close_session:
            await self._session.close()
