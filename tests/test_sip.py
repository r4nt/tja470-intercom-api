import asyncio
import pytest
from unittest.mock import MagicMock, patch
from pyVoIP.VoIP.status import PhoneStatus
from pyVoIP.VoIP import CallState

# Import both implementations
from aiotja470_intercom._sip.pyvoip_impl import (
    TJA470SipPhone as PyvoipSipPhone,
    TJA470SipCall as PyvoipSipCall,
    TJA470SipError as PyvoipSipError,
)

from aiotja470_intercom._sip.impl import (
    TJA470SipPhone as CustomSipPhone,
    TJA470SipCall as CustomSipCall,
    TJA470SipError as CustomSipError,
)


@pytest.fixture
def mock_voip_phone():
    with patch("aiotja470_intercom._sip.pyvoip_impl.VoIPPhone") as mock:
        phone_instance = MagicMock()
        phone_instance.get_status.return_value = PhoneStatus.REGISTERED
        mock.return_value = phone_instance
        yield phone_instance


# --- pyVoIP Specific Tests ---

@pytest.mark.asyncio
async def test_pyvoip_phone_lifecycle(mock_voip_phone):
    phone = PyvoipSipPhone("127.0.0.1", "6004", "pass", "127.0.0.1", 5060)
    assert phone.get_status() == PhoneStatus.INACTIVE
    
    await phone.start()
    mock_voip_phone.start.assert_called_once()
    assert phone.get_status() == PhoneStatus.REGISTERED
    
    await phone.stop()
    mock_voip_phone.stop.assert_called_once()
    assert phone.get_status() == PhoneStatus.INACTIVE


@pytest.mark.asyncio
async def test_pyvoip_phone_double_start(mock_voip_phone):
    phone = PyvoipSipPhone("127.0.0.1", "6004", "pass", "127.0.0.1", 5060)
    await phone.start()
    with pytest.raises(PyvoipSipError):
        await phone.start()
    await phone.stop()


@pytest.mark.asyncio
async def test_pyvoip_phone_call(mock_voip_phone):
    phone = PyvoipSipPhone("127.0.0.1", "6004", "pass", "127.0.0.1", 5060)
    await phone.start()
    
    mock_call = MagicMock()
    mock_call.state = CallState.DIALING
    mock_voip_phone.call.return_value = mock_call
    
    call = await phone.call("6000")
    assert isinstance(call, PyvoipSipCall)
    assert call.state == CallState.DIALING
    mock_voip_phone.call.assert_called_once_with("6000")
    
    await phone.stop()


# --- Custom asyncio-native Specific Tests ---

@pytest.mark.asyncio
async def test_custom_phone_lifecycle():
    loop = asyncio.get_running_loop()
    
    # Mock datagram endpoint creation
    mock_transport = MagicMock(spec=asyncio.DatagramTransport)
    with patch.object(loop, "create_datagram_endpoint", return_value=(mock_transport, MagicMock())) as mock_create:
        phone = CustomSipPhone("127.0.0.1", "6008", "pass", "127.0.0.1", 5060)
        assert phone.get_status() == PhoneStatus.INACTIVE
        
        await phone.start()
        mock_create.assert_called_once()
        
        await phone.stop()
        mock_transport.close.assert_called_once()
        assert phone.get_status() == PhoneStatus.INACTIVE


# --- Dual Implementation Parameterized Tests ---

def make_call_instance(call_class, loop):
    if call_class == PyvoipSipCall:
        mock_raw_call = MagicMock()
        mock_raw_call.state = CallState.RINGING
        return call_class(mock_raw_call, loop), mock_raw_call
    else:
        mock_phone = MagicMock()
        mock_phone.local_ip = "127.0.0.1"
        mock_phone.rtp_port = None
        mock_phone._get_free_port.return_value = 10000
        call = call_class(phone=mock_phone, is_incoming=True)
        # CustomSipCall starts in RINGING
        call._rtp_transport = MagicMock()
        return call, call._rtp_transport


@pytest.mark.asyncio
@pytest.mark.parametrize("SipCallClass", [PyvoipSipCall, CustomSipCall])
async def test_sip_call_actions(SipCallClass):
    loop = asyncio.get_running_loop()
    call, mock_backend = make_call_instance(SipCallClass, loop)
    
    if SipCallClass == PyvoipSipCall:
        await call.answer()
        mock_backend.answer.assert_called_once()
        
        await call.hangup()
        mock_backend.hangup.assert_called_once()
        
        await call.deny()
        mock_backend.deny.assert_called_once()
    else:
        # For CustomSipCall, answer/hangup/deny send SIP packets to phone
        phone = call.phone
        # Mock create_datagram_endpoint to avoid binding actual sockets
        mock_transport = MagicMock(spec=asyncio.DatagramTransport)
        with patch.object(loop, "create_datagram_endpoint", return_value=(mock_transport, MagicMock())):
            await call.answer()
        assert phone._send_packet.called
        assert call.state == CallState.ANSWERED
        
        phone._send_packet.reset_mock()
        await call.hangup()
        assert phone._send_packet.called
        assert call.state == CallState.ENDED


