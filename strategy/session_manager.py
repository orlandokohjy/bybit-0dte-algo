"""
Session orchestration: manages the three daily trading sessions.

Each session:
  1. Refresh the options chain
  2. Select the ITM put
  3. Size the position
  4. Build N straddles
  5. Monitor for TP (handled by ExitManager)
  6. Hard close at session end time
"""
from __future__ import annotations

import asyncio

import structlog

import config
from config import SessionConfig, SessionId
from core import notifier
from core.exchange import BybitExchange
from core.portfolio import Portfolio
from data.market_data import MarketData
from data.option_chain import OptionChain
from risk.risk_manager import RiskManager
from strategy.exit_manager import ExitManager
from strategy.option_selector import select_put
from strategy.position_sizer import compute_num_straddles, compute_straddle_cost
from strategy.straddle_builder import build_straddle

log = structlog.get_logger(__name__)


class SessionManager:
    """Runs the full lifecycle for one trading session."""

    def __init__(
        self,
        exchange: BybitExchange,
        market: MarketData,
        chain: OptionChain,
        portfolio: Portfolio,
        risk: RiskManager,
        exit_mgr: ExitManager,
    ) -> None:
        self._exchange = exchange
        self._market = market
        self._chain = chain
        self._portfolio = portfolio
        self._risk = risk
        self._exit_mgr = exit_mgr

    async def run_entry(self, session_cfg: SessionConfig) -> int:
        """
        Execute the entry phase for a session.
        Returns the number of straddles opened.
        """
        sid = session_cfg.session_id.value
        log.info("session_entry_start", session=sid)

        # Session 3 at 14:00 UTC must close Session 2 first (same timestamp).
        if session_cfg.session_id == SessionId.SESSION_3:
            s2_open = self._portfolio.get_open_straddles(SessionId.SESSION_2.value)
            if s2_open:
                log.info("closing_s2_before_s3_entry", count=len(s2_open))
                await self._exit_mgr.close_session(SessionId.SESSION_2.value)

        # ── Pre-checks ──
        api_check = self._risk.check_api_health(self._exchange.error_count)
        if not api_check.allowed:
            log.warning("session_blocked_api", session=sid, reason=api_check.reason)
            await notifier.notify_error(f"Session {sid}", api_check.reason)
            return 0

        loss_check = self._risk.check_daily_loss(await self._exchange.get_total_equity_usd())
        if not loss_check.allowed:
            log.warning("session_blocked_loss", session=sid, reason=loss_check.reason)
            await notifier.notify_error(f"Session {sid}", loss_check.reason)
            return 0

        # ── Refresh chain ──
        n_puts = await self._chain.refresh()
        if n_puts == 0:
            log.error("no_0dte_puts_available", session=sid)
            await notifier.notify_error(f"Session {sid}", "No 0DTE puts found")
            return 0

        # ── Get spot and select put ──
        spot = await self._market.get_spot_price()
        put = select_put(self._chain, spot)
        if put is None:
            log.error("no_suitable_put", session=sid, spot=spot)
            await notifier.notify_error(f"Session {sid}", f"No ITM put near spot ${spot:,.0f}")
            return 0

        # ── Size ──
        qty = config.QTY_PER_STRADDLE
        straddle_cost = compute_straddle_cost(spot, put.ask, qty)
        current_capital = await self._exchange.get_total_equity_usd()
        n_straddles = compute_num_straddles(
            session_cfg, current_capital, straddle_cost, self._portfolio
        )

        if n_straddles == 0:
            log.warning("zero_straddles", session=sid, capital=current_capital, cost=straddle_cost)
            return 0

        # ── Risk check ──
        verdict = self._risk.check_entry(n_straddles, straddle_cost, put, current_capital)
        if not verdict.allowed:
            log.warning("entry_blocked", session=sid, reason=verdict.reason)
            await notifier.notify_error(f"Session {sid}", verdict.reason)
            return 0

        # ── Build straddles ──
        built = 0
        for i in range(n_straddles):
            straddle = await build_straddle(
                self._exchange, self._market, self._portfolio,
                put, qty, sid,
            )
            if straddle:
                built += 1
                log.info("straddle_opened", session=sid, index=i + 1, id=straddle.id)
            else:
                log.error("straddle_build_failed", session=sid, index=i + 1)
                break  # stop on first failure to avoid cascading issues

            # Small delay between entries to avoid rate limits
            if i < n_straddles - 1:
                await asyncio.sleep(1.0)

        if built > 0:
            total_cost = sum(
                s.entry_cost for s in self._portfolio.get_open_straddles(sid)
            )
            await notifier.notify_entry(sid, built, total_cost, spot, put.strike)

        log.info("session_entry_done", session=sid, built=built, target=n_straddles)
        return built

    async def run_close(self, session_cfg: SessionConfig) -> float:
        """Execute hard close for a session."""
        sid = session_cfg.session_id.value
        log.info("session_close_start", session=sid)
        pnl = await self._exit_mgr.close_session(sid)
        log.info("session_close_done", session=sid, pnl=f"${pnl:,.2f}")
        return pnl
