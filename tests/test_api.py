"""
Automated tests.

Run with:  pytest -v

Covers (mapped to brief requirements):
  - test_credit_purchase_claim_flow       -> basic contract behaviour
  - test_duplicate_credit_is_idempotent   -> requirement 2 (exactly-once)
  - test_duplicate_purchase_is_idempotent -> requirement 2
  - test_idempotency_key_reuse_conflict   -> misuse detection
  - test_concurrent_purchases_race_only_one_wins  -> requirement 5
  - test_concurrent_duplicate_credits_only_one_effect -> requirement 2 under real concurrency
  - test_insufficient_funds_no_partial_effect -> requirement 4
  - test_input_safety                     -> requirement 6
"""
import os
import threading
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path):
    db_path = str(tmp_path / "wallet.db")
    os.environ["WALLET_DB_PATH"] = db_path
    # Reimport app fresh so it picks up the new DB path via db.py's module-level DB_PATH.
    import importlib
    from app import db as db_module
    importlib.reload(db_module)
    from app import service as service_module
    importlib.reload(service_module)
    from app import main as main_module
    importlib.reload(main_module)

    with TestClient(main_module.app) as c:
        yield c


def credit(client, player, amount, reason="battle", key=None):
    key = key or str(uuid.uuid4())
    return client.post(
        f"/v1/wallets/{player}/credit",
        json={"amount": amount, "reason": reason},
        headers={"Idempotency-Key": key},
    )


def purchase(client, player, item, price, key=None):
    key = key or str(uuid.uuid4())
    return client.post(
        f"/v1/wallets/{player}/purchase",
        json={"itemId": item, "price": price},
        headers={"Idempotency-Key": key},
    )


def claim(client, reward, player):
    return client.post(f"/v1/rewards/{reward}/claim", json={"playerId": player})


def get_wallet(client, player):
    return client.get(f"/v1/wallets/{player}")


# ---------------------------------------------------------------- basics --

def test_credit_purchase_claim_flow(client):
    r = credit(client, "alice", 100)
    assert r.status_code == 201
    assert r.json()["balance"] == 100

    r = purchase(client, "alice", "sword", 40)
    assert r.status_code == 200
    assert r.json() == {"balance": 60, "item": "sword"}

    r = claim(client, "welcome_bonus", "alice")
    assert r.status_code == 200
    assert r.json() == {"rewardId": "welcome_bonus", "claimed": True}

    r = get_wallet(client, "alice")
    body = r.json()
    assert body["balance"] == 60
    assert body["inventory"] == [{"itemId": "sword", "qty": 1}]
    assert body["claimedRewards"] == ["welcome_bonus"]


def test_unknown_player_returns_zero_state(client):
    r = get_wallet(client, "nobody")
    assert r.status_code == 200
    assert r.json() == {"balance": 0, "inventory": [], "claimedRewards": []}


# ------------------------------------------------------------ idempotency --

def test_duplicate_credit_is_idempotent(client):
    r1 = credit(client, "bob", 100, key="fixed-key-1")
    r2 = credit(client, "bob", 100, key="fixed-key-1")
    assert r1.json() == r2.json()
    assert get_wallet(client, "bob").json()["balance"] == 100  # not 200


def test_duplicate_purchase_is_idempotent(client):
    credit(client, "carol", 100)
    r1 = purchase(client, "carol", "shield", 30, key="fixed-key-2")
    r2 = purchase(client, "carol", "shield", 30, key="fixed-key-2")
    assert r1.json() == r2.json()
    body = get_wallet(client, "carol").json()
    assert body["balance"] == 70
    assert body["inventory"] == [{"itemId": "shield", "qty": 1}]  # granted once


def test_duplicate_claim_is_idempotent(client):
    r1 = claim(client, "daily1", "dave")
    r2 = claim(client, "daily1", "dave")
    assert r1.json() == r2.json()
    assert get_wallet(client, "dave").json()["claimedRewards"] == ["daily1"]


def test_idempotency_key_reuse_conflict(client):
    credit(client, "erin", 100, key="k")
    r = credit(client, "erin", 999, key="k")  # same key, different body
    assert r.status_code == 409


def test_missing_idempotency_key_rejected(client):
    r = client.post("/v1/wallets/frank/credit", json={"amount": 10, "reason": "x"})
    assert r.status_code == 400


# ------------------------------------------------------------- atomicity --

def test_insufficient_funds_no_partial_effect(client):
    credit(client, "grace", 10)
    r = purchase(client, "grace", "castle", 9999)
    assert r.status_code == 402
    body = get_wallet(client, "grace").json()
    assert body["balance"] == 10          # untouched
    assert body["inventory"] == []        # nothing granted


def test_balance_never_goes_negative(client):
    credit(client, "heidi", 5)
    for _ in range(3):
        purchase(client, "heidi", "potion", 4)  # each call gets its own key -> distinct purchases
    assert get_wallet(client, "heidi").json()["balance"] >= 0


# ----------------------------------------------------------- concurrency --

def test_concurrent_purchases_race_only_one_wins(client):
    """
    Two purchase requests race a wallet that can afford exactly one of them.
    Exactly one must succeed; the other must be rejected cleanly; balance
    must never go negative. This is requirement 5.
    """
    credit(client, "ivan", 100)
    results = []
    lock = threading.Lock()

    def do_purchase():
        r = purchase(client, "ivan", "rare_sword", 100, key=str(uuid.uuid4()))
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=do_purchase) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = results.count(200)
    rejections = results.count(402)
    assert successes == 1, f"expected exactly 1 success, got {successes} (results={results})"
    assert successes + rejections == 10
    final_balance = get_wallet(client, "ivan").json()["balance"]
    assert final_balance == 0
    assert final_balance >= 0


def test_concurrent_duplicate_credits_only_one_effect(client):
    """
    The SAME credit request (same Idempotency-Key) fired concurrently many
    times must still only apply once.
    """
    results = []
    lock = threading.Lock()

    def do_credit():
        r = credit(client, "judy", 50, key="same-key-concurrent")
        with lock:
            results.append(r.json())

    threads = [threading.Thread(target=do_credit) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All responses must be identical (same effect, replayed).
    assert all(r == results[0] for r in results)
    assert get_wallet(client, "judy").json()["balance"] == 50  # not 500


# ------------------------------------------------------------- input safety --

def test_input_safety(client):
    # negative amount
    assert credit(client, "kevin", -5).status_code in (400, 422)
    # zero amount
    assert credit(client, "kevin", 0).status_code in (400, 422)
    # absurdly large amount
    r = client.post(
        "/v1/wallets/kevin/credit",
        json={"amount": 10 ** 30, "reason": "x"},
        headers={"Idempotency-Key": "big"},
    )
    assert r.status_code in (400, 422)
    # missing key
    r = client.post(
        "/v1/wallets/kevin/credit",
        json={"amount": 10},
        headers={"Idempotency-Key": "missing-reason"},
    )
    assert r.status_code == 422
    # garbage JSON
    r = client.post(
        "/v1/wallets/kevin/credit",
        data="{not valid json",
        headers={"Idempotency-Key": "garbage", "Content-Type": "application/json"},
    )
    assert r.status_code == 422
    # service still up after all that
    assert get_wallet(client, "kevin").status_code == 200
