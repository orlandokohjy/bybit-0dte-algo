"""
Real-time market data manager.

Bridges WebSocket feeds into async-friendly cached snapshots.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import structlog

import config
from core.exchange import BybitExchange, TickerSnapshot
from data.option_chain import OptionChain

log = structlog.get_logger(__name__)


class MarketData:
    """Centralised market data access for the algo."""

    def __init__(self, exchange: BybitExchange, chain: OptionChain) -> None:
        self._exchange = exchange
        self._chain = chain
        self._subscribed_options: set[str] = set()

    async def start(self) -> None:
        self._exchange.start_spot_ws()
        await asyncio.sleep(2)  # let the first tick arrive
        log.info("market_data_started")

    async def get_spot_price(self) -> float:
        cached = self._exchange.get_cached_spot()
        if cached and cached.last > 0:
            return cached.last
        return await self._exchange.get_spot_price()

    async def get_spot_bid_ask(self) -> tuple[float, float]:
        cached = self._exchange.get_cached_spot()
        if cached and cached.bid > 0:
            return cached.bid, cached.ask
        price = await self._exchange.get_spot_price()
        return price, price

    def get_option_snapshot(self, symbol: str) -> Optional[TickerSnapshot]:
        return self._exchange.get_cached_option(symbol)

    async def get_option_mark(self, symbol: str) -> float:
        """Get current mark price for an option, prefer WS cache, fall back to REST."""
        cached = self._exchange.get_cached_option(symbol)
        if cached and cached.mark > 0:
            return cached.mark

        try:
            book = await self._exchange.get_option_orderbook(symbol, depth=1)
            bid = float(book["b"][0][0]) if book.get("b") else 0
            ask = float(book["a"][0][0]) if book.get("a") else 0
            if bid > 0 and ask > 0:
                return (bid + ask) / 2
            return max(bid, ask)
        except Exception:
            log.warning("option_mark_fallback_failed", symbol=symbol)
            return 0.0

    async def get_option_bid_ask(self, symbol: str) -> tuple[float, float]:
        cached = self._exchange.get_cached_option(symbol)
        if cached and cached.bid > 0:
            return cached.bid, cached.ask

        try:
            book = await self._exchange.get_option_orderbook(symbol, depth=1)
            bid = float(book["b"][0][0]) if book.get("b") else 0
            ask = float(book["a"][0][0]) if book.get("a") else 0
            return bid, ask
        except Exception:
            return 0.0, 0.0

    def subscribe_option(self, symbol: str) -> None:
        if symbol not in self._subscribed_options:
            self._exchange.subscribe_option_ticker(symbol)
            self._subscribed_options.add(symbol)

    async def refresh_chain(self) -> int:
        return await self._chain.refresh()

    def stop(self) -> None:
        self._exchange.close()
        log.info("market_data_stopped")
