import argparse
import asyncio
import math
import os
import struct
import sys
import wave
from aiotja470_intercom.cli import load_config, get_local_ip, get_free_sip_port
from aiotja470_intercom.client import TJA470IntercomClient
from aiotja470_intercom.runner import AiohttpRunner
from aiotja470_intercom.sip import TJA470SipPhone, TJA470SipCall


async def main():
    parser = argparse.ArgumentParser(description="SIP audio loop test")
    parser.add_argument("--call", required=True, help="SIP ID/number to dial (e.g. the door station)")
    parser.add_argument("--output", default="loop_test.wav", help="Path to output WAV file")
    parser.add_argument("--local-ip", help="Override local IP to route to the intercom")
    parser.add_argument("--rtp-port", type=int, help="Override/lock the UDP port used for RTP audio (default: 10000)")
    args = parser.parse_args()

    config = load_config()
    if not config:
        print("❌ Configuration not found. Please run `tja470 pair` first.")
        sys.exit(1)

    host = config["host"]
    uuid_val = config["uuid"]

    print("Fetching provisioning data...")
    runner = AiohttpRunner()
    client = TJA470IntercomClient(
        host=host,
        username=config["username"],
        password=config["password"],
        runner=runner
    )
    if config.get("cookies"):
        client.set_cookies(config["cookies"])

    try:
        prov = await client.get_provisioning(uuid_val)
        sip_id = prov.sip_info.sip_id
        sip_password = prov.sip_info.sip_password
    finally:
        await runner.close()

    local_ip = args.local_ip if args.local_ip else get_local_ip(host)
    sip_port = get_free_sip_port(local_ip)

    print(f"Local IP: {local_ip}")
    print(f"SIP Port: {sip_port}")
    print(f"Sip ID: {sip_id}")
    print(f"Dialing target: {args.call}")

    phone = TJA470SipPhone(
        host=host,
        sip_id=sip_id,
        sip_password=sip_password,
        local_ip=local_ip,
        sip_port=sip_port,
        rtp_port=args.rtp_port,
    )

    call_answered_event = asyncio.Event()
    active_call = None

    async def record_audio(call: TJA470SipCall):
        print(f"🎙️ Recording incoming audio to {args.output}...")
        try:
            with wave.open(args.output, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(8000)
                async for frame in call.audio_stream(convert_16bit=True):
                    wav_file.writeframes(frame)
            print("💾 Recording stopped and saved.")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"❌ Recording error: {e}")

    async def play_tone(call: TJA470SipCall):
        print("🔊 Playing a little song (Twinkle Twinkle Little Star)...")
        # Melody: tuples of (frequency_hz, duration_seconds)
        C4, D4, E4, F4, G4, A4 = 261.63, 293.66, 329.63, 349.23, 392.00, 440.00
        
        melody = [
            (C4, 0.4), (C4, 0.4), (G4, 0.4), (G4, 0.4), (A4, 0.4), (A4, 0.4), (G4, 0.8),
            (F4, 0.4), (F4, 0.4), (E4, 0.4), (E4, 0.4), (D4, 0.4), (D4, 0.4), (C4, 0.8),
            (G4, 0.4), (G4, 0.4), (F4, 0.4), (F4, 0.4), (E4, 0.4), (E4, 0.4), (D4, 0.8),
            (G4, 0.4), (G4, 0.4), (F4, 0.4), (F4, 0.4), (E4, 0.4), (E4, 0.4), (D4, 0.8),
            (C4, 0.4), (C4, 0.4), (G4, 0.4), (G4, 0.4), (A4, 0.4), (A4, 0.4), (G4, 0.8),
            (F4, 0.4), (F4, 0.4), (E4, 0.4), (E4, 0.4), (D4, 0.4), (D4, 0.4), (C4, 0.8)
        ]
        
        try:
            from pyVoIP.VoIP import CallState
            note_idx = 0
            elapsed_note_time = 0.0
            sample_count = 0
            
            while call.state == CallState.ANSWERED:
                freq, duration = melody[note_idx % len(melody)]
                
                # Check if we are in the last 15% of the note's duration (rest / gap)
                is_rest = elapsed_note_time > (duration * 0.85)
                
                chunk_duration = 0.02
                num_samples = int(8000 * chunk_duration)
                audio_data = bytearray()
                
                if is_rest or freq == 0:
                    audio_data.extend(b"\x00" * (num_samples * 2))
                else:
                    for _ in range(num_samples):
                        t = sample_count / 8000.0
                        sample = int(16384 * math.sin(2 * math.pi * freq * t))
                        audio_data.extend(struct.pack("<h", sample))
                        sample_count += 1
                        
                await call.write_audio_16bit(bytes(audio_data))
                
                elapsed_note_time += chunk_duration
                if elapsed_note_time >= duration:
                    note_idx += 1
                    elapsed_note_time = 0.0
                    sample_count = 0  # reset phase for next note
                    
                await asyncio.sleep(chunk_duration)
        except asyncio.CancelledError:
            pass

    async def handle_incoming_call(call: TJA470SipCall):
        pass

    phone.register_incoming_call_callback(handle_incoming_call)

    print("Registering SIP Phone...")
    await phone.start()

    # Give it a moment to register
    await asyncio.sleep(2)
    print(f"Registration Status: {phone.get_status()}")

    print(f"Initiating outgoing call to {args.call}...")
    active_call = await phone.call(args.call)
    print(f"Call initiated. State: {active_call.state}")

    # Wait for the call to be answered
    from pyVoIP.VoIP import CallState
    for _ in range(50):
        if active_call.state == CallState.ANSWERED:
            call_answered_event.set()
            break
        await asyncio.sleep(0.1)

    if not call_answered_event.is_set():
        print(f"❌ Call was not answered (Current state: {active_call.state}). Exiting...")
        await active_call.hangup()
        await phone.stop()
        sys.exit(1)

    print("📞 Call answered! Recording and tone output starting.")
    record_task = asyncio.create_task(record_audio(active_call))
    play_task = asyncio.create_task(play_tone(active_call))

    await asyncio.sleep(10)

    print("⌛ 10 seconds elapsed. Hanging up call...")
    if active_call and hasattr(active_call, "_raw_call") and active_call._raw_call.RTPClients:
        print(f"RTP Packets received in pmin.log: {len(active_call._raw_call.RTPClients[0].pmin.log)}")
        print(f"RTP Packets sent in pmout.log: {len(active_call._raw_call.RTPClients[0].pmout.log)}")

    play_task.cancel()
    await active_call.hangup()
    record_task.cancel()
    await record_task
    await phone.stop()

    print("\n--- Verifying Audio Content ---")
    if not os.path.exists(args.output):
        print("❌ Error: Output file was not created!")
        sys.exit(1)

    with wave.open(args.output, "rb") as w:
        frames = w.readframes(w.getnframes())
        length = len(frames)
        # 16-bit PCM silence is 0x00. Let's count non-zero bytes.
        non_zero = sum(1 for x in frames if x != 0)

    print(f"Recorded file size: {length} bytes")
    print(f"Non-silent (non-zero) bytes: {non_zero}")
    if non_zero > 0:
        pct = (non_zero / length) * 100
        print(f"✅ Success! {non_zero} bytes ({pct:.2f}%) of non-silent audio data recorded.")
    else:
        print("❌ Failure: Recorded audio is 100% silent (only contains zeros).")


if __name__ == "__main__":
    asyncio.run(main())
