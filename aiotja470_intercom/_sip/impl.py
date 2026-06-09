import asyncio
import logging
import struct
import os
import hashlib
import re
import socket
import audioop
from typing import Any, AsyncGenerator, Callable, Coroutine, Optional, Dict

from pyVoIP.VoIP.status import PhoneStatus
from pyVoIP.VoIP import CallState

from ..exceptions import TJA470Error

_LOGGER = logging.getLogger(__name__)


class TJA470SipError(TJA470Error):
    """Exception raised for errors during SIP operations."""
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code



class SipMessage:
    """Helper to parse raw SIP network messages."""

    def __init__(self, raw: bytes):
        self.raw = raw
        self.headers: Dict[str, Any] = {}
        self.body = b""
        self.method = ""
        self.uri = ""
        self.version = ""
        self.status_code = 0
        self.status_phrase = ""
        self.is_request = False

        parts = raw.split(b"\r\n\r\n", 1)
        header_part = parts[0]
        if len(parts) > 1:
            self.body = parts[1]

        lines = header_part.split(b"\r\n")
        if not lines or not lines[0]:
            return
        start_line = lines[0].decode("utf-8", errors="replace")

        start_parts = start_line.split(" ", 2)
        if start_parts[0].startswith("SIP/"):
            self.is_request = False
            self.version = start_parts[0]
            if len(start_parts) > 1:
                try:
                    self.status_code = int(start_parts[1])
                except ValueError:
                    self.status_code = 0
            self.status_phrase = start_parts[2] if len(start_parts) > 2 else ""
        else:
            self.is_request = True
            self.method = start_parts[0]
            self.uri = start_parts[1] if len(start_parts) > 1 else ""
            self.version = start_parts[2] if len(start_parts) > 2 else "SIP/2.0"

        for line in lines[1:]:
            if not line:
                continue
            if b":" not in line:
                continue
            k, v = line.split(b":", 1)
            key = k.strip().decode("utf-8", errors="replace")
            val = v.strip().decode("utf-8", errors="replace")
            # Handle multiple headers like Via as a list
            key_lower = key.lower()
            if key_lower in self.headers:
                if isinstance(self.headers[key_lower], list):
                    self.headers[key_lower].append(val)
                else:
                    self.headers[key_lower] = [self.headers[key_lower], val]
            else:
                self.headers[key_lower] = val

    def get_header(self, key: str) -> str:
        """Case-insensitively retrieve header value."""
        val = self.headers.get(key.lower(), "")
        if isinstance(val, list):
            return val[0]
        return val


def parse_www_authenticate(header_val: str) -> Dict[str, str]:
    """Parse digest authenticate challenge parameters."""
    params = {}
    if not header_val.startswith("Digest"):
        return params
    header_val = header_val[7:]
    matches = re.findall(r'(\w+)="?([^",]+)"?', header_val)
    for k, v in matches:
        params[k] = v
    return params


def compute_digest_auth(username: str, password: str, method: str, uri: str, challenge: Dict[str, str]) -> str:
    """Compute standard SIP MD5 digest authentication response."""
    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    qop = challenge.get("qop", "")
    algorithm = challenge.get("algorithm", "MD5")
    opaque = challenge.get("opaque", "")

    cnonce = os.urandom(8).hex()
    nc = "00000001"

    if algorithm.upper() == "MD5":
        a1 = f"{username}:{realm}:{password}"
    else:
        raise NotImplementedError(f"Unsupported algorithm {algorithm}")
    ha1 = hashlib.md5(a1.encode("utf-8")).hexdigest()

    a2 = f"{method}:{uri}"
    ha2 = hashlib.md5(a2.encode("utf-8")).hexdigest()

    if qop == "auth" or "auth" in qop.split(","):
        response = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode("utf-8")).hexdigest()
        auth_hdr = (
            f'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{response}", algorithm="MD5", '
            f'cnonce="{cnonce}", qop="auth", nc={nc}'
        )
    else:
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode("utf-8")).hexdigest()
        auth_hdr = (
            f'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{response}"'
        )

    if opaque:
        auth_hdr += f', opaque="{opaque}"'

    return auth_hdr


def parse_sdp(sdp_body: bytes) -> Dict[str, Any]:
    """Parse remote media connection IP, port and codec payload type from SDP."""
    info = {"ip": "", "port": 0, "codec": 0}
    sdp_str = sdp_body.decode("utf-8", errors="replace")
    for line in sdp_str.split("\n"):
        line = line.strip()
        if line.startswith("c="):
            parts = line.split(" ")
            if len(parts) >= 3:
                info["ip"] = parts[2].split("/")[-1] # IP4 connection IP
        elif line.startswith("m="):
            parts = line.split(" ")
            if len(parts) >= 4:
                try:
                    info["port"] = int(parts[1])
                    info["codec"] = int(parts[3])
                except ValueError:
                    pass
    return info


class SipProtocol(asyncio.DatagramProtocol):
    """Protocol callback handler for the SIP UDP socket."""

    def __init__(self, phone: "TJA470SipPhone"):
        self.phone = phone
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        asyncio.create_task(self.phone._handle_sip_packet(data, addr))


