# RESILIENCE.md

## Scenario

Item grant moves to a separate inventory service, reached over an API that
can time out, fail, or process the request twice, and which **cannot share
a transaction** with the currency store. I lose the one thing that made
`/purchase` easy: one commit covering both the debit and the grant.

## The partial-failure window

Between "I committed the debit locally" and "I have confirmation the
inventory service committed the grant," there's now a real window where the
two stores can disagree. Concretely:

1. Debit committed locally.
2. Call to inventory service — this can fail *before* the remote side
   does anything (network drop, timeout with no processing), fail *after*
   it processed the grant but before the response reaches me (I can't tell
   these two apart from a timeout alone), or succeed cleanly.
3. If I don't handle this, a timeout in step 2 leaves me not knowing
   whether the player was charged with no item, or actually got the item
   and I just didn't hear back.

That ambiguity — "did it happen or not?" — is the whole problem. I can't
make the two stores commit together anymore, so I have to make the
*sequence* of separate commits recoverable instead.

## Approach: transactional outbox + idempotent remote call + saga-style compensation

**1. Local outbox, same transaction as the debit.**
When `/purchase` runs, in the *same* local transaction that debits the
balance, I also insert an `outbox` row: `{purchase_id, player_id, item_id,
status: PENDING}`. This is the same trick that already makes `/purchase`
atomic today — I'm just moving the "second half" of the operation into a
row I can act on later instead of a second live call I have to complete
right now. After this transaction commits, the debit and "intent to grant"
are durably linked; nothing about the *remote* call has happened yet, and
that's fine — the row is proof the intent survived.

**2. A separate publisher drains the outbox.**
A background worker (could be the same process on a timer, or a separate
one) reads `PENDING` outbox rows and calls the inventory service's grant
endpoint, passing `purchase_id` as *that service's* idempotency key — this
only works if the inventory service does the same idempotency-key pattern
this service already uses (dedup table + cached response), which is a
reasonable thing to require of any service that says "can process my
request twice." If the call succeeds, mark the outbox row `GRANTED`. If it
fails or times out, leave it `PENDING` and retry with backoff — because the
call carries `purchase_id` as an idempotency key, a retry after a timeout
is safe even if the first attempt actually landed; the inventory service
will just replay its own cached response.

**3. Compensation if the grant is never going to happen.**
If retries exhaust a reasonable budget (e.g. the inventory service
permanently rejects the item — discontinued, etc.), I can't leave the
player debited with nothing. Mark the outbox row `FAILED` and run a
compensating transaction: credit the player back the `price`, tagged with
the original `purchase_id` (so *that* credit is also idempotent and
traceable — "this credit is a refund for purchase X," not an unexplained
balance bump). This is the saga pattern: no distributed transaction, just a
forward action plus a defined backward action, and a durable record of
which one actually happened.

**4. What the player sees while this is in flight.**
`/purchase` returns immediately after the local debit + outbox insert
commits, with a status that's honest about the state — e.g. `202` with
`{"status": "processing"}` rather than claiming the item is already
granted, or `GET /wallets/{id}` simply doesn't show the item in inventory
until the grant confirms and the outbox row reaches `GRANTED`. I'd rather
have a brief "processing" window that's visible and eventually resolves
than a synchronous-looking `200` that's actually a lie about remote state I
don't have yet.

**End-to-end exactly-once, despite two stores:** the debit is exactly-once
because it's still a single local transaction (unchanged from today). The
grant is exactly-once because the inventory service dedups on
`purchase_id`. The *pairing* of the two is guaranteed by the outbox row
existing durably before any remote call is attempted, plus compensation as
the explicit fallback if the pairing can't be completed. Nothing is ever
silently half-done — every purchase is `PENDING`, `GRANTED`, or
`FAILED+refunded`, and that status is itself durable and queryable.

## Sub-question: a bug double-granted currency to some players last week — detect and correct without downtime

**Detect.**
The `ledger` table this service already writes (see `DESIGN.md`) is
append-only and records every credit/debit/grant with its `idem_key`. Two
checks, both runnable online as read-only queries against a replica or
off-peak:

- **Invariant check:** for every player, `wallet.balance` should equal
  `SUM(ledger deltas for that player)`. A background job recomputes this
  per-player (or incrementally, per-player-since-last-check) and flags any
  mismatch. This catches drift regardless of *how* it happened, not just
  this specific bug.
- **Duplicate-idempotency-key check:** the bug's actual footprint would
  show up as either (a) two `CREDIT` ledger rows with the same `idem_key`
  for the same player — which shouldn't be possible given the idempotency
  design in `DESIGN.md`, so if this shows up it means the bug bypassed that
  path entirely (e.g. a direct write, a code path that didn't go through
  `_run_idempotent`) — or (b) a spike in `CREDIT` row count or total amount
  per player/time-window relative to the expected rate of legitimate battle
  payouts, caught by comparing ledger credit volume against an independent
  signal (e.g. matchmaking/battle-service event counts) rather than
  trusting the wallet service's own count of its own writes.

**Correct, without downtime.**
Once affected players and amounts are identified from the ledger (each
double-grant should be attributable to a specific pair of ledger rows), run
a **compensating credit/debit** per affected player — never edit or delete
existing ledger rows (the ledger is a historical record, not a working
balance; "correcting" it by rewriting history would destroy the audit
trail that let me find the bug in the first place). Each compensation is
itself an idempotent, ledger-recorded operation (`type: CORRECTION`,
referencing the original bad ledger row's id), so the fix goes through the
exact same exactly-once machinery as everything else and is itself
auditable. This can run live, player by player, with no service
interruption — it's just more transactions through the same machinery, not
a schema change or a lock on the whole table.

**What would have caught it sooner.**
The invariant check above (`balance == sum(ledger deltas)`) run
continuously rather than only after a bug report — if that job runs on a
schedule (say every few minutes) and pages on mismatch, a double-grant bug
gets caught within one interval instead of a week. The other structural
invariant worth alerting on: every `CREDIT` ledger row should have a
non-null `idem_key`, and no `idem_key` should ever produce two `CREDIT`
rows for the same `(player_id, endpoint)` — since the idempotency table
already enforces this for anything going through `_run_idempotent`, a
violation is itself a strong signal that some code path is writing credits
outside that guarded path entirely, which is exactly the kind of bug that
produced the double-grant in the first place.
