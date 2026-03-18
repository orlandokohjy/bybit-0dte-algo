"""
Bybit 0DTE BTC Options Algo — Main Entry Point

Synthetic straddle strategy: long spot + long ITM put for gamma exposure.
Three sessions daily: 08:30, 12:00, 14:00 UTC.
"""
from __future__ import annotations

import asyncio
import signal
import sys

import structlog

import config
from config import SESSIONS
from core import notifier
from core.exchange import BybitExchange
from core.portfolio import Portfolio
from core.scheduler import Scheduler
from data.market_data import MarketData
from data.option_chain import OptionChain
from risk.risk_manager import RiskManager
from strategy.exit_manager import ExitManager
from strategy.session_manager import SessionManager
from utils.logging_config import setup_logging
from utils.time_utils import format_utc_sgt, now_utc

log = structlog.get_logger(__name__)


class Algo:
    """Top-level orchestrator."""

    def __init__(self) -> None:
        self.exchange = BybitExchange()
        self.chain = OptionChain(self.exchange)
        self.market = MarketData(self.exchange, self.chain)
        self.portfolio = Portfolio()
        self.risk = RiskManager(self.portfolio, self.market)
        self.exit_mgr = ExitManager(self.exchange, self.market, self.portfolio)
        self.session_mgr = SessionManager(
            self.exchange, self.market, self.chain,
            self.portfolio, self.risk, self.exit_mgr,
        )
        self.scheduler = Scheduler()
        self._shutdown = asyncio.Event()

    async def start(self) -> None:
        setup_logging()
        log.info("algo_starting", testnet=config.TESTNET, demo=config.DEMO)

        # ── Validate credentials ──
        if not config.BYBIT_API_KEY or not config.BYBIT_API_SECRET:
            log.error("missing_api_credentials")
            sys.exit(1)

        # ── Connect & fetch initial state ──
        await self.market.start()
        spot = await self.market.get_spot_price()
        capital = await self.exchange.get_total_equity_usd()
        self.portfolio.set_sod_capital(capital)

        log.info(
            "algo_initialized",
            spot=f"${spot:,.2f}",
            capital=f"${capital:,.2f}",
            ivc=f"${self.portfolio.sod_ivc:,.2f}",
            open_straddles=self.portfolio.total_open,
        )

        await notifier.send(
            f"<b>ALGO STARTED</b>\n"
            f"Mode: {'TESTNET' if config.TESTNET else 'MAINNET'}\n"
            f"Spot: ${spot:,.2f}\n"
            f"Capital: ${capital:,.2f}\n"
            f"IVC: ${self.portfolio.sod_ivc:,.2f}\n"
            f"Time: {format_utc_sgt(now_utc())}\n"
        )

        # ── Start TP monitor ──
        await self.exit_mgr.start()

        # ── Start private WS for order/wallet updates ──
        self.exchange.start_private_ws()

        # ── Schedule sessions ──
        for sess_cfg in SESSIONS:
            sid = sess_cfg.session_id.value
            self.scheduler.register_session(
                sess_cfg,
                on_entry=self._make_entry_callback(sess_cfg),
                on_close=self._make_close_callback(sess_cfg),
            )
        self.scheduler.register_daily_reset(self._daily_reset)
        self.scheduler.start()

        fire_times = self.scheduler.get_next_fire_times()
        for job_id, ft in fire_times.items():
            if ft:
                log.info("next_fire", job=job_id, time=format_utc_sgt(ft))

        # ── Run until shutdown ──
        log.info("algo_running")
        await self._shutdown.wait()

    def _make_entry_callback(self, sess_cfg):
        async def _entry():
            try:
                await self.session_mgr.run_entry(sess_cfg)
            except Exception:
                log.error("session_entry_error", session=sess_cfg.session_id.value, exc_info=True)
                await notifier.notify_error(
                    f"Session {sess_cfg.session_id.value} Entry",
                    "Unhandled exception — check logs",
                )
        return _entry

    def _make_close_callback(self, sess_cfg):
        async def _close():
            try:
                await self.session_mgr.run_close(sess_cfg)
            except Exception:
                log.error("session_close_error", session=sess_cfg.session_id.value, exc_info=True)
                await notifier.notify_error(
                    f"Session {sess_cfg.session_id.value} Close",
                    "Unhandled exception — check logs",
                )
        return _close

    async def _daily_reset(self) -> None:
        """08:00 UTC settlement: close any stragglers, reset daily state, refresh capital."""
        log.info("daily_reset_start")
        try:
            if self.portfolio.total_open > 0:
                log.warning("settlement_force_close", count=self.portfolio.total_open)
                await self.exit_mgr.close_all()

            yesterday_pnl = self.portfolio.daily_pnl
            yesterday_trades = self.portfolio.trade_count
            self.portfolio.reset_daily()

            capital = await self.exchange.get_total_equity_usd()
            self.portfolio.set_sod_capital(capital)

            await notifier.notify_daily_summary(yesterday_pnl, capital, yesterday_trades)
            log.info("daily_reset_done", pnl=yesterday_pnl, capital=capital)
        except Exception:
            log.error("daily_reset_error", exc_info=True)
            await notifier.notify_error("Daily Reset", "Failed — check logs")

    async def shutdown(self) -> None:
        log.info("shutdown_initiated")
        await notifier.send("<b>ALGO SHUTTING DOWN</b>")

        self.scheduler.stop()
        await self.exit_mgr.stop()

        if self.portfolio.total_open > 0:
            log.warning("closing_remaining_positions", count=self.portfolio.total_open)
            await self.exit_mgr.close_all()

        self.market.stop()
        log.info("algo_stopped")
        self._shutdown.set()


async def main() -> None:
    algo = Algo()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(algo.shutdown()))

    try:
        await algo.start()
    except KeyboardInterrupt:
        await algo.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
