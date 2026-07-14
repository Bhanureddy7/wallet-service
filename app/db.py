"""
Database layer.

Datastore choice: SQLite (file-based), accessed via Python's stdlib sqlite3.
See DESIGN.md for the full justification. Short version:
  - Single-node service, so we don't need a client/server DB.
  - SQLite gives us real ACID transactions and durable commits (WAL + fsync)
    without operating a separate database process/container.
  - Writers are serialized by SQLite itself (single-writer), which is exactly
    the concurrency model we want for a wallet: we WANT balance-affecting
    writes to be strictly ordered, not to overlap.
"""
import sqlite3
import os
import time

DB_PATH = os.environ.get("WALLET_DB_PATH", "/data/wallet.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    player_id   TEXT PRIMARY KEY,
    balance     INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0)
);

CREATE TABLE IF NOT EXISTS inventory (
    player_id   TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    qty         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (player_id, item_id)
);

CREATE TABLE IF NOT EXISTS claimed_rewards (
    player_id   TEXT NOT NULL,
    reward_id   TEXT NOT NULL,
    claimed_at  TEXT NOT NULL,
    PRIMARY KEY (player_id, reward_id)
);

-- Idempotency ledger for exactly-once dedup of mutating requests.
-- Scoped by (player_id, endpoint, idem_key) so two different players (or
-- endpoints) can never collide on the same client-chosen key.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    player_id       TEXT NOT NULL,
    endpoint        TEXT NOT NULL,
    idem_key        TEXT NOT NULL,
    request_hash    TEXT NOT NULL,
    status_code     INTEGER NOT NULL,
    response_body   TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (player_id, endpoint, idem_key)
);

-- Append-only audit trail. Never updated, only inserted. Used for
-- reconciliation (see RESILIENCE.md, "detect and correct" section).
CREATE TABLE IF NOT EXISTS ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id   TEXT NOT NULL,
    type        TEXT NOT NULL,   -- CREDIT, DEBIT, GRANT
    amount      INTEGER,
    ref         TEXT,            -- reason / itemId / rewardId
    idem_key    TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_player ON ledger(player_id);
"""


def get_conn() -> sqlite3.Connection:
    """
    Open a fresh connection for this request/thread.

    PRAGMA choices (this is the crux of the durability story):
      - journal_mode=WAL: writers don't block readers, and commits are a
        single append to the WAL file rather than rewriting the main DB file,
        which is both faster and gives us a well-defined crash-recovery
        format (SQLite replays/truncates the WAL on next open).
      - synchronous=FULL: fsync on every commit. This is what actually makes
        a commit survive kill -9 + power loss, not just process crash. It's
        slower than NORMAL, but correctness > throughput per the brief.
      - busy_timeout: instead of failing immediately when another writer
        holds the lock, block (up to the timeout) and retry. This is how we
        get "concurrency correctness on a balance" for free from SQLite's
        own single-writer serialization instead of hand-rolled locking.
      - foreign_keys=ON: defensive; we don't currently rely on FKs, but keep
        integrity checks on in case the schema grows them.
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
