"""
Central configuration for the Bybit 0DTE BTC Options Algo.

All parameters controlling capital, sessions, execution, and risk are here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from enum import Enum
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


# ─────────────────────────── Bybit API ───────────────────────────

BYBIT_API_KEY: str = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET: str = os.getenv("BYBIT_API_SECRET", "")

TESTNET: bool = os.getenv("BYBIT_TESTNET", "true").lower() == "true"
DEMO: bool = os.getenv("BYBIT_DEMO", "false").lower() == "true"
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"

PERP_SYMBOL = "BTCUSDT"
PERP_CATEGORY = "linear"
PERP_LEVERAGE: int = 10
BASE_COIN = "BTC"
SETTLE_COIN = "USDT"
ACCOUNT_TYPE = "UNIFIED"

# ─────────────────────────── Capital ─────────────────────────────

INITIAL_CAPITAL_USD: float = 10_000.0
IVC_FLOOR_USD: float = 6_000.0
IVC_PCT: float = 0.60  # IVC = max(IVC_FLOOR, IVC_PCT * total_capital)

# ──────────────────────── Option Params ──────────────────────────

MIN_ORDER_QTY: float = 0.01  # Bybit minimum for BTC options
QTY_PER_STRADDLE: float = 0.01  # BTC per straddle leg

MAX_BID_ASK_SPREAD_PCT: float = 0.10  # skip puts with spread > 10% of mid
MIN_OPEN_INTEREST: float = 0.0  # minimum OI to consider a strike

# ────────────────────────── Sessions ─────────────────────────────


class SessionId(Enum):
    SESSION_1 = 1
    SESSION_2 = 2
    SESSION_3 = 3


@dataclass(frozen=True)
class SessionConfig:
    session_id: SessionId
    entry_time_utc: time
    close_time_utc: time
    # If True, sizing divides by 2 (sessions 1 & 2 share remaining capital)
    half_allocation: bool = True
    # Session 3 uses start-of-day IVC instead of current IVC
    use_sod_ivc: bool = False


SESSIONS: list[SessionConfig] = [
    SessionConfig(
        session_id=SessionId.SESSION_1,
        entry_time_utc=time(8, 30),
        close_time_utc=time(18, 0),
        half_allocation=True,
    ),
    SessionConfig(
        session_id=SessionId.SESSION_2,
        entry_time_utc=time(12, 0),
        close_time_utc=time(14, 0),
        half_allocation=True,
    ),
    SessionConfig(
        session_id=SessionId.SESSION_3,
        entry_time_utc=time(14, 0),
        close_time_utc=time(18, 0),
        half_allocation=False,
        use_sod_ivc=True,
    ),
]

ALLOWED_WEEKDAYS: set[int] = {0, 1, 2, 3, 4}  # Mon-Fri

# ───────────────────── Take Profit / Exit ────────────────────────

TAKE_PROFIT_PCT: float = 0.30  # sell straddle when value up 30%
TP_CHECK_INTERVAL_SEC: float = 5.0  # poll every 5s for TP

# Reserve: max(MIN_RESERVE_CONTRACTS * straddle_cost, RESERVE_IVC_PCT * IVC)
MIN_RESERVE_CONTRACTS: int = 2
RESERVE_IVC_PCT: float = 0.50

# ──────────────────── Execution Settings ─────────────────────────

# Options on Bybit are limit-only. Chase logic for fills.
OPTION_LIMIT_AGGRESSION: float = 0.002  # initial premium over ask (0.2%)
OPTION_CHASE_INTERVAL_SEC: float = 2.0
OPTION_CHASE_MAX_ATTEMPTS: int = 10
OPTION_MAX_SLIPPAGE_PCT: float = 0.05  # max 5% above initial ask

PERP_ORDER_TYPE: str = "Market"

# ──────────────────── Risk Management ────────────────────────────

MAX_DAILY_LOSS_PCT: float = 0.10  # halt if daily loss > 10% of capital
MAX_OPEN_STRADDLES: int = 12  # absolute cap on concurrent straddles
MAX_SINGLE_SESSION_STRADDLES: int = 6

CIRCUIT_BREAKER_API_ERRORS: int = 5  # halt after N consecutive API errors
CIRCUIT_BREAKER_COOLDOWN_SEC: float = 300.0

# ───────────────────────── Telegram ──────────────────────────────

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_ENABLED: bool = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

# ───────────────────────── Logging ───────────────────────────────

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE: str = "logs/algo.log"
LOG_JSON: bool = True

# ───────────────────── State Persistence ─────────────────────────

STATE_FILE: str = "state/positions.json"
PNL_LOG_FILE: str = "state/pnl_log.csv"

