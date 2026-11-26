import asyncio
import time

import requests


async def fetch_user(uid):
    await asyncio.sleep(0)
    return uid


class Service:
    async def refresh(self):
        return 1

    def sync_helper(self):
        return 2

    async def run_bad(self):
        self.refresh()  # BUG: never awaited
        fetch_user(1)  # BUG: never awaited
        asyncio.sleep(1)  # BUG
        self.sync_helper()  # fine

    async def run_ok(self):
        await self.refresh()
        t = asyncio.create_task(fetch_user(2))
        coro = fetch_user(3)
        await coro
        return await asyncio.gather(fetch_user(4), self.refresh()), t

    async def blocking(self):
        time.sleep(1)  # BUG: blocks event loop
        requests.get("http://x")  # BUG
        return heavy_parse("x")  # BUG (inferred: offloaded elsewhere)


def heavy_parse(s):
    return s


async def offload_a():
    return await asyncio.to_thread(heavy_parse, "a")


async def offload_b():
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, heavy_parse, "b")


class FakeRedis:
    async def expire(self, key, ttl):
        return True


def ok_sync_namesakes(seen, r):
    seen.discard(1)  # set.discard; sync
    r.expire("k", 10)  # sync redis client; `.expire` is never awaited anywhere


async def awaited_a(r):
    await r.ping_async()


async def awaited_b(r):
    await r.ping_async()


async def awaited_c(r):
    await r.ping_async()


async def dropped(r):
    r.ping_async()  # BUG (MAY): awaited at 3 of 4 sites


class Pinger:
    async def ping_async(self):
        return 1
