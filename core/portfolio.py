"""
Portfolio and capital management.

Tracks open straddles, computes IVC, and persists state to disk.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

import structlog

import config
from utils.time_utils import now_utc

log = structlog.get_logger(__name__)


class StraddleStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    TP_TRIGGERED = "tp_triggered"
    CLOSED = "closed"
    FAILED = "failed"


@dataclass
class StraddleLeg:
    instrument: str
    side: str
    qty: float
    entry_price: float
    order_id: str = ""
    avg_fill_price: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Straddle:
    id: str
    session_id: int
    spot_leg: StraddleLeg
    put_leg: StraddleLeg
    put_strike: float
    qty_btc: float
    entry_time: str
    entry_cost: float       # total USDT: qty * spot_price + qty * put_premium
    put_premium: float      # per-BTC put premium at entry

    status: str = StraddleStatus.OPEN.value
    exit_time: Optional[str] = None
    exit_value: Optional[float] = None
    pnl: Optional[float] = None

    def current_value(self, spot_now: float, put_mark_now: float) -> float:
        return self.qty_btc * spot_now + self.qty_btc * put_mark_now

    def pnl_usd(self, spot_now: float, put_mark_now: float) -> float:
        return self.current_value(spot_now, put_mark_now) - self.entry_cost

    def pnl_pct(self, spot_now: float, put_mark_now: float) -> float:
        if self.entry_cost == 0:
            return 0.0
        return self.pnl_usd(spot_now, put_mark_now) / self.entry_cost

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "session_id": self.session_id,
            "spot_leg": self.spot_leg.to_dict(),
            "put_leg": self.put_leg.to_dict(),
            "put_strike": self.put_strike,
            "qty_btc": self.qty_btc,
            "entry_time": self.entry_time,
            "entry_cost": self.entry_cost,
            "put_premium": self.put_premium,
            "status": self.status,
            "exit_time": self.exit_time,
            "exit_value": self.exit_value,
            "pnl": self.pnl,
        }
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Straddle:
        return cls(
            id=d["id"],
            session_id=d["session_id"],
            spot_leg=StraddleLeg(**d["spot_leg"]),
            put_leg=StraddleLeg(**d["put_leg"]),
            put_strike=d["put_strike"],
            qty_btc=d["qty_btc"],
            entry_time=d["entry_time"],
            entry_cost=d["entry_cost"],
            put_premium=d["put_premium"],
            status=d.get("status", StraddleStatus.OPEN.value),
            exit_time=d.get("exit_time"),
            exit_value=d.get("exit_value"),
            pnl=d.get("pnl"),
        )


class Portfolio:
    """Tracks all straddle positions and capital allocation."""

    def __init__(self) -> None:
        self._straddles: dict[str, Straddle] = {}
        self._sod_capital: float = 0.0  # start-of-day capital
        self._sod_ivc: float = 0.0      # start-of-day IVC
        self._daily_pnl: float = 0.0
        self._trade_count: int = 0
        self._load_state()

    # ────────────────── Capital Calculations ─────────────────────

    def set_sod_capital(self, capital: float) -> None:
        self._sod_capital = capital
        self._sod_ivc = self.compute_ivc(capital)
        log.info("sod_capital_set", capital=capital, ivc=self._sod_ivc)

    @staticmethod
    def compute_ivc(capital: float) -> float:
        return max(config.IVC_FLOOR_USD, config.IVC_PCT * capital)

    @property
    def sod_ivc(self) -> float:
        return self._sod_ivc

    def compute_allocation(
        self,
        current_capital: float,
        straddle_cost: float,
        *,
        half: bool = True,
        use_sod_ivc: bool = False,
    ) -> float:
        """
        Compute capital to allocate for a session.

        Formula: (Capital - max(MIN_RESERVE * straddle_cost, RESERVE_PCT * IVC)) [/ 2]
        """
        ivc = self._sod_ivc if use_sod_ivc else self.compute_ivc(current_capital)
        reserve = max(
            config.MIN_RESERVE_CONTRACTS * straddle_cost,
            config.RESERVE_IVC_PCT * ivc,
        )
        available = current_capital - reserve
        if half:
            available /= 2
        return max(0.0, available)

    def max_straddles(
        self,
        current_capital: float,
        straddle_cost: float,
        *,
        half: bool = True,
        use_sod_ivc: bool = False,
    ) -> int:
        allocation = self.compute_allocation(
            current_capital, straddle_cost, half=half, use_sod_ivc=use_sod_ivc
        )
        if straddle_cost <= 0:
            return 0
        n = int(allocation // straddle_cost)
        return min(n, config.MAX_SINGLE_SESSION_STRADDLES)

    # ──────────────── Straddle Management ────────────────────────

    def add_straddle(self, s: Straddle) -> None:
        self._straddles[s.id] = s
        self._trade_count += 1
        self._save_state()
        log.info("straddle_added", id=s.id, session=s.session_id, cost=s.entry_cost)

    def close_straddle(self, straddle_id: str, exit_value: float) -> float:
        s = self._straddles.get(straddle_id)
        if not s:
            log.warning("straddle_not_found", id=straddle_id)
            return 0.0

        s.status = StraddleStatus.CLOSED.value
        s.exit_time = now_utc().isoformat()
        s.exit_value = exit_value
        s.pnl = exit_value - s.entry_cost
        self._daily_pnl += s.pnl
        self._save_state()
        log.info("straddle_closed", id=s.id, pnl=s.pnl)
        return s.pnl

    def get_open_straddles(self, session_id: int | None = None) -> list[Straddle]:
        result = [
            s for s in self._straddles.values()
            if s.status == StraddleStatus.OPEN.value
        ]
        if session_id is not None:
            result = [s for s in result if s.session_id == session_id]
        return result

    def get_straddle(self, straddle_id: str) -> Straddle | None:
        return self._straddles.get(straddle_id)

    @property
    def total_open(self) -> int:
        return len(self.get_open_straddles())

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def trade_count(self) -> int:
        return self._trade_count

    def reset_daily(self) -> None:
        closed = [
            sid for sid, s in self._straddles.items()
            if s.status != StraddleStatus.OPEN.value
        ]
        for sid in closed:
            del self._straddles[sid]
        self._daily_pnl = 0.0
        self._trade_count = 0
        self._save_state()

    # ──────────────── State Persistence ──────────────────────────

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(config.STATE_FILE), exist_ok=True)
            data = {
                "straddles": {k: v.to_dict() for k, v in self._straddles.items()},
                "sod_capital": self._sod_capital,
                "sod_ivc": self._sod_ivc,
                "daily_pnl": self._daily_pnl,
                "trade_count": self._trade_count,
            }
            with open(config.STATE_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            log.warning("state_save_failed", exc_info=True)

    def _load_state(self) -> None:
        if not os.path.exists(config.STATE_FILE):
            return
        try:
            with open(config.STATE_FILE) as f:
                data = json.load(f)
            for k, v in data.get("straddles", {}).items():
                self._straddles[k] = Straddle.from_dict(v)
            self._sod_capital = data.get("sod_capital", 0)
            self._sod_ivc = data.get("sod_ivc", 0)
            self._daily_pnl = data.get("daily_pnl", 0)
            self._trade_count = data.get("trade_count", 0)
            log.info("state_loaded", open_straddles=self.total_open)
        except Exception:
            log.warning("state_load_failed", exc_info=True)
