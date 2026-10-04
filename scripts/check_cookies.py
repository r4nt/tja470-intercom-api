"""Check session cookie handling against a real TJA470.

Uses a session with aiohttp's default cookie jar (like Home Assistant's shared
session) and counts how often the device asks for a new login.

Only read-only manifest requests are made; no pairing is performed.

Usage:
    TJA470_PASSWORD=... python scripts/check_cookies.py HOST USERNAME [--requests N] [--pause SECONDS]
"""
import argparse
import asyncio
import logging
import os

import aiohttp

from aiotja470_intercom import AiohttpRunner, TJA470IntercomClient


class StatusCounter(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.statuses: list[str] = []
        self.sent_cookies = 0

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith("Response Status: "):
            self.statuses.append(message.removeprefix("Response Status: "))
        elif message.startswith("Sending Cookies: "):
            self.sent_cookies += 1

    def take(self) -> tuple[list[str], int]:
        result = (self.statuses, self.sent_cookies)
        self.statuses, self.sent_cookies = [], 0
        return result


async def run_requests(client: TJA470IntercomClient, counter: StatusCounter, count: int, label: str) -> None:
    for i in range(count):
        await client.get_manifest()
        statuses, sent = counter.take()
        print(f"  {label} request {i + 1}: statuses={statuses} cookies_sent={'yes' if sent else 'no'}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("host")
    parser.add_argument("username")
    parser.add_argument("--requests", type=int, default=3)
    parser.add_argument("--pause", type=int, default=0, help="seconds to wait before a final request")
    args = parser.parse_args()
    password = os.environ["TJA470_PASSWORD"]

    counter = StatusCounter()
    runner_logger = logging.getLogger("aiotja470_intercom.runner")
    runner_logger.setLevel(logging.DEBUG)
    runner_logger.addHandler(counter)

    async with aiohttp.ClientSession() as session:
        print("1. Shared session with default cookie jar:")
        client = TJA470IntercomClient(args.host, args.username, password, AiohttpRunner(session))
        await run_requests(client, counter, args.requests, "shared")
        cookies = client.get_cookies()
        print(f"  cookie names stored: {sorted(cookies)}")

        print("2. Fresh runner with restored cookies (like an HA restart):")
        restored = TJA470IntercomClient(args.host, args.username, password, AiohttpRunner(session))
        restored.set_cookies(cookies)
        await run_requests(restored, counter, 1, "restored")

        if args.pause:
            print(f"3. Waiting {args.pause}s to see whether the session survives:")
            await asyncio.sleep(args.pause)
            await run_requests(restored, counter, 1, "after pause")

    print()
    print("Expected with working cookies: one 401 then 200 on the first request,")
    print("plain 200 with cookies_sent=yes afterwards, and a plain 200 after restore.")


if __name__ == "__main__":
    asyncio.run(main())
