"""
Pre-trade and runtime risk checks.
"""
from __future__ import annotations

from dataclasses import dataclass

import structlog

import config
from core.portfolio import Portfolio
from data.market_data import MarketData
from data.option_chain import OptionInfo

log = structlog.get_logger(__name__)


@dataclass
class RiskVerdict:
    allowed: bool
    reason: str = ""


class RiskManager:
    def __init__(self, portfolio: Portfolio, market: MarketData) -> None:
        self._portfolio = portfolio
        self._market = market
        self._circuit_open = False

    # ────────────────── Pre-trade Checks ─────────────────────────

    def check_entry(
        self,
        n_straddles: int,
        straddle_cost: float,
        put: OptionInfo,
        current_capital: float,
    ) -> RiskVerdict:
        if self._circuit_open:
            return RiskVerdict(False, "circuit breaker is open")

        if not _is_trading_day():
            return RiskVerdict(False, "not a trading day")

        total_open = self._portfolio.total_open
        if total_open + n_straddles > config.MAX_OPEN_STRADDLES:
            return RiskVerdict(
                False,
                f"max open straddles exceeded: {total_open}+{n_straddles} > {config.MAX_OPEN_STRADDLES}",
            )

        total_cost = n_straddles * straddle_cost
        if total_cost > current_capital * 0.95:
            return RiskVerdict(False, f"cost ${total_cost:.0f} exceeds 95% of capital ${current_capital:.0f}")

        if put.spread_pct > config.MAX_BID_ASK_SPREAD_PCT:
            return RiskVerdict(
                False,
                f"put spread too wide: {put.spread_pct:.1%} > {config.MAX_BID_ASK_SPREAD_PCT:.1%}",
            )

        if put.ask <= 0:
            return RiskVerdict(False, "put has no ask price")

        return RiskVerdict(True)

    # ──────────────── Runtime Checks ─────────────────────────────

    def check_daily_loss(self, current_capital: float) -> RiskVerdict:
        pnl = self._portfolio.daily_pnl
        sod = self._portfolio.sod_ivc
        if sod > 0 and pnl < 0 and abs(pnl) > config.MAX_DAILY_LOSS_PCT * sod:
            return RiskVerdict(
                False,
                f"daily loss ${pnl:.0f} exceeds {config.MAX_DAILY_LOSS_PCT:.0%} of SOD IVC ${sod:.0f}",
            )
        return RiskVerdict(True)

    def check_api_health(self, consecutive_errors: int) -> RiskVerdict:
        if consecutive_errors >= config.CIRCUIT_BREAKER_API_ERRORS:
            self._circuit_open = True
            return RiskVerdict(False, f"API errors: {consecutive_errors}")
        return RiskVerdict(True)

    def reset_circuit_breaker(self) -> None:
        self._circuit_open = False
        log.info("circuit_breaker_reset")

    @property
    def circuit_open(self) -> bool:
        return self._circuit_open


class GreeksMonitor:
    """Tracks portfolio-level greeks across all open straddles."""

    def __init__(self, portfolio: Portfolio, market: MarketData) -> None:
        self._portfolio = portfolio
        self._market = market

    async def snapshot(self) -> dict:
        total_delta = 0.0
        total_gamma = 0.0
        total_theta = 0.0
        total_vega = 0.0

        for s in self._portfolio.get_open_straddles():
            # Spot leg contributes delta = +1 per BTC
            total_delta += s.qty_btc

            opt = self._market.get_option_snapshot(s.put_leg.instrument)
            if opt:
                total_delta += opt.delta * s.qty_btc
                total_gamma += opt.gamma * s.qty_btc
                total_theta += opt.theta * s.qty_btc
                total_vega += opt.vega * s.qty_btc

        return {
            "delta": round(total_delta, 6),
            "gamma": round(total_gamma, 8),
            "theta": round(total_theta, 4),
            "vega": round(total_vega, 4),
            "n_positions": self._portfolio.total_open,
        }


def _is_trading_day() -> bool:
    from utils.time_utils import now_utc
    return now_utc().weekday() in config.ALLOWED_WEEKDAYS
