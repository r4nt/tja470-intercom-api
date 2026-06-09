import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import uuid
from typing import Any, Dict, Optional

from aiotja470_intercom.client import TJA470IntercomClient
from aiotja470_intercom.runner import AiohttpRunner
from aiotja470_intercom.exceptions import TJA470AuthError

CONFIG_FILE = os.path.expanduser("~/.tja470_config.json")

def load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(config: Dict[str, Any]) -> None:
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

class CustomParser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write(f'error: {message}\n\n')
        self.print_help()
        sys.exit(2)

async def async_main():
    parser = CustomParser(description="TJA-470 Intercom CLI")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging (prints raw HTTP requests and responses)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # `pair` command
    pair_parser = subparsers.add_parser("pair", help="Authenticate and pair a new client UUID")
    pair_parser.add_argument("--host", required=True, help="IP address of the TJA-470")
    pair_parser.add_argument("--username", required=True, help="Username for the TJA-470")
    pair_parser.add_argument("--password", required=True, help="Password for the TJA-470")
    pair_parser.add_argument("--uuid", help="Optional custom UUID. If omitted, a random one is generated.")

    # `status` command
    status_parser = subparsers.add_parser("status", help="Show current connection status and device information")

    # `run` command
    run_parser = subparsers.add_parser("run", help="Run commands using the cached session")
    run_parser.add_argument("--open-door", action="store_true", help="Open the door (door ID 1)")
    run_parser.add_argument("--open-door-at", type=int, help="Open the door at a specific camera position index (0, 1, ...)")
    run_parser.add_argument("--switch-camera", action="store_true", help="Switch the camera feed")
    run_parser.add_argument("--provisioning", action="store_true", help="Print the provisioning info")

    # `sip` command
    sip_parser = subparsers.add_parser("sip", help="Start SIP client to receive/make calls")
    sip_parser.add_argument("--call", help="Optional target number/SIP ID to call immediately upon startup")
    sip_parser.add_argument("--record-to", help="Optional WAV file path to record incoming call audio to")
    sip_parser.add_argument("--play-tone", action="store_true", help="Play a 440Hz test tone to the intercom speaker when a call is active")
    sip_parser.add_argument("--local-ip", help="Override the automatically detected local IP address for SIP routing (useful inside WSL/NAT)")
    sip_parser.add_argument("--rtp-port", type=int, help="Override/lock the UDP port used for RTP audio (default: 10000)")

    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")

    if args.command == "pair":
        await run_pair(args)
    elif args.command == "status":
        await run_status(args)
    elif args.command == "run":
        await run_command(args)
    elif args.command == "sip":
        await run_sip(args)

async def run_pair(args):
    client_uuid = args.uuid if args.uuid else str(uuid.uuid4())
    print(f"Connecting to TJA470 at {args.host}...")
    runner = AiohttpRunner()
    client = TJA470IntercomClient(
        host=args.host,
        username=args.username,
        password=args.password,
        runner=runner
    )

    try:
        print("Checking API manifest...")
        await client.get_manifest()

        print("Fetching free devices...")
        devices = await client.get_free_devices()
        
        if not devices:
            print("\n❌ No free devices found!")
            print("Please create/free a mobile client in the TJA-470 UI.")
            sys.exit(1)
            
        target_device = devices[0]
        
        print(f"\nRegistering UUID '{client_uuid}' to device ID {target_device.id}...")
        await client.set_uid(target_device.id, client_uuid)
        print("UUID registered successfully!")

        # Save config
        config = {
            "host": args.host,
            "username": args.username,
            "password": args.password,
            "uuid": client_uuid,
            "cookies": client.get_cookies()
        }
        save_config(config)
        print(f"\n✅ Configuration and session cookies saved to {CONFIG_FILE}!")
        print("You can now use `tja470 run ...` commands.")

    except Exception as e:
        print(f"\n❌ An error occurred: {e}")
    finally:
        await runner.close()


