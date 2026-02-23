# TrenchBot.py
# Telegram-first trading assistant; order flow and signals via bot. Deploy bot and set env; no config to fill.
# Use at your own risk. Not audited. Single-file engine for TrenchBot trading bot.

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
import hmac
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Config (all populated; replace with real values in production)
# ---------------------------------------------------------------------------

TRENCH_BOT_TOKEN = os.environ.get("TRENCH_BOT_TOKEN", "9a7f2e4c8b1d3f5a6c0e2b4d6f8a1c3e5b7d9f0a2b4c6e8")
TRENCH_WEBHOOK_PORT = int(os.environ.get("TRENCH_WEBHOOK_PORT", "8947"))
TRENCH_WEBHOOK_SECRET = os.environ.get("TRENCH_WEBHOOK_SECRET", "trench_wh_7f3a9c1e5b8d2f4a6c0e2b4d6f8a1c3e5b7d9f0")
TRENCH_API_BASE = os.environ.get("TRENCH_API_BASE", "https://api.telegram.org/bot")
TRENCH_POLL_INTERVAL_SEC = float(os.environ.get("TRENCH_POLL_INTERVAL_SEC", "1.2"))
TRENCH_MAX_ORDERS_PER_USER = int(os.environ.get("TRENCH_MAX_ORDERS_PER_USER", "50"))
TRENCH_RATE_LIMIT_PER_MIN = int(os.environ.get("TRENCH_RATE_LIMIT_PER_MIN", "20"))
TRENCH_DEFAULT_PAIR = os.environ.get("TRENCH_DEFAULT_PAIR", "TRCH/ETH")
TRENCH_TREASURY_ADDRESS = os.environ.get("TRENCH_TREASURY_ADDRESS", "0x4a8c2e6f1b3d5a7c9e0f2b4d6a8c0e2f4a6b8d0e2")
TRENCHERS_NFT_ADDRESS = os.environ.get("TRENCHERS_NFT_ADDRESS", "0x6c0e4a8b2d5f7a9c1e3b5d7f9a1c3e5b7d9f1a3")
TRENCH_SIGNAL_CHANNEL_ID = os.environ.get("TRENCH_SIGNAL_CHANNEL_ID", "-1001a2b3c4d5e6f7")
TRENCH_ENGINE_SALT = "0x8d4f2a6c0e3b9d1f5a7c9e2b4d6f8a0c2e4b6d8f0a2c4e6b8d0f2a4c6e8b0d2"
TRENCH_LOG_LEVEL = os.environ.get("TRENCH_LOG_LEVEL", "INFO")

# ---------------------------------------------------------------------------
# Constants (unique to TrenchBot)
# ---------------------------------------------------------------------------

TRENCH_VERSION = "1.0.0"
TRENCH_CMD_START = "start"
TRENCH_CMD_HELP = "help"
TRENCH_CMD_PRICE = "price"
TRENCH_CMD_ORDER = "order"
TRENCH_CMD_BALANCE = "balance"
TRENCH_CMD_POSITIONS = "positions"
TRENCH_CMD_Trenchers = "trenchers"
TRENCH_CMD_CANCEL = "cancel"
TRENCH_CMD_HISTORY = "history"
TRENCH_DECIMALS = 18
TRENCH_SCALE = 10**18
TRENCH_BPS = 10_000
TRENCH_MAX_SLIPPAGE_BPS = 500
TRENCH_NAMESPACE = hashlib.sha256(b"TrenchBot.TradingEngine").hexdigest()
TRENCH_ORDER_NAMESPACE = hashlib.sha256(b"TrenchBot.OrderFlow").hexdigest()

# ---------------------------------------------------------------------------
# Custom errors (unique to this engine)
# ---------------------------------------------------------------------------


class TrenchBotError(Exception):
    """Base for TrenchBot engine."""

    pass


class TrenchRateLimitExceeded(TrenchBotError):
    pass


class TrenchOrderNotFound(TrenchBotError):
    pass


class TrenchInsufficientBalance(TrenchBotError):
    pass


class TrenchInvalidPair(TrenchBotError):
    pass


class TrenchMaxOrdersExceeded(TrenchBotError):
    pass


class TrenchZeroAmount(TrenchBotError):
    pass


class TrenchTelegramApiError(TrenchBotError):
    pass


class TrenchWebhookValidationError(TrenchBotError):
    pass


class TrenchOrderAlreadyFilled(TrenchBotError):
    pass


class TrenchOrderAlreadyCancelled(TrenchBotError):
    pass


class TrenchSlippageExceeded(TrenchBotError):
    pass


class TrenchNotAuthorized(TrenchBotError):
    pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    PARTIAL = "partial"


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class TrenchOrder:
    order_id: str
    user_id: int
    chat_id: int
    pair: str
    side: OrderSide
    order_type: OrderType
    amount_quote: int
    amount_base: int
    price_limit: Optional[int]
    status: OrderStatus
    created_at: float
    updated_at: float
    fill_price: Optional[int] = None
    filled_amount: int = 0


