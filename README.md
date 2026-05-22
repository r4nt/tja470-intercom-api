# aiotja470_intercom

An asynchronous, stateless Python client for the Hager TJA470 Intercom API. 

This library was heavily designed to act as the underlying foundation for Home Assistant integrations. It relies purely on dependency injection for network requests, correctly manages raw session cookies over IP addresses, and provides an end-to-end command-line interface for local testing.

> ⚠️ **Disclaimer:** This is an unofficial library. It is **not** affiliated with, endorsed by, or supported by Hager. Use it at your own risk.

## Installation

```bash
pip install aiotja470_intercom
```

## CLI Usage

This package bundles a highly useful `tja470` binary that acts as a local test bench. It replicates exact Home Assistant session storage to `~/.tja470_config.json`, including caching the `aiohttp` cookie jars!

### 1. Pair a new device
You only need to run this once. It verifies credentials, registers a UUID as a mobile client, and saves your configuration and `JSESSIONID` cookies.
```bash
# If you don't provide a --uuid, a random one will be generated and registered.
tja470 pair --host 192.168.1.100 --username user@example.com --password my_password 
```

### 2. View Connection Status
Verifies the current session, fetches the latest provisioning data (including SIP credentials and RTSP URLs), and lists all extensions.
```bash
tja470 status
```

### 3. Run Intercom Commands
Run commands utilizing the cached session cookies without re-authenticating!
```bash
# Open the door
tja470 run --open-door

# Open the door at a specific camera position index (e.g. 0)
# (Cycles the camera feed to position 0 first, then triggers release)
tja470 run --open-door-at 0

# Switch camera
tja470 run --switch-camera

# View raw provisioning data
tja470 run --provisioning
```

### Debugging
You can attach `--debug` to **any** command to enable verbose HTTP tracing. It will print the exact requests, headers, sent cookies, and raw JSON response content!
```bash
tja470 --debug status
```

---

## Python API Usage

The client is completely decoupled from the network request layer via a `Runner` protocol. This allows you to easily inject a mock runner during tests.

### Basic Setup
```python
import asyncio
import aiohttp
from aiotja470_intercom import TJA470IntercomClient, AiohttpRunner

async def main():
    # The AiohttpRunner automatically initializes an aiohttp.CookieJar(unsafe=True)
    # under the hood so it correctly caches cookies from local IP addresses.
    runner = AiohttpRunner()
    
    client = TJA470IntercomClient(
        host="192.168.1.100",
        username="user@example.com",
        password="your_password",
        runner=runner
    )

    try:
        # Verify the manifest
        manifest = await client.get_manifest()
        print(f"Firmware: {manifest.fw}")

        # Execute commands
        await client.open_door(door_id=1)

    finally:
        await runner.close()

if __name__ == "__main__":
    asyncio.run(main())
```

### Session & Cookie Management
For Home Assistant integrations, you do not want to spam the TJA-470 with `Basic Auth` headers on every request. 
The client is hard-coded with a **cookie-first fallback loop**. It attempts the request utilizing the cached cookies first. If the intercom rejects it (e.g. cookie expired), it automatically catches the `401 Unauthorized`, re-authenticates using your credentials to get a fresh session cookie, and seamlessly retries the request!

```python
# Extract all cookies to persist across reboots (e.g. in HA ConfigEntry.data)
cookies_dict = client.get_cookies()

# Later, on integration startup, re-inject the cookies
client.set_cookies(cookies_dict)
```

### Advanced API Features

```python
# 1. Fetching available devices for pairing
# Retrieves all Unassigned 'MOBILE_CLIENT' slots in the Hager UI
devices = await client.get_free_devices()
device_id = devices[0].id

# 2. Registering a UUID to an open device slot
await client.set_uid(device_id, "your-uuid-string")

# 3. Fetching the Provisioning Info
# This returns all the juicy details (SIP passwords, RTSP urls, etc.)
prov = await client.get_provisioning("your-uuid-string")

print(f"SIP Password: {prov.sip_info.sip_password}")
print(f"RTSP Stream: {prov.rtsp_video_url}")
for ext in prov.called_elements:
    print(f"Known device: {ext.name} (SIP: {ext.sip_id})")

# 4. Opening the door
await client.open_door(door_id=1)

# 5. Switching camera feeds
await client.switch_camera("your-uuid-string")

# 6. Switching to a specific camera position and opening the door
await client.open_door_at_position("your-uuid-string", position=0)
```

## Exception Handling
The library provides native Home Assistant style typed exceptions:
- `TJA470Error`: Base class for all library errors.
- `TJA470ConnectionError`: Raised on timeouts, unreachable host, or DNS failures.
- `TJA470AuthError`: Raised on `401 Unauthorized` or `403 Forbidden`.
- `TJA470ResponseError`: Raised when the intercom returns invalid JSON or unexpected schema data.