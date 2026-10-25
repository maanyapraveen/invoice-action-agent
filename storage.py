
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.getenv("A2A_DB_PATH", str(Path(__file__).with_name("invoice_agent.db")))).resolve()


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        DB_PATH,
        timeout=41,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 41000")
    connection.execute("PRAGMA journal_mode = WAL")
    try:
        yield connection
    finally:
        connection.close()


def initialize_database():
    with connect() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                principal TEXT NOT NULL,
                context_id TEXT NOT NULL,
                batch_id TEXT,
                state TEXT NOT NULL,
                task_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_principal
            ON tasks(principal, created_at);

            CREATE TABLE IF NOT EXISTS idempotency (
                principal TEXT NOT NULL,
                message_id TEXT NOT NULL,
                message_hash TEXT NOT NULL,
                task_id TEXT NOT NULL,
                response_json TEXT NOT NULL,
                PRIMARY KEY (principal, message_id),
                FOREIGN KEY (task_id) REFERENCES tasks(task_id)
            );

            CREATE TABLE IF NOT EXISTS decision_cache (
                content_hash TEXT PRIMARY KEY,
                decision_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS executions (
                execution_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                receipt_nonce TEXT NOT NULL,
                receipt_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(task_id, action_id)
            );
        """)


def canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def message_hash(message):
    encoded = canonical_json(message).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def get_idempotent_response(principal, message_id, message):
    with connect() as connection:
        row = connection.execute(
            """
            SELECT message_hash, response_json
            FROM idempotency
            WHERE principal = ? AND message_id = ?
            """,
            (principal, message_id),
        ).fetchone()

    if row is None:
        return None

    if row["message_hash"] != message_hash(message):
        raise ValueError("IDEMPOTENCY_CONFLICT")

    return json.loads(row["response_json"])


def get_task(principal, task_id):
    with connect() as connection:
        row = connection.execute(
            """
            SELECT task_json
            FROM tasks
            WHERE principal = ? AND task_id = ?
            """,
            (principal, task_id),
        ).fetchone()

    if row is None:
        return None

    return json.loads(row["task_json"])


def list_tasks(principal):
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT task_json
            FROM tasks
            WHERE principal = ?
            ORDER BY created_at DESC
            """,
            (principal,),
        ).fetchall()

    return [json.loads(row["task_json"]) for row in rows]


initialize_database()
