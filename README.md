# Wallet / Economy Service

A crash-durable, exactly-once wallet service: earn currency, spend it in a
shop, claim a one-time reward. See `DESIGN.md` for the architecture and
reasoning, `RESILIENCE.md` for the distributed-inventory-service follow-up
question, and `AI_DISCLOSURE.md` for tool-use disclosure.

## Stack

Python 3.12, FastAPI, and SQLite (WAL mode) as the datastore — no external
DB container required. See `DESIGN.md` for why.

## Run it

### With Docker (recommended)

```bash
docker compose up --build
```

The service listens on `http://localhost:8000`. Data persists in a named
Docker volume (`wallet_data`), so `docker compose down && docker compose up`
keeps your wallets. `docker compose down -v` wipes the volume if you want a
clean slate.

Plain Docker, no compose:

```bash
docker build -t wallet-service .
docker run -p 8000:8000 -v wallet_data:/data wallet-service
```

### Without Docker

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
WALLET_DB_PATH=./wallet.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Run the tests

```bash
pip install -r requirements.txt
pytest -v
```

- `tests/test_api.py` — contract behaviour, idempotency, insufficient
  funds, input safety, and **real concurrent-thread races** on a single
  wallet (`test_concurrent_purchases_race_only_one_wins`,
  `test_concurrent_duplicate_credits_only_one_effect`).
- `tests/test_crash_recovery.py` — spawns the actual server as a subprocess,
  drives it over real HTTP, injects a deterministic delay so it can
  **SIGKILL (`kill -9`) the process while a purchase transaction is open but
  not yet committed**, restarts it against the same DB file, and asserts
  the transaction left no partial effect and that retrying the same request
  afterward completes exactly once.

## API

All mutating endpoints (`/credit`, `/purchase`) **require** an
`Idempotency-Key` header. `/claim` doesn't need one — a claim's identity is
already `(playerId, rewardId)`, which is naturally idempotent. See
`DESIGN.md` for why credit/purchase can't safely infer their own key from
the request body.

### Credit a wallet (simulated battle payout)

```bash
curl -X POST http://localhost:8000/v1/wallets/alice/credit \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"amount": 100, "reason": "battle_win"}'
# -> 201 {"balance": 100}
```

Retrying the exact same request (same Idempotency-Key) returns the same
`201 {"balance": 100}` again — the balance is not incremented a second
time.

### Purchase an item

```bash
curl -X POST http://localhost:8000/v1/wallets/alice/purchase \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"itemId": "sword", "price": 40}'
# -> 200 {"balance": 60, "item": "sword"}
```

Insufficient funds:

```bash
curl -X POST http://localhost:8000/v1/wallets/alice/purchase \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"itemId": "castle", "price": 999999}'
# -> 402 {"error": "insufficient_funds", "balance": 60, "price": 999999}
```

### Claim a one-time reward

```bash
curl -X POST http://localhost:8000/v1/rewards/welcome_bonus/claim \
  -H "Content-Type: application/json" \
  -d '{"playerId": "alice"}'
# -> 200 {"rewardId": "welcome_bonus", "claimed": true}
```

Claiming again returns the same `200` body — it does not error and does not
grant a second time.

### Read wallet state

```bash
curl http://localhost:8000/v1/wallets/alice
# -> 200 {"balance": 60, "inventory": [{"itemId": "sword", "qty": 1}], "claimedRewards": ["welcome_bonus"]}
```

An unknown `playerId` returns `200` with a zero-state wallet (`balance: 0`,
empty lists) rather than a `404` — accounts aren't a separate concept in
this service; a wallet exists as soon as something happens to it. See
`DESIGN.md`.

## Status codes used

| Code | Meaning |
|---|---|
| 200 | Success (purchase, claim, get) |
| 201 | Success, resource created (credit) |
| 400 | Bad request: invalid player/reward id, or missing `Idempotency-Key` on a route that requires one |
| 402 | Well-defined rejection: insufficient funds |
| 409 | Idempotency key reused with a different request body |
| 422 | Request body failed validation (missing field, wrong type, out-of-range amount, malformed JSON) |

## Limits

- `amount` / `price`: integer, `1 <= n <= 1,000,000,000`
- `itemId` / `rewardId` / `playerId`: non-empty string, max 200 characters
- `reason`: non-empty string, max 200 characters
- Idempotency keys are retained **forever** in this implementation (see
  `DESIGN.md` for the tradeoff and a real retention strategy).
