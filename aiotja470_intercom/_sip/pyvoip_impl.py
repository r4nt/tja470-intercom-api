import asyncio
import logging
from typing import Any, AsyncGenerator, Callable, Coroutine, Dict, Optional
import pyVoIP
from pyVoIP.VoIP import VoIPPhone, VoIPCall, CallState
from pyVoIP.VoIP.status import PhoneStatus

from ..exceptions import TJA470Error

_LOGGER = logging.getLogger(__name__)

# Monkey patch pyVoIP to support OPTIONS keep-alive requests.
if "OPTIONS" not in pyVoIP.SIPCompatibleMethods:
    pyVoIP.SIPCompatibleMethods.append("OPTIONS")

from pyVoIP.SIP import SIPClient, SIPMessage, SIPMessageType

_original_parse_message = SIPClient.parse_message


def _patched_parse_message(self, message: SIPMessage) -> None:
    if message.type == SIPMessageType.MESSAGE and message.method == "OPTIONS":
        _LOGGER.debug("Received SIP OPTIONS ping, responding with 200 OK")
        response = self.gen_ok(message)
        try:
            self.out.sendto(response.encode("utf8"), (self.server, self.port))
        except Exception as e:
            _LOGGER.error("Failed to send 200 OK response to OPTIONS request: %s", e)
        return
    _original_parse_message(self, message)


SIPClient.parse_message = _patched_parse_message


class TJA470SipError(TJA470Error):
    """Exception raised for errors during SIP operations."""
    pass


