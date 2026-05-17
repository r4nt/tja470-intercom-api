import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict

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

async def async_main():
    parser = argparse.ArgumentParser(description="TJA-470 Intercom CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # `pair` command
    pair_parser = subparsers.add_parser("pair", help="Authenticate and pair a new client UUID")
    pair_parser.add_argument("--host", required=True, help="IP address of the TJA-470")
    pair_parser.add_argument("--username", required=True, help="Username for the TJA-470")
    pair_parser.add_argument("--password", required=True, help="Password for the TJA-470")
    pair_parser.add_argument("--uuid", required=True, help="UUID for the client (e.g. from uuidgen)")

    # `run` command
    run_parser = subparsers.add_parser("run", help="Run commands using the cached session")
    run_parser.add_argument("--open-door", action="store_true", help="Open the door (door ID 1)")
    run_parser.add_argument("--switch-camera", action="store_true", help="Switch the camera feed")
    run_parser.add_argument("--provisioning", action="store_true", help="Print the provisioning info")

    args = parser.parse_args()

    if args.command == "pair":
        await run_pair(args)
    elif args.command == "run":
        await run_command(args)

async def run_pair(args):
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
        
        print(f"\nRegistering UUID '{args.uuid}' to device ID {target_device.id}...")
        await client.set_uid(target_device.id, args.uuid)
        print("UUID registered successfully!")

        # Save config
        config = {
            "host": args.host,
            "username": args.username,
            "password": args.password,
            "uuid": args.uuid,
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
            print(f"  SIP Password: {prov.sip_info.sip_password}")
            print(f"  RTSP Video URL: {prov.rtsp_video_url}")

        if args.open_door:
            print("\n🚪 Opening the door...")
            await client.open_door(door_id=1)
            print("Door opened successfully!")

        if args.switch_camera:
            print("\n📷 Switching the camera feed...")
            await client.switch_camera(uuid)
            print("Camera switched successfully!")

    except Exception as e:
        print(f"\n❌ An error occurred: {e}")
    finally:
        await runner.close()


def main():
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
