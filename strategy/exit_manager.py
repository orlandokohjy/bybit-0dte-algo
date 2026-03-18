"""
Exit management: take-profit monitoring and time-based hard closes.
"""
from __future__ import annotations

import asyncio

import structlog

import config
from core import notifier
from core.exchange import BybitExchange
from core.portfolio import Portfolio, Straddle, StraddleStatus
from data.market_data import MarketData
from strategy.straddle_builder import unwind_straddle

log = structlog.get_logger(__name__)


class ExitManager:
    """
    Continuously monitors open straddles for take-profit conditions.

    Runs as an asyncio task, checking every TP_CHECK_INTERVAL_SEC.
    """

    def __init__(
        self,
        exchange: BybitExchange,
        market: MarketData,
        portfolio: Portfolio,
    ) -> None:
        self._exchange = exchange
        self._market = market
        self._portfolio = portfolio
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        log.info("exit_manager_started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("exit_manager_stopped")

    async def _monitor_loop(self) -> None:
        while self._running:
            try:
                await self._check_tp()
            except Exception:
                log.error("tp_check_error", exc_info=True)
            await asyncio.sleep(config.TP_CHECK_INTERVAL_SEC)

    async def _check_tp(self) -> None:
        open_straddles = self._portfolio.get_open_straddles()
        if not open_straddles:
            return

        spot = await self._market.get_spot_price()

        for straddle in open_straddles:
            put_mark = await self._market.get_option_mark(straddle.put_leg.instrument)
            if put_mark <= 0:
                continue

            current_value = straddle.current_value(spot, put_mark)
            pnl_pct = straddle.pnl_pct(spot, put_mark)

            if pnl_pct >= config.TAKE_PROFIT_PCT:
                log.info(
                    "tp_triggered",
                    id=straddle.id,
                    session=straddle.session_id,
                    pnl_pct=f"{pnl_pct:.1%}",
                    current_value=f"${current_value:,.2f}",
                    entry_cost=f"${straddle.entry_cost:,.2f}",
                )

                exit_value = await unwind_straddle(
                    self._exchange, self._market, self._portfolio,
                    straddle, reason="take_profit",
                )

                pnl_usd = exit_value - straddle.entry_cost
                await notifier.notify_tp(
                    straddle.session_id,
                    straddle.id,
                    pnl_usd,
                    pnl_pct,
                )

    async def close_session(self, session_id: int) -> float:
        """Hard close all open straddles for a given session."""
        straddles = self._portfolio.get_open_straddles(session_id)
        if not straddles:
            log.info("session_nothing_to_close", session=session_id)
            return 0.0

        log.info("session_hard_close", session=session_id, count=len(straddles))

        total_pnl = 0.0
        for straddle in straddles:
            exit_value = await unwind_straddle(
                self._exchange, self._market, self._portfolio,
                straddle, reason=f"session_{session_id}_close",
            )
            total_pnl += exit_value - straddle.entry_cost

        await notifier.notify_close(session_id, len(straddles), total_pnl)
        log.info("session_closed", session=session_id, total_pnl=f"${total_pnl:,.2f}")
        return total_pnl

    async def close_all(self) -> float:
        """Emergency: close every open straddle."""
        total_pnl = 0.0
        for session_id in (1, 2, 3):
            total_pnl += await self.close_session(session_id)
        return total_pnl