@pytest.mark.asyncio
@pytest.mark.parametrize("SipCallClass", [PyvoipSipCall, CustomSipCall])
async def test_sip_call_16bit_audio(SipCallClass):
    loop = asyncio.get_running_loop()
    call, mock_backend = make_call_instance(SipCallClass, loop)
    
    if SipCallClass == PyvoipSipCall:
        mock_backend.state = CallState.ANSWERED
        mock_backend.read_audio.return_value = b"\x80" * 80
    else:
        call._state = CallState.ANSWERED
        call._incoming_audio_queue.put_nowait(b"\x00" * 160)
        
    audio = await call.read_audio_16bit(160, True)
    assert len(audio) == 160
    assert audio == b"\x00" * 160
    
    if SipCallClass == PyvoipSipCall:
        mock_backend.read_audio.assert_called_once_with(80, True)
        await call.write_audio_16bit(b"\x00" * 160)
        mock_backend.write_audio.assert_called_once_with(b"\x80" * 80)
    else:
        call.codec = 0  # PCMU
        await call.write_audio_16bit(b"\x00" * 160)
        assert mock_backend.sendto.called
        packet, _ = mock_backend.sendto.call_args[0]
        # 12 byte RTP header + 80 byte PCMU payload
        assert len(packet) == 92
        assert packet[12:] == b"\xff" * 80  # PCMU silence is 0xff


@pytest.mark.asyncio
@pytest.mark.parametrize("SipCallClass", [PyvoipSipCall, CustomSipCall])
async def test_sip_call_audio_stream(SipCallClass):
    loop = asyncio.get_running_loop()
    call, mock_backend = make_call_instance(SipCallClass, loop)
    
    if SipCallClass == PyvoipSipCall:
        mock_backend.state = CallState.ANSWERED
        mock_backend.read_audio.return_value = b"\x80" * 80
    else:
        call._state = CallState.ANSWERED
        call._incoming_audio_queue.put_nowait(b"\x00" * 160)
        call._incoming_audio_queue.put_nowait(b"\x00" * 160)
        
    frames = []
    async for frame in call.audio_stream(160):
        frames.append(frame)
        if len(frames) == 2:
            if SipCallClass == PyvoipSipCall:
                mock_backend.state = CallState.ENDED
            else:
                call._state = CallState.ENDED
                
    assert len(frames) == 2
    assert frames[0] == b"\x00" * 160
    assert frames[1] == b"\x00" * 160


# --- OPTIONS Ping Parse Test ---

def test_sip_options_ping():
    from pyVoIP.SIP import SIPMessage, SIPClient
    
    raw_options = (
        b"OPTIONS sip:6008@192.168.42.5:5060;transport=UDP SIP/2.0\r\n"
        b"Via: SIP/2.0/UDP 192.168.42.2:5060;branch=z9hG4bK12345\r\n"
        b"From: <sip:6001@192.168.42.2>;tag=abc\r\n"
        b"To: <sip:6008@192.168.42.5>\r\n"
        b"Call-ID: call-12345@192.168.42.2\r\n"
        b"CSeq: 1 OPTIONS\r\n"
        b"Max-Forwards: 70\r\n"
        b"Content-Length: 0\r\n\r\n"
    )
    
    msg = SIPMessage(raw_options)
    mock_client = MagicMock(spec=SIPClient)
    mock_client.server = "192.168.42.2"
    mock_client.port = 5060
    mock_client.out = MagicMock()
    mock_client.gen_tag.return_value = "xyz"
    
    mock_client.parse_message = SIPClient.parse_message.__get__(mock_client, SIPClient)
    mock_client.gen_ok = SIPClient.gen_ok.__get__(mock_client, SIPClient)
    mock_client._gen_response_via_header = SIPClient._gen_response_via_header.__get__(mock_client, SIPClient)
    
    mock_client.parse_message(msg)
    
    assert mock_client.out.sendto.called
    sent_data, addr = mock_client.out.sendto.call_args[0]
    assert addr == ("192.168.42.2", 5060)
    
    sent_str = sent_data.decode("utf8")
    assert "SIP/2.0 200 OK" in sent_str
    assert "CSeq: 1 OPTIONS" in sent_str
    assert "Call-ID: call-12345@192.168.42.2" in sent_str