async def run_command(args):
    config = load_config()
    if not config:
        print(f"❌ Configuration not found at {CONFIG_FILE}.")
        print("Please run `tja470 pair` first.")
        sys.exit(1)

    runner = AiohttpRunner()
    client = TJA470IntercomClient(
        host=config["host"],
        username=config["username"],
        password=config["password"],
        runner=runner
    )

    # Load cached cookies
    cached_cookies = config.get("cookies", {})
    if cached_cookies:
        client.set_cookies(cached_cookies)

    try:
        uuid = config["uuid"]

        # Validate session by getting manifest (this will re-authenticate with basic auth if cookie is expired)
        try:
            await client.get_manifest()
        except TJA470AuthError:
            print("❌ Authentication failed. Your credentials might be invalid.")
            sys.exit(1)

        # Update cached cookies just in case they were refreshed
        new_cookies = client.get_cookies()
        if new_cookies != cached_cookies:
            config["cookies"] = new_cookies
            save_config(config)

        if args.provisioning:
            print("\nRetrieving provisioning configuration...")
            prov = await client.get_provisioning(uuid)
            print("\n✅ Provisioning Configuration:")
            print(f"  SIP ID: {prov.sip_info.sip_id}")
            sip_pass_local = prov.sip_info.sip_password if args.debug else ("*" * len(prov.sip_info.sip_password))
            print(f"  SIP Password: {sip_pass_local}")
            print(f"  RTSP Video URL: {prov.rtsp_video_url}")
            print(f"  HTTP Video URL: {prov.http_video_url}")
            print(f"  Local IP Address: {prov.local_ip_address}")
            print(f"  Door Release Allowed: {prov.door_release_allowed}")
            if prov.remote_access:
                print("\n  Remote Access Configuration:")
                print(f"    SIP ID: {prov.remote_access.sip_id}")
                sip_pass_remote = prov.remote_access.sip_password if args.debug else ("*" * len(prov.remote_access.sip_password))
                print(f"    SIP Password: {sip_pass_remote}")
                print(f"    Ngrok URL: {prov.remote_access.ngrok_url}")
                print(f"    RTSP URL: {prov.remote_access.rtsp_url}")
                print(f"    RTSP Port: {prov.remote_access.rtsp_port}")
                print(f"    SIP TCP URL: {prov.remote_access.sip_tcp_url}")
                print(f"    SIP TCP Port: {prov.remote_access.sip_tcp_port}")
                print(f"    WS Port: {prov.remote_access.ws_port}")
                print(f"    STUN/TURN Prefix: {prov.remote_access.stun_turn_prefix}")
                print(f"    STUN/TURN User: {prov.remote_access.stun_turn_user}")
                stun_pass = prov.remote_access.stun_turn_password if args.debug else ("*" * len(prov.remote_access.stun_turn_password))
                print(f"    STUN/TURN Password: {stun_pass}")
                print(f"    STUN/TURN Hostname: {prov.remote_access.stun_turn_hostname}")
                print(f"    STUN/TURN Port: {prov.remote_access.stun_turn_port}")

        if args.open_door:
            print("\n🚪 Opening the door...")
            await client.open_door(door_id=1)
            print("Door opened successfully!")

        if args.open_door_at is not None:
            print(f"\n🚪 Opening the door at camera position {args.open_door_at}...")
            await client.open_door_at_position(uuid, args.open_door_at, door_id=1)
            print("Door opened successfully!")

        if args.switch_camera:
            print("\n📷 Switching the camera feed...")
            new_pos = await client.switch_camera(uuid)
            print(f"Camera switched successfully! New position index: {new_pos}")

    except Exception as e:
        print(f"\n❌ An error occurred: {e}")
    finally:
        await runner.close()


