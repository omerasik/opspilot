import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class Conflict(Exception):
    pass


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, request_key TEXT UNIQUE, payload TEXT NOT NULL,
                    state TEXT NOT NULL, status TEXT NOT NULL, available REAL NOT NULL,
                    lease TEXT, lease_until REAL NOT NULL DEFAULT 0, created REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ready_runs ON runs(status, available, lease_until, created);
            """)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def submit(self, payload: dict, key: str | None):
        serialized = json.dumps(payload, sort_keys=True)
        now = time.time()
        state = {"stage": "plan", "attempt": 0, "tool_calls": 0, "history": [], "outputs": {}}
        self.event(state, "run_accepted")
        with self.connect() as conn:
            row = conn.execute("""
                INSERT INTO runs(id, request_key, payload, state, status, available, created)
                VALUES(?, ?, ?, ?, 'queued', ?, ?)
                ON CONFLICT(request_key) DO NOTHING RETURNING *
            """, (str(uuid4()), key, serialized, json.dumps(state), now, now)).fetchone()
            if row is None:
                row = conn.execute("SELECT * FROM runs WHERE request_key = ?", (key,)).fetchone()
                if row["payload"] != serialized:
                    raise Conflict("request_key_conflict")
            return self.view(row)

    def list(self):
        with self.connect() as conn:
            return [self.view(row) for row in conn.execute("SELECT * FROM runs ORDER BY created DESC LIMIT 100")]

    def get(self, run_id: str):
        with self.connect() as conn:
            return self.view(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())

    def claim(self):
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("""
                SELECT * FROM runs WHERE (status = 'queued' AND available <= ?)
                OR (status = 'running' AND lease_until <= ?) ORDER BY available, created LIMIT 1
            """, (now, now)).fetchone()
            if row is None:
                conn.commit()
                return None
            run = self.view(row)
            state = run["state"]
            if state["attempt"] >= 3:
                self.event(state, "lease_exhausted")
                conn.execute("UPDATE runs SET state=?, status='failed', lease=NULL WHERE id=?",
                             (json.dumps(state), run["id"]))
                conn.commit()
                return None
            if state["stage"] == "retrieve_vendor":
                if state["tool_calls"] >= run["payload"]["tool_budget"]:
                    self.event(state, "tool_budget_exhausted")
                    conn.execute("UPDATE runs SET state=?, status='failed', lease=NULL WHERE id=?",
                                 (json.dumps(state), run["id"]))
                    conn.commit()
                    return None
                state["tool_calls"] += 1
            state["attempt"] += 1
            lease = str(uuid4())
            self.event(state, "lease_recovered" if run["status"] == "running" else "step_started")
            conn.execute("UPDATE runs SET state=?, status='running', lease=?, lease_until=? WHERE id=?",
                         (json.dumps(state), lease, now + 20, run["id"]))
            conn.commit()
            return run, lease

    def checkpoint(self, run: dict, lease: str, status: str, delay: float = 0):
        now = time.time()
        with self.connect() as conn:
            return conn.execute("""
                UPDATE runs SET state=?, status=?, available=?, lease=NULL, lease_until=0
                WHERE id=? AND status='running' AND lease=? AND lease_until>?
            """, (json.dumps(run["state"]), status, now + delay, run["id"], lease, now)).rowcount == 1

    def decision(self, run_id: str, decision: str):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            run = self.view(row)
            if run is None:
                return None
            if decision == "cancel":
                if run["status"] not in {"queued", "running", "awaiting_approval"}:
                    raise Conflict("cannot_cancel")
                status = "cancelled"
            elif run["status"] == "awaiting_approval":
                status = "completed" if decision == "approve" else "rejected"
            else:
                raise Conflict("approval_unavailable")
            self.event(run["state"], f"operator_{decision}")
            updated = conn.execute("""
                UPDATE runs SET status=?, state=?, lease=NULL, lease_until=0 WHERE id=? RETURNING *
            """, (status, json.dumps(run["state"]), run_id)).fetchone()
            conn.commit()
            return self.view(updated)

    @staticmethod
    def event(state: dict, code: str, **attributes):
        state["history"].append({"event": code, "stage": state["stage"], "at": time.time(), **attributes})

    @staticmethod
    def view(row):
        if row is None:
            return None
        return {"id": row["id"], "status": row["status"], "created": row["created"],
                "payload": json.loads(row["payload"]), "state": json.loads(row["state"])}
