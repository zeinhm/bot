from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from binance import AsyncClient
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from app.auth import decrypt
from config import COMMISSION_PCT
import app.db as db

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def compute_stats(trades):
    closed = [t for t in trades if t.result in ("win", "loss")]
    wins = [t for t in closed if t.result == "win"]
    losses = [t for t in closed if t.result == "loss"]
    total = len(closed)
    win_rate = (len(wins) / total * 100) if total > 0 else 0
    total_r = sum(t.r_value or 0 for t in closed)
    total_pnl = sum(t.pnl_usdt or 0 for t in closed)

    gross_wins = sum(t.pnl_usdt or 0 for t in wins)
    gross_losses = abs(sum(t.pnl_usdt or 0 for t in losses))
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else 0

    hold_times = []
    for t in closed:
        if t.entry_time and t.exit_time:
            hold_times.append((t.exit_time - t.entry_time).total_seconds())
    avg_hold = (sum(hold_times) / len(hold_times)) if hold_times else 0

    peak = 0
    equity = 0
    max_dd = 0
    for t in closed:
        equity += (t.pnl_usdt or 0)
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    max_dd_pct = (max_dd / peak * 100) if peak > 0 else 0

    equity_data = []
    cumulative = 0
    for t in closed:
        cumulative += (t.pnl_usdt or 0)
        ts = t.exit_time or t.entry_time
        if ts:
            equity_data.append({"time": ts.strftime("%Y-%m-%d"), "value": round(cumulative, 2)})

    return {
        "total": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "total_r": total_r,
        "total_pnl": total_pnl,
        "profit_factor": profit_factor,
        "avg_hold_secs": avg_hold,
        "max_dd_pct": max_dd_pct,
        "equity_data": equity_data,
    }


@router.get("/admin/user/{user_id}")
async def admin_user_detail(request: Request, user_id: int):
    from routes.admin import require_admin, AdminNotFound
    admin = await require_admin(request)

    target = await db.get_user(user_id)
    if not target:
        raise AdminNotFound()

    manager = request.app.state.bot_manager
    live_status = manager.get_status(user_id, "live")
    paper_status = manager.get_status(user_id, "paper")

    live_trades = await db.get_all_trades(user_id, is_paper=False)
    paper_trades = await db.get_all_trades(user_id, is_paper=True)

    live_stats = compute_stats(live_trades)
    paper_stats = compute_stats(paper_trades)

    config = await db.get_user_config(user_id)
    has_api_keys = config is not None and config.binance_api_key_enc is not None

    paper_balance = await db.get_paper_balance(user_id)

    live_balance = None
    live_worker = manager.get_worker(user_id, "live")
    if live_worker and live_worker.exchange.client:
        try:
            live_balance = await live_worker.exchange.get_balance()
        except Exception:
            pass
    if live_balance is None and has_api_keys:
        live_balance = await db.get_state("last_balance", None, user_id=user_id, is_paper=False)

    recent_trades = await db.get_recent_trades(20, user_id=user_id, is_paper=False)
    events = await db.get_recent_events(20, user_id=user_id)

    avg_hold_mins = int(live_stats["avg_hold_secs"] / 60) if live_stats["avg_hold_secs"] > 0 else 0

    return templates.TemplateResponse(request, "admin_user_detail.html", {
        "user": admin,
        "page": "admin_dashboard",
        "admin_mode": True,
        "target": target,
        "live_status": live_status,
        "paper_status": paper_status,
        "live_stats": live_stats,
        "paper_stats": paper_stats,
        "has_api_keys": has_api_keys,
        "live_balance": live_balance,
        "paper_balance": paper_balance,
        "recent_trades": recent_trades,
        "events": events,
        "avg_hold_mins": avg_hold_mins,
        "equity_json": json.dumps(live_stats["equity_data"]),
    })