async def run_status(args):
    config = load_config()
    if not config:
        print(f"❌ Configuration not found at {CONFIG_FILE}.")
        print("Please run `tja470 pair` first.")
        sys.exit(1)

    print("Checking connection to TJA-470...")
    runner = AiohttpRunner()
    client = TJA470IntercomClient(
        host=config["host"],
        username=config["username"],
        password=config["password"],
        runner=runner
    )

    if config.get("cookies"):
        client.set_cookies(config["cookies"])

    try:
        # Check authentication
        await client.get_manifest()
        
        # Save cookies if updated
        new_cookies = client.get_cookies()
        if new_cookies != config.get("cookies", {}):
            config["cookies"] = new_cookies
            save_config(config)

        print("\n✅ Authentication successful!")
        print(f"Host: {config['host']}")
        print(f"UUID: {config['uuid']}")
        
        print("\nFetching provisioning data...")
        prov = await client.get_provisioning(config["uuid"])
        print("\n📋 Intercom Information:")
        print(f"  SIP ID: {prov.sip_info.sip_id}")
        sip_pass_local = prov.sip_info.sip_password if args.debug else ("*" * len(prov.sip_info.sip_password))
        print(f"  SIP Password: {sip_pass_local}")
        print(f"  RTSP Video URL: {prov.rtsp_video_url}")
        print(f"  HTTP Video URL: {prov.http_video_url}")
        print(f"  Local IP Address: {prov.local_ip_address}")
        print(f"  Door Release Allowed: {prov.door_release_allowed}")
        
        if prov.remote_access:
            print("\n  Remote Access Configuration:")
            print(f"    SIP ID: {prov.remote_access.sip_id}")
            sip_pass_remote = prov.remote_access.sip_password if args.debug else ("*" * len(prov.remote_access.sip_password))
            print(f"    SIP Password: {sip_pass_remote}")
            print(f"    Ngrok URL: {prov.remote_access.ngrok_url}")
            print(f"    RTSP URL: {prov.remote_access.rtsp_url}")
            print(f"    RTSP Port: {prov.remote_access.rtsp_port}")
            print(f"    SIP TCP URL: {prov.remote_access.sip_tcp_url}")
            print(f"    SIP TCP Port: {prov.remote_access.sip_tcp_port}")
            print(f"    WS Port: {prov.remote_access.ws_port}")
            print(f"    STUN/TURN Prefix: {prov.remote_access.stun_turn_prefix}")
            print(f"    STUN/TURN User: {prov.remote_access.stun_turn_user}")
            stun_pass = prov.remote_access.stun_turn_password if args.debug else ("*" * len(prov.remote_access.stun_turn_password))
            print(f"    STUN/TURN Password: {stun_pass}")
            print(f"    STUN/TURN Hostname: {prov.remote_access.stun_turn_hostname}")
            print(f"    STUN/TURN Port: {prov.remote_access.stun_turn_port}")

        if prov.called_elements:
            print("\n  Extensions (Called Elements):")
            for ext in prov.called_elements:
                pos_str = f", Camera Position: {ext.order}" if ext.order is not None else ""
                print(f"    - Name: {ext.name or 'Unknown'} (SIP ID: {ext.sip_id}{pos_str})")
        
    except TJA470AuthError:
        print("\n❌ Authentication failed. Your session or credentials might be invalid.")
        print("Try running `tja470 pair` again.")
    except Exception as e:
        print(f"\n❌ An error occurred while fetching status: {e}")
    finally:
        await runner.close()


def get_local_ip(target_host: str) -> str:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_host, 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip


def get_free_sip_port(ip: str) -> int:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((ip, 5060))
        s.close()
        return 5060
    except OSError:
        try:
            s.bind((ip, 0))
            port = s.getsockname()[1]
            s.close()
            return port
        except Exception:
            return 5061


