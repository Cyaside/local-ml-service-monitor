"""CLI for backend, traffic, collection, and local incident history."""
import asyncio
import json
import sqlite3
import httpx


COMMANDS = {"serve", "traffic", "collect", "monitor", "export", "incidents",
            "scenario", "scenario-status", "scenario-reset"}


def register(commands):
    p = commands.add_parser("serve", help="Run the local measured checkout backend")
    p.add_argument("--host", choices=["127.0.0.1", "localhost", "0.0.0.0"], default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--service-id", default="checkout-local")
    p.add_argument("--enable-dev-scenarios", action="store_true")
    p.add_argument("--baseline-error-probability", type=float, default=.002)
    p.add_argument("--seed", type=int, default=42)

    p = commands.add_parser("traffic", help="Generate bounded real requests")
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--duration-minutes", type=float, default=30)
    p.add_argument("--rate", type=float, default=3)
    p.add_argument("--profile", choices=["constant", "normal-cycle", "healthy-surge", "validation-cycle"], default="normal-cycle")
    p.add_argument("--seed", type=int, default=42)

    for name in ("collect", "monitor"):
        p = commands.add_parser(name)
        p.add_argument("--url", default="http://127.0.0.1:8000")
        p.add_argument("--db", required=True)
        p.add_argument("--duration-minutes", type=float, default=30)
        if name == "monitor":
            p.add_argument("--model", required=True,
                           help="Path to a trained model bundle")
            p.add_argument("--allow-experimental", action="store_true")
            p.add_argument("--pretty", action="store_true", help="Readable terminal output")
    p = commands.add_parser("export")
    p.add_argument("--db", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--phase", choices=["normal", "fault", "recovery", "idle"])
    p.add_argument("--run-id")
    p = commands.add_parser("incidents")
    p.add_argument("--db", required=True)
    p.add_argument("--limit", type=int, default=10)
    for name in ("scenario", "scenario-status", "scenario-reset"):
        p = commands.add_parser(name)
        p.add_argument("--url", default="http://127.0.0.1:8000")
        if name == "scenario":
            p.add_argument("--name", required=True, choices=["slow-response", "gradual-latency",
                                                              "error-burst", "memory-growth",
                                                              "combined-degradation"])
            p.add_argument("--duration-seconds", type=float, default=180)
            p.add_argument("--delay-ms", type=float, default=600)
            p.add_argument("--error-probability", type=float, default=.2)
            p.add_argument("--step-mib", type=int, default=2)
            p.add_argument("--cap-mib", type=int, default=64)


def dispatch(args):
    if args.command == "serve":
        import uvicorn
        from .checkout.app import create_app
        if not 1 <= args.port <= 65535:
            raise ValueError("Port must be 1..65535")
        app = create_app(args.service_id, args.enable_dev_scenarios,
                         args.baseline_error_probability, args.seed)
        uvicorn.run(app, host=args.host, port=args.port, workers=1, access_log=False)
    elif args.command == "traffic":
        from .traffic import traffic
        print(json.dumps(asyncio.run(traffic(args.url, args.duration_minutes,
                                            args.rate, args.profile, args.seed)), indent=2))
    elif args.command in {"collect", "monitor"}:
        from .collector import collect
        asyncio.run(collect(args.url, args.db, args.duration_minutes,
                            getattr(args, "model", None),
                            getattr(args, "allow_experimental", False),
                            getattr(args, "pretty", False)))
    elif args.command == "export":
        from .storage import export_csv
        print(json.dumps({"exported_rows": export_csv(args.db, args.out, args.phase, args.run_id)}))
    elif args.command == "incidents":
        from .storage import history
        if not 1 <= args.limit <= 1000:
            raise ValueError("Limit must be 1..1000")
        print(json.dumps(history(args.db, args.limit), indent=2))
    else:
        with httpx.Client(base_url=args.url.rstrip("/"), timeout=3, trust_env=False) as client:
            if args.command == "scenario":
                from .checkout.scenarios import ScenarioConfig
                config = ScenarioConfig(name=args.name, duration_seconds=args.duration_seconds,
                    delay_ms=args.delay_ms, error_probability=args.error_probability,
                    step_mib=args.step_mib, cap_mib=args.cap_mib)
                response = client.post("/dev/scenario", json=config.model_dump())
            elif args.command == "scenario-reset":
                response = client.post("/dev/reset")
            else:
                response = client.get("/dev/scenario")
            response.raise_for_status()
            print(json.dumps(response.json(), indent=2))
