"""
Business logic. Every mutating operation follows the same shape:

    BEGIN IMMEDIATE                      <- acquire write lock NOW, not
                                             lazily on first write. This is
                                             what gives us "exactly one
                                             winner" when two purchases race
                                             the same wallet (requirement 5).
    look up (player_id, endpoint, idem_key) in idempotency_keys
        -> found, same request body   => return cached response (no-op)
        -> found, different body      => 409 (key reuse misuse)
        -> not found                  => run the effect, insert the
                                          idempotency row, in the SAME
                                          transaction
    COMMIT                               <- single fsync'd write. If we get
                                             kill -9'd before this line,
                                             SQLite's WAL recovery discards
                                             the whole uncommitted transaction
                                             on next open: no partial debit,
                                             no partial grant. If kill -9
                                             happens AFTER this line returns
                                             but before the HTTP response
                                             reaches the client, the retry
                                             finds the idempotency row and
                                             replays the same response.

This one pattern is what satisfies requirements 2 and 3 together.
"""
import hashlib
import json
import os
import sqlite3
import time

from .db import get_conn, now_iso

# Test-only hook: when set, sleep for this many seconds AFTER the debit has
# been written but BEFORE commit, so a crash test can land kill -9
# deterministically inside the open transaction instead of hoping timing
# works out. Never set in normal operation.
_CRASH_TEST_DELAY = float(os.environ.get("WALLET_CRASH_TEST_DELAY_SEC", "0"))


class DomainError(Exception):
    """A well-defined, expected rejection (e.g. insufficient funds)."""

    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self.body = body


class IdempotencyConflict(Exception):
    """Same idempotency key reused with a different request body."""


def _hash_body(body: dict) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _with_retry(fn, attempts=5):
    """
    SQLite can raise 'database is locked' even with busy_timeout set, under
    the exact same brief pressure that also makes it correct: many
    concurrent writers on one file. busy_timeout handles the common case by
    blocking; this retry loop is the belt-and-suspenders fallback so a
    client sees a transient 200/402/etc instead of an occasional flaky 500.
    """
    delay = 0.05
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and i < attempts - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise


def _run_idempotent(player_id: str, endpoint: str, idem_key: str,
                     request_body: dict, effect):
    """
    effect(conn) -> (status_code:int, response_body:dict)
    effect may raise DomainError for well-defined rejections; those are
    cached too, so a retried "insufficient funds" request replays the same
    rejection instead of being re-evaluated against a balance that may have
    changed since.
    """
    def txn():
        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            req_hash = _hash_body(request_body)
            row = conn.execute(
                "SELECT request_hash, status_code, response_body "
                "FROM idempotency_keys WHERE player_id=? AND endpoint=? AND idem_key=?",
                (player_id, endpoint, idem_key),
            ).fetchone()

            if row is not None:
                if row["request_hash"] != req_hash:
                    conn.execute("ROLLBACK")
                    raise IdempotencyConflict()
                conn.execute("COMMIT")
                return row["status_code"], json.loads(row["response_body"])

            try:
                status_code, response_body = effect(conn)
            except DomainError as e:
                status_code, response_body = e.status_code, e.body

            conn.execute(
                "INSERT INTO idempotency_keys "
                "(player_id, endpoint, idem_key, request_hash, status_code, response_body, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (player_id, endpoint, idem_key, req_hash, status_code,
                 json.dumps(response_body), now_iso()),
            )
            conn.execute("COMMIT")
            return status_code, response_body
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            conn.close()

    return _with_retry(txn)


def _ensure_wallet(conn, player_id: str):
    conn.execute(
        "INSERT INTO wallets (player_id, balance) VALUES (?, 0) "
        "ON CONFLICT(player_id) DO NOTHING",
        (player_id,),
    )


def credit(player_id: str, idem_key: str, amount: int, reason: str):
    body = {"amount": amount, "reason": reason}

    def effect(conn):
        _ensure_wallet(conn, player_id)
        conn.execute(
            "UPDATE wallets SET balance = balance + ? WHERE player_id=?",
            (amount, player_id),
        )
        new_balance = conn.execute(
            "SELECT balance FROM wallets WHERE player_id=?", (player_id,)
        ).fetchone()["balance"]
        conn.execute(
            "INSERT INTO ledger (player_id, type, amount, ref, idem_key, created_at) "
            "VALUES (?, 'CREDIT', ?, ?, ?, ?)",
            (player_id, amount, reason, idem_key, now_iso()),
        )
        return 201, {"balance": new_balance}

    return _run_idempotent(player_id, "credit", idem_key, body, effect)


