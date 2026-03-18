"""Telegram notification helper."""
from __future__ import annotations

import httpx
import structlog

import config

log = structlog.get_logger(__name__)

_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"


async def send(message: str, *, parse_mode: str = "HTML") -> None:
    if not config.TELEGRAM_ENABLED:
        return
    url = _SEND_URL.format(token=config.TELEGRAM_BOT_TOKEN)
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": parse_mode,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
    except Exception:
        log.warning("telegram_send_failed", exc_info=True)


async def notify_entry(session_id: int, n_straddles: int, cost_usd: float, spot: float, strike: float) -> None:
    msg = (
        f"<b>ENTRY Session {session_id}</b>\n"
        f"Straddles: {n_straddles}\n"
        f"Total cost: ${cost_usd:,.2f}\n"
        f"Spot: ${spot:,.2f} | Put strike: ${strike:,.0f}\n"
    )
    await send(msg)


async def notify_tp(session_id: int, straddle_id: str, pnl_usd: float, pnl_pct: float) -> None:
    msg = (
        f"<b>TP HIT Session {session_id}</b>\n"
        f"Straddle: {straddle_id}\n"
        f"PnL: ${pnl_usd:,.2f} ({pnl_pct:+.1%})\n"
    )
    await send(msg)


async def notify_close(session_id: int, n_closed: int, total_pnl: float) -> None:
    msg = (
        f"<b>SESSION {session_id} CLOSED</b>\n"
        f"Straddles closed: {n_closed}\n"
        f"Session PnL: ${total_pnl:,.2f}\n"
    )
    await send(msg)


async def notify_error(context: str, error: str) -> None:
    msg = f"<b>ERROR</b> [{context}]\n<code>{error[:500]}</code>"
    await send(msg)


async def notify_daily_summary(total_pnl: float, capital: float, trades: int) -> None:
    msg = (
        f"<b>DAILY SUMMARY</b>\n"
        f"Total PnL: ${total_pnl:,.2f}\n"
        f"Capital: ${capital:,.2f}\n"
        f"Trades: {trades}\n"
    )
    await send(msg)
