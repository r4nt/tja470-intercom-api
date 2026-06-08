import asyncio
import logging
from typing import Any, AsyncGenerator, Callable, Coroutine, Dict, Optional
import pyVoIP
from pyVoIP.VoIP import VoIPPhone, VoIPCall, CallState
from pyVoIP.VoIP.status import PhoneStatus

from .exceptions import TJA470Error

_LOGGER = logging.getLogger(__name__)


class TJA470SipError(TJA470Error):
    """Exception raised for errors during SIP operations."""
    pass


class TJA470SipCall:
    """Asynchronous wrapper for a pyVoIP call.

    This class provides non-blocking, async methods to answer, deny, and hang up
    SIP calls, as well as read and write audio frames. It handles the threading
    by offloading blocking pyVoIP I/O operations to an executor thread.
    """

    def __init__(self, raw_call: VoIPCall, loop: asyncio.AbstractEventLoop) -> None:
        """Initialize the async SIP call wrapper.

        Args:
            raw_call: The underlying pyVoIP VoIPCall instance.
            loop: The asyncio event loop to run executor tasks in.
        """
        self._raw_call = raw_call
        self._loop = loop

    @property
    def state(self) -> CallState:
        """Get the current call state.

        Returns:
            CallState: The state of the VoIPCall (e.g. CallState.ANSWERED, CallState.ENDED).
        """
        return self._raw_call.state

    @property
    def caller(self) -> str:
        """Get the caller number or SIP ID.

        Returns:
            str: The caller's SIP username/number extracted from the 'From' header,
                or 'unknown' if not available.
        """
        try:
            return self._raw_call.request.headers.get("From", {}).get("number", "unknown")
        except Exception:
            return "unknown"

    async def answer(self) -> None:
        """Answer the incoming call.

        Raises:
            TJA470SipError: If the call cannot be answered.
        """
        try:
            await self._loop.run_in_executor(None, self._raw_call.answer)
        except Exception as e:
            raise TJA470SipError(f"Failed to answer call: {e}") from e

    async def hangup(self) -> None:
        """Hang up the call.

        Raises:
            TJA470SipError: If the call cannot be hung up.
        """
        try:
            await self._loop.run_in_executor(None, self._raw_call.hangup)
        except Exception as e:
            raise TJA470SipError(f"Failed to hang up call: {e}") from e

    async def deny(self) -> None:
        """Reject/deny the call.

        Raises:
            TJA470SipError: If the call cannot be rejected.
        """
        try:
            await self._loop.run_in_executor(None, self._raw_call.deny)
        except Exception as e:
            raise TJA470SipError(f"Failed to deny call: {e}") from e

    def _safe_read_audio(self, length: int, blocking: bool) -> bytes:
        """Safely read audio from the raw call buffer.

        This method is a helper designed to run in a background executor thread.
        It accesses the raw RTP buffer of pyVoIP to extract linear PCM frames
        while avoiding buffer underflows or blocks.

        Args:
            length: Number of bytes to read.
            blocking: If True, blocks until the requested length is available.

        Returns:
            bytes: The read audio data.
        """
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
        """Read raw audio frames from the call.

        By default, reads 8-bit linear PCM (PCMU/A-law width=1) at 8000Hz, mono.

        Args:
            length: Number of bytes/samples to read (default 160, which is 20ms of audio).
            blocking: Whether to block until the data is available.

        Returns:
            bytes: The raw 8-bit PCM audio frames.

        Raises:
            TJA470SipError: If reading audio fails.
        """
        try:
            return await self._loop.run_in_executor(
                None, lambda: self._safe_read_audio(length, blocking)
            )
        except Exception as e:
            raise TJA470SipError(f"Failed to read audio: {e}") from e

    async def write_audio(self, data: bytes) -> None:
        """Write raw audio frames to the call.

        By default, accepts 8-bit linear PCM (width=1) at 8000Hz, mono.

        Args:
            data: The 8-bit PCM audio frames to send.

        Raises:
            TJA470SipError: If writing audio fails.
        """
        try:
            await self._loop.run_in_executor(
                None, lambda: self._raw_call.write_audio(data)
            )
        except Exception as e:
            raise TJA470SipError(f"Failed to write audio: {e}") from e

    async def read_audio_16bit(self, length: int = 320, blocking: bool = False) -> bytes:
        """Read audio frames converted to standard 16-bit linear PCM at 8000Hz.

        Args:
            length: The requested size of the 16-bit audio buffer in bytes
                (default 320, which is 160 samples of 2 bytes each, or 20ms).
            blocking: Whether to block until the data is available.

        Returns:
            bytes: The 16-bit linear PCM audio frames.

        Raises:
            TJA470SipError: If reading or conversion fails.
        """
        # pyVoIP's read_audio yields width=1 (8-bit) samples. 
        # A 16-bit sample (width=2) requires half the sample count for the same bytes length.
        raw_8bit = await self.read_audio(length // 2, blocking)
        import audioop
        try:
            # pyVoIP uses unsigned 8-bit PCM (silence is 128/0x80).
            # Convert unsigned 8-bit to signed 8-bit (subtract 128) before converting to 16-bit.
            signed_8bit = audioop.bias(raw_8bit, 1, -128)
            return audioop.lin2lin(signed_8bit, 1, 2)
        except Exception as e:
            raise TJA470SipError(f"Failed to convert audio from 8-bit to 16-bit: {e}") from e

    async def write_audio_16bit(self, data: bytes) -> None:
        """Write audio frames provided in standard 16-bit linear PCM at 8000Hz.

        Args:
            data: The 16-bit linear PCM audio frames to send.

        Raises:
            TJA470SipError: If writing or conversion fails.
        """
        import audioop
        try:
            # Convert signed 16-bit to signed 8-bit.
            raw_8bit_signed = audioop.lin2lin(data, 2, 1)
            # pyVoIP expects unsigned 8-bit PCM. Convert signed 8-bit to unsigned 8-bit (add 128).
            raw_8bit_unsigned = audioop.bias(raw_8bit_signed, 1, 128)
        except Exception as e:
            raise TJA470SipError(f"Failed to convert audio from 16-bit to 8-bit: {e}") from e
        await self.write_audio(raw_8bit_unsigned)


    async def audio_stream(
        self, frame_size: int = 320, convert_16bit: bool = True
    ) -> AsyncGenerator[bytes, None]:
        """Async generator yielding incoming audio frames (8000Hz, mono).

        Args:
            frame_size: Size in bytes of each yielded frame (default 320 bytes,
                which is 20ms of 16-bit audio or 40ms of 8-bit audio).
            convert_16bit: If True, yields 16-bit PCM; otherwise, 8-bit PCM.

        Yields:
            bytes: An audio frame of the requested size.
        """
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
    """Async SIP Phone client for the Hager TJA-470 intercom.

    This class handles connecting and registering with the Hager TJA-470 intercom
    SIP server, listening for incoming calls, registering callback handlers,
    and initiating outgoing calls.
    """

    def __init__(
        self,
        host: str,
        sip_id: str,
        sip_password: str,
        local_ip: str,
        sip_port: int = 5060,
        rtp_port: Optional[int] = None,
    ) -> None:
        """Initialize the SIP Phone client.

        Args:
            host: The IP address or hostname of the TJA470 SIP server.
            sip_id: The SIP ID/username used to register (e.g. extension number).
            sip_password: The SIP password.
            local_ip: The local IP address of the machine running this client.
            sip_port: The local SIP port to bind to (default: 5060).
            rtp_port: The local RTP port to bind to (optional).
        """
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
        """Register an async callback for incoming calls.

        Args:
            callback: An async callback function that receives a TJA470SipCall.
        """
        self._on_incoming_call_cb = callback

    def register_registration_state_callback(
        self, callback: Callable[[PhoneStatus], Coroutine[Any, Any, None]]
    ) -> None:
        """Register an async callback for registration status changes.

        Args:
            callback: An async callback function that receives a PhoneStatus.
        """
        self._on_registration_state_changed_cb = callback

    def get_status(self) -> PhoneStatus:
        """Get the current SIP registration status.

        Returns:
            PhoneStatus: The current registration state of the phone.
        """
        if self._phone is None:
            return PhoneStatus.INACTIVE
        return self._phone.get_status()

    def _incoming_call_thread_callback(self, raw_call: VoIPCall) -> None:
        """Internal callback invoked by pyVoIP threads when a call is received.

        This method schedules the user's async callback thread-safely on the
        main asyncio event loop.

        Args:
            raw_call: The raw VoIPCall instance from pyVoIP.
        """
        if self._on_incoming_call_cb is not None:
            call = TJA470SipCall(raw_call, self._loop)
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._on_incoming_call_cb(call))
            )

    def _fatal_thread_callback(self, exception: Exception) -> None:
        """Internal callback invoked by pyVoIP threads when a fatal error occurs.

        This method schedules the user's state change callback thread-safely on
        the main asyncio event loop to notify of registration failure.

        Args:
            exception: The exception raised by pyVoIP.
        """
        _LOGGER.error("Fatal SIP engine thread error: %s", exception)
        if self._on_registration_state_changed_cb is not None:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    self._on_registration_state_changed_cb(PhoneStatus.FAILED)
                )
            )

    async def start(self) -> None:
        """Start the SIP client, bind sockets, and register with the server.

        Raises:
            TJA470SipError: If the SIP client is already started or fails to start.
        """
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
        """Stop the SIP client and unregister from the server.

        Raises:
            TJA470SipError: If stopping the client fails.
        """
        if self._phone is None:
            return
        try:
            await self._loop.run_in_executor(None, self._phone.stop)
        except Exception as e:
            raise TJA470SipError(f"Failed to stop SIP client: {e}") from e
        finally:
            self._phone = None

    async def call(self, number: str) -> TJA470SipCall:
        """Initiate an outgoing call.

        Args:
            number: The SIP extension number/address to call.

        Returns:
            TJA470SipCall: The initiated call instance.

        Raises:
            TJA470SipError: If the phone is not registered or the call fails.
        """
        if self._phone is None:
            raise TJA470SipError("SIP Client is not registered")
        try:
            raw_call = await self._loop.run_in_executor(None, lambda: self._phone.call(number))
            return TJA470SipCall(raw_call, self._loop)
        except Exception as e:
            raise TJA470SipError(f"Failed to initiate call to {number}: {e}") from e

