"""
Atomic straddle construction: buy spot, then buy ITM put.

Execution order matters:
  Entry:  spot first (market) → put second (aggressive limit/chase)
  Exit:   spot first (market) → put second (aggressive limit/chase)
"""
from __future__ import annotations

import uuid
from typing import Optional

import structlog

import config
from core.exchange import BybitExchange
from core.portfolio import Portfolio, Straddle, StraddleLeg, StraddleStatus
from data.market_data import MarketData
from data.option_chain import OptionInfo
from utils.time_utils import now_utc

log = structlog.get_logger(__name__)


async def build_straddle(
    exchange: BybitExchange,
    market: MarketData,
    portfolio: Portfolio,
    put: OptionInfo,
    qty_btc: float,
    session_id: int,
) -> Optional[Straddle]:
    """
    Execute the atomic entry sequence for one synthetic straddle.

    1. Market buy spot BTC
    2. Chase-buy the ITM put
    3. Subscribe to option ticker for monitoring
    4. Register straddle in portfolio

    Returns the Straddle object or None on failure.
    """
    straddle_id = f"S{session_id}-{uuid.uuid4().hex[:8]}"
    spot_price = await market.get_spot_price()

    log.info(
        "building_straddle",
        id=straddle_id,
        spot=spot_price,
        put=put.symbol,
        strike=put.strike,
        qty=qty_btc,
    )

    # ── Step 1: Open long perp ──
    try:
        perp_result = await exchange.open_long_perp(qty_btc)
        perp_order_id = perp_result.get("orderId", "")
    except Exception as exc:
        log.error("perp_buy_failed", id=straddle_id, error=str(exc))
        return None

    spot_fill_price = float(perp_result.get("avgPrice", 0)) or spot_price
    log.info("perp_leg_filled", id=straddle_id, price=spot_fill_price, order_id=perp_order_id)

    # ── Step 2: Buy put (chase) ──
    put_ask = put.ask
    if put_ask <= 0:
        _, put_ask = await market.get_option_bid_ask(put.symbol)
    if put_ask <= 0:
        log.error("put_no_ask_price", id=straddle_id, symbol=put.symbol)
        await _emergency_close_perp(exchange, qty_btc)
        return None

    put_result = await exchange.chase_buy_put(put.symbol, qty_btc, put_ask)
    if put_result is None:
        log.error("put_buy_failed", id=straddle_id, symbol=put.symbol)
        await _emergency_close_perp(exchange, qty_btc)
        return None

    put_fill_price = float(put_result.get("avgPrice", put_ask))
    put_order_id = put_result.get("orderId", "")
    log.info("put_leg_filled", id=straddle_id, price=put_fill_price, order_id=put_order_id)

    # ── Step 3: Subscribe to option ticker ──
    market.subscribe_option(put.symbol)

    # ── Step 4: Register ──
    straddle = Straddle(
        id=straddle_id,
        session_id=session_id,
        spot_leg=StraddleLeg(
            instrument=config.PERP_SYMBOL,
            side="Buy",
            qty=qty_btc,
            entry_price=spot_fill_price,
            order_id=perp_order_id,
            avg_fill_price=spot_fill_price,
        ),
        put_leg=StraddleLeg(
            instrument=put.symbol,
            side="Buy",
            qty=qty_btc,
            entry_price=put_fill_price,
            order_id=put_order_id,
            avg_fill_price=put_fill_price,
        ),
        put_strike=put.strike,
        qty_btc=qty_btc,
        entry_time=now_utc().isoformat(),
        entry_cost=qty_btc * spot_fill_price + qty_btc * put_fill_price,
        put_premium=put_fill_price,
    )

    portfolio.add_straddle(straddle)
    log.info(
        "straddle_built",
        id=straddle_id,
        entry_cost=f"${straddle.entry_cost:,.2f}",
        spot=spot_fill_price,
        put_premium=put_fill_price,
        strike=put.strike,
    )
    return straddle


async def unwind_straddle(
    exchange: BybitExchange,
    market: MarketData,
    portfolio: Portfolio,
    straddle: Straddle,
    reason: str = "close",
) -> float:
    """
    Close a straddle: sell spot first, then sell put.
    Returns the exit value (USDT recovered).
    """
    log.info("unwinding_straddle", id=straddle.id, reason=reason)

    exit_value = 0.0

    # ── Close long perp first ──
    try:
        await exchange.close_long_perp(straddle.qty_btc)
        spot_price = await market.get_spot_price()
        exit_value += straddle.qty_btc * spot_price
        log.info("perp_closed", id=straddle.id, price=spot_price)
    except Exception as exc:
        log.error("perp_close_failed", id=straddle.id, error=str(exc))
        spot_price = await market.get_spot_price()
        exit_value += straddle.qty_btc * spot_price

    # ── Sell put ──
    put_bid, _ = await market.get_option_bid_ask(straddle.put_leg.instrument)
    if put_bid > 0:
        result = await exchange.chase_sell_put(
            straddle.put_leg.instrument,
            straddle.qty_btc,
            put_bid,
        )
        if result:
            put_sell_price = float(result.get("avgPrice", put_bid))
            exit_value += straddle.qty_btc * put_sell_price
            log.info("put_sold", id=straddle.id, price=put_sell_price)
        else:
            log.warning("put_sell_chase_failed", id=straddle.id)
    else:
        log.warning("put_no_bid", id=straddle.id, symbol=straddle.put_leg.instrument)

    # ── Update portfolio ──
    pnl = portfolio.close_straddle(straddle.id, exit_value)
    log.info(
        "straddle_unwound",
        id=straddle.id,
        reason=reason,
        exit_value=f"${exit_value:,.2f}",
        pnl=f"${pnl:,.2f}",
    )
    return exit_value


async def _emergency_close_perp(exchange: BybitExchange, qty: float) -> None:
    """Best-effort unwind of a perp position after a failed put entry."""
    try:
        await exchange.close_long_perp(qty)
        log.info("emergency_perp_closed", qty=qty)
    except Exception:
        log.error("emergency_perp_close_failed", qty=qty, exc_info=True)