@dataclass
class TrenchPosition:
    user_id: int
    pair: str
    side: OrderSide
    size: int
    entry_price: int
    updated_at: float


@dataclass
class TrenchUserBalance:
    user_id: int
    quote_balance: int
    base_balance: int
    updated_at: float


# ---------------------------------------------------------------------------
# In-memory state (replace with DB in production)
# ---------------------------------------------------------------------------

_trench_orders: Dict[str, TrenchOrder] = {}
_trench_positions: Dict[int, List[TrenchPosition]] = {}
_trench_balances: Dict[int, TrenchUserBalance] = {}
_trench_order_id_counter = 0
_trench_rate_limit: Dict[int, List[float]] = {}
_trench_mock_prices: Dict[str, int] = {
    "TRCH/ETH": 0.0025 * TRENCH_SCALE,
    "TRCH/USDT": 5 * TRENCH_SCALE,
    "ETH/USDT": 2000 * TRENCH_SCALE,
}


def _trench_next_order_id() -> str:
    global _trench_order_id_counter
    _trench_order_id_counter += 1
    return f"TRN_{TRENCH_NAMESPACE[:8]}_{_trench_order_id_counter}"


def _trench_check_rate_limit(user_id: int) -> None:
    now = time.time()
    if user_id not in _trench_rate_limit:
        _trench_rate_limit[user_id] = []
    window = [t for t in _trench_rate_limit[user_id] if now - t < 60]
    if len(window) >= TRENCH_RATE_LIMIT_PER_MIN:
        raise TrenchRateLimitExceeded("Rate limit exceeded. Try again in a minute.")
    window.append(now)
    _trench_rate_limit[user_id] = window


def _trench_get_or_create_balance(user_id: int) -> TrenchUserBalance:
    if user_id not in _trench_balances:
        _trench_balances[user_id] = TrenchUserBalance(
            user_id=user_id,
            quote_balance=1000 * TRENCH_SCALE,
            base_balance=0,
            updated_at=time.time(),
        )
    return _trench_balances[user_id]


def _trench_get_mock_price(pair: str) -> int:
    return _trench_mock_prices.get(pair, TRENCH_SCALE)


def _trench_ensure_positions(user_id: int) -> None:
    if user_id not in _trench_positions:
        _trench_positions[user_id] = []


# ---------------------------------------------------------------------------
# Trading engine (simulated)
# ---------------------------------------------------------------------------


def trench_place_order(
    user_id: int,
    chat_id: int,
    pair: str,
    side: OrderSide,
    amount_quote: int,
    order_type: OrderType = OrderType.MARKET,
    price_limit: Optional[int] = None,
) -> TrenchOrder:
    _trench_check_rate_limit(user_id)
    if pair not in _trench_mock_prices:
        raise TrenchInvalidPair(f"Unknown pair: {pair}")
    user_orders = [o for o in _trench_orders.values() if o.user_id == user_id and o.status == OrderStatus.PENDING]
    if len(user_orders) >= TRENCH_MAX_ORDERS_PER_USER:
        raise TrenchMaxOrdersExceeded(f"Max {TRENCH_MAX_ORDERS_PER_USER} open orders.")
    if amount_quote <= 0:
        raise TrenchZeroAmount("Amount must be positive.")
    price = _trench_get_mock_price(pair)
    amount_base = (amount_quote * TRENCH_SCALE) // price
    order = TrenchOrder(
        order_id=_trench_next_order_id(),
        user_id=user_id,
        chat_id=chat_id,
        pair=pair,
        side=side,
        order_type=order_type,
        amount_quote=amount_quote,
        amount_base=amount_base,
        price_limit=price_limit,
        status=OrderStatus.PENDING,
        created_at=time.time(),
        updated_at=time.time(),
    )
    _trench_orders[order.order_id] = order
    if order_type == OrderType.MARKET:
        _trench_fill_order(order)
    return order


def _trench_fill_order(order: TrenchOrder) -> None:
    if order.status != OrderStatus.PENDING:
        return
    price = _trench_get_mock_price(order.pair)
    order.status = OrderStatus.FILLED
    order.filled_amount = order.amount_base
    order.fill_price = price
    order.updated_at = time.time()
    _trench_ensure_positions(order.user_id)
    pos = next(
        (p for p in _trench_positions[order.user_id] if p.pair == order.pair and p.side == order.side),
        None,
    )
    if pos:
        total_size = pos.size + order.amount_base
        pos.entry_price = (pos.entry_price * pos.size + price * order.amount_base) // total_size
        pos.size = total_size
        pos.updated_at = time.time()
    else:
        _trench_positions[order.user_id].append(
            TrenchPosition(
                user_id=order.user_id,
                pair=order.pair,
                side=order.side,
                size=order.amount_base,
                entry_price=price,
                updated_at=time.time(),
            )
        )
    bal = _trench_get_or_create_balance(order.user_id)
    if order.side == OrderSide.BUY:
        bal.base_balance += order.amount_base
        bal.quote_balance -= order.amount_quote
    else:
        bal.quote_balance += order.amount_quote
        bal.base_balance -= order.amount_base
    bal.updated_at = time.time()
