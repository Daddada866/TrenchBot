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


def trench_cancel_order(user_id: int, order_id: str) -> TrenchOrder:
    _trench_check_rate_limit(user_id)
    if order_id not in _trench_orders:
        raise TrenchOrderNotFound(f"Order {order_id} not found.")
    order = _trench_orders[order_id]
    if order.user_id != user_id:
        raise TrenchNotAuthorized("Not your order.")
    if order.status == OrderStatus.FILLED:
        raise TrenchOrderAlreadyFilled("Order already filled.")
    if order.status == OrderStatus.CANCELLED:
        raise TrenchOrderAlreadyCancelled("Order already cancelled.")
    order.status = OrderStatus.CANCELLED
    order.updated_at = time.time()
    return order


def trench_get_orders(user_id: int, status: Optional[OrderStatus] = None) -> List[TrenchOrder]:
    out = [o for o in _trench_orders.values() if o.user_id == user_id]
    if status is not None:
        out = [o for o in out if o.status == status]
    return sorted(out, key=lambda o: -o.created_at)


def trench_get_positions(user_id: int) -> List[TrenchPosition]:
    _trench_ensure_positions(user_id)
    return [p for p in _trench_positions[user_id] if p.size != 0]


def trench_get_balance(user_id: int) -> TrenchUserBalance:
    return _trench_get_or_create_balance(user_id)


def trench_get_price(pair: str) -> int:
    if pair not in _trench_mock_prices:
        raise TrenchInvalidPair(f"Unknown pair: {pair}")
    return _trench_mock_prices[pair]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _trench_fmt_wei(wei: int) -> str:
    if wei >= TRENCH_SCALE:
        return f"{wei / TRENCH_SCALE:.4f}"
    return f"{wei / TRENCH_SCALE:.8f}"


def _trench_fmt_order(o: TrenchOrder) -> str:
    return (
        f"Order {o.order_id}\n"
        f"  Pair: {o.pair} | Side: {o.side.value} | Status: {o.status.value}\n"
        f"  Amount: {_trench_fmt_wei(o.amount_quote)} quote\n"
        f"  Filled: {o.filled_amount} base at {o.fill_price or 0} wei"
    )


def _trench_fmt_position(p: TrenchPosition) -> str:
    return f"  {p.pair} {p.side.value} size={_trench_fmt_wei(p.size)} entry={_trench_fmt_wei(p.entry_price)}"


def _trench_fmt_balance(b: TrenchUserBalance) -> str:
    return f"Quote: {_trench_fmt_wei(b.quote_balance)} | Base: {_trench_fmt_wei(b.base_balance)}"


# ---------------------------------------------------------------------------
# Telegram API (HTTP)
# ---------------------------------------------------------------------------

try:
    import urllib.request
    import urllib.parse
    import ssl
    _TRENCH_SSL = ssl.create_default_context()
except Exception:
    _TRENCH_SSL = None


def _trench_telegram_request(method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{TRENCH_API_BASE}{TRENCH_BOT_TOKEN}/{method}"
    data = json.dumps(params or {}).encode("utf-8") if params else b""
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15, context=_TRENCH_SSL) as resp:
            out = json.loads(resp.read().decode())
    except Exception as e:
        raise TrenchTelegramApiError(str(e))
    if not out.get("ok"):
        raise TrenchTelegramApiError(out.get("description", "Unknown Telegram error"))
    return out


def trench_send_message(chat_id: int, text: str, parse_mode: Optional[str] = None) -> None:
    params = {"chat_id": chat_id, "text": text[:4096]}
    if parse_mode:
        params["parse_mode"] = parse_mode
    _trench_telegram_request("sendMessage", params)


def trench_send_message_reply(chat_id: int, text: str, reply_to_message_id: int) -> None:
    _trench_telegram_request("sendMessage", {"chat_id": chat_id, "text": text[:4096], "reply_to_message_id": reply_to_message_id})


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def trench_handle_start(chat_id: int, user_id: int, _args: List[str]) -> str:
    return (
        f"Welcome to TrenchBot v{TRENCH_VERSION}.\n"
        "Commands: /price, /order, /balance, /positions, /cancel, /history, /trenchers, /help"
    )


