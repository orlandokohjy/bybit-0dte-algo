"""
Position sizing for each session.

Formulas per the spec:
  Sessions 1 & 2: (Capital - max(2 * cost, 50% * IVC)) / 2
  Session 3:      (Capital - max(2 * cost, 50% * SOD_IVC))
"""
from __future__ import annotations

import structlog

import config
from config import SessionConfig
from core.portfolio import Portfolio

log = structlog.get_logger(__name__)


def compute_straddle_cost(spot: float, put_premium: float, qty: float) -> float:
    """
    Total USDT outflow for one straddle unit.

    One straddle = qty BTC perp (margin) + qty BTC of put premium.
    Perp margin = notional / leverage.
    """
    perp_margin = (qty * spot) / config.PERP_LEVERAGE
    put_cost = qty * put_premium
    return perp_margin + put_cost


def compute_num_straddles(
    session_cfg: SessionConfig,
    current_capital: float,
    straddle_cost: float,
    portfolio: Portfolio,
) -> int:
    """Determine how many straddles to open for this session."""
    if straddle_cost <= 0:
        return 0

    n = portfolio.max_straddles(
        current_capital,
        straddle_cost,
        half=session_cfg.half_allocation,
        use_sod_ivc=session_cfg.use_sod_ivc,
    )

    already_open = portfolio.total_open
    cap = config.MAX_OPEN_STRADDLES - already_open
    n = min(n, cap, config.MAX_SINGLE_SESSION_STRADDLES)

    log.info(
        "position_sized",
        session=session_cfg.session_id.value,
        capital=f"${current_capital:,.0f}",
        straddle_cost=f"${straddle_cost:,.2f}",
        num_straddles=n,
        half=session_cfg.half_allocation,
        use_sod=session_cfg.use_sod_ivc,
    )
    return max(0, n)
