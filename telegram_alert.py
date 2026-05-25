"""
Telegram alerts for trade entry and exit.
"""

from __future__ import annotations

import logging
import aiohttp

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{}/sendMessage"


async def send_alert(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = API_URL.format(TELEGRAM_BOT_TOKEN)
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    log.warning("Telegram alert failed: %s", await resp.text())
    except Exception as e:
        log.warning("Telegram alert error: %s", e)


async def alert_entry(symbol: str, direction: str, entry_price: float, sl: float, tp: float, quantity: float):
    arrow = "↗️" if direction == "long" else "↘️"
    text = (
        f"{arrow} <b>ENTRY — {symbol}</b>\n"
        f"\n"
        f"Direction: <b>{direction.upper()}</b>\n"
        f"Entry: <code>${entry_price:,.2f}</code>\n"
        f"Stop Loss: <code>${sl:,.2f}</code>\n"
        f"Take Profit: <code>${tp:,.2f}</code>\n"
        f"Size: <code>{quantity}</code>"
    )
    await send_alert(text)


async def alert_exit(symbol: str, direction: str, result: str, entry_price: float, exit_price: float, pnl: float, r_value: float):
    icon = "✅" if result == "win" else "❌"
    text = (
        f"{icon} <b>EXIT — {symbol}</b>\n"
        f"\n"
        f"Result: <b>{result.upper()}</b> ({r_value:+.1f}R)\n"
        f"Direction: {direction.upper()}\n"
        f"Entry: <code>${entry_price:,.2f}</code>\n"
        f"Exit: <code>${exit_price:,.2f}</code>\n"
        f"PnL: <code>${pnl:,.2f}</code>"
    )
    await send_alert(text)
