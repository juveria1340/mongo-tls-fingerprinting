"""
Traffic generator: drives realistic, randomised workflows across the three APIs.

Env:
  USER_API, TXN_API, AUTH_API  base URLs (defaults = in-cluster service DNS)
  DURATION_S   how long to run           (default 1800 = 30 min)
  WORKERS      concurrent virtual users  (default 6)
  THINK_MS     max think time per step   (default 400)
"""
import asyncio
import os
import random
import string
import time

import httpx

USER = os.environ.get("USER_API", "http://user-api:8000")
TXN = os.environ.get("TXN_API", "http://txn-api:8000")
AUTH = os.environ.get("AUTH_API", "http://auth-api:8000")
DURATION = int(os.environ.get("DURATION_S", "1800"))
WORKERS = int(os.environ.get("WORKERS", "6"))
THINK = int(os.environ.get("THINK_MS", "400"))

MERCHANTS = ["tesco", "amazon", "uber", "spotify", "lidl", "trainline", "boots", "argos"]

users: list[str] = []
tokens: list[str] = []
stats: dict[str, int] = {}


def rstr(lo: int, hi: int) -> str:
    return "".join(random.choices(string.ascii_letters + " ", k=random.randint(lo, hi)))


def bump(k: str):
    stats[k] = stats.get(k, 0) + 1


async def step(c: httpx.AsyncClient):
    # Seed users first so other workflows have something to touch.
    if len(users) < 20 or random.random() < 0.12:
        r = await c.post(f"{USER}/users", json={
            "name": rstr(5, 30), "email": f"{rstr(4, 12).replace(' ', '')}@x.io",
            "bio": rstr(0, 1500)})
        users.append(r.json()["id"]); bump("user.insert"); return

    roll = random.random()
    uid = random.choice(users)

    if roll < 0.15:
        await c.get(f"{USER}/users/{uid}"); bump("user.find_one")
    elif roll < 0.22:
        await c.put(f"{USER}/users/{uid}", json={"bio": rstr(0, 2000)}); bump("user.update")
    elif roll < 0.25 and len(users) > 30:
        users.remove(uid)
        await c.delete(f"{USER}/users/{uid}"); bump("user.delete")
    elif roll < 0.45:
        for _ in range(random.randint(1, 3)):
            await c.post(f"{TXN}/transactions", json={
                "user_id": uid, "amount": round(random.uniform(1, 500), 2),
                "merchant": random.choice(MERCHANTS), "note": rstr(0, 300)})
            bump("txn.insert")
    elif roll < 0.57:
        await c.get(f"{TXN}/transactions", params={
            "user_id": uid, "limit": random.choice([5, 10, 20, 50])}); bump("txn.find_many")
    elif roll < 0.65:
        await c.get(f"{TXN}/transactions/summary/{uid}"); bump("txn.aggregate")
    elif roll < 0.77:
        r = await c.post(f"{AUTH}/sessions", json={
            "user_id": uid, "user_agent": rstr(10, 120)})
        tokens.append(r.json()["token"]); bump("auth.insert")
    elif roll < 0.90 and tokens:
        await c.get(f"{AUTH}/sessions/{random.choice(tokens)}"); bump("auth.find_one")
    elif roll < 0.96 and tokens:
        await c.put(f"{AUTH}/sessions/{random.choice(tokens)}"); bump("auth.update")
    elif tokens:
        t = tokens.pop(random.randrange(len(tokens)))
        await c.delete(f"{AUTH}/sessions/{t}"); bump("auth.delete")


async def worker(deadline: float):
    async with httpx.AsyncClient(timeout=30) as c:
        while time.time() < deadline:
            try:
                await step(c)
            except Exception as e:  # keep going; a failed request just isn't labelled
                bump(f"error.{type(e).__name__}")
            await asyncio.sleep(random.uniform(0, THINK) / 1000)


async def reporter(deadline: float):
    while time.time() < deadline:
        await asyncio.sleep(30)
        print(f"[{int(deadline - time.time())}s left] {sum(stats.values())} requests "
              f"{dict(sorted(stats.items()))}", flush=True)


async def main():
    # wait for APIs
    async with httpx.AsyncClient(timeout=5) as c:
        for base in (USER, TXN, AUTH):
            for _ in range(60):
                try:
                    if (await c.get(f"{base}/healthz")).status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(2)
    deadline = time.time() + DURATION
    print(f"running {WORKERS} workers for {DURATION}s", flush=True)
    await asyncio.gather(reporter(deadline), *(worker(deadline) for _ in range(WORKERS)))
    print("DONE", dict(sorted(stats.items())), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
