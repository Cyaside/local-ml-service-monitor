"""Single-worker FastAPI backend. No real purchases or external services."""
import asyncio
import random
import time
from contextlib import asynccontextmanager, suppress
from uuid import uuid4
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from .scenarios import Scenarios, ScenarioConfig
from .telemetry import Telemetry


class CheckoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str = Field(min_length=1, max_length=80)
    quantity: int = Field(default=1, ge=1, le=100)


def create_app(service_id="checkout-local", enable_dev=False, baseline_error_probability=.002, seed=42):
    if not 0 <= baseline_error_probability <= 1:
        raise ValueError("baseline error probability must be in [0, 1]")
    rng = random.Random(seed)

    @asynccontextmanager
    async def lifespan(app):
        app.state.scenarios = Scenarios()
        app.state.telemetry = Telemetry(service_id, app.state.scenarios)

        async def maintain():
            deadline = time.monotonic()+10
            while True:
                await asyncio.sleep(.2)
                app.state.scenarios.tick()
                if time.monotonic() >= deadline:
                    app.state.telemetry.sample()
                    deadline = time.monotonic()+10

        task = asyncio.create_task(maintain())
        app.state.sampler = task
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            app.state.scenarios.reset("shutdown")

    app = FastAPI(title="Local Checkout Demo", lifespan=lifespan)

    @app.middleware("http")
    async def measure_checkout(request, call_next):
        if request.url.path != "/checkout" or request.method != "POST":
            return await call_next(request)
        started, status = time.perf_counter(), 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            app.state.telemetry.record((time.perf_counter()-started)*1000, status)

    @app.get("/health")
    async def health():
        if app.state.sampler.done():
            raise HTTPException(503, "Telemetry sampler stopped")
        return {"status": "ok", "service_id": service_id,
                "run_id": app.state.telemetry.run_id,
                "baseline_error_probability": baseline_error_probability}

    @app.post("/checkout")
    async def checkout(payload: CheckoutRequest):
        app.state.scenarios.tick()
        delay = rng.uniform(.08, .15)
        extra_delay, probability = app.state.scenarios.effects(baseline_error_probability)
        delay += extra_delay
        await asyncio.sleep(delay)
        if rng.random() < probability:
            return JSONResponse({"detail": "Simulated checkout failure"}, status_code=500)
        return {"order_id": str(uuid4()), "sku": payload.sku, "quantity": payload.quantity}

    @app.get("/metrics")
    async def metrics(run_id: str | None = None, since_seq: int = Query(default=-1, ge=-1)):
        return app.state.telemetry.since(run_id, since_seq)

    if enable_dev:
        @app.post("/dev/scenario")
        async def start_scenario(config: ScenarioConfig):
            try:
                return app.state.scenarios.start(config)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc

        @app.get("/dev/scenario")
        async def get_scenario():
            return app.state.scenarios.snapshot()

        @app.post("/dev/reset")
        async def reset_scenario():
            app.state.scenarios.reset()
            return app.state.scenarios.snapshot()
    return app