@router.post("/admin/user/{user_id}/reconcile")
async def reconcile_trades(request: Request, user_id: int):
    from routes.admin import require_admin
    await require_admin(request)

    cfg = await db.get_user_config(user_id)
    if not cfg or not cfg.binance_api_key_enc:
        return JSONResponse({"ok": False, "error": "No API keys configured"}, status_code=400)

    client = await AsyncClient.create(
        api_key=decrypt(cfg.binance_api_key_enc),
        api_secret=decrypt(cfg.binance_api_secret_enc),
    )

    try:
        live_trades = await db.get_all_trades(user_id, is_paper=False)
        closed = [t for t in live_trades if t.result in ("win", "loss")]
        fixed = []

        for trade in closed:
            needs_fix = (
                not trade.entry_price or trade.entry_price == 0
                or not trade.exit_price or trade.exit_price == 0
                or not trade.quantity or trade.quantity == 0
            )
            if not needs_fix:
                continue

            update = {}

            if trade.entry_order_id:
                entry_fills = await _get_fills(client, trade.symbol, int(trade.entry_order_id))
                if entry_fills:
                    total_qty = sum(float(f["qty"]) for f in entry_fills)
                    avg_price = sum(float(f["price"]) * float(f["qty"]) for f in entry_fills) / total_qty if total_qty else 0
                    entry_comm = sum(float(f.get("commission", 0)) for f in entry_fills)
                    if avg_price > 0:
                        update["entry_price"] = avg_price
                    if total_qty > 0:
                        update["quantity"] = total_qty
                    if entry_comm > 0:
                        update["commission"] = entry_comm

            exit_order_id = None
            exit_is_sl = None
            for oid_str, is_sl in [(trade.sl_order_id, True), (trade.tp_order_id, False)]:
                if not oid_str:
                    continue
                order_info = await _get_order(client, trade.symbol, int(oid_str))
                if order_info and order_info.get("status") == "FILLED":
                    exit_order_id = int(oid_str)
                    exit_is_sl = is_sl
                    break

            if exit_order_id:
                exit_fills = await _get_fills(client, trade.symbol, exit_order_id)
                if exit_fills:
                    total_qty = sum(float(f["qty"]) for f in exit_fills)
                    avg_exit = sum(float(f["price"]) * float(f["qty"]) for f in exit_fills) / total_qty if total_qty else 0
                    exit_comm = sum(float(f.get("commission", 0)) for f in exit_fills)
                    if avg_exit > 0:
                        update["exit_price"] = avg_exit
                    update["result"] = "loss" if exit_is_sl else "win"
                    update["r_value"] = -1.0 if exit_is_sl else (trade.r_value or 2.0)

                    entry_p = update.get("entry_price", trade.entry_price) or 0
                    exit_p = avg_exit
                    qty = update.get("quantity", trade.quantity) or 0
                    entry_c = update.get("commission", trade.commission) or 0
                    if entry_p > 0 and qty > 0:
                        if trade.direction == "long":
                            raw_pnl = (exit_p - entry_p) * qty
                        else:
                            raw_pnl = (entry_p - exit_p) * qty
                        total_comm = entry_c + exit_comm
                        update["pnl_usdt"] = raw_pnl - total_comm
                        update["commission"] = total_comm

            if not exit_order_id and not trade.sl_order_id and not trade.tp_order_id:
                all_fills = await _get_all_fills(client, trade.symbol, trade.entry_time)
                entry_oid = int(trade.entry_order_id) if trade.entry_order_id else 0
                exit_fills = [f for f in all_fills if int(f.get("orderId", 0)) != entry_oid]
                if exit_fills:
                    total_qty = sum(float(f["qty"]) for f in exit_fills)
                    avg_exit = sum(float(f["price"]) * float(f["qty"]) for f in exit_fills) / total_qty if total_qty else 0
                    exit_comm = sum(float(f.get("commission", 0)) for f in exit_fills)
                    if avg_exit > 0:
                        update["exit_price"] = avg_exit
                        entry_p = update.get("entry_price", trade.entry_price) or 0
                        qty = update.get("quantity", trade.quantity) or total_qty
                        if entry_p > 0 and qty > 0:
                            sl_dist = abs(entry_p - trade.sl_price) if trade.sl_price else 0
                            tp_dist = abs(entry_p - trade.tp_price) if trade.tp_price else 0
                            exit_dist = abs(entry_p - avg_exit)
                            is_sl = (exit_dist - sl_dist) < (exit_dist - tp_dist) if sl_dist and tp_dist else True
                            update["result"] = "loss" if is_sl else "win"
                            update["r_value"] = -1.0 if is_sl else (trade.r_value or 2.0)
                            if trade.direction == "long":
                                raw_pnl = (avg_exit - entry_p) * qty
                            else:
                                raw_pnl = (entry_p - avg_exit) * qty
                            entry_c = update.get("commission", trade.commission) or 0
                            update["pnl_usdt"] = raw_pnl - (entry_c + exit_comm)
                            update["commission"] = entry_c + exit_comm

            if update:
                await db.update_trade(trade.id, update)
                fixed.append({"trade_id": trade.id, "updates": {k: round(v, 4) if isinstance(v, float) else v for k, v in update.items()}})
                log.info("Reconciled trade #%d for user %d: %s", trade.id, user_id, update)

        return JSONResponse({"ok": True, "fixed": fixed, "total_checked": len(closed)})

    finally:
        await client.close_connection()


async def _get_order(client: AsyncClient, symbol: str, order_id: int) -> dict | None:
    try:
        return await client.futures_get_order(symbol=symbol, orderId=order_id)
    except Exception:
        return None


async def _get_fills(client: AsyncClient, symbol: str, order_id: int) -> list[dict]:
    try:
        trades = await client.futures_account_trades(symbol=symbol)
        return [t for t in trades if int(t.get("orderId", 0)) == order_id]
    except Exception:
        return []


async def _get_all_fills(client: AsyncClient, symbol: str, after: datetime | None) -> list[dict]:
    try:
        params = {"symbol": symbol}
        if after:
            params["startTime"] = int(after.timestamp() * 1000)
        trades = await client.futures_account_trades(**params)
        return trades
    except Exception:
        return []
