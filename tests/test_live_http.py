"""Small real HTTP integration test; fixed 10-second production sampling."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import sqlite3
import httpx
from service_monitor.collector import collect
from service_monitor.traffic import traffic
from service_monitor.storage import export_csv


def test_live_backend_traffic_collector(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    with (tmp_path / "server.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "service_monitor.cli", "serve", "--port", str(port),
             "--enable-dev-scenarios", "--baseline-error-probability", "0"],
            stdout=log, stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            with httpx.Client(base_url=url, timeout=1, trust_env=False) as client:
                for _ in range(100):
                    if process.poll() is not None:
                        raise AssertionError("Backend exited: "+(tmp_path / "server.log").read_text())
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(.1)
                else:
                    raise AssertionError("Backend did not start")
                response = client.post("/dev/scenario", json={
                    "name": "error-burst", "error_probability": 1, "duration_seconds": 12})
                assert response.status_code == 200
                db = tmp_path / "live.sqlite"

                async def exercise():
                    return await asyncio.gather(
                        collect(url, db, duration_minutes=.4),
                        traffic(url, duration_minutes=.4, rate=2, profile="constant"))
                _, stats = asyncio.run(exercise())
                assert stats["completed"] >= 20 and stats["http_errors"] > 0
                assert client.get("/dev/scenario").json()["active"] is None
            with sqlite3.connect(db) as connection:
                rows = [json.loads(r[0]) for r in connection.execute("SELECT payload FROM metrics")]
                assert len(rows) >= 1
                assert all(r["source"] == "measured" for r in rows)
                assert any(r["server_errors"] > 0 and r["phase"] == "fault" for r in rows)
                assert all(r["memory_rss_mib"] > 0 for r in rows)
            assert export_csv(db, tmp_path / "live.csv") >= 1
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
