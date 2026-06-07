import asyncio
import logging
from typing import Any, Callable, Coroutine, Dict, Optional
import pyVoIP
from pyVoIP.VoIP import VoIPPhone, VoIPCall, CallState
from pyVoIP.VoIP.status import PhoneStatus

from .exceptions import TJA470Error

_LOGGER = logging.getLogger(__name__)


class TJA470SipError(TJA470Error):
    """Base class for all SIP client errors."""
    pass


class TJA470SipCall:
    """Async wrapper for a pyVoIP call."""

    def __init__(self, raw_call: VoIPCall, loop: asyncio.AbstractEventLoop) -> None:
        self._raw_call = raw_call
        self._loop = loop

    @property
    def state(self) -> CallState:
        """Get the current call state."""
        return self._raw_call.state

    @property
    def caller(self) -> str:
        """Get the caller number/SIP ID."""
        try:
            return self._raw_call.request.headers.get("From", {}).get("number", "unknown")
        except Exception:
            return "unknown"

    async def answer(self) -> None:
        """Answer the call."""
        try:
            await self._loop.run_in_executor(None, self._raw_call.answer)
        except Exception as e:
            raise TJA470SipError(f"Failed to answer call: {e}") from e

    async def hangup(self) -> None:
        """Hang up the call."""
        try:
            await self._loop.run_in_executor(None, self._raw_call.hangup)
        except Exception as e:
            raise TJA470SipError(f"Failed to hang up call: {e}") from e

    async def deny(self) -> None:
        """Reject the call."""
        try:
            await self._loop.run_in_executor(None, self._raw_call.deny)
        except Exception as e:
            raise TJA470SipError(f"Failed to deny call: {e}") from e

    async def read_audio(self, length: int = 160, blocking: bool = True) -> bytes:
        """Read audio frames from the call."""
        try:
            return await self._loop.run_in_executor(
                None, lambda: self._raw_call.read_audio(length, blocking)
            )
        except Exception as e:
            raise TJA470SipError(f"Failed to read audio: {e}") from e

    async def write_audio(self, data: bytes) -> None:
        """Write audio frames to the call."""
        try:
            await self._loop.run_in_executor(
                None, lambda: self._raw_call.write_audio(data)
            )
        except Exception as e:
            raise TJA470SipError(f"Failed to write audio: {e}") from e


class TJA470SipPhone:
    """Async SIP Phone client for the Hager TJA-470 intercom."""

    def __init__(
        self,
        host: str,
        sip_id: str,
        sip_password: str,
        local_ip: str,
        sip_port: int = 5060,
    ) -> None:
        self.host = host
        self.sip_id = sip_id
        self.sip_password = sip_password
        self.local_ip = local_ip
        self.sip_port = sip_port

        self._phone: Optional[VoIPPhone] = None
        self._loop = asyncio.get_running_loop()

        self._on_incoming_call_cb: Optional[Callable[[TJA470SipCall], Coroutine[Any, Any, None]]] = None
        self._on_registration_state_changed_cb: Optional[Callable[[PhoneStatus], Coroutine[Any, Any, None]]] = None

    def register_incoming_call_callback(
        self, callback: Callable[[TJA470SipCall], Coroutine[Any, Any, None]]
    ) -> None:
        """Register an async callback for incoming calls."""
        self._on_incoming_call_cb = callback

    def register_registration_state_callback(
        self, callback: Callable[[PhoneStatus], Coroutine[Any, Any, None]]
    ) -> None:
        """Register an async callback for registration status changes."""
        self._on_registration_state_changed_cb = callback

    def get_status(self) -> PhoneStatus:
        """Get the current SIP registration status."""
        if self._phone is None:
            return PhoneStatus.INACTIVE
        return self._phone.get_status()

    def _incoming_call_thread_callback(self, raw_call: VoIPCall) -> None:
        sip_call = TJA470SipCall(raw_call, self._loop)
        if self._on_incoming_call_cb:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._on_incoming_call_cb(sip_call))
            )

    def _fatal_thread_callback(self) -> None:
        _LOGGER.error("SIP registration encountered a fatal failure")
        if self._on_registration_state_changed_cb:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    self._on_registration_state_changed_cb(PhoneStatus.FAILED)
                )
            )

    async def start(self) -> None:
        """Start the SIP client and register with the server."""
        if self._phone is not None:
            raise TJA470SipError("SIP Phone is already started")

        self._phone = VoIPPhone(
            server=self.host,
            port=5060,
            username=self.sip_id,
            password=self.sip_password,
            myIP=self.local_ip,
            sipPort=self.sip_port,
            callCallback=self._incoming_call_thread_callback,
        )
        self._phone.sip.fatalCallback = self._fatal_thread_callback

        _LOGGER.debug("Starting VoIPPhone registration")
        try:
            await self._loop.run_in_executor(None, self._phone.start)
        except Exception as e:
            self._phone = None
            raise TJA470SipError(f"Failed to start SIP client: {e}") from e

    async def stop(self) -> None:
        """Stop the SIP client and unregister."""
        if self._phone is None:
            return
        try:
            await self._loop.run_in_executor(None, self._phone.stop)
        except Exception as e:
            raise TJA470SipError(f"Failed to stop SIP client: {e}") from e
        finally:
            self._phone = None

    async def call(self, number: str) -> TJA470SipCall:
        """Initiate an outgoing call."""
        if self._phone is None:
            raise TJA470SipError("SIP Client is not registered")
        try:
            raw_call = await self._loop.run_in_executor(None, lambda: self._phone.call(number))
            return TJA470SipCall(raw_call, self._loop)
        except Exception as e:
            raise TJA470SipError(f"Failed to initiate call to {number}: {e}") from e