class RtpProtocol(asyncio.DatagramProtocol):
    """Protocol callback handler for the RTP UDP socket."""

    def __init__(self, call: "TJA470SipCall"):
        self.call = call
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.call._handle_rtp_packet(data)


class TJA470SipCall:
    """Asynchronous drop-in wrapper representing a SIP call session."""

    def __init__(
        self,
        phone: "TJA470SipPhone",
        is_incoming: bool,
        remote_tag: str = "",
        call_id: str = "",
        cseq_num: int = 1,
        remote_uri: str = "",
        from_hdr: str = "",
        to_hdr: str = "",
    ):
        self.phone = phone
        self.is_incoming = is_incoming
        self.remote_tag = remote_tag
        self.local_tag = os.urandom(8).hex()
        self.call_id = call_id or f"{os.urandom(16).hex()}@{phone.local_ip}"
        self.cseq_num = cseq_num
        self.remote_uri = remote_uri

        if is_incoming:
            self._from_hdr = from_hdr or f"<sip:unknown@{phone.host}>"
            self._to_hdr = to_hdr or f"<sip:{phone.sip_id}@{phone.host}>"
            if ";tag=" not in self._to_hdr:
                self._to_hdr = f"{self._to_hdr};tag={self.local_tag}"
        else:
            self._from_hdr = f"<sip:{phone.sip_id}@{phone.host}>;tag={self.local_tag}"
            self._to_hdr = f"<{remote_uri}>"

        self._state = CallState.RINGING if is_incoming else CallState.DIALING
        self._caller = "unknown"
        self._on_state_changed_cb = None

        self.rtp_port = 0
        self.remote_rtp_ip = ""
        self.remote_rtp_port = 0
        self.codec = 0  # 0 for PCMU, 8 for PCMA

        self._rtp_transport: Optional[asyncio.DatagramTransport] = None
        self._incoming_audio_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._read_buffer = bytearray()

        self._rtp_seq_num = 0
        self._rtp_timestamp = 0
        self._rtp_ssrc = struct.unpack(">I", os.urandom(4))[0]

        # Future used to wait for DIALING -> ANSWERED transitions in outbound calls
        self._answered_future: Optional[asyncio.Future[None]] = None

        self._silence_generator_task: Optional[asyncio.Task] = None
        self._last_write_time = 0.0

    @property
    def state(self) -> CallState:
        """Get the current call state."""
        return self._state

    def register_state_callback(
        self, callback: Callable[[CallState], Coroutine[Any, Any, None]]
    ) -> None:
        """Register an async callback for call state changes."""
        self._on_state_changed_cb = callback

    async def _notify_state_changed(self) -> None:
        if self._state == CallState.ANSWERED and not self._silence_generator_task:
            self._silence_generator_task = asyncio.create_task(self._silence_loop())
        elif self._state == CallState.ENDED and self._silence_generator_task:
            self._silence_generator_task.cancel()
            self._silence_generator_task = None

        if self._on_state_changed_cb:
            try:
                await self._on_state_changed_cb(self._state)
            except Exception as e:
                _LOGGER.error("Error in call state callback: %s", e)

    async def _silence_loop(self):
        """Loop sending G.711 PCMU/PCMA silence packets to keep call alive when no audio is written."""
        linear_silence = b"\x00" * 320
        try:
            if self.codec == 0:
                silence_packet = audioop.lin2ulaw(linear_silence, 2)
            elif self.codec == 8:
                silence_packet = audioop.lin2alaw(linear_silence, 2)
            else:
                silence_packet = b"\xff" * 160
        except Exception:
            silence_packet = b"\xff" * 160

        try:
            while self._state == CallState.ANSWERED:
                current_time = asyncio.get_running_loop().time()
                # If no audio was written in the last 40ms, send a silence packet
                if current_time - self._last_write_time > 0.04:
                    if self._rtp_transport:
                        self._rtp_seq_num = (self._rtp_seq_num + 1) & 0xFFFF
                        self._rtp_timestamp = (self._rtp_timestamp + 160) & 0xFFFFFFFF
                        header = struct.pack(">BBHII", 0x80, self.codec, self._rtp_seq_num, self._rtp_timestamp, self._rtp_ssrc)
                        packet = header + silence_packet
                        try:
                            self._rtp_transport.sendto(packet, (self.remote_rtp_ip, self.remote_rtp_port))
                        except Exception:
                            pass
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            pass

    @property
    def caller(self) -> str:
        """Get the caller number/SIP ID."""
        return self._caller

    def _handle_rtp_packet(self, data: bytes):
        """Parse incoming RTP packets and push decoded PCM to queue."""
        if len(data) < 12:
            return
        
        # Verify RTP payload type matches
        payload_type = data[1] & 0x7F
        payload = data[12:]

        try:
            if payload_type == 0:
                # Decode PCMU to signed 16-bit PCM
                pcm_16bit = audioop.ulaw2lin(payload, 2)
                self._incoming_audio_queue.put_nowait(pcm_16bit)
            elif payload_type == 8:
                # Decode PCMA to signed 16-bit PCM
                pcm_16bit = audioop.alaw2lin(payload, 2)
                self._incoming_audio_queue.put_nowait(pcm_16bit)
        except Exception as e:
            _LOGGER.debug("Error decoding RTP audio: %s", e)

    async def _bind_rtp(self):
        """Bind local RTP UDP socket."""
        loop = asyncio.get_running_loop()
        self.rtp_port = self.phone.rtp_port or self.phone._get_free_port()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: RtpProtocol(self),
            local_addr=(self.phone.local_ip, self.rtp_port)
        )
        self._rtp_transport = transport

    async def answer(self) -> None:
        """Answer the incoming call."""
        if self._state == CallState.ENDED:
            return

        try:
            await self._bind_rtp()

            # Generate SDP response
            sdp = (
                f"v=0\r\n"
                f"o=aiotja470 0 0 IN IP4 {self.phone.local_ip}\r\n"
                f"s=Talk\r\n"
                f"c=IN IP4 {self.phone.local_ip}\r\n"
                f"t=0 0\r\n"
                f"m=audio {self.rtp_port} RTP/AVP {self.codec}\r\n"
                f"a=rtpmap:{self.codec} {'PCMU' if self.codec == 0 else 'PCMA'}/8000\r\n"
            )

            # Send 200 OK
            response = (
                f"SIP/2.0 200 OK\r\n"
                f"Via: {self.phone._last_via_header}\r\n"
                f"From: {self._from_hdr}\r\n"
                f"To: {self._to_hdr}\r\n"
                f"Call-ID: {self.call_id}\r\n"
                f"CSeq: {self.cseq_num} INVITE\r\n"
                f"Contact: <sip:{self.phone.sip_id}@{self.phone.local_ip}:{self.phone.sip_port}>\r\n"
                f"Content-Type: application/sdp\r\n"
                f"Content-Length: {len(sdp)}\r\n\r\n"
                f"{sdp}"
            )

            self.phone._send_packet(response.encode("utf-8"), self.phone._remote_addr)
            self._state = CallState.ANSWERED
            await self._notify_state_changed()
        except Exception as e:
            await self._cleanup()
            raise TJA470SipError(f"Failed to answer call: {e}") from e

    async def hangup(self) -> None:
        """Hang up the call."""
        if self._state == CallState.ENDED:
            return

        try:
            self.cseq_num += 1
            # Send BYE
            bye = (
                f"BYE {self.remote_uri} SIP/2.0\r\n"
                f"Via: SIP/2.0/UDP {self.phone.local_ip}:{self.phone.sip_port};branch=z9hG4bK{os.urandom(8).hex()}\r\n"
                f"From: {self._from_hdr}\r\n"
                f"To: {self._to_hdr}\r\n"
                f"Call-ID: {self.call_id}\r\n"
                f"CSeq: {self.cseq_num} BYE\r\n"
                f"Max-Forwards: 70\r\n"
                f"Content-Length: 0\r\n\r\n"
            )
            self.phone._send_packet(bye.encode("utf-8"), self.phone._remote_addr)
        except Exception as e:
            _LOGGER.debug("Error sending BYE: %s", e)
        finally:
            await self._cleanup()

    async def deny(self) -> None:
        """Reject the call."""
        if self._state == CallState.ENDED:
            return

        try:
            # Send 480 Temporarily Unavailable
            response = (
                f"SIP/2.0 480 Temporarily Unavailable\r\n"
                f"Via: {self.phone._last_via_header}\r\n"
                f"From: {self._from_hdr}\r\n"
                f"To: {self._to_hdr}\r\n"
                f"Call-ID: {self.call_id}\r\n"
                f"CSeq: {self.cseq_num} INVITE\r\n"
                f"Content-Length: 0\r\n\r\n"
            )
            self.phone._send_packet(response.encode("utf-8"), self.phone._remote_addr)
        except Exception as e:
            raise TJA470SipError(f"Failed to deny call: {e}") from e
        finally:
            await self._cleanup()

    async def _cleanup(self):
        """Clean up sockets and set state to ENDED."""
        self._state = CallState.ENDED
        if self._rtp_transport:
            self._rtp_transport.close()
            self._rtp_transport = None
        if self.phone._active_call == self:
            self.phone._active_call = None
        await self._notify_state_changed()


    async def read_audio(self, length: int = 160, blocking: bool = False) -> bytes:
        """Read raw audio frames from the call (default 8-bit linear PCM at 8000Hz)."""
        # Read 16-bit samples first (which are double the size of 8-bit samples)
        pcm_16bit = await self.read_audio_16bit(length * 2, blocking)
        try:
            # Convert signed 16-bit to signed 8-bit
            pcm_8bit_signed = audioop.lin2lin(pcm_16bit, 2, 1)
            # Convert signed 8-bit to unsigned 8-bit (add 128 bias)
            return audioop.bias(pcm_8bit_signed, 1, 128)
        except Exception as e:
            raise TJA470SipError(f"Failed to down-convert audio: {e}") from e

    async def write_audio(self, data: bytes) -> None:
        """Write raw audio frames to the call (default 8-bit linear PCM at 8000Hz)."""
        try:
            # Convert unsigned 8-bit (with 128 bias) to signed 8-bit (subtract 128)
            signed_8bit = audioop.bias(data, 1, -128)
            # Convert signed 8-bit to signed 16-bit
            pcm_16bit = audioop.lin2lin(signed_8bit, 1, 2)
            await self.write_audio_16bit(pcm_16bit)
        except Exception as e:
            raise TJA470SipError(f"Failed to write raw audio: {e}") from e

    async def read_audio_16bit(self, length: int = 320, blocking: bool = False) -> bytes:
        """Read audio frames converted to standard 16-bit linear PCM at 8000Hz."""
        if self._state != CallState.ANSWERED and self._state != CallState.ENDED: # Ended can still read remainder
            return b"\x00" * length

        while len(self._read_buffer) < length:
            if blocking:
                try:
                    packet = await asyncio.wait_for(self._incoming_audio_queue.get(), timeout=1.0)
                    self._read_buffer.extend(packet)
                except asyncio.TimeoutError:
                    self._read_buffer.extend(b"\x00" * (length - len(self._read_buffer)))
                    break
            else:
                if not self._incoming_audio_queue.empty():
                    packet = self._incoming_audio_queue.get_nowait()
                    self._read_buffer.extend(packet)
                else:
                    # Pad with silence
                    self._read_buffer.extend(b"\x00" * (length - len(self._read_buffer)))
                    break

        ret = bytes(self._read_buffer[:length])
        del self._read_buffer[:length]
        return ret

    async def write_audio_16bit(self, data: bytes) -> None:
        """Write audio frames provided in standard 16-bit linear PCM at 8000Hz."""
        if self._state != CallState.ANSWERED or not self._rtp_transport:
            return

        self._last_write_time = asyncio.get_running_loop().time()

        try:
            if self.codec == 0:
                payload = audioop.lin2ulaw(data, 2)
            elif self.codec == 8:
                payload = audioop.lin2alaw(data, 2)
            else:
                payload = data

            # Increment sequence and timestamp
            self._rtp_seq_num = (self._rtp_seq_num + 1) & 0xFFFF
            self._rtp_timestamp = (self._rtp_timestamp + (len(data) // 2)) & 0xFFFFFFFF

            # Build RTP packet
            header = struct.pack(">BBHII", 0x80, self.codec, self._rtp_seq_num, self._rtp_timestamp, self._rtp_ssrc)
            packet = header + payload

            self._rtp_transport.sendto(packet, (self.remote_rtp_ip, self.remote_rtp_port))
        except Exception as e:
            raise TJA470SipError(f"Failed to write audio: {e}") from e

    async def audio_stream(
        self, frame_size: int = 320, convert_16bit: bool = True
    ) -> AsyncGenerator[bytes, None]:
        """Async generator yielding incoming audio frames (8000Hz, mono)."""
        samples = (frame_size // 2) if convert_16bit else frame_size
        frame_duration = samples / 8000.0
        while self._state == CallState.ANSWERED:
            try:
                if convert_16bit:
                    frame = await self.read_audio_16bit(frame_size, blocking=False)
                else:
                    frame = await self.read_audio(frame_size, blocking=False)
                yield frame
                await asyncio.sleep(frame_duration)
            except Exception:
                break


class TJA470SipPhone:
    """Asynchronous SIP Phone client built natively for Hager TJA-470."""

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

        self._remote_addr = (host, 5060)
        self._sip_transport: Optional[asyncio.DatagramTransport] = None
        self._cseq = 0
        self._status = PhoneStatus.INACTIVE
        self._active_call: Optional[TJA470SipCall] = None

        self._on_incoming_call_cb: Optional[Callable[[TJA470SipCall], Coroutine[Any, Any, None]]] = None
        self._on_registration_state_changed_cb: Optional[Callable[[PhoneStatus], Coroutine[Any, Any, None]]] = None

        self._registration_task: Optional[asyncio.Task] = None
        self._last_via_header = ""
        self._unregister_future: Optional[asyncio.Future[None]] = None
        self._unregister_call_id = ""

        # Preemptive authentication state
        self._reg_call_id = f"{os.urandom(16).hex()}@{local_ip}"
        self._reg_from_tag = os.urandom(8).hex()
        self._cached_auth_challenge: Optional[Dict[str, str]] = None

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
        return self._status

    def _get_free_port(self) -> int:
        """Get a free ephemeral UDP port."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def _send_packet(self, data: bytes, addr: tuple):
        """Send raw packet over UDP SIP socket."""
        _LOGGER.debug("Sending SIP packet to %s:\n%s", addr, data.decode("utf-8", errors="replace"))
        if self._sip_transport:
            self._sip_transport.sendto(data, addr)

    def send_bye(self, call_id: str, from_hdr: str, to_hdr: str, remote_uri: str) -> None:
        """Send a BYE request to terminate an active call leg (e.g. to clean up a stale call on startup)."""
        self._cseq += 1
        bye = (
            f"BYE {remote_uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.sip_port};branch=z9hG4bK{os.urandom(8).hex()}\r\n"
            f"From: {from_hdr}\r\n"
            f"To: {to_hdr}\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {self._cseq} BYE\r\n"
            f"Max-Forwards: 70\r\n"
            f"Content-Length: 0\r\n\r\n"
        )
        self._send_packet(bye.encode("utf-8"), self._remote_addr)

    async def start(self) -> None:
        """Start the SIP client, bind socket, and register."""
        if self._sip_transport:
            raise TJA470SipError("SIP Phone is already started")

        loop = asyncio.get_running_loop()
        self._loop = loop
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: SipProtocol(self),
                local_addr=(self.local_ip, self.sip_port)
            )
            self._sip_transport = transport
        except Exception as e:
            raise TJA470SipError(f"Failed to start SIP socket: {e}") from e

        self._registration_task = asyncio.create_task(self._registration_loop())

    async def stop(self) -> None:
        """Stop the SIP client and unregister."""
        if not self._sip_transport:
            return

        if self._active_call:
            await self._active_call.hangup()

        if self._registration_task:
            self._registration_task.cancel()
            self._registration_task = None

        # Send deregister REGISTER
        try:
            self._cseq += 1
            self._unregister_call_id = self._reg_call_id
            
            auth_header = ""
            if self._cached_auth_challenge:
                auth_str = compute_digest_auth(
                    self.sip_id,
                    self.sip_password,
                    "REGISTER",
                    f"sip:{self.host}",
                    self._cached_auth_challenge
                )
                auth_header = f"Authorization: {auth_str}\r\n"

            dereg = (
                f"REGISTER sip:{self.host} SIP/2.0\r\n"
                f"Via: SIP/2.0/UDP {self.local_ip}:{self.sip_port};branch=z9hG4bK{os.urandom(8).hex()}\r\n"
                f"From: <sip:{self.sip_id}@{self.host}>;tag={self._reg_from_tag}\r\n"
                f"To: <sip:{self.sip_id}@{self.host}>\r\n"
                f"Call-ID: {self._reg_call_id}\r\n"
                f"CSeq: {self._cseq} REGISTER\r\n"
                f"Contact: *\r\n"
                f"{auth_header}"
                f"Expires: 0\r\n"
                f"Content-Length: 0\r\n\r\n"
            )
            self._unregister_future = self._loop.create_future()
            self._send_packet(dereg.encode("utf-8"), self._remote_addr)
            # Wait for unregistration confirmation or timeout
            try:
                await asyncio.wait_for(self._unregister_future, timeout=2.0)
            except asyncio.TimeoutError:
                _LOGGER.debug("Deregister timeout")
        except Exception as e:
            _LOGGER.debug("Deregister failed: %s", e)

        self._sip_transport.close()
        self._sip_transport = None
        self._set_status(PhoneStatus.INACTIVE)

    def _set_status(self, new_status: PhoneStatus):
        """Update registration status and notify callback."""
        if self._status != new_status:
            self._status = new_status
            if self._on_registration_state_changed_cb:
                asyncio.create_task(self._on_registration_state_changed_cb(new_status))

    async def _registration_loop(self):
        """Loop handling register refresh."""
        while True:
            try:
                self._cseq += 1
                auth_header = ""
                if self._cached_auth_challenge:
                    auth_str = compute_digest_auth(
                        self.sip_id,
                        self.sip_password,
                        "REGISTER",
                        f"sip:{self.host}",
                        self._cached_auth_challenge
                    )
                    auth_header = f"Authorization: {auth_str}\r\n"

                reg = (
                    f"REGISTER sip:{self.host} SIP/2.0\r\n"
                    f"Via: SIP/2.0/UDP {self.local_ip}:{self.sip_port};branch=z9hG4bK{os.urandom(8).hex()}\r\n"
                    f"From: <sip:{self.sip_id}@{self.host}>;tag={self._reg_from_tag}\r\n"
                    f"To: <sip:{self.sip_id}@{self.host}>\r\n"
                    f"Call-ID: {self._reg_call_id}\r\n"
                    f"CSeq: {self._cseq} REGISTER\r\n"
                    f"Contact: <sip:{self.sip_id}@{self.local_ip}:{self.sip_port}>\r\n"
                    f"{auth_header}"
                    f"Expires: 120\r\n"
                    f"Content-Length: 0\r\n\r\n"
                )
                self._send_packet(reg.encode("utf-8"), self._remote_addr)
            except Exception as e:
                _LOGGER.error("Registration failed to send: %s", e)
                self._set_status(PhoneStatus.FAILED)

            # Wait 60 seconds before refresh
            await asyncio.sleep(60)

    async def _handle_sip_packet(self, data: bytes, addr: tuple):
        """Process incoming raw SIP packet."""
        _LOGGER.debug("Received SIP packet from %s:\n%s", addr, data.decode("utf-8", errors="replace"))
        try:
            msg = SipMessage(data)
            if not msg.version:
                return # Malformed / empty packet
        except Exception as e:
            _LOGGER.debug("Failed to parse incoming SIP: %s", e)
            return

        # 1. Handle incoming requests
        if msg.is_request:
            if msg.method == "OPTIONS":
                # Respond 200 OK
                response = (
                    f"SIP/2.0 200 OK\r\n"
                    f"Via: {msg.get_header('Via')}\r\n"
                    f"From: {msg.get_header('From')}\r\n"
                    f"To: {msg.get_header('To')}\r\n"
                    f"Call-ID: {msg.get_header('Call-ID')}\r\n"
                    f"CSeq: {msg.get_header('CSeq')}\r\n"
                    f"Allow: INVITE, ACK, BYE, CANCEL, OPTIONS\r\n"
                    f"Content-Length: 0\r\n\r\n"
                )
                self._send_packet(response.encode("utf-8"), addr)
                return

            if msg.method == "INVITE":
                self._last_via_header = msg.get_header("Via")
                from_hdr = msg.get_header("From")
                remote_tag = parse_tag(from_hdr)
                call_id = msg.get_header("Call-ID")
                cseq_val = msg.get_header("CSeq")
                cseq_num = int(cseq_val.split(" ")[0]) if cseq_val else 1

                # Extract caller
                caller = "unknown"
                matches = re.search(r"sip:([^@;>]+)", from_hdr)
                if matches:
                    caller = matches.group(1)

                if self._active_call:
                    _LOGGER.info(
                        "Replacing existing active call %s (state %s) with new incoming call %s",
                        self._active_call.call_id, self._active_call._state, call_id
                    )
                    if self._active_call._rtp_transport:
                        self._active_call._rtp_transport.close()
                        self._active_call._rtp_transport = None
                    self._active_call._state = CallState.ENDED
                    self._active_call = None

                # Parse SDP
                sdp_info = parse_sdp(msg.body)

                call = TJA470SipCall(
                    phone=self,
                    is_incoming=True,
                    remote_tag=remote_tag,
                    call_id=call_id,
                    cseq_num=cseq_num,
                    remote_uri=clean_uri(from_hdr),
                    from_hdr=from_hdr,
                    to_hdr=msg.get_header("To"),
                )
                call._caller = caller
                call._state = CallState.RINGING
                call.remote_rtp_ip = sdp_info["ip"] or addr[0]
                call.remote_rtp_port = sdp_info["port"]
                call.codec = sdp_info["codec"]

                self._active_call = call

                # Send 180 Ringing
                ringing = (
                    f"SIP/2.0 180 Ringing\r\n"
                    f"Via: {self._last_via_header}\r\n"
                    f"From: {call._from_hdr}\r\n"
                    f"To: {call._to_hdr}\r\n"
                    f"Call-ID: {call_id}\r\n"
                    f"CSeq: {cseq_num} INVITE\r\n"
                    f"Content-Length: 0\r\n\r\n"
                )
                self._send_packet(ringing.encode("utf-8"), addr)

                if self._on_incoming_call_cb:
                    asyncio.create_task(self._on_incoming_call_cb(call))

            elif msg.method == "ACK":
                if self._active_call and self._active_call.call_id == msg.get_header("Call-ID"):
                    if self._active_call._state == CallState.RINGING or self._active_call._state == CallState.ANSWERED:
                        self._active_call._state = CallState.ANSWERED
                        await self._active_call._notify_state_changed()

            elif msg.method == "BYE":
                if self._active_call and self._active_call.call_id == msg.get_header("Call-ID"):
                    # Respond 200 OK
                    response = (
                        f"SIP/2.0 200 OK\r\n"
                        f"Via: {msg.get_header('Via')}\r\n"
                        f"From: {msg.get_header('From')}\r\n"
                        f"To: {msg.get_header('To')}\r\n"
                        f"Call-ID: {msg.get_header('Call-ID')}\r\n"
                        f"CSeq: {msg.get_header('CSeq')}\r\n"
                        f"Content-Length: 0\r\n\r\n"
                    )
                    self._send_packet(response.encode("utf-8"), addr)
                    await self._active_call._cleanup()

            elif msg.method == "CANCEL":
                if self._active_call and self._active_call.call_id == msg.get_header("Call-ID"):
                    # Respond 200 OK
                    response = (
                        f"SIP/2.0 200 OK\r\n"
                        f"Via: {msg.get_header('Via')}\r\n"
                        f"From: {msg.get_header('From')}\r\n"
                        f"To: {msg.get_header('To')}\r\n"
                        f"Call-ID: {msg.get_header('Call-ID')}\r\n"
                        f"CSeq: {msg.get_header('CSeq')}\r\n"
                        f"Content-Length: 0\r\n\r\n"
                    )
                    self._send_packet(response.encode("utf-8"), addr)
                    # Respond 487 Request Terminated
                    terminated = (
                        f"SIP/2.0 487 Request Terminated\r\n"
                        f"Via: {self._last_via_header}\r\n"
                        f"From: {msg.get_header('From')}\r\n"
                        f"To: {msg.get_header('To')};tag={self._active_call.local_tag}\r\n"
                        f"Call-ID: {msg.get_header('Call-ID')}\r\n"
                        f"CSeq: {self._active_call.cseq_num} INVITE\r\n"
                        f"Content-Length: 0\r\n\r\n"
                    )
                    self._send_packet(terminated.encode("utf-8"), addr)
                    await self._active_call._cleanup()

            elif msg.method == "NOTIFY":
                call_id = msg.get_header("Call-ID")
                # Respond 200 OK
                response = (
                    f"SIP/2.0 200 OK\r\n"
                    f"Via: {msg.get_header('Via')}\r\n"
                    f"From: {msg.get_header('From')}\r\n"
                    f"To: {msg.get_header('To')}\r\n"
                    f"Call-ID: {call_id}\r\n"
                    f"CSeq: {msg.get_header('CSeq')}\r\n"
                    f"Content-Length: 0\r\n\r\n"
                )
                self._send_packet(response.encode("utf-8"), addr)

        # 2. Handle incoming responses (status codes)
        else:
            cseq_val = msg.get_header("CSeq")
            if not cseq_val:
                return
            cseq_num_str, cseq_method = cseq_val.split(" ", 1)
            cseq_num = int(cseq_num_str)

            if cseq_method == "REGISTER":
                is_unreg = (self._unregister_future is not None and 
                            msg.get_header("Call-ID") == self._unregister_call_id)
                
                if msg.status_code == 401:
                    auth_hdr = msg.get_header("WWW-Authenticate")
                    if auth_hdr:
                        challenge = parse_www_authenticate(auth_hdr)
                        self._cached_auth_challenge = challenge
                        auth_str = compute_digest_auth(
                            self.sip_id,
                            self.sip_password,
                            "REGISTER",
                            f"sip:{self.host}",
                            challenge
                        )
                        self._cseq += 1
                        
                        if is_unreg:
                            # Send authenticated unregistration
                            reg = (
                                f"REGISTER sip:{self.host} SIP/2.0\r\n"
                                f"Via: SIP/2.0/UDP {self.local_ip}:{self.sip_port};branch=z9hG4bK{os.urandom(8).hex()}\r\n"
                                f"From: {msg.get_header('From')}\r\n"
                                f"To: {strip_tag(msg.get_header('To'))}\r\n"
                                f"Call-ID: {self._unregister_call_id}\r\n"
                                f"CSeq: {self._cseq} REGISTER\r\n"
                                f"Contact: *\r\n"
                                f"Authorization: {auth_str}\r\n"
                                f"Expires: 0\r\n"
                                f"Content-Length: 0\r\n\r\n"
                            )
                        else:
                            # Send authenticated registration
                            reg = (
                                f"REGISTER sip:{self.host} SIP/2.0\r\n"
                                f"Via: SIP/2.0/UDP {self.local_ip}:{self.sip_port};branch=z9hG4bK{os.urandom(8).hex()}\r\n"
                                f"From: {msg.get_header('From')}\r\n"
                                f"To: {strip_tag(msg.get_header('To'))}\r\n"
                                f"Call-ID: {msg.get_header('Call-ID')}\r\n"
                                f"CSeq: {self._cseq} REGISTER\r\n"
                                f"Contact: <sip:{self.sip_id}@{self.local_ip}:{self.sip_port}>\r\n"
                                f"Authorization: {auth_str}\r\n"
                                f"Expires: 120\r\n"
                                f"Content-Length: 0\r\n\r\n"
                            )
                        self._send_packet(reg.encode("utf-8"), addr)
                elif msg.status_code == 200:
                    if is_unreg:
                        if self._unregister_future and not self._unregister_future.done():
                            self._unregister_future.set_result(None)
                    else:
                        self._set_status(PhoneStatus.REGISTERED)


            elif cseq_method == "INVITE":
                if self._active_call and self._active_call.call_id == msg.get_header("Call-ID"):
                    if msg.status_code == 200:
                        # Extract remote tag
                        to_hdr = msg.get_header("To")
                        self._active_call.remote_tag = parse_tag(to_hdr)
                        self._active_call._to_hdr = to_hdr
                        
                        # Use Contact header for remote Request-URI in subsequent requests (ACK, BYE)
                        contact_hdr = msg.get_header("Contact")
                        if contact_hdr:
                            self._active_call.remote_uri = clean_uri(contact_hdr)
                        else:
                            self._active_call.remote_uri = clean_uri(to_hdr)

                        # Parse remote SDP
                        sdp_info = parse_sdp(msg.body)
                        self._active_call.remote_rtp_ip = sdp_info["ip"] or addr[0]
                        self._active_call.remote_rtp_port = sdp_info["port"]
                        self._active_call.codec = sdp_info["codec"]

                        # Send ACK
                        ack = (
                            f"ACK {self._active_call.remote_uri} SIP/2.0\r\n"
                            f"Via: SIP/2.0/UDP {self.local_ip}:{self.sip_port};branch=z9hG4bK{os.urandom(8).hex()}\r\n"
                            f"From: {self._active_call._from_hdr}\r\n"
                            f"To: {self._active_call._to_hdr}\r\n"
                            f"Call-ID: {self._active_call.call_id}\r\n"
                            f"CSeq: {cseq_num} ACK\r\n"
                            f"Max-Forwards: 70\r\n"
                            f"Content-Length: 0\r\n\r\n"
                        )
                        self._send_packet(ack.encode("utf-8"), addr)

                        self._active_call._state = CallState.ANSWERED
                        await self._active_call._notify_state_changed()
                        if self._active_call._answered_future and not self._active_call._answered_future.done():
                            self._active_call._answered_future.set_result(None)

                    elif msg.status_code == 401 or msg.status_code == 407:
                        # Resend INVITE with Authorization header
                        auth_hdr = msg.get_header("WWW-Authenticate") or msg.get_header("Proxy-Authenticate")
                        if auth_hdr:
                            challenge = parse_www_authenticate(auth_hdr)
                            self._cached_auth_challenge = challenge
                            dst_uri = f"sip:{self._active_call.caller}@{self.host}"
                            auth_str = compute_digest_auth(
                                self.sip_id,
                                self.sip_password,
                                "INVITE",
                                dst_uri,
                                challenge
                            )
                            self._cseq += 1
                            self._active_call.cseq_num = self._cseq
                            
                            # Local SDP
                            sdp = (
                                f"v=0\r\n"
                                f"o=aiotja470 0 0 IN IP4 {self.local_ip}\r\n"
                                f"s=Talk\r\n"
                                f"c=IN IP4 {self.local_ip}\r\n"
                                f"t=0 0\r\n"
                                f"m=audio {self._active_call.rtp_port} RTP/AVP 0\r\n"
                                f"a=rtpmap:0 PCMU/8000\r\n"
                            )
                            
                            self._active_call._from_hdr = msg.get_header("From")
                            self._active_call._to_hdr = strip_tag(msg.get_header("To"))
                            invite = (
                                f"INVITE {dst_uri} SIP/2.0\r\n"
                                f"Via: SIP/2.0/UDP {self.local_ip}:{self.sip_port};branch=z9hG4bK{os.urandom(8).hex()}\r\n"
                                f"From: {self._active_call._from_hdr}\r\n"
                                f"To: {self._active_call._to_hdr}\r\n"
                                f"Call-ID: {self._active_call.call_id}\r\n"
                                f"CSeq: {self._active_call.cseq_num} INVITE\r\n"
                                f"Contact: <sip:{self.sip_id}@{self.local_ip}:{self.sip_port}>\r\n"
                                f"Authorization: {auth_str}\r\n"
                                f"Max-Forwards: 70\r\n"
                                f"Content-Type: application/sdp\r\n"
                                f"Content-Length: {len(sdp)}\r\n\r\n"
                                f"{sdp}"
                            )
                            self._send_packet(invite.encode("utf-8"), addr)

                    elif msg.status_code == 180 or msg.status_code == 183:
                        self._active_call._state = CallState.RINGING
                        await self._active_call._notify_state_changed()

                    elif msg.status_code >= 300:
                        # Call failed or declined
                        call = self._active_call
                        await call._cleanup()
                        if call._answered_future and not call._answered_future.done():
                            call._answered_future.set_exception(
                                TJA470SipError(f"Call rejected with status: {msg.status_code}")
                            )

    async def call(self, number: str) -> TJA470SipCall:
        """Initiate an outgoing call."""
        if self._active_call:
            raise TJA470SipError("Already have an active call")

        call = TJA470SipCall(
            phone=self,
            is_incoming=False,
            remote_uri=f"sip:{number}@{self.host}",
        )
        call._caller = number
        call._state = CallState.DIALING
        call._answered_future = asyncio.get_running_loop().create_future()

        await call._bind_rtp()

        # Generate local SDP
        sdp = (
            f"v=0\r\n"
            f"o=aiotja470 0 0 IN IP4 {self.local_ip}\r\n"
            f"s=Talk\r\n"
            f"c=IN IP4 {self.local_ip}\r\n"
            f"t=0 0\r\n"
            f"m=audio {call.rtp_port} RTP/AVP 0\r\n"
            f"a=rtpmap:0 PCMU/8000\r\n"
        )

        self._cseq += 1
        call.cseq_num = self._cseq

        auth_header = ""
        if self._cached_auth_challenge:
            auth_str = compute_digest_auth(
                self.sip_id,
                self.sip_password,
                "INVITE",
                f"sip:{number}@{self.host}",
                self._cached_auth_challenge
            )
            auth_header = f"Authorization: {auth_str}\r\n"

        # Send INVITE
        invite = (
            f"INVITE sip:{number}@{self.host} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.sip_port};branch=z9hG4bK{os.urandom(8).hex()}\r\n"
            f"From: {call._from_hdr}\r\n"
            f"To: {call._to_hdr}\r\n"
            f"Call-ID: {call.call_id}\r\n"
            f"CSeq: {call.cseq_num} INVITE\r\n"
            f"Contact: <sip:{self.sip_id}@{self.local_ip}:{self.sip_port}>\r\n"
            f"{auth_header}"
            f"Max-Forwards: 70\r\n"
            f"Content-Type: application/sdp\r\n"
            f"Content-Length: {len(sdp)}\r\n\r\n"
            f"{sdp}"
        )

        self._active_call = call
        self._send_packet(invite.encode("utf-8"), self._remote_addr)

        return call


def parse_tag(header_val: str) -> str:
    """Extract tag value from SIP header (like From or To)."""
    if ";tag=" in header_val:
        return header_val.split(";tag=")[1].split(";")[0]
    return ""


def strip_tag(header_val: str) -> str:
    """Remove tag parameter from a SIP header value like To or From."""
    if not header_val:
        return ""
    parts = header_val.split(";")
    clean_parts = [p for p in parts if not p.strip().startswith("tag=")]
    return ";".join(clean_parts)


def clean_uri(header_val: str) -> str:
    """Extract URI from header value, removing angle brackets and parameters."""
    if not header_val:
        return ""
    # Look for content between < and >
    match = re.search(r"<([^>]+)>", header_val)
    if match:
        uri = match.group(1).strip()
    else:
        uri = header_val.strip()
    # Remove parameters after semicolon in the URI
    return uri.split(";")[0].strip()


