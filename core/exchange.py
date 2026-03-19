"""
Bybit V5 Unified Trading API wrapper.

Provides both REST (via pybit) and WebSocket interfaces.
All REST calls are sync (pybit) wrapped in asyncio.to_thread for use in
the async main loop.
"""
from __future__ import annotations

import asyncio
import threading
import time as _time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

import structlog
from pybit.unified_trading import HTTP, WebSocket

import config
from utils.time_utils import now_utc, today_expiry_date_str

log = structlog.get_logger(__name__)

OPTION_TICK_SIZE = 5.0


def _round_price_up(price: float) -> float:
    """Round option price UP to nearest tick (for buys)."""
    import math
    return math.ceil(price / OPTION_TICK_SIZE) * OPTION_TICK_SIZE


def _round_price_down(price: float) -> float:
    """Round option price DOWN to nearest tick (for sells)."""
    import math
    return max(OPTION_TICK_SIZE, math.floor(price / OPTION_TICK_SIZE) * OPTION_TICK_SIZE)


@dataclass
class TickerSnapshot:
    symbol: str
    bid: float
    ask: float
    last: float
    mark: float
    iv: float = 0.0
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    ts: datetime = field(default_factory=now_utc)


class BybitExchange:
    """Unified interface over pybit REST + WebSocket."""

    def __init__(self) -> None:
        self._http = HTTP(
            testnet=config.TESTNET,
            demo=config.DEMO,
            api_key=config.BYBIT_API_KEY,
            api_secret=config.BYBIT_API_SECRET,
        )

        self._perp_ticker: Optional[TickerSnapshot] = None
        self._option_tickers: dict[str, TickerSnapshot] = {}
        self._ws_perp: Optional[WebSocket] = None
        self._ws_option: Optional[WebSocket] = None
        self._ws_private: Optional[WebSocket] = None
        self._ws_lock = threading.Lock()

        self._on_order_update: Optional[Callable] = None
        self._on_wallet_update: Optional[Callable] = None

        self._consecutive_errors = 0

    # ──────────── REST helpers (sync, called via to_thread) ──────────

    def _call_sync(self, func: Callable, **kwargs: Any) -> dict:
        """Centralised REST call with error counting."""
        try:
            result = func(**kwargs)
            self._consecutive_errors = 0
            return result
        except Exception as exc:
            self._consecutive_errors += 1
            log.error("api_error", func=func.__name__, error=str(exc),
                      consecutive=self._consecutive_errors)
            raise

    async def _call(self, func: Callable, **kwargs: Any) -> dict:
        return await asyncio.to_thread(self._call_sync, func, **kwargs)

    @property
    def error_count(self) -> int:
        return self._consecutive_errors

    # ──────────────────── Perp Setup ──────────────────────────────

    async def set_leverage(self) -> None:
        """Set perp leverage (idempotent — ignores 'not modified' error)."""
        try:
            await self._call(
                self._http.set_leverage,
                category=config.PERP_CATEGORY,
                symbol=config.PERP_SYMBOL,
                buyLeverage=str(config.PERP_LEVERAGE),
                sellLeverage=str(config.PERP_LEVERAGE),
            )
            log.info("leverage_set", leverage=config.PERP_LEVERAGE)
        except Exception as exc:
            if "110043" in str(exc):
                log.info("leverage_already_set", leverage=config.PERP_LEVERAGE)
            else:
                raise

    # ──────────────────── Market Data (REST) ─────────────────────

    async def get_perp_price(self) -> float:
        if self._perp_ticker:
            return self._perp_ticker.last
        data = await self._call(
            self._http.get_tickers,
            category=config.PERP_CATEGORY,
            symbol=config.PERP_SYMBOL,
        )
        return float(data["result"]["list"][0]["lastPrice"])

    async def get_option_instruments(self) -> list[dict]:
        """Fetch all BTC option instruments. Handles pagination."""
        instruments: list[dict] = []
        cursor = ""
        while True:
            params: dict[str, Any] = {
                "category": "option",
                "baseCoin": config.BASE_COIN,
                "limit": 500,
            }
            if cursor:
                params["cursor"] = cursor
            data = await self._call(self._http.get_instruments_info, **params)
            items = data["result"]["list"]
            instruments.extend(items)
            cursor = data["result"].get("nextPageCursor", "")
            if not cursor:
                break
        return instruments

    async def get_0dte_instruments(self) -> list[dict]:
        """Filter instruments to today's 0DTE expiry only."""
        all_inst = await self.get_option_instruments()
        exp_str = today_expiry_date_str()
        return [i for i in all_inst if exp_str in i["symbol"]]

    async def get_option_tickers_rest(self, exp_date: str | None = None) -> list[dict]:
        params: dict[str, Any] = {
            "category": "option",
            "baseCoin": config.BASE_COIN,
        }
        if exp_date:
            params["expDate"] = exp_date
        data = await self._call(self._http.get_tickers, **params)
        return data["result"]["list"]

    async def get_option_orderbook(self, symbol: str, depth: int = 25) -> dict:
        data = await self._call(
            self._http.get_orderbook,
            category="option",
            symbol=symbol,
            limit=depth,
        )
        return data["result"]

    # ──────────────────── Account (REST) ─────────────────────────

    async def get_wallet_balance(self) -> dict:
        data = await self._call(
            self._http.get_wallet_balance,
            accountType=config.ACCOUNT_TYPE,
        )
        return data["result"]["list"][0]

    async def get_usdt_balance(self) -> float:
        wallet = await self.get_wallet_balance()
        for coin in wallet.get("coin", []):
            if coin["coin"] == config.SETTLE_COIN:
                return float(coin["walletBalance"])
        return 0.0

    async def get_total_equity_usd(self) -> float:
        if config.DRY_RUN:
            return config.INITIAL_CAPITAL_USD
        wallet = await self.get_wallet_balance()
        return float(wallet.get("totalEquity", 0))

    async def get_positions(self, category: str = "option") -> list[dict]:
        data = await self._call(
            self._http.get_positions,
            category=category,
            baseCoin=config.BASE_COIN,
            settleCoin=config.SETTLE_COIN,
        )
        return data["result"]["list"]

    async def get_open_orders(self, category: str = "option") -> list[dict]:
        data = await self._call(
            self._http.get_open_orders,
            category=category,
        )
        return data["result"]["list"]

    # ──────────────────── Order Execution ────────────────────────

    @staticmethod
    def _fake_order(side: str, symbol: str, qty: float, price: float) -> dict:
        """Return a simulated fill for DRY_RUN mode."""
        import uuid
        oid = f"dry-{uuid.uuid4().hex[:12]}"
        log.info("DRY_RUN_ORDER", side=side, symbol=symbol, qty=qty, price=price, orderId=oid)
        return {"orderId": oid, "orderStatus": "Filled", "avgPrice": str(price)}

    async def open_long_perp(self, qty: float) -> dict:
        """Market buy BTCUSDT perp (open long). Returns order result."""
        log.info("open_long_perp", qty=qty)
        if config.DRY_RUN:
            price = await self.get_perp_price()
            return self._fake_order("Buy", config.PERP_SYMBOL, qty, price)
        data = await self._call(
            self._http.place_order,
            category=config.PERP_CATEGORY,
            symbol=config.PERP_SYMBOL,
            side="Buy",
            orderType=config.PERP_ORDER_TYPE,
            qty=str(qty),
        )
        return data["result"]

    async def close_long_perp(self, qty: float) -> dict:
        """Market sell BTCUSDT perp (close long). Returns order result."""
        log.info("close_long_perp", qty=qty)
        if config.DRY_RUN:
            price = await self.get_perp_price()
            return self._fake_order("Sell", config.PERP_SYMBOL, qty, price)
        data = await self._call(
            self._http.place_order,
            category=config.PERP_CATEGORY,
            symbol=config.PERP_SYMBOL,
            side="Sell",
            orderType="Market",
            qty=str(qty),
            reduceOnly=True,
        )
        return data["result"]

    async def buy_put(self, symbol: str, qty: float, price: float) -> dict:
        """Limit buy a put option."""
        tick_price = _round_price_up(price)
        log.info("buy_put", symbol=symbol, qty=qty, price=tick_price)
        if config.DRY_RUN:
            return self._fake_order("Buy", symbol, qty, tick_price)
        data = await self._call(
            self._http.place_order,
            category="option",
            symbol=symbol,
            side="Buy",
            orderType="Limit",
            qty=str(qty),
            price=str(tick_price),
            timeInForce="IOC",
            orderLinkId=f"bp-{uuid.uuid4().hex[:16]}",
        )
        return data["result"]

    async def sell_put(self, symbol: str, qty: float, price: float) -> dict:
        """Limit sell (close) a put option."""
        tick_price = _round_price_down(price)
        log.info("sell_put", symbol=symbol, qty=qty, price=tick_price)
        if config.DRY_RUN:
            return self._fake_order("Sell", symbol, qty, tick_price)
        data = await self._call(
            self._http.place_order,
            category="option",
            symbol=symbol,
            side="Sell",
            orderType="Limit",
            qty=str(qty),
            price=str(tick_price),
            timeInForce="IOC",
            reduceOnly=True,
            orderLinkId=f"sp-{uuid.uuid4().hex[:16]}",
        )
        return data["result"]

    async def cancel_order(self, category: str, symbol: str, order_id: str) -> dict:
        if config.DRY_RUN:
            log.info("DRY_RUN_CANCEL", symbol=symbol, orderId=order_id)
            return {}
        data = await self._call(
            self._http.cancel_order,
            category=category,
            symbol=symbol,
            orderId=order_id,
        )
        return data["result"]

    async def chase_buy_put(self, symbol: str, qty: float, initial_ask: float) -> dict | None:
        """
        Aggressive fill logic for buying a put.

        Posts IOC limit at ask + aggression. If not filled, re-prices up to
        max_slippage_pct above initial ask.
        """
        if config.DRY_RUN:
            return self._fake_order("Buy", symbol, qty, _round_price_up(initial_ask))

        max_price = _round_price_up(initial_ask * (1 + config.OPTION_MAX_SLIPPAGE_PCT))
        price = _round_price_up(initial_ask * (1 + config.OPTION_LIMIT_AGGRESSION))

        for attempt in range(config.OPTION_CHASE_MAX_ATTEMPTS):
            price = min(price, max_price)
            result = await self.buy_put(symbol, qty, price)
            order_id = result.get("orderId", "")

            await asyncio.sleep(0.5)
            order_status = await self._get_order_status("option", symbol, order_id)

            if order_status and order_status.get("orderStatus") == "Filled":
                avg_price = float(order_status.get("avgPrice", price))
                log.info("put_filled", symbol=symbol, price=avg_price, attempt=attempt + 1)
                return order_status

            if order_status and order_status.get("orderStatus") in ("New", "PartiallyFilled"):
                await self.cancel_order("option", symbol, order_id)

            price = _round_price_up(price * (1 + config.OPTION_LIMIT_AGGRESSION))
            log.debug("chase_reprice", symbol=symbol, new_price=price, attempt=attempt + 1)
            await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)

        log.warning("chase_exhausted", symbol=symbol, final_price=price)
        return None

    async def chase_sell_put(self, symbol: str, qty: float, initial_bid: float) -> dict | None:
        """Aggressive fill logic for selling a put. Walks the bid down."""
        if config.DRY_RUN:
            return self._fake_order("Sell", symbol, qty, _round_price_down(initial_bid))

        min_price = _round_price_down(initial_bid * (1 - config.OPTION_MAX_SLIPPAGE_PCT))
        price = _round_price_down(initial_bid * (1 - config.OPTION_LIMIT_AGGRESSION))

        for attempt in range(config.OPTION_CHASE_MAX_ATTEMPTS):
            price = max(price, min_price)
            result = await self.sell_put(symbol, qty, price)
            order_id = result.get("orderId", "")

            await asyncio.sleep(0.5)
            order_status = await self._get_order_status("option", symbol, order_id)

            if order_status and order_status.get("orderStatus") == "Filled":
                avg_price = float(order_status.get("avgPrice", price))
                log.info("put_sold", symbol=symbol, price=avg_price, attempt=attempt + 1)
                return order_status

            if order_status and order_status.get("orderStatus") in ("New", "PartiallyFilled"):
                await self.cancel_order("option", symbol, order_id)

            price = _round_price_down(price * (1 - config.OPTION_LIMIT_AGGRESSION))
            await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)

        log.warning("chase_sell_exhausted", symbol=symbol, final_price=price)
        return None

    async def _get_order_status(self, category: str, symbol: str, order_id: str) -> dict | None:
        try:
            data = await self._call(
                self._http.get_open_orders,
                category=category,
                symbol=symbol,
                orderId=order_id,
            )
            orders = data["result"]["list"]
            if orders:
                return orders[0]
            data = await self._call(
                self._http.get_order_history,
                category=category,
                symbol=symbol,
                orderId=order_id,
            )
            orders = data["result"]["list"]
            return orders[0] if orders else None
        except Exception:
            log.warning("get_order_status_failed", order_id=order_id, exc_info=True)
            return None

    # ─────────────────── WebSocket Streams ───────────────────────

    def start_perp_ws(self) -> None:
        """Start perp ticker WebSocket in background."""
        self._ws_perp = WebSocket(
            testnet=config.TESTNET,
            channel_type="linear",
        )
        self._ws_perp.ticker_stream(
            symbol=config.PERP_SYMBOL,
            callback=self._handle_perp_ticker,
        )
        log.info("ws_perp_started")

    def _handle_perp_ticker(self, msg: dict) -> None:
        try:
            d = msg.get("data", msg)
            self._perp_ticker = TickerSnapshot(
                symbol=config.PERP_SYMBOL,
                bid=float(d.get("bid1Price", 0)),
                ask=float(d.get("ask1Price", 0)),
                last=float(d.get("lastPrice", 0)),
                mark=float(d.get("markPrice", d.get("lastPrice", 0))),
            )
        except Exception:
            log.debug("perp_ticker_parse_error", exc_info=True)

    def subscribe_option_ticker(self, symbol: str) -> None:
        """Subscribe to a single option symbol's ticker."""
        if self._ws_option is None:
            self._ws_option = WebSocket(
                testnet=config.TESTNET,
                channel_type="option",
            )
        self._ws_option.ticker_stream(
            symbol=symbol,
            callback=self._handle_option_ticker,
        )
        log.debug("ws_option_subscribed", symbol=symbol)

    def _handle_option_ticker(self, msg: dict) -> None:
        try:
            d = msg.get("data", msg)
            symbol = d.get("symbol", "")
            self._option_tickers[symbol] = TickerSnapshot(
                symbol=symbol,
                bid=float(d.get("bid1Price", 0)),
                ask=float(d.get("ask1Price", 0)),
                last=float(d.get("lastPrice", 0)),
                mark=float(d.get("markPrice", 0)),
                iv=float(d.get("markIv", 0)),
                delta=float(d.get("delta", 0)),
                gamma=float(d.get("gamma", 0)),
                theta=float(d.get("theta", 0)),
                vega=float(d.get("vega", 0)),
            )
        except Exception:
            log.debug("option_ticker_parse_error", exc_info=True)

    def start_private_ws(
        self,
        on_order: Callable | None = None,
        on_wallet: Callable | None = None,
    ) -> None:
        self._ws_private = WebSocket(
            testnet=config.TESTNET,
            channel_type="private",
            api_key=config.BYBIT_API_KEY,
            api_secret=config.BYBIT_API_SECRET,
        )
        if on_order:
            self._ws_private.order_stream(callback=on_order)
        if on_wallet:
            self._ws_private.wallet_stream(callback=on_wallet)
        log.info("ws_private_started")

    def get_cached_perp(self) -> TickerSnapshot | None:
        return self._perp_ticker

    def get_cached_option(self, symbol: str) -> TickerSnapshot | None:
        return self._option_tickers.get(symbol)

    # ────────────────────── Shutdown ─────────────────────────────

    def close(self) -> None:
        for ws in (self._ws_perp, self._ws_option, self._ws_private):
            if ws is not None:
                try:
                    ws.exit()
                except Exception:
                    pass
        log.info("exchange_connections_closed")
