"""
Crash-durability test (requirement 3).

This does NOT use TestClient/in-process calls. It starts the actual
`uvicorn` server as a child OS process, drives it over real HTTP, sends
SIGKILL (kill -9, not SIGTERM) to that process mid-purchase, restarts the
same server against the same DB file, and asserts:

  1. Every operation that had already been committed before the kill is
     still there after restart.
  2. No purchase ever produced a debit without its matching grant, or vice
     versa (all-or-nothing).
  3. Retrying the in-flight request after restart produces exactly the same
     effect as if it had completed normally the first time (idempotency
     survives the crash, because the idempotency record lives in the same
     durable store as the balance).

Run with:  pytest -v -s tests/test_crash_recovery.py
(needs the app's dependencies installed; spawns a real subprocess on
127.0.0.1, so it won't work in network-sandboxed CI without loopback.)
"""
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid

import pytest
import urllib.request
import urllib.error

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http(method, url, body=None, headers=None, timeout=5):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                  headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _wait_up(base_url, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _ = _http("GET", f"{base_url}/healthz")
            if status == 200:
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("server did not come up in time")


def _spawn_server(db_path, port, crash_delay_sec=0):
    env = dict(os.environ)
    env["WALLET_DB_PATH"] = db_path
    if crash_delay_sec:
        env["WALLET_CRASH_TEST_DELAY_SEC"] = str(crash_delay_sec)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=APP_DIR, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc


def test_kill_9_mid_purchase_no_partial_effect(tmp_path):
    db_path = str(tmp_path / "crash.db")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    proc = _spawn_server(db_path, port, crash_delay_sec=1.0)
    try:
        _wait_up(base)

        # 1. Commit a credit before the crash -- must survive.
        status, body = _http(
            "POST", f"{base}/v1/wallets/zed/credit",
            {"amount": 500, "reason": "battle"},
            {"Idempotency-Key": "credit-1"},
        )
        assert status == 201 and body["balance"] == 500

        # 2. Fire a purchase. Thanks to WALLET_CRASH_TEST_DELAY_SEC, the
        #    server will have written the debit and the item grant to the
        #    (uncommitted) transaction and then sleep for 1s BEFORE
        #    committing. We kill -9 during that sleep, so this is a
        #    deterministic mid-transaction kill, not a timing gamble.
        import threading
        purchase_key = "purchase-crash-1"
        result_holder = {}

        def fire():
            try:
                result_holder["result"] = _http(
                    "POST", f"{base}/v1/wallets/zed/purchase",
                    {"itemId": "sword", "price": 300},
                    {"Idempotency-Key": purchase_key},
                    timeout=5,
                )
            except Exception as e:
                result_holder["error"] = str(e)

        t = threading.Thread(target=fire)
        t.start()
        time.sleep(0.3)  # well inside the 1s injected delay, before commit
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=5)
        t.join(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()

    # 3. Restart against the SAME db file.
    port2 = _free_port()
    base2 = f"http://127.0.0.1:{port2}"
    proc2 = _spawn_server(db_path, port2)
    try:
        _wait_up(base2)

        status, body = _http("GET", f"{base2}/v1/wallets/zed")
        assert status == 200
        balance = body["balance"]
        inventory = body["inventory"]
        has_sword = any(i["itemId"] == "sword" for i in inventory)

        # Because we killed during the injected pre-commit delay, the
        # transaction was never committed: WAL recovery must discard it
        # entirely on the next open. We expect the pre-crash state exactly.
        assert balance == 500 and not has_sword, (
            f"crash happened before commit; expected untouched wallet, "
            f"got balance={balance}, has_sword={has_sword}"
        )

        # 4. Retry the SAME request (same idempotency key) after restart.
        #    Must produce exactly one effect total, matching whichever state
        #    we landed in above.
        status, body2 = _http(
            "POST", f"{base2}/v1/wallets/zed/purchase",
            {"itemId": "sword", "price": 300},
            {"Idempotency-Key": purchase_key},
        )

        status, final = _http("GET", f"{base2}/v1/wallets/zed")
        final_has_sword = any(i["itemId"] == "sword" for i in final["inventory"])
        final_sword_qty = sum(i["qty"] for i in final["inventory"] if i["itemId"] == "sword")

        assert final_has_sword, "retry after crash should have completed the purchase"
        assert final_sword_qty == 1, "sword must be granted exactly once, never duplicated"
        assert final["balance"] == 200, "balance must reflect exactly one debit, never double-debited"
    finally:
        proc2.kill()
        proc2.wait(timeout=5)


if __name__ == "__main__":
    # Allow running directly for a quick manual check without pytest markers.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        class FakeTmpPath:
            def __truediv__(self, other):
                return os.path.join(d, other)
        test_kill_9_mid_purchase_no_partial_effect(FakeTmpPath())
        print("OK: crash recovery test passed")
