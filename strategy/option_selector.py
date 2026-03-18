"""
Selects the optimal ITM put for the synthetic straddle.

Rule: buy the nearest ITM put (lowest strike above spot) that passes
liquidity and spread filters. Optionally rank by gamma/theta ratio.
"""
from __future__ import annotations

from typing import Optional

import structlog

import config
from data.option_chain import OptionChain, OptionInfo

log = structlog.get_logger(__name__)


def select_put(chain: OptionChain, spot: float) -> Optional[OptionInfo]:
    """
    Select the best ITM put for the straddle.

    Priority:
    1. Nearest ITM strike (lowest strike > spot)
    2. Must have valid ask price
    3. Bid-ask spread within limits
    """
    itm_puts = chain.get_itm_puts(spot)

    if not itm_puts:
        log.warning("no_itm_puts", spot=spot, strikes=chain.available_strikes[:10])
        return None

    for put in itm_puts:
        if put.ask <= 0:
            log.debug("put_no_ask", strike=put.strike, symbol=put.symbol)
            continue

        if put.spread_pct > config.MAX_BID_ASK_SPREAD_PCT:
            log.debug(
                "put_spread_too_wide",
                strike=put.strike,
                spread_pct=f"{put.spread_pct:.1%}",
            )
            continue

        log.info(
            "put_selected",
            symbol=put.symbol,
            strike=put.strike,
            ask=put.ask,
            bid=put.bid,
            mid=put.mid,
            iv=put.iv,
            delta=put.delta,
            gamma=put.gamma,
            theta=put.theta,
        )
        return put

    # Fallback: take the nearest ITM even if spread is wide
    fallback = itm_puts[0]
    if fallback.ask > 0:
        log.warning(
            "put_fallback",
            symbol=fallback.symbol,
            strike=fallback.strike,
            spread_pct=f"{fallback.spread_pct:.1%}",
        )
        return fallback

    log.error("no_valid_put_found", spot=spot)
    return None


def compute_time_value(put: OptionInfo, spot: float) -> float:
    """Time value = premium - intrinsic. This is the true gamma cost."""
    intrinsic = max(0.0, put.strike - spot)
    return max(0.0, put.mid - intrinsic)