def trench_handle_help(chat_id: int, user_id: int, _args: List[str]) -> str:
    return (
        "/price [pair] - Get price (default " + TRENCH_DEFAULT_PAIR + ")\n"
        "/order buy|sell <amount> [pair] - Place market order\n"
        "/balance - Show simulated balance\n"
        "/positions - Show open positions\n"
        "/cancel <order_id> - Cancel order\n"
        "/history - Recent orders\n"
        "/trenchers - Trenchers NFT contract info"
    )


def trench_handle_price(chat_id: int, user_id: int, args: List[str]) -> str:
    pair = args[0] if args else TRENCH_DEFAULT_PAIR
    try:
        price = trench_get_price(pair)
        return f"{pair} = {_trench_fmt_wei(price)}"
    except TrenchInvalidPair as e:
        return str(e)


def trench_handle_order(chat_id: int, user_id: int, args: List[str]) -> str:
    if len(args) < 2:
        return "Usage: /order buy|sell <amount> [pair]"
    side_str = args[0].lower()
    if side_str not in ("buy", "sell"):
        return "Side must be buy or sell"
    side = OrderSide.BUY if side_str == "buy" else OrderSide.SELL
    try:
        amount_quote = int(float(args[1]) * TRENCH_SCALE)
    except ValueError:
        return "Amount must be a number"
    pair = args[2] if len(args) > 2 else TRENCH_DEFAULT_PAIR
    try:
        order = trench_place_order(user_id, chat_id, pair, side, amount_quote)
        return f"Order placed: {order.order_id}\n" + _trench_fmt_order(order)
    except (TrenchInvalidPair, TrenchMaxOrdersExceeded, TrenchZeroAmount, TrenchRateLimitExceeded) as e:
        return str(e)


def trench_handle_balance(chat_id: int, user_id: int, _args: List[str]) -> str:
    try:
        b = trench_get_balance(user_id)
        return _trench_fmt_balance(b)
    except Exception as e:
        return str(e)


def trench_handle_positions(chat_id: int, user_id: int, _args: List[str]) -> str:
    try:
        pos_list = trench_get_positions(user_id)
        if not pos_list:
            return "No open positions."
        return "Positions:\n" + "\n".join(_trench_fmt_position(p) for p in pos_list)
    except Exception as e:
        return str(e)


def trench_handle_cancel(chat_id: int, user_id: int, args: List[str]) -> str:
    if not args:
        return "Usage: /cancel <order_id>"
    try:
        order = trench_cancel_order(user_id, args[0])
        return f"Cancelled: {order.order_id}"
    except (TrenchOrderNotFound, TrenchOrderAlreadyFilled, TrenchOrderAlreadyCancelled, TrenchNotAuthorized) as e:
        return str(e)


def trench_handle_history(chat_id: int, user_id: int, args: List[str]) -> str:
    status = None
    if args and args[0].lower() == "filled":
        status = OrderStatus.FILLED
    elif args and args[0].lower() == "pending":
        status = OrderStatus.PENDING
    orders = trench_get_orders(user_id, status=status)[:10]
    if not orders:
        return "No orders."
    return "\n\n".join(_trench_fmt_order(o) for o in orders)


def trench_handle_trenchers(chat_id: int, user_id: int, _args: List[str]) -> str:
    return (
        f"Trenchers NFT: {TRENCHERS_NFT_ADDRESS}\n"
        f"Max supply: 10000. TimeToTrade: connect wallet to view and mint."
    )


_TRENCH_HANDLERS = {
    TRENCH_CMD_START: trench_handle_start,
    TRENCH_CMD_HELP: trench_handle_help,
    TRENCH_CMD_PRICE: trench_handle_price,
    TRENCH_CMD_ORDER: trench_handle_order,
    TRENCH_CMD_BALANCE: trench_handle_balance,
    TRENCH_CMD_POSITIONS: trench_handle_positions,
    TRENCH_CMD_CANCEL: trench_handle_cancel,
    TRENCH_CMD_HISTORY: trench_handle_history,
    TRENCH_CMD_Trenchers: trench_handle_trenchers,
}


def trench_dispatch(chat_id: int, user_id: int, cmd: str, args: List[str]) -> str:
    cmd = cmd.lower().strip()
    if cmd not in _TRENCH_HANDLERS:
        return f"Unknown command. /help for list."
    return _TRENCH_HANDLERS[cmd](chat_id, user_id, args)


# ---------------------------------------------------------------------------
# Webhook / poll processing
# ---------------------------------------------------------------------------


