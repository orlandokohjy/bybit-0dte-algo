"""
Manual session: immediate entry, live TP monitoring, timed close.

Uses the real algo components (exchange, portfolio, exit_manager, etc.)
to run a full session on Bybit Demo — just without the scheduler.
"""
import asyncio
import os
import signal
import sys

os.environ.setdefault("DRY_RUN", "false")

from dotenv import load_dotenv
load_dotenv(override=True)

import structlog
import config
from core.exchange import BybitExchange
from core.portfolio import Portfolio
from data.market_data import MarketData
from data.option_chain import OptionChain
from risk.risk_manager import RiskManager
from strategy.exit_manager import ExitManager
from strategy.option_selector import select_put
from strategy.position_sizer import compute_straddle_cost, compute_num_straddles
from strategy.straddle_builder import build_straddle, unwind_straddle
from config import SessionConfig, SessionId
from datetime import datetime, timezone, time

log = structlog.get_logger(__name__)

HOLD_SECONDS = int(os.getenv("HOLD_SECONDS", "120"))


def print_banner(msg: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {msg}")
    print(f"{'─' * 60}")


async def main():
    print("=" * 60)
    print("  MANUAL SESSION — Bybit Demo")
    print("=" * 60)
    now = datetime.now(timezone.utc)
    print(f"  UTC:        {now.strftime('%H:%M:%S')}")
    print(f"  DRY_RUN:    {config.DRY_RUN}")
    print(f"  DEMO:       {config.DEMO}")
    print(f"  Hold time:  {HOLD_SECONDS}s")
    print()

    if config.DRY_RUN:
        print("[ABORT] Set DRY_RUN=false in .env")
        return

    # ── Init components ──
    exchange = BybitExchange()
    chain = OptionChain(exchange)
    market = MarketData(exchange, chain)
    portfolio = Portfolio()
    risk = RiskManager(portfolio, market)
    exit_mgr = ExitManager(exchange, market, portfolio)

    if not config.DRY_RUN:
        await exchange.set_leverage()

    # Use Session 1 config but we'll run it immediately
    session_cfg = SessionConfig(
        session_id=SessionId.SESSION_1,
        entry_time_utc=time(0, 0),
        close_time_utc=time(23, 59),
        half_allocation=True,
    )

    print_banner("STEP 1: MARKET DATA")
    spot = await market.get_spot_price()
    print(f"  BTC spot:   ${spot:,.2f}")

    capital = await exchange.get_total_equity_usd()
    print(f"  Equity:     ${capital:,.2f}")

    portfolio.set_sod_capital(capital)
    print(f"  IVC:        ${portfolio.sod_ivc:,.2f}")

    # ── Refresh chain ──
    print_banner("STEP 2: OPTION CHAIN")
    n_puts = await chain.refresh()
    print(f"  0DTE puts:  {n_puts}")

    if n_puts == 0:
        print("  [FAIL] No 0DTE puts found")
        return

    # ── Select put ──
    put = select_put(chain, spot)
    if put is None:
        print("  [FAIL] No suitable ITM put")
        return

    print(f"  Selected:   {put.symbol}")
    print(f"  Strike:     ${put.strike:,.0f}")
    print(f"  Bid/Ask:    ${put.bid:,.0f} / ${put.ask:,.0f}")
    print(f"  IV:         {put.iv:.2f}")
    print(f"  Delta:      {put.delta:.4f}")

    # ── Size ──
    print_banner("STEP 3: POSITION SIZING")
    qty = config.QTY_PER_STRADDLE
    straddle_cost = compute_straddle_cost(spot, put.ask, qty)
    n_straddles = compute_num_straddles(session_cfg, capital, straddle_cost, portfolio)

    print(f"  Qty/straddle: {qty} BTC")
    print(f"  Cost/straddle: ${straddle_cost:,.2f}")
    print(f"  Num straddles: {n_straddles}")
    print(f"  Total outlay:  ${n_straddles * straddle_cost:,.2f}")

    if n_straddles == 0:
        print("  [FAIL] Zero straddles — insufficient capital or cost too high")
        return

    # ── Risk check ──
    verdict = risk.check_entry(n_straddles, straddle_cost, put, capital)
    if not verdict.allowed:
        print(f"  [BLOCKED] {verdict.reason}")
        return
    print(f"  Risk check: PASS")

    # ── Build straddles ──
    print_banner(f"STEP 4: BUILDING {n_straddles} STRADDLES")
    built_straddles = []
    for i in range(n_straddles):
        print(f"\n  Building straddle {i + 1}/{n_straddles}...")
        straddle = await build_straddle(
            exchange, market, portfolio, put, qty, session_cfg.session_id.value,
        )
        if straddle:
            built_straddles.append(straddle)
            print(f"    [{straddle.id}] OPEN")
            print(f"      Spot:  ${straddle.spot_leg.avg_fill_price:,.2f}")
            print(f"      Put:   ${straddle.put_premium:,.2f}")
            print(f"      Cost:  ${straddle.entry_cost:,.2f}")
        else:
            print(f"    FAILED — stopping")
            break
        if i < n_straddles - 1:
            await asyncio.sleep(1)

    if not built_straddles:
        print("\n  [FAIL] No straddles built")
        return

    total_entry = sum(s.entry_cost for s in built_straddles)
    print(f"\n  Total built: {len(built_straddles)}")
    print(f"  Total cost:  ${total_entry:,.2f}")

    # ── Start TP monitor ──
    print_banner(f"STEP 5: MONITORING (TP={config.TAKE_PROFIT_PCT:.0%}, polling every {config.TP_CHECK_INTERVAL_SEC}s)")
    print(f"  Will hard close after {HOLD_SECONDS}s")
    print(f"  Press Ctrl+C to close early")
    print()

    tp_triggered = set()
    shutdown_requested = False

    def handle_sigint(sig, frame):
        nonlocal shutdown_requested
        shutdown_requested = True
        print("\n  [Ctrl+C] Closing positions...")

    signal.signal(signal.SIGINT, handle_sigint)

    start_time = asyncio.get_event_loop().time()
    check_count = 0

    while not shutdown_requested:
        elapsed = asyncio.get_event_loop().time() - start_time
        remaining = HOLD_SECONDS - elapsed

        if remaining <= 0:
            print(f"\n  ⏰ Hold time expired ({HOLD_SECONDS}s)")
            break

        # Get live prices
        try:
            spot_now = await market.get_spot_price()
        except Exception:
            await asyncio.sleep(config.TP_CHECK_INTERVAL_SEC)
            continue

        open_straddles = portfolio.get_open_straddles()
        if not open_straddles:
            print("\n  All straddles closed (TP or manual)")
            break

        check_count += 1
        header = f"  [{int(elapsed):>3d}s / {HOLD_SECONDS}s]  spot=${spot_now:,.2f}  open={len(open_straddles)}"
        lines = [header]

        for s in open_straddles:
            if s.id in tp_triggered:
                continue
            try:
                put_mark = await market.get_option_mark(s.put_leg.instrument)
            except Exception:
                put_mark = 0

            if put_mark <= 0:
                lines.append(f"    {s.id}: put mark unavailable")
                continue

            pnl_usd = s.pnl_usd(spot_now, put_mark)
            pnl_pct = s.pnl_pct(spot_now, put_mark)
            current_val = s.current_value(spot_now, put_mark)
            spot_pnl = s.qty_btc * (spot_now - s.spot_leg.entry_price)
            put_pnl = s.qty_btc * (put_mark - s.put_premium)

            lines.append(
                f"    {s.id}: val=${current_val:,.2f}  pnl=${pnl_usd:+,.2f} ({pnl_pct:+.2%})"
                f"  [spot:{spot_pnl:+.2f} put:{put_pnl:+.2f}]"
            )

            # Check TP
            if pnl_pct >= config.TAKE_PROFIT_PCT:
                tp_triggered.add(s.id)
                lines.append(f"    >>> TP TRIGGERED on {s.id} at {pnl_pct:.1%}! Unwinding...")
                exit_val = await unwind_straddle(exchange, market, portfolio, s, reason="take_profit")
                realized = exit_val - s.entry_cost
                lines.append(f"    >>> CLOSED {s.id}: realized=${realized:+,.2f}")

        print("\n".join(lines))
        await asyncio.sleep(config.TP_CHECK_INTERVAL_SEC)

    # ── Hard close remaining ──
    print_banner("STEP 6: HARD CLOSE")
    remaining_straddles = portfolio.get_open_straddles()
    if remaining_straddles:
        print(f"  Closing {len(remaining_straddles)} remaining straddle(s)...")
        for s in remaining_straddles:
            print(f"    Unwinding {s.id}...")
            exit_val = await unwind_straddle(exchange, market, portfolio, s, reason="manual_close")
            realized = exit_val - s.entry_cost
            print(f"    {s.id}: realized=${realized:+,.2f}")
            await asyncio.sleep(0.5)
    else:
        print("  Nothing to close (all hit TP)")

    # ── Summary ──
    print_banner("STEP 7: SUMMARY")
    final_equity = await exchange.get_total_equity_usd()
    equity_delta = final_equity - capital

    print(f"  Straddles built:   {len(built_straddles)}")
    print(f"  TP triggered:      {len(tp_triggered)}")
    print(f"  Hard closed:       {len(built_straddles) - len(tp_triggered)}")
    print()
    for s in built_straddles:
        status = "TP" if s.id in tp_triggered else "CLOSED"
        pnl = s.pnl if s.pnl is not None else 0
        print(f"    {s.id}  {status:>6s}  pnl=${pnl:+,.2f}")
    print()
    print(f"  Daily P&L:         ${portfolio.daily_pnl:+,.2f}")
    print(f"  Equity before:     ${capital:,.2f}")
    print(f"  Equity after:      ${final_equity:,.2f}")
    print(f"  Equity delta:      ${equity_delta:+,.2f}")
    print()
    print("=" * 60)
    print("  SESSION COMPLETE")
    print("=" * 60)

    exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
