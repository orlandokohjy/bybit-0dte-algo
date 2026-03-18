#!/usr/bin/env python3
"""
Test script for the Bybit 0DTE algo.

Run levels (cumulative):
  1  Offline   — logic tests only, no API calls
  2  Public    — public market data (no API key needed)
  3  Auth      — authenticated endpoints (needs API key)
  4  Dry run   — full session simulation with fake orders

Usage:
    conda activate bybit_0dte
    python test_algo.py              # runs all levels
    python test_algo.py --level 1    # offline only
    python test_algo.py --level 2    # up to public API
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("LOG_LEVEL", "WARNING")

import config
from utils.logging_config import setup_logging

setup_logging()

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
SKIP = "\033[93mSKIP\033[0m"

results: list[tuple[str, str, str]] = []


def record(test: str, status: str, detail: str = "") -> None:
    results.append((test, status, detail))
    tag = PASS if status == "PASS" else (FAIL if status == "FAIL" else SKIP)
    line = f"  [{tag}] {test}"
    if detail:
        line += f"  — {detail}"
    print(line)


# ═══════════════════════ LEVEL 1: OFFLINE ═══════════════════════

def test_level_1() -> None:
    print("\n══ Level 1: Offline Logic Tests ══")

    # --- Config loads ---
    try:
        assert len(config.SESSIONS) == 3
        assert config.SESSIONS[0].entry_time_utc.hour == 8
        assert config.SESSIONS[1].entry_time_utc.hour == 12
        assert config.SESSIONS[2].entry_time_utc.hour == 14
        record("config_sessions", "PASS", "3 sessions at 08:30, 12:00, 14:00 UTC")
    except Exception as e:
        record("config_sessions", "FAIL", str(e))

    # --- IVC calculation ---
    try:
        from core.portfolio import Portfolio
        p = Portfolio()
        p.set_sod_capital(10_000)
        assert p.sod_ivc == 6_000, f"expected 6000, got {p.sod_ivc}"
        p.set_sod_capital(20_000)
        assert p.sod_ivc == 12_000, f"expected 12000, got {p.sod_ivc}"
        record("ivc_calculation", "PASS", "max(floor, 60% * capital)")
    except Exception as e:
        record("ivc_calculation", "FAIL", str(e))

    # --- Position sizing ---
    try:
        from core.portfolio import Portfolio
        from strategy.position_sizer import compute_straddle_cost

        p = Portfolio()
        p.set_sod_capital(10_000)
        cost = compute_straddle_cost(85_000, 1_500, 0.01)
        assert cost == 865.0, f"expected 865, got {cost}"

        n_half = p.max_straddles(10_000, cost, half=True)
        n_full = p.max_straddles(10_000, cost, half=False)
        assert n_half > 0, "should size > 0 straddles for S1/S2"
        assert n_full > n_half, "S3 full alloc should be more than half"
        record("position_sizing", "PASS", f"S1/2: {n_half} straddles, S3: {n_full} straddles @ ${cost:.0f}/ea")
    except Exception as e:
        record("position_sizing", "FAIL", str(e))

    # --- ITM put selection ---
    try:
        from data.option_chain import OptionChain, OptionInfo
        from utils.time_utils import now_utc

        chain = OptionChain.__new__(OptionChain)
        chain._puts = {
            71_500: OptionInfo("BTC-X-71500-P", 71_500, "P", "X", bid=100, ask=120, mid=110, mark=110),
            72_000: OptionInfo("BTC-X-72000-P", 72_000, "P", "X", bid=450, ask=480, mid=465, mark=465),
            73_000: OptionInfo("BTC-X-73000-P", 73_000, "P", "X", bid=1200, ask=1250, mid=1225, mark=1225),
        }
        chain._calls = {}
        chain._last_refresh = now_utc()
        chain._exp_date_str = "X"

        nearest = chain.get_nearest_itm_put(71_640)
        assert nearest is not None
        assert nearest.strike == 72_000, f"expected 72000, got {nearest.strike}"
        record("itm_put_selection", "PASS", "spot=71640 → picks 72000 (nearest above)")
    except Exception as e:
        record("itm_put_selection", "FAIL", str(e))

    # --- Straddle P&L tracking ---
    try:
        from core.portfolio import Straddle, StraddleLeg

        s = Straddle(
            id="test", session_id=1,
            spot_leg=StraddleLeg("BTCUSDT", "Buy", 0.01, 85_000),
            put_leg=StraddleLeg("BTC-X-86000-P", "Buy", 0.01, 1_500),
            put_strike=86_000, qty_btc=0.01,
            entry_time="now", entry_cost=0.01 * 85_000 + 0.01 * 1_500,
            put_premium=1_500,
        )
        assert s.entry_cost == 865.0

        pnl_flat = s.pnl_pct(85_000, 1_500)
        assert abs(pnl_flat) < 0.001, f"expected ~0% pnl when flat, got {pnl_flat}"

        pnl_up = s.pnl_pct(87_000, 800)
        detail_up = f"spot +2000, put -700 → pnl {pnl_up:+.1%}"

        pnl_down = s.pnl_pct(83_000, 3_200)
        detail_down = f"spot -2000, put +1700 → pnl {pnl_down:+.1%}"

        record("straddle_pnl", "PASS", f"flat: {pnl_flat:.1%} | up: {detail_up} | down: {detail_down}")
    except Exception as e:
        record("straddle_pnl", "FAIL", str(e))

    # --- Time utils ---
    try:
        from utils.time_utils import today_expiry_date_str, is_weekday, now_utc
        exp = today_expiry_date_str()
        assert len(exp) == 7, f"expected 7-char date like 18MAR26, got {exp}"
        record("time_utils", "PASS", f"expiry={exp}, weekday={is_weekday()}")
    except Exception as e:
        record("time_utils", "FAIL", str(e))

    # --- Risk manager ---
    try:
        from risk.risk_manager import RiskManager
        from core.portfolio import Portfolio
        from data.option_chain import OptionInfo

        p = Portfolio()
        p.set_sod_capital(10_000)
        rm = RiskManager(p, None)

        put_ok = OptionInfo("X", 86000, "P", "X", bid=100, ask=110, mid=105, mark=105)
        v = rm.check_entry(2, 865, put_ok, 10_000)
        assert v.allowed, f"should allow: {v.reason}"

        put_wide = OptionInfo("X", 86000, "P", "X", bid=100, ask=200, mid=150, mark=150)
        v2 = rm.check_entry(2, 865, put_wide, 10_000)
        assert not v2.allowed, "should block wide spread"

        record("risk_checks", "PASS", "allows normal, blocks wide spread")
    except Exception as e:
        record("risk_checks", "FAIL", str(e))


# ═══════════════════════ LEVEL 2: PUBLIC API ═══════════════════════

async def test_level_2() -> None:
    print("\n══ Level 2: Public API (no auth) ══")

    from pybit.unified_trading import HTTP
    from core.exchange import BybitExchange
    from data.option_chain import OptionChain

    # Use mainnet for public data (testnet may lack 0DTE contracts)
    mainnet_http = HTTP(testnet=False)

    try:
        exchange = BybitExchange()
    except Exception as e:
        record("exchange_init", "FAIL", str(e))
        return

    # --- Spot price ---
    try:
        data = mainnet_http.get_tickers(category="spot", symbol="BTCUSDT")
        spot = float(data["result"]["list"][0]["lastPrice"])
        assert spot > 0
        record("spot_price", "PASS", f"BTC = ${spot:,.2f}")
    except Exception as e:
        record("spot_price", "FAIL", str(e))
        return

    # --- Show available expiries ---
    try:
        data = mainnet_http.get_tickers(category="option", baseCoin="BTC")
        all_symbols = [t["symbol"] for t in data["result"]["list"]]
        expiries = sorted({s.split("-")[1] for s in all_symbols if len(s.split("-")) >= 4})
        record("available_expiries", "PASS", f"{len(expiries)} expiries: {expiries[:6]}...")
    except Exception as e:
        record("available_expiries", "FAIL", str(e))

    # --- 0DTE chain ---
    from utils.time_utils import today_expiry_date_str
    exp_tag = today_expiry_date_str()
    try:
        data = mainnet_http.get_tickers(category="option", baseCoin="BTC", expDate=exp_tag)
        tickers = data["result"]["list"]
        puts = [t for t in tickers if "P" in t["symbol"].split("-")]
        calls = [t for t in tickers if "C" in t["symbol"].split("-")]

        if puts:
            strikes = sorted({float(t["symbol"].split("-")[2]) for t in puts if len(t["symbol"].split("-")) >= 4})
            itm_puts = [s for s in strikes if s > spot]
            record(
                "0dte_chain",
                "PASS",
                f"{len(puts)} puts, {len(calls)} calls, "
                f"strikes: {strikes[:3]}...{strikes[-3:]}, "
                f"ITM puts (strike > spot): {itm_puts[:5]}",
            )
        else:
            record("0dte_chain", "SKIP", f"no {exp_tag} puts — 0DTE may not be listed yet (listed at 08:00 UTC)")
    except Exception as e:
        record("0dte_chain", "FAIL", str(e))
        puts = []

    # --- ITM put selection on live chain ---
    if puts:
        try:
            from strategy.option_selector import select_put, compute_time_value
            from data.option_chain import OptionInfo

            chain = OptionChain.__new__(OptionChain)
            chain._puts = {}
            chain._calls = {}
            chain._last_refresh = __import__("utils.time_utils", fromlist=["now_utc"]).now_utc()
            chain._exp_date_str = exp_tag

            for t in puts:
                sym = t["symbol"]
                strike = float(sym.split("-")[2])
                bid = float(t.get("bid1Price", 0))
                ask = float(t.get("ask1Price", 0))
                mid = (bid + ask) / 2 if bid > 0 and ask > 0 else float(t.get("markPrice", 0))
                chain._puts[strike] = OptionInfo(
                    symbol=sym, strike=strike, option_type="P", expiry_str=exp_tag,
                    bid=bid, ask=ask, mid=mid, mark=float(t.get("markPrice", 0)),
                    iv=float(t.get("markIv", 0)), delta=float(t.get("delta", 0)),
                    gamma=float(t.get("gamma", 0)), theta=float(t.get("theta", 0)),
                    vega=float(t.get("vega", 0)),
                )

            put = select_put(chain, spot)
            if put:
                tv = compute_time_value(put, spot)
                record(
                    "live_put_select",
                    "PASS",
                    f"strike={put.strike:,.0f} ask={put.ask:,.2f} bid={put.bid:,.2f} "
                    f"iv={put.iv:.2f} delta={put.delta:.3f} gamma={put.gamma:.6f} "
                    f"time_value={tv:,.2f}",
                )

                # --- Sizing on live data ---
                from core.portfolio import Portfolio
                from strategy.position_sizer import compute_straddle_cost, compute_num_straddles
                p = Portfolio()
                p.set_sod_capital(10_000.0)
                cost = compute_straddle_cost(spot, put.ask, config.QTY_PER_STRADDLE)
                for sess in config.SESSIONS:
                    n = compute_num_straddles(sess, 10_000.0, cost, p)
                    record(
                        f"sizing_S{sess.session_id.value}",
                        "PASS",
                        f"{n} straddles @ ${cost:,.2f}/ea ({'half' if sess.half_allocation else 'full'} alloc)",
                    )
            else:
                record("live_put_select", "FAIL", "no suitable ITM put found")
        except Exception as e:
            record("live_put_select", "FAIL", str(e))
    else:
        record("live_put_select", "SKIP", "no 0DTE chain available")

    exchange.close()


# ═══════════════════════ LEVEL 3: AUTH ═══════════════════════

async def test_level_3() -> None:
    print("\n══ Level 3: Authenticated Endpoints ══")

    if not config.BYBIT_API_KEY:
        record("auth_check", "SKIP", "no BYBIT_API_KEY in .env — set it to test")
        return

    from core.exchange import BybitExchange
    exchange = BybitExchange()

    try:
        equity = await exchange.get_total_equity_usd()
        record("wallet_equity", "PASS", f"${equity:,.2f}")
    except Exception as e:
        record("wallet_equity", "FAIL", str(e))

    try:
        usdt = await exchange.get_usdt_balance()
        record("usdt_balance", "PASS", f"${usdt:,.2f}")
    except Exception as e:
        record("usdt_balance", "FAIL", str(e))

    try:
        positions = await exchange.get_positions()
        record("positions", "PASS", f"{len(positions)} open option positions")
    except Exception as e:
        record("positions", "FAIL", str(e))

    exchange.close()


# ═══════════════════════ LEVEL 4: DRY RUN SESSION ═══════════════════════

async def test_level_4() -> None:
    print("\n══ Level 4: Dry-Run Session Simulation ══")

    if not config.BYBIT_API_KEY:
        record("dry_run", "SKIP", "no API key — need at least public data for dry run")

    assert config.DRY_RUN, "DRY_RUN must be true for level 4"

    from core.exchange import BybitExchange
    from core.portfolio import Portfolio
    from data.market_data import MarketData
    from data.option_chain import OptionChain
    from risk.risk_manager import RiskManager
    from strategy.exit_manager import ExitManager
    from strategy.session_manager import SessionManager

    exchange = BybitExchange()
    chain = OptionChain(exchange)
    market = MarketData(exchange, chain)
    portfolio = Portfolio()
    risk = RiskManager(portfolio, market)
    exit_mgr = ExitManager(exchange, market, portfolio)
    session_mgr = SessionManager(exchange, market, chain, portfolio, risk, exit_mgr)

    try:
        await market.start()
        spot = await market.get_spot_price()
        record("dry_market_data", "PASS", f"spot=${spot:,.2f}")
    except Exception as e:
        record("dry_market_data", "FAIL", str(e))
        exchange.close()
        return

    capital = 10_000.0
    portfolio.set_sod_capital(capital)

    # Simulate Session 1 entry
    sess_cfg = config.SESSIONS[0]
    try:
        built = await session_mgr.run_entry(sess_cfg)
        open_straddles = portfolio.get_open_straddles(sess_cfg.session_id.value)
        record(
            "dry_session1_entry",
            "PASS" if built > 0 else "FAIL",
            f"built {built} straddles" + (f", IDs: {[s.id for s in open_straddles]}" if open_straddles else ""),
        )
    except Exception as e:
        record("dry_session1_entry", "FAIL", str(e))

    # Simulate Session 1 close
    try:
        pnl = await exit_mgr.close_session(sess_cfg.session_id.value)
        remaining = portfolio.get_open_straddles(sess_cfg.session_id.value)
        record("dry_session1_close", "PASS", f"closed, pnl=${pnl:,.2f}, remaining={len(remaining)}")
    except Exception as e:
        record("dry_session1_close", "FAIL", str(e))

    market.stop()


# ═══════════════════════ MAIN ═══════════════════════

def print_summary() -> None:
    print("\n" + "═" * 50)
    passed = sum(1 for _, s, _ in results if s == "PASS")
    failed = sum(1 for _, s, _ in results if s == "FAIL")
    skipped = sum(1 for _, s, _ in results if s == "SKIP")
    total = len(results)
    print(f"Results: {passed}/{total} passed, {failed} failed, {skipped} skipped")
    if failed:
        print(f"\nFailed tests:")
        for name, status, detail in results:
            if status == "FAIL":
                print(f"  - {name}: {detail}")
    print("═" * 50)


async def run_tests(max_level: int) -> None:
    if max_level >= 1:
        test_level_1()
    if max_level >= 2:
        await test_level_2()
    if max_level >= 3:
        await test_level_3()
    if max_level >= 4:
        await test_level_4()
    print_summary()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test the 0DTE algo")
    parser.add_argument("--level", type=int, default=4, choices=[1, 2, 3, 4],
                        help="Max test level to run (1=offline, 2=public, 3=auth, 4=dry-run)")
    args = parser.parse_args()
    asyncio.run(run_tests(args.level))
