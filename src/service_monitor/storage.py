"""SQLite persistence with one process writer; readers remain available."""
import csv
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
import portalocker


@contextmanager
def read_connection(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=3)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = portalocker.Lock(str(self.path)+".writer.lock", timeout=0)
        try:
            self.lock.acquire()
        except portalocker.exceptions.LockException as exc:
            raise ValueError("Database already has a collector/monitor writer") from exc
        try:
            self.db = sqlite3.connect(self.path, timeout=3)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY, service_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL, timestamp TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(service_id,run_id,seq));
                CREATE INDEX IF NOT EXISTS metric_time ON metrics(timestamp);
                CREATE TABLE IF NOT EXISTS predictions (
                    metric_id INTEGER NOT NULL REFERENCES metrics(id),
                    model_version TEXT NOT NULL, score REAL NOT NULL, threshold REAL NOT NULL,
                    is_anomaly INTEGER NOT NULL, observed_at TEXT NOT NULL,
                    PRIMARY KEY(metric_id,model_version));
                CREATE TABLE IF NOT EXISTS incidents (
                    id TEXT PRIMARY KEY, service_id TEXT NOT NULL, status TEXT NOT NULL,
                    opened_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_open_incident
                    ON incidents(service_id) WHERE status='OPEN';
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
                    mode TEXT NOT NULL, target_url TEXT NOT NULL);
            """)
        except Exception:
            if hasattr(self, "db"):
                self.db.close()
            self.lock.release()
            raise

    def close(self):
        try:
            self.db.close()
        finally:
            self.lock.release()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def insert_metric(self, row):
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO metrics(service_id,run_id,seq,timestamp,payload) VALUES(?,?,?,?,?)",
            (row["service_id"], row["run_id"], row["seq"], row["timestamp"],
             json.dumps(row, allow_nan=False)))
        return cursor.lastrowid if cursor.rowcount else None

    def save_incident(self, event):
        self.db.execute(
            "INSERT INTO incidents VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,payload=excluded.payload",
            (event["id"], event["service_id"], event["status"], event["opened_at"], json.dumps(event)))

    def interrupt_open(self, timestamp, reason):
        for row in self.db.execute("SELECT payload FROM incidents WHERE status='OPEN'").fetchall():
            event = json.loads(row["payload"])
            event.update(status="INTERRUPTED", ended_at=timestamp, reason=reason)
            self.save_incident(event)


def export_csv(database, output, phase=None, run_id=None):
    output = Path(output)
    if output.exists():
        raise FileExistsError(f"Export already exists: {output}")
    with read_connection(database) as connection:
        rows = [json.loads(row["payload"]) for row in connection.execute(
            "SELECT payload FROM metrics ORDER BY timestamp,id")]
    if run_id:
        rows = [row for row in rows if row["run_id"] == run_id]
    if phase:
        rows = [row for row in rows if row["phase"] == phase]
    if not rows:
        raise ValueError("No matching metrics to export")
    if len({row["service_id"] for row in rows}) != 1:
        raise ValueError("Export requires one service")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def history(database, limit=10):
    with read_connection(database) as connection:
        return [json.loads(r["payload"]) for r in connection.execute(
            "SELECT payload FROM incidents ORDER BY opened_at DESC LIMIT ?", (limit,))]