class TJA470SipCall:
    """Asynchronous wrapper for a pyVoIP call."""

    def __init__(self, raw_call: VoIPCall, loop: asyncio.AbstractEventLoop) -> None:
        self._raw_call = raw_call
        self._loop = loop

    @property
    def state(self) -> CallState:
        return self._raw_call.state

    @property
    def caller(self) -> str:
        try:
            return self._raw_call.request.headers.get("From", {}).get("number", "unknown")
        except Exception:
            return "unknown"

    async def answer(self) -> None:
        try:
            await self._loop.run_in_executor(None, self._raw_call.answer)
        except Exception as e:
            raise TJA470SipError(f"Failed to answer call: {e}") from e

    async def hangup(self) -> None:
        try:
            await self._loop.run_in_executor(None, self._raw_call.hangup)
        except Exception as e:
            raise TJA470SipError(f"Failed to hang up call: {e}") from e

    async def deny(self) -> None:
        try:
            await self._loop.run_in_executor(None, self._raw_call.deny)
        except Exception as e:
            raise TJA470SipError(f"Failed to deny call: {e}") from e

    def _safe_read_audio(self, length: int, blocking: bool) -> bytes:
        if (
            not hasattr(self._raw_call, "RTPClients")
            or not isinstance(self._raw_call.RTPClients, list)
            or not self._raw_call.RTPClients
        ):
            return self._raw_call.read_audio(length, blocking)
        
        client = self._raw_call.RTPClients[0]
        pmin = client.pmin
        import time

        if blocking:
            while self.state == CallState.ANSWERED:
                with pmin.bufferLock:
                    if pmin.log:
                        max_key = max(pmin.log.keys())
                        max_offset = max_key - pmin.offset + len(pmin.log[max_key])
                        if pmin.buffer.tell() < max_offset:
                            break
                time.sleep(0.01)

        with pmin.bufferLock:
            if not pmin.log:
                return b"\x80" * length
            
            bufferloc = pmin.buffer.tell()
            max_key = max(pmin.log.keys())
            max_offset = max_key - pmin.offset + len(pmin.log[max_key])
            
            if bufferloc >= max_offset:
                return b"\x80" * length
            
            readable = max_offset - bufferloc
            to_read = min(length, readable)
            
            packet = pmin.buffer.read(to_read)
            if len(packet) < length:
                packet = packet + (b"\x80" * (length - len(packet)))
            return packet

    async def read_audio(self, length: int = 160, blocking: bool = False) -> bytes:
        try:
            return await self._loop.run_in_executor(
                None, lambda: self._safe_read_audio(length, blocking)
            )
        except Exception as e:
            raise TJA470SipError(f"Failed to read audio: {e}") from e

    async def write_audio(self, data: bytes) -> None:
        try:
            await self._loop.run_in_executor(
                None, lambda: self._raw_call.write_audio(data)
            )
        except Exception as e:
            raise TJA470SipError(f"Failed to write audio: {e}") from e

    async def read_audio_16bit(self, length: int = 320, blocking: bool = False) -> bytes:
        raw_8bit = await self.read_audio(length // 2, blocking)
        import audioop
        try:
            signed_8bit = audioop.bias(raw_8bit, 1, -128)
            return audioop.lin2lin(signed_8bit, 1, 2)
        except Exception as e:
            raise TJA470SipError(f"Failed to convert audio from 8-bit to 16-bit: {e}") from e

    async def write_audio_16bit(self, data: bytes) -> None:
        import audioop
        try:
            raw_8bit_signed = audioop.lin2lin(data, 2, 1)
            raw_8bit_unsigned = audioop.bias(raw_8bit_signed, 1, 128)
        except Exception as e:
            raise TJA470SipError(f"Failed to convert audio from 16-bit to 8-bit: {e}") from e
        await self.write_audio(raw_8bit_unsigned)

    async def audio_stream(
        self, frame_size: int = 320, convert_16bit: bool = True
    ) -> AsyncGenerator[bytes, None]:
        samples = (frame_size // 2) if convert_16bit else frame_size
        frame_duration = samples / 8000.0
        while self.state == CallState.ANSWERED:
            try:
                if convert_16bit:
                    frame = await self.read_audio_16bit(frame_size, blocking=False)
                else:
                    frame = await self.read_audio(frame_size, blocking=False)
                yield frame
                await asyncio.sleep(frame_duration)
            except TJA470SipError:
                break
            except Exception:
                break


class TJA470SipPhone:
    def __init__(
        self,
        host: str,
        sip_id: str,
        sip_password: str,
        local_ip: str,
        sip_port: int = 5060,
        rtp_port: Optional[int] = None,
    ) -> None:
        self.host = host
        self.sip_id = sip_id
        self.sip_password = sip_password
        self.local_ip = local_ip
        self.sip_port = sip_port
        self.rtp_port = rtp_port

        self._phone: Optional[VoIPPhone] = None
        self._loop = asyncio.get_running_loop()

        self._on_incoming_call_cb: Optional[Callable[[TJA470SipCall], Coroutine[Any, Any, None]]] = None
        self._on_registration_state_changed_cb: Optional[Callable[[PhoneStatus], Coroutine[Any, Any, None]]] = None

    def register_incoming_call_callback(
        self, callback: Callable[[TJA470SipCall], Coroutine[Any, Any, None]]
    ) -> None:
        self._on_incoming_call_cb = callback

    def register_registration_state_callback(
        self, callback: Callable[[PhoneStatus], Coroutine[Any, Any, None]]
    ) -> None:
        self._on_registration_state_changed_cb = callback

    def get_status(self) -> PhoneStatus:
        if self._phone is None:
            return PhoneStatus.INACTIVE
        return self._phone.get_status()

    def _incoming_call_thread_callback(self, raw_call: VoIPCall) -> None:
        if self._on_incoming_call_cb is not None:
            call = TJA470SipCall(raw_call, self._loop)
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._on_incoming_call_cb(call))
            )

    def _fatal_thread_callback(self, exception: Exception) -> None:
        _LOGGER.error("Fatal SIP engine thread error: %s", exception)
        if self._on_registration_state_changed_cb is not None:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    self._on_registration_state_changed_cb(PhoneStatus.FAILED)
                )
            )

    async def start(self) -> None:
        if self._phone is not None:
            raise TJA470SipError("SIP Phone is already started")

        kwargs = {}
        if self.rtp_port is not None:
            kwargs["rtpPortLow"] = self.rtp_port
            kwargs["rtpPortHigh"] = self.rtp_port

        self._phone = VoIPPhone(
            server=self.host,
            port=5060,
            username=self.sip_id,
            password=self.sip_password,
            myIP=self.local_ip,
            sipPort=self.sip_port,
            callCallback=self._incoming_call_thread_callback,
            **kwargs
        )
        self._phone.sip.fatalCallback = self._fatal_thread_callback

        _LOGGER.debug("Starting VoIPPhone registration")
        try:
            await self._loop.run_in_executor(None, self._phone.start)
        except Exception as e:
            self._phone = None
            raise TJA470SipError(f"Failed to start SIP client: {e}") from e

    async def stop(self) -> None:
        if self._phone is None:
            return
        try:
            await self._loop.run_in_executor(None, self._phone.stop)
        except Exception as e:
            raise TJA470SipError(f"Failed to stop SIP client: {e}") from e
        finally:
            self._phone = None

    async def call(self, number: str) -> TJA470SipCall:
        if self._phone is None:
            raise TJA470SipError("SIP Client is not registered")
        try:
            raw_call = await self._loop.run_in_executor(None, lambda: self._phone.call(number))
            return TJA470SipCall(raw_call, self._loop)
        except Exception as e:
            raise TJA470SipError(f"Failed to initiate call to {number}: {e}") from e
