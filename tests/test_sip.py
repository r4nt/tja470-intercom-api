import asyncio
import pytest
from unittest.mock import MagicMock, patch
from pyVoIP.VoIP.status import PhoneStatus
from pyVoIP.VoIP import CallState
from aiotja470_intercom.sip import TJA470SipPhone, TJA470SipCall, TJA470SipError

@pytest.fixture
def mock_voip_phone():
    with patch("aiotja470_intercom.sip.VoIPPhone") as mock:
        phone_instance = MagicMock()
        phone_instance.get_status.return_value = PhoneStatus.REGISTERED
        mock.return_value = phone_instance
        yield phone_instance

@pytest.mark.asyncio
async def test_sip_phone_lifecycle(mock_voip_phone):
    phone = TJA470SipPhone("127.0.0.1", "6004", "pass", "127.0.0.1", 5060)
    
    assert phone.get_status() == PhoneStatus.INACTIVE
    
    await phone.start()
    mock_voip_phone.start.assert_called_once()
    assert phone.get_status() == PhoneStatus.REGISTERED
    
    await phone.stop()
    mock_voip_phone.stop.assert_called_once()
    assert phone.get_status() == PhoneStatus.INACTIVE

@pytest.mark.asyncio
async def test_sip_phone_double_start(mock_voip_phone):
    phone = TJA470SipPhone("127.0.0.1", "6004", "pass", "127.0.0.1", 5060)
    await phone.start()
    with pytest.raises(TJA470SipError):
        await phone.start()
    await phone.stop()

@pytest.mark.asyncio
async def test_sip_phone_call(mock_voip_phone):
    phone = TJA470SipPhone("127.0.0.1", "6004", "pass", "127.0.0.1", 5060)
    await phone.start()
    
    mock_call = MagicMock()
    mock_call.state = CallState.DIALING
    mock_voip_phone.call.return_value = mock_call
    
    call = await phone.call("6000")
    assert isinstance(call, TJA470SipCall)
    assert call.state == CallState.DIALING
    mock_voip_phone.call.assert_called_once_with("6000")
    
    await phone.stop()

@pytest.mark.asyncio
async def test_sip_call_actions():
    mock_raw_call = MagicMock()
    loop = asyncio.get_running_loop()
    call = TJA470SipCall(mock_raw_call, loop)
    
    await call.answer()
    mock_raw_call.answer.assert_called_once()
    
    await call.hangup()
    mock_raw_call.hangup.assert_called_once()
    
    await call.deny()
    mock_raw_call.deny.assert_called_once()
    
    mock_raw_call.read_audio.return_value = b"\x80" * 160
    audio = await call.read_audio(160, True)
    assert audio == b"\x80" * 160
    mock_raw_call.read_audio.assert_called_once_with(160, True)
    
    await call.write_audio(b"some_audio")
    mock_raw_call.write_audio.assert_called_once_with(b"some_audio")

@pytest.mark.asyncio
async def test_sip_call_16bit_audio():
    mock_raw_call = MagicMock()
    loop = asyncio.get_running_loop()
    call = TJA470SipCall(mock_raw_call, loop)
    
    # 8-bit linear data (width=1), 80 samples
    mock_raw_call.read_audio.return_value = b"\x00" * 80
    
    # We want 160 bytes of 16-bit PCM (80 samples, width=2)
    audio = await call.read_audio_16bit(160, True)
    assert len(audio) == 160
    # verify conversion
    mock_raw_call.read_audio.assert_called_once_with(80, True)
    
    # Write 160 bytes of 16-bit PCM
    await call.write_audio_16bit(b"\x00" * 160)
    # verify it converts to 80 bytes of 8-bit PCM
    mock_raw_call.write_audio.assert_called_once_with(b"\x00" * 80)

@pytest.mark.asyncio
async def test_sip_call_audio_stream():
    mock_raw_call = MagicMock()
    mock_raw_call.state = CallState.ANSWERED
    loop = asyncio.get_running_loop()
    call = TJA470SipCall(mock_raw_call, loop)
    
    mock_raw_call.read_audio.return_value = b"\x00" * 80
    
    frames = []
    # Consume 2 frames from generator
    async for frame in call.audio_stream(160):
        frames.append(frame)
        if len(frames) == 2:
            mock_raw_call.state = CallState.ENDED  # Stop the generator loop
            
    assert len(frames) == 2
    assert frames[0] == b"\x00" * 160
    assert frames[1] == b"\x00" * 160