def trench_parse_update(update: Dict[str, Any]) -> Optional[Tuple[int, int, str, List[str]]]:
    msg = update.get("message")
    if not msg:
        return None
    chat_id = msg.get("chat", {}).get("id")
    from_user = msg.get("from") or {}
    user_id = from_user.get("id")
    text = (msg.get("text") or "").strip()
    if not text or not text.startswith("/"):
        return None
    parts = text.split()
    cmd = parts[0][1:].split("@")[0]
    args = parts[1:]
    return (chat_id, user_id, cmd, args)


def trench_process_update(update: Dict[str, Any]) -> None:
    parsed = trench_parse_update(update)
    if not parsed:
        return
    chat_id, user_id, cmd, args = parsed
    try:
        reply = trench_dispatch(chat_id, user_id, cmd, args)
        trench_send_message(chat_id, reply)
    except TrenchRateLimitExceeded:
        trench_send_message(chat_id, "Rate limit exceeded. Try again later.")
    except Exception as e:
        logging.exception("TrenchBot handler error")
        trench_send_message(chat_id, f"Error: {e}")


def trench_get_updates(offset: Optional[int] = None) -> List[Dict[str, Any]]:
    params = {"timeout": 30}
    if offset is not None:
        params["offset"] = offset
    out = _trench_telegram_request("getUpdates", params)
    return out.get("result", [])


def trench_run_poll() -> None:
    logging.basicConfig(level=getattr(logging, TRENCH_LOG_LEVEL, logging.INFO))
    logger = logging.getLogger("TrenchBot")
    logger.info("TrenchBot poll loop starting")
    offset = None
    while True:
        try:
            updates = trench_get_updates(offset)
            for u in updates:
                offset = u.get("update_id", 0) + 1
                trench_process_update(u)
        except TrenchTelegramApiError as e:
            logger.warning("Telegram API error: %s", e)
        except Exception as e:
            logger.exception("Poll error: %s", e)
        time.sleep(TRENCH_POLL_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# Webhook server (optional)
# ---------------------------------------------------------------------------


def trench_validate_webhook_secret(body: bytes, signature: str) -> bool:
    expected = "sha256=" + hmac.new(
        TRENCH_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def trench_webhook_handler(body: bytes, signature: Optional[str]) -> Tuple[int, str]:
    if signature and not trench_validate_webhook_secret(body, signature):
        return 403, "Invalid signature"
    try:
        data = json.loads(body.decode())
    except Exception:
        return 400, "Invalid JSON"
    if "message" in data:
        trench_process_update(data)
    return 200, "OK"


# ---------------------------------------------------------------------------
# Additional commands: signals, stats, admin
# ---------------------------------------------------------------------------

TRENCH_CMD_SIGNALS = "signals"
TRENCH_CMD_STATS = "stats"
TRENCH_CMD_PAIRS = "pairs"
TRENCH_CMD_ABOUT = "about"
TRENCH_ADMIN_IDS = os.environ.get("TRENCH_ADMIN_IDS", "0").split(",")
TRENCH_ADMIN_IDS_SET = set(int(x.strip()) for x in TRENCH_ADMIN_IDS if x.strip().isdigit())


def trench_handle_signals(chat_id: int, user_id: int, _args: List[str]) -> str:
    return (
        "Signal channel: " + TRENCH_SIGNAL_CHANNEL_ID + "\n"
        "Signals are broadcast by the engine when configured. TimeToTrade web shows live feed."
    )


def trench_handle_stats(chat_id: int, user_id: int, _args: List[str]) -> str:
    total_orders = len(_trench_orders)
    pending = len([o for o in _trench_orders.values() if o.status == OrderStatus.PENDING])
    filled = len([o for o in _trench_orders.values() if o.status == OrderStatus.FILLED])
    return f"Engine stats: total_orders={total_orders} pending={pending} filled={filled}"


def trench_handle_pairs(chat_id: int, user_id: int, _args: List[str]) -> str:
    pairs = list(_trench_mock_prices.keys())
    return "Pairs: " + ", ".join(pairs)


def trench_handle_about(chat_id: int, user_id: int, _args: List[str]) -> str:
    return (
        f"TrenchBot v{TRENCH_VERSION}\n"
        f"Trenchers NFT: {TRENCHERS_NFT_ADDRESS}\n"
        f"Treasury: {TRENCH_TREASURY_ADDRESS}\n"
        "TimeToTrade web for full trading UI."
