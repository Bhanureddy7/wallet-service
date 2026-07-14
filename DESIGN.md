# DESIGN.md

## Architecture

One process, one FastAPI app, one SQLite file. No message queue, no
separate cache, no ORM. For the scope described in the brief (a single
wallet service, not a distributed system yet — that's `RESILIENCE.md`),
adding more moving parts would add failure modes without buying anything.

```
HTTP request
   -> FastAPI route (app/main.py)      input validation, header checks
       -> service function (app/service.py)  idempotency + business logic
           -> SQLite (app/db.py)       the only source of truth
```

Every mutating operation (`credit`, `purchase`, `claim`) goes through one
function, `_run_idempotent`, which wraps the whole thing — idempotency
lookup, business logic, idempotency write — in a single SQLite transaction.
That single function is the entire correctness story for requirements 2 and
3; everything else in the codebase is plumbing around it.

## Datastore choice: SQLite (file-based, WAL mode)

**What I'd actually reach for here, and why:** for a single-node service
where the whole point is "never lose or duplicate a transaction," I want
the strongest, simplest consistency guarantee I can get with the least
number of moving parts. An embedded, transactional, ACID datastore gives me
that without needing to also operate a second process/container, tune a
separate connection pool, or reason about network partition between app and
DB. Postgres would be a fine choice too (and arguably a more common default
for a "real" service, and where I'd land if I expected to scale writes
across multiple app instances) — but the brief asks me to justify the
choice and not "go overboard with a premade tool", and a single-writer
wallet ledger doesn't need a client/server database to get correctness. If
this service needed multiple app instances or a separate inventory service
sharing this store, I'd move to Postgres (see `RESILIENCE.md`, which
assumes exactly that split).

Concretely, three PRAGMAs do the actual work (see `app/db.py`):

- `journal_mode=WAL` — commits are appended to a separate write-ahead log
  file rather than rewriting the main DB file in place. This is what makes
  crash recovery well-defined: on next open, SQLite replays any complete
  transactions in the WAL and discards anything incomplete. It's also what
  lets reads (`GET /wallets/{id}`) proceed without blocking behind writers.
- `synchronous=FULL` — fsync on every commit. Without this, "commit"
  only means "handed to the OS page cache," and a machine power-loss (a
  strictly harder case than `kill -9`, which the brief's crash-durability
  language gestures at) could still lose a committed transaction. This is
  the single PRAGMA that turns "the transaction abstraction is correct" into
  "the transaction abstraction is correct on this disk, right now."
- `busy_timeout=30000` — SQLite allows exactly one writer at a time. Instead
  of a second concurrent writer failing immediately with `database is
  locked`, it blocks (up to 30s) and retries. Combined with `BEGIN
  IMMEDIATE` (below), this is where requirement 5's concurrency correctness
  comes from — I don't have to hand-roll a lock, SQLite's own writer
  serialization is the lock.

## Non-duplicate (idempotency) strategy

