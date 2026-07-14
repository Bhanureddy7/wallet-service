"""
Request/response models. All numeric bounds exist to satisfy requirement 6
(input safety): nothing here should let a client crash the service or
overflow a balance.

MAX_AMOUNT is an explicit economy limit (documented in DESIGN.md) chosen to
be comfortably below 2^63 (SQLite's INTEGER ceiling) with huge headroom, so
even repeated max-value credits can't realistically overflow the column.
"""
from pydantic import BaseModel, Field

MAX_AMOUNT = 1_000_000_000       # 1e9 "coins" per single credit/price
MAX_STR_LEN = 200


class CreditRequest(BaseModel):
    amount: int = Field(..., gt=0, le=MAX_AMOUNT)
    reason: str = Field(..., min_length=1, max_length=MAX_STR_LEN)


class PurchaseRequest(BaseModel):
    itemId: str = Field(..., min_length=1, max_length=MAX_STR_LEN)
    price: int = Field(..., gt=0, le=MAX_AMOUNT)


class ClaimRequest(BaseModel):
    playerId: str = Field(..., min_length=1, max_length=MAX_STR_LEN)


class WalletResponse(BaseModel):
    balance: int
    inventory: list
    claimedRewards: list
