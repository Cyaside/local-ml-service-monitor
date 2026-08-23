"""Bounded async traffic; drops scheduler slots instead of growing a queue."""
import asyncio
import random
import time
import httpx


async def traffic(url, duration_minutes=30, rate=3, profile="constant", seed=42):
    if not 0 < duration_minutes <= 1440 or not 0 < rate <= 10:
        raise ValueError("Duration must be 0..1440 minutes and rate 0..10 req/s")
    if profile not in {"constant", "normal-cycle", "healthy-surge", "validation-cycle"}:
        raise ValueError("Unknown traffic profile")
    rng = random.Random(seed)
    start = time.monotonic()
    deadline = start+duration_minutes*60
    stats = {"scheduled": 0, "completed": 0, "http_errors": 0,
             "timeouts": 0, "network_errors": 0, "dropped_schedule_count": 0}
    tasks = set()
    async with httpx.AsyncClient(base_url=url.rstrip("/"), timeout=5, trust_env=False,
                                 limits=httpx.Limits(max_connections=8)) as client:
        async def request():
            try:
                response = await client.post("/checkout", json={"sku": f"demo-{rng.randint(1, 10)}", "quantity": 1})
                stats["completed"] += 1
                stats["http_errors"] += int(response.status_code >= 400)
            except httpx.TimeoutException:
                stats["timeouts"] += 1
            except httpx.HTTPError:
                stats["network_errors"] += 1
        try:
            while time.monotonic() < deadline:
                elapsed = time.monotonic()-start
                current_rate = rate
                if profile == "normal-cycle":
                    current_rate = (1, 3, 6)[int(elapsed//300) % 3]
                elif profile == "healthy-surge":
                    current_rate = 6 if 300 <= elapsed < 600 else 1
                elif profile == "validation-cycle":
                    # Negative control: a healthy traffic surge at minute 10–15.
                    current_rate = 6 if 600 <= elapsed < 900 else 3
                stats["scheduled"] += 1
                if len(tasks) < 8:
                    task = asyncio.create_task(request())
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                else:
                    stats["dropped_schedule_count"] += 1
                await asyncio.sleep(min(1/current_rate, max(0, deadline-time.monotonic())))
            if tasks:
                await asyncio.gather(*list(tasks))
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*list(tasks), return_exceptions=True)
    stats["elapsed_seconds"] = time.monotonic()-start
    stats["achieved_rps"] = stats["completed"]/stats["elapsed_seconds"]
    return stats