async def run_sip_client_async(
    host: str,
    sip_id: str,
    sip_password: str,
    initial_call: Optional[str] = None,
    debug_sip: bool = False,
    record_to: Optional[str] = None,
    play_tone: bool = False,
    local_ip: Optional[str] = None,
    rtp_port: Optional[int] = None,
):
    import time
    import pyVoIP
    from aiotja470_intercom.sip import TJA470SipPhone, TJA470SipCall
    from pyVoIP.VoIP.status import PhoneStatus
    from pyVoIP.VoIP import CallState

    if debug_sip:
        pyVoIP.DEBUG = True
        print("SIP client debug logging enabled.")

    if not local_ip:
        local_ip = get_local_ip(host)
        print(f"Detected local IP to route to intercom: {local_ip}")
    else:
        print(f"Using overridden local IP: {local_ip}")
    
    sip_port = get_free_sip_port(local_ip)
    print(f"Local SIP Client Port: {sip_port}")
    print(f"SIP Server IP: {host}")
    print(f"SIP ID: {sip_id}")

    active_call: Optional[TJA470SipCall] = None
    call_lock = asyncio.Lock()
    monitored_calls = set()
    record_task: Optional[asyncio.Task] = None
    tone_task: Optional[asyncio.Task] = None

    async def stop_call_tasks():
        nonlocal record_task, tone_task
        if record_task:
            record_task.cancel()
            try:
                await record_task
            except asyncio.CancelledError:
                pass
            record_task = None
        if tone_task:
            tone_task.cancel()
            try:
                await tone_task
            except asyncio.CancelledError:
                pass
            tone_task = None

    async def record_audio_task(call: TJA470SipCall, path: str):
        import wave
        print(f"\n🎙️ Recording incoming audio to {path}...")
        try:
            with wave.open(path, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(8000)
                async for frame in call.audio_stream(convert_16bit=True):
                    wav_file.writeframes(frame)
            print(f"\n💾 Audio recording saved to {path}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"\n❌ Audio recording error: {e}")

    async def play_tone_task(call: TJA470SipCall):
        import math
        import struct
        print("\n🔊 Playing 440Hz test tone to the intercom speaker...")
        num_samples = int(8000 * (20 / 1000.0))
        audio_data = bytearray()
        for i in range(num_samples):
            sample = int(32767 * math.sin(2 * math.pi * 440 * (i / 8000)))
            audio_data.extend(struct.pack("<h", sample))
        tone_20ms = bytes(audio_data)
        
        try:
            while call.state == CallState.ANSWERED:
                await call.write_audio_16bit(tone_20ms)
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"\n❌ Tone playback error: {e}")

    async def monitor_call_audio(call: TJA470SipCall):
        if call in monitored_calls:
            return
        monitored_calls.add(call)

        tasks = []

        async def on_call_state_changed(state: CallState):
            nonlocal active_call
            print(f"\n📞 Call State: {state.name}")
            sys.stdout.write("> ")
            sys.stdout.flush()

            if state == CallState.ANSWERED:
                # Start recording or tone tasks if they aren't already running
                if record_to and not any(t.get_name() == "record" for t in tasks):
                    t = asyncio.create_task(record_audio_task(call, record_to))
                    t.set_name("record")
                    tasks.append(t)
                if play_tone and not any(t.get_name() == "tone" for t in tasks):
                    t = asyncio.create_task(play_tone_task(call))
                    t.set_name("tone")
                    tasks.append(t)
            elif state == CallState.ENDED:
                async with call_lock:
                    if active_call == call:
                        active_call = None
                for t in tasks:
                    t.cancel()
                tasks.clear()
                await stop_call_tasks()

        call.register_state_callback(on_call_state_changed)
        # Notify of the initial state
        await on_call_state_changed(call.state)


    async def on_incoming_call(call: TJA470SipCall) -> None:
        nonlocal active_call
        async with call_lock:
            active_call = call
        print(f"\n📞 INCOMING CALL from {call.caller}!")
        print("Press 'a' to answer, 'r' to reject/busy.")
        sys.stdout.write("> ")
        sys.stdout.flush()
        asyncio.create_task(monitor_call_audio(call))

    async def on_registration_state_changed(status: PhoneStatus) -> None:
        print(f"\n🔄 SIP Registration State Changed: {status}")
        sys.stdout.write("> ")
        sys.stdout.flush()

    phone = TJA470SipPhone(
        host=host,
        sip_id=sip_id,
        sip_password=sip_password,
        local_ip=local_ip,
        sip_port=sip_port,
        rtp_port=rtp_port,
    )
    phone.register_incoming_call_callback(on_incoming_call)
    phone.register_registration_state_callback(on_registration_state_changed)

    print("Registering SIP client with TJA-470...")
    await phone.start()

    # Wait a bit for registration status to settle
    await asyncio.sleep(2)
    print(f"SIP Registration Status: {phone.get_status()}")

    print("\nSIP Interactive CLI commands:")
    print("  c <number>   : Make outgoing call to <number> (e.g. extension or camera SIP ID)")
    print("  a            : Answer incoming call")
    print("  h            : Hang up active call")
    print("  r            : Reject/deny incoming call")
    print("  record <file>: Record incoming audio to a WAV file")
    print("  record off   : Stop recording")
    print("  tone         : Play a 440Hz test tone to the intercom speaker")
    print("  tone off     : Stop playing the test tone")
    print("  status       : Show current registration and call status")
    print("  q            : Quit")

    if initial_call:
        print(f"\nDialing initial target {initial_call}...")
        async with call_lock:
            active_call = await phone.call(initial_call)
        print("Call initiated.")
        asyncio.create_task(monitor_call_audio(active_call))

    loop = asyncio.get_running_loop()

    try:
        while True:
            cmd = await loop.run_in_executor(None, lambda: input("> ").strip())
            if not cmd:
                continue
            if cmd == "q":
                break
            elif cmd.startswith("c "):
                target = cmd[2:].strip()
                print(f"Dialing {target}...")
                async with call_lock:
                    await stop_call_tasks()
                    active_call = await phone.call(target)
                print("Call initiated.")
                asyncio.create_task(monitor_call_audio(active_call))
            elif cmd == "a":
                async with call_lock:
                    if active_call:
                        print("Answering call...")
                        await active_call.answer()
                        print("Call answered.")
                    else:
                        print("No active call to answer.")
            elif cmd == "h":
                async with call_lock:
                    await stop_call_tasks()
                    if active_call:
                        print("Hanging up...")
                        await active_call.hangup()
                        print("Call hung up.")
                        active_call = None
                    else:
                        print("No active call to hang up.")
            elif cmd == "r":
                async with call_lock:
                    await stop_call_tasks()
                    if active_call:
                        print("Rejecting call...")
                        await active_call.deny()
                        print("Call rejected.")
                        active_call = None
                    else:
                        print("No active call to reject.")
            elif cmd.startswith("record "):
                arg = cmd[7:].strip()
                if arg == "off":
                    if record_task:
                        record_task.cancel()
                        record_task = None
                        print("Recording stopped.")
                    else:
                        print("No active recording to stop.")
                else:
                    async with call_lock:
                        if not active_call or active_call.state != CallState.ANSWERED:
                            print("Error: No active answered call.")
                        elif record_task:
                            print("Already recording to a file.")
                        else:
                            record_task = asyncio.create_task(record_audio_task(active_call, arg))
            elif cmd == "record":
                print("Usage: record <file> or record off")
            elif cmd == "tone":
                async with call_lock:
                    if not active_call or active_call.state != CallState.ANSWERED:
                        print("Error: No active answered call.")
                    elif tone_task:
                        print("Tone is already playing.")
                    else:
                        tone_task = asyncio.create_task(play_tone_task(active_call))
            elif cmd == "tone off":
                if tone_task:
                    tone_task.cancel()
                    tone_task = None
                    print("Tone stopped.")
                else:
                    print("No tone playing.")
            elif cmd == "status":
                print(f"SIP Registration Status: {phone.get_status()}")
                async with call_lock:
                    if active_call:
                        print(f"Active Call State: {active_call.state}")
                    else:
                        print("No active call.")
            else:
                print("Unknown command. Try: c <number>, a, h, r, record <file>, record off, tone, tone off, status, q")
    except KeyboardInterrupt:
        print("\nExiting...")
    finally:
        await stop_call_tasks()
        print("Stopping SIP client...")
        await phone.stop()


async def run_sip(args):
    config = load_config()
    if not config:
        print(f"❌ Configuration not found at {CONFIG_FILE}.")
        print("Please run `tja470 pair` first.")
        sys.exit(1)

    runner = AiohttpRunner()
    client = TJA470IntercomClient(
        host=config["host"],
        username=config["username"],
        password=config["password"],
        runner=runner
    )

    # Load cached cookies
    cached_cookies = config.get("cookies", {})
    if cached_cookies:
        client.set_cookies(cached_cookies)

    try:
        uuid_val = config["uuid"]

        # Validate session by getting manifest (this will re-authenticate with basic auth if cookie is expired)
        try:
            await client.get_manifest()
        except TJA470AuthError:
            print("❌ Authentication failed. Your credentials might be invalid.")
            sys.exit(1)

        # Update cached cookies just in case they were refreshed
        new_cookies = client.get_cookies()
        if new_cookies != cached_cookies:
            config["cookies"] = new_cookies
            save_config(config)

        print("\nRetrieving provisioning configuration for SIP client...")
        prov = await client.get_provisioning(uuid_val)

        # Close runner since we don't need the HTTP runner anymore
        await runner.close()

        # Extract SIP details
        sip_id = prov.sip_info.sip_id
        sip_password = prov.sip_info.sip_password
        host = config["host"]

        if not sip_id or not sip_password:
            print("❌ Provisioning returned empty SIP ID or Password.")
            sys.exit(1)

        # Run pyVoIP client in async mode
        await run_sip_client_async(
            host,
            sip_id,
            sip_password,
            args.call,
            debug_sip=args.debug,
            record_to=args.record_to,
            play_tone=args.play_tone,
            local_ip=args.local_ip,
            rtp_port=args.rtp_port,
        )

    except Exception as e:
        print(f"\n❌ An error occurred: {e}")
        await runner.close()


def main():
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