**Mechanism.** Every mutating request carries a client-supplied
`Idempotency-Key` header (required on `/credit` and `/purchase`; see below
for why `/claim` doesn't need one). Inside a single transaction:

1. `BEGIN IMMEDIATE` — acquire the write lock *before* touching any data,
   not lazily on first write. This is what prevents two concurrent requests
   from both passing the "have I seen this key?" check before either has
   written its answer.
2. Look up `(player_id, endpoint, idem_key)` in `idempotency_keys`.
   - Found, and the stored request hash matches this request's body ->
     return the **stored** response verbatim. No business logic re-runs.
   - Found, but the body differs -> `409 Conflict`. This is a *misuse*
     case (client reused a key for a different logical request), distinct
     from a legitimate retry, and I'd rather fail loudly than silently
     apply the old key to new data.
   - Not found -> run the effect, write the response into
     `idempotency_keys` in the *same* transaction, commit.

Scoping the key by `(player_id, endpoint, idem_key)` rather than just
`idem_key` means two different players (or the same player on two
different endpoints) can never collide on a client's chosen key string.

**Why a client-supplied key instead of hashing the request body.** I
considered deriving the dedup key from a hash of the request body instead
of requiring a header, since it needs no client cooperation. I rejected it:
`{"itemId": "potion", "price": 5}` is a perfectly reasonable request to make
*twice on purpose* (buy two potions in two separate calls). Hashing the
body can't distinguish "this is a retry of the request I already sent" from
"this is a new request that happens to look identical." Only the client
knows which one it means, so the client has to say so — this is the same
reasoning Stripe and most payment APIs use for idempotency keys.

`/claim` is the exception: the reward contract itself defines the request's
identity as `(playerId, rewardId)` — "grant once per player" *is* the
idempotency key, there's no legitimate case where the same player claims
the same reward twice on purpose. So `/claim` doesn't require the header;
`rewardId` is used as the idempotency key directly.

**Failure responses are cached too.** If the effect raises a well-defined
rejection (e.g. insufficient funds), that rejection is written to
`idempotency_keys` exactly like a success. A retried request replays the
*original* rejection rather than being re-evaluated against a balance that
may have changed in the meantime — that's what "the same response as the
first time" means for a request that failed the first time, too.

**Key retention.** Rows in `idempotency_keys` are retained forever in this
implementation — I made this decision explicitly rather than leaving it
unaddressed. Real tradeoff: unbounded retention means the table grows with
total request volume, forever. A production version would retain keys for
a bounded window (I'd pick something like 24-72h, matching a generous
client retry budget — long enough that no reasonable client-side retry
loop would still be firing after that, short enough that the table doesn't
grow forever) and run a periodic sweep (`DELETE FROM idempotency_keys WHERE
created_at < now() - retention_window`). I did not implement the sweep
because it's operationally straightforward and didn't want to spend
scope-time on a cron job instead of the core correctness mechanism; it's a
one-line addition when this goes to production.

## Atomicity & durability strategy

**What's atomic.** For `/purchase`, the debit, the inventory grant, the
ledger rows, and the idempotency-key write all happen inside **one SQLite
transaction**. There is no code path where the debit is committed without
the grant, or vice versa — they're not two operations coordinated to look
atomic, they're literally one `COMMIT`.

**What happens on `kill -9` mid-purchase.** Two cases:

- Killed *before* `COMMIT` reaches disk: WAL replay on next startup finds
  an incomplete transaction and discards it entirely. The wallet is exactly
  as it was before the request arrived — no debit, no grant, no
  idempotency row. The client's original request (if it retries) is
  indistinguishable from a fresh request and runs the effect once.
- Killed *after* `COMMIT` returns from SQLite but before the HTTP response
  reaches the client (the classic "did my write actually happen?"
  ambiguity): the transaction — including the idempotency row — is durable
  on disk. A retry with the same `Idempotency-Key` finds the stored
  response and replays it. The client can't tell the difference between
  "first successful attempt" and "retry of a request that actually
  succeeded the first time," which is exactly what "exactly-once" should
  feel like from outside.

There is no third state. That's the property `tests/test_crash_recovery.py`
exercises directly: it spawns the real server as a subprocess, sends a
purchase, `kill -9`s it while deliberately paused *inside* the open
transaction (via an injected delay — see that file's docstring for why I
did this instead of hoping timing would land there by luck), restarts
against the same DB file, and asserts the wallet is in exactly the
pre-request state, then retries and asserts it completes exactly once.

**Isolation level.** Effectively `SERIALIZABLE` for writers: `BEGIN
IMMEDIATE` takes SQLite's write lock up front, so at most one write
transaction is ever active — there's no window for a second writer to read
a stale balance and act on it. Readers (`GET /wallets/{id}`) run in WAL
mode against a snapshot and don't block or get blocked by writers, which is
the isolation behavior I want for a read that's explicitly documented as
"used to assert state," not "authoritative for the next write."

## Concurrency correctness on a balance

Two purchases racing a wallet that can only afford one: both open a
transaction, but `BEGIN IMMEDIATE` means only one actually acquires the
write lock at a time. The second blocks (via `busy_timeout`) until the
first commits, then sees the *already-debited* balance and is correctly
rejected with `402`. There is no read-balance-then-write-balance gap for a
second transaction to land in. `UPDATE wallets SET balance = balance -
:price WHERE player_id = :id AND balance >= :price` is also written as a
conditional, single-statement update (not read-then-check-then-write in
application code) as defense in depth, so even if the isolation story were
weaker than it is, a negative balance still can't happen — the row simply
wouldn't update and `rowcount == 0` is treated as a rejection.
`tests/test_api.py::test_concurrent_purchases_race_only_one_wins` fires ten
real concurrent threads at one wallet that can afford exactly one $100
purchase and asserts exactly one 200 and nine 402s.

## Authoritative economy

The client never supplies a balance or asserts state — every response is
computed server-side from the row SQLite just wrote, in the same
transaction, so the number a client sees is guaranteed to match what's on
disk when it was read. Price is client-supplied on `/purchase` (the
contract doesn't specify a shop catalog for this service, so there's no
server-side price list to check it against yet) but is *charged
atomically* against the balance the server owns — a client can't inflate
their own balance or make a debit not happen; they can only choose what
price to attempt against their real, server-tracked funds.

## API contract details

- **Status codes / bodies:** see `README.md`'s table — kept there since
  it's also the "how to call it" reference.
- **Currency units:** integers, no fractional currency (avoids
  floating-point drift entirely — this is why balances, amounts, and
  prices are all `INTEGER` columns, never `REAL`).
- **Idempotency-Key:** required header on `/credit` and `/purchase`, any
  non-empty string, scoped per-player-per-endpoint (see above).
- **Unknown player on GET:** returns `200` with a zero-state wallet rather
  than `404`. This service has no separate "create account" step — a
  wallet exists implicitly the moment anything happens to it (matches the
  brief: "accounts... are out of scope"). A `GET` for a player who's never
  interacted with anything is a completely valid question with a
  well-defined answer (they have nothing), so I didn't see a reason to make
  it an error.
- **Limits:** amount/price bounded to `[1, 1_000_000_000]`; id/reason
  strings bounded to 200 chars. Chosen to be generous for a game economy
  while keeping every value comfortably inside SQLite's 64-bit `INTEGER`
  range even under repeated max-value operations, and to reject obviously
  pathological input (negative, zero, oversized, wrong type) at the
  Pydantic boundary before it ever reaches a query.

## Things I decided rather than left open

- SQLite over Postgres for a single-node service (justified above).
- Client-supplied `Idempotency-Key` over body-hash-derived key (justified
  above) — except for `/claim`, where the contract itself supplies the key.
- Forever-retention for idempotency keys in this submission, with the
  bounded-retention version specified but not implemented (justified
  above).
- Unknown-player `GET` returns zero-state `200`, not `404`.
- `SERIALIZABLE`-equivalent isolation via `BEGIN IMMEDIATE` rather than a
  weaker isolation level with manual row locking — for a wallet, I'd rather
  pay the (small, single-node) serialization cost than reason about
  anomalies under a weaker level.
