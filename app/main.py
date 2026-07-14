"""
HTTP layer. Thin — validation lives in models.py, atomicity/idempotency
lives in service.py. This file's only job is wiring the mandated contract
to that logic and translating results into HTTP status codes.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from . import service
from .db import init_db
from .models import CreditRequest, PurchaseRequest, ClaimRequest

MAX_ID_LEN = 200


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Wallet / Economy Service", lifespan=lifespan)


def _valid_id(player_id: str) -> bool:
    return 0 < len(player_id) <= MAX_ID_LEN


@app.exception_handler(service.IdempotencyConflict)
def _idem_conflict_handler(request: Request, exc: service.IdempotencyConflict):
    return JSONResponse(
        status_code=409,
        content={
            "error": "idempotency_key_reused",
            "message": "This Idempotency-Key was already used with a different request body.",
        },
    )


@app.post("/v1/wallets/{playerId}/credit")
def credit(playerId: str, body: CreditRequest,
           idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    if not _valid_id(playerId):
        return JSONResponse(status_code=400, content={"error": "invalid_player_id"})
    if not idempotency_key:
        return JSONResponse(
            status_code=400,
            content={"error": "missing_idempotency_key",
                     "message": "Idempotency-Key header is required for /credit."},
        )
    status_code, resp = service.credit(playerId, idempotency_key, body.amount, body.reason)
    return JSONResponse(status_code=status_code, content=resp)


@app.post("/v1/wallets/{playerId}/purchase")
def purchase(playerId: str, body: PurchaseRequest,
             idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    if not _valid_id(playerId):
        return JSONResponse(status_code=400, content={"error": "invalid_player_id"})
    if not idempotency_key:
        return JSONResponse(
            status_code=400,
            content={"error": "missing_idempotency_key",
                     "message": "Idempotency-Key header is required for /purchase."},
        )
    status_code, resp = service.purchase(playerId, idempotency_key, body.itemId, body.price)
    return JSONResponse(status_code=status_code, content=resp)


@app.post("/v1/rewards/{rewardId}/claim")
def claim(rewardId: str, body: ClaimRequest):
    if not _valid_id(rewardId) or not _valid_id(body.playerId):
        return JSONResponse(status_code=400, content={"error": "invalid_id"})
    status_code, resp = service.claim(rewardId, body.playerId)
    return JSONResponse(status_code=status_code, content=resp)


@app.get("/v1/wallets/{playerId}")
def get_wallet(playerId: str):
    if not _valid_id(playerId):
        return JSONResponse(status_code=400, content={"error": "invalid_player_id"})
    return JSONResponse(status_code=200, content=service.get_wallet(playerId))


@app.get("/healthz")
def healthz():
    return {"ok": True}