def purchase(player_id: str, idem_key: str, item_id: str, price: int):
    body = {"itemId": item_id, "price": price}

    def effect(conn):
        _ensure_wallet(conn, player_id)
        row = conn.execute(
            "SELECT balance FROM wallets WHERE player_id=?", (player_id,)
        ).fetchone()
        balance = row["balance"]

        if balance < price:
            raise DomainError(402, {
                "error": "insufficient_funds",
                "balance": balance,
                "price": price,
            })

        cur = conn.execute(
            "UPDATE wallets SET balance = balance - ? WHERE player_id=? AND balance >= ?",
            (price, player_id, price),
        )
        if cur.rowcount == 0:
            # Defensive: shouldn't happen given BEGIN IMMEDIATE serializes
            # writers, but the WHERE-guard means we'd fail safe, not silent.
            raise DomainError(402, {
                "error": "insufficient_funds",
                "balance": balance,
                "price": price,
            })

        conn.execute(
            "INSERT INTO inventory (player_id, item_id, qty) VALUES (?, ?, 1) "
            "ON CONFLICT(player_id, item_id) DO UPDATE SET qty = qty + 1",
            (player_id, item_id),
        )

        if _CRASH_TEST_DELAY:
            # Deliberately widen the window between "debit written" and
            # "transaction committed" so the crash-recovery test can kill
            # the process here with certainty, proving the transaction
            # really is all-or-nothing rather than getting lucky on timing.
            time.sleep(_CRASH_TEST_DELAY)

        new_balance = conn.execute(
            "SELECT balance FROM wallets WHERE player_id=?", (player_id,)
        ).fetchone()["balance"]

        conn.execute(
            "INSERT INTO ledger (player_id, type, amount, ref, idem_key, created_at) "
            "VALUES (?, 'DEBIT', ?, ?, ?, ?)",
            (player_id, price, item_id, idem_key, now_iso()),
        )
        conn.execute(
            "INSERT INTO ledger (player_id, type, amount, ref, idem_key, created_at) "
            "VALUES (?, 'GRANT', 1, ?, ?, ?)",
            (player_id, item_id, idem_key, now_iso()),
        )
        return 200, {"balance": new_balance, "item": item_id}

    return _run_idempotent(player_id, "purchase", idem_key, body, effect)


def claim(reward_id: str, player_id: str):
    # Natural idempotency key: (playerId, rewardId) IS the request identity
    # here -- the contract itself defines "once per player" per rewardId, so
    # we don't need a client-supplied Idempotency-Key header on this route.
    body = {"playerId": player_id, "rewardId": reward_id}

    def effect(conn):
        _ensure_wallet(conn, player_id)
        conn.execute(
            "INSERT INTO claimed_rewards (player_id, reward_id, claimed_at) VALUES (?, ?, ?)",
            (player_id, reward_id, now_iso()),
        )
        conn.execute(
            "INSERT INTO ledger (player_id, type, amount, ref, idem_key, created_at) "
            "VALUES (?, 'GRANT', NULL, ?, ?, ?)",
            (player_id, reward_id, reward_id, now_iso()),
        )
        return 200, {"rewardId": reward_id, "claimed": True}

    return _run_idempotent(player_id, "claim", reward_id, body, effect)


def get_wallet(player_id: str):
    conn = get_conn()
    try:
        w = conn.execute(
            "SELECT balance FROM wallets WHERE player_id=?", (player_id,)
        ).fetchone()
        balance = w["balance"] if w else 0

        inv_rows = conn.execute(
            "SELECT item_id, qty FROM inventory WHERE player_id=? ORDER BY item_id",
            (player_id,),
        ).fetchall()
        inventory = [{"itemId": r["item_id"], "qty": r["qty"]} for r in inv_rows]

        reward_rows = conn.execute(
            "SELECT reward_id FROM claimed_rewards WHERE player_id=? ORDER BY reward_id",
            (player_id,),
        ).fetchall()
        claimed_rewards = [r["reward_id"] for r in reward_rows]

        return {
            "balance": balance,
            "inventory": inventory,
            "claimedRewards": claimed_rewards,
        }
    finally:
        conn.close()
