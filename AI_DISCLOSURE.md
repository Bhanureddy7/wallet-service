# AI_DISCLOSURE.md

I used Claude (Anthropic) as an AI coding assistant for this entire
submission — architecture, implementation, tests, and documentation.
Roughly the whole thing was produced with AI assistance, not just spot
edits, and I want to be direct about that rather than understate it.

**What it was used for:**
- Designing the idempotency + transaction pattern (`_run_idempotent` in
  `app/service.py`) and the SQLite PRAGMA choices (`app/db.py`).
- Writing the FastAPI route layer, Pydantic models, and business logic.
- Writing the automated tests, including the concurrent-thread race test
  and the subprocess-based `kill -9` crash-recovery test.
- Writing `DESIGN.md`, `RESILIENCE.md`, and this file.
- Iterating based on actual test runs — the tests were executed and passed
  before being included; the crash-recovery test in particular went through
  a revision (adding an injected pre-commit delay) after noticing the
  first version's timing was too lucky to be a rigorous proof of
  mid-transaction atomicity, not just a passing test.

**What I did not do:** I have not personally re-derived every line from
first principles the way I would if I'd typed it myself over two days. I
reviewed the design and the tradeoffs and understand *why* each decision
was made (SQLite over Postgres, client-supplied idempotency keys, BEGIN
IMMEDIATE for write serialization, WAL+fsync for durability, the outbox
pattern for the distributed-inventory scenario) well enough to defend and
extend them, which is the standard I'm holding myself to for honesty here.

I'm flagging this plainly because the brief is explicit that a
disclosure mismatched against the code is worse than a missing feature —
so: heavy AI use throughout, reviewed and understood by me, not a
light-touch assist.
