from __future__ import annotations

import json
import logging

from binance import AsyncClient
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from app.auth import decrypt
from app.core.close_position import close_position, PositionNotFound, AmbiguousPosition
from exchange import resolve_trade_exit, r_value_for_exit, position_pnl_breakdown, fills_time
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

    # Open live positions, so an admin can force-close one when the user's own
    # SL/TP went missing. Three distinct row states, never conflated:
    #   exchange — a real position on Binance (live uPnL; `sl_resting` says
    #              whether a STOP_MARKET is actually on the book)
    #   stale    — the exchange WAS read and has no such position, but a trade
    #              row is still open: a divergence to resolve, not something to close
    #   db       — the exchange could not be read (bot stopped / unreachable), so
    #              the tracked rows stand in and nothing about them is confirmed
    open_trades = {t.symbol: t for t in await db.get_open_trades(user_id, is_paper=False)}
    open_positions = []
    exchange_read = False
    if live_worker and live_worker.exchange.client:
        try:
            exchange_positions = await live_worker.exchange.get_all_positions()
            exchange_read = True
        except Exception:
            exchange_positions = []
            log.warning("Could not read live positions for user %d", user_id, exc_info=True)
        for pos in exchange_positions:
            bt = open_trades.get(pos["symbol"])
            if bt is not None and bt.direction != pos["side"]:
                bt = None  # same symbol, other side — not this bot trade
            # Only the bot's own trades have an SL to be missing, and each probe
            # is a signed call — don't spend one per unrelated manual position.
            sl_resting = None
            if bt is not None:
                try:
                    cond = await live_worker.exchange.get_conditional_orders(pos["symbol"], strict=True)
                    # Hedge mode: a stop on the OTHER leg must not count as this
                    # leg's protection (same filter as worker_live._count_sltp_orders).
                    expected_ps = (
                        ("LONG" if pos["side"] == "long" else "SHORT")
                        if live_worker.exchange.hedge_mode else None
                    )
                    sl_resting = any(
                        (o.get("orderType") or o.get("type")) == "STOP_MARKET"
                        and (expected_ps is None or o.get("positionSide") == expected_ps)
                        for o in cond)
                except Exception:
                    pass  # unreadable -> unknown, shown as such
            open_positions.append({
                "symbol": pos["symbol"],
                "direction": pos["side"],
                "quantity": pos["quantity"],
                "entry_price": pos["entry_price"],
                "unrealized_pnl": pos["unrealized_pnl"],
                "sl_price": bt.sl_price if bt else None,
                "tp_price": bt.tp_price if bt else None,
                "trade_id": bt.id if bt else None,
                "sl_resting": sl_resting,
                "source": "exchange",
            })

    on_exchange = {(p["symbol"], p["direction"]) for p in open_positions}
    for t in open_trades.values():
        if (t.symbol, t.direction) in on_exchange:
            continue
        open_positions.append({
            "symbol": t.symbol,
            "direction": t.direction,
            "quantity": t.quantity,
            "entry_price": t.entry_price,
            "unrealized_pnl": None,
            "sl_price": t.sl_price,
            "tp_price": t.tp_price,
            "trade_id": t.id,
            "sl_resting": None,
            "source": "stale" if exchange_read else "db",
        })

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
        "open_positions": open_positions,
        "live_balance": live_balance,
        "paper_balance": paper_balance,
        "recent_trades": recent_trades,
        "events": events,
        "avg_hold_mins": avg_hold_mins,
        "equity_json": json.dumps(live_stats["equity_data"]),
    })


@router.post("/admin/user/{user_id}/position/close")
async def admin_close_position(request: Request, user_id: int):
    """Admin force-close of ONE of a user's live positions.

    The owner's own close button (`POST /api/position/close`) needs their
    session, so when their SL/TP goes missing an admin previously had no way to
    get them out — the only lever was the user's own Binance app. This closes
    from the server (whose IP is whitelisted on the user's key) and books the
    trade the same way the owner's close does.

    Prefers the running live worker's exchange so position mode and in-memory
    SL/TP alert timers stay consistent; falls back to a one-off connection from
    the stored keys when that user's bot is stopped.
    """
    from routes.admin import require_admin
    admin = await require_admin(request)

    try:
        body = await request.json()
    except Exception:
        body = {}
    symbol = (body.get("symbol") or "").upper()
    if not symbol:
        return JSONResponse({"ok": False, "error": "Missing symbol"}, status_code=400)
    direction = (body.get("direction") or "").lower() or None
    if direction not in (None, "long", "short"):
        return JSONResponse({"ok": False, "error": "Invalid direction"}, status_code=400)

    manager = request.app.state.bot_manager
    worker = manager.get_worker(user_id, "live")
    # Decided ONCE: re-reading worker.exchange.client could flip (the bot may be
    # stopped mid-request), which would reach the keys branch with no cfg loaded.
    use_worker_exchange = bool(worker and worker.exchange.client)

    cfg = None
    if not use_worker_exchange:
        cfg = await db.get_user_config(user_id)
        if not cfg or not cfg.binance_api_key_enc:
            return JSONResponse(
                {"ok": False, "error": "User has no API keys and no running bot"}, status_code=400)

    temp_exchange = None
    try:
        if use_worker_exchange:
            exchange = worker.exchange
        else:
            from exchange import BinanceExchange
            # Built and connected inside the try so a bad/revoked key (-2015)
            # still unwinds through finally instead of leaking its HTTP session.
            temp_exchange = BinanceExchange(
                decrypt(cfg.binance_api_key_enc), decrypt(cfg.binance_api_secret_enc))
            await temp_exchange.connect_minimal()
            exchange = temp_exchange

        closed = await close_position(
            user_id, symbol, exchange=exchange, is_paper=False,
            direction=direction, worker=worker,
            reason=f"Admin force-close (by #{admin.id})",
        )
        log.warning("Admin %d force-closed %s for user %d: %s", admin.id, symbol, user_id, closed)
        return JSONResponse({"ok": True, **closed})
    except PositionNotFound as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
    except AmbiguousPosition as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=409)
    except Exception as e:
        log.exception("Admin force-close failed for user %d %s", user_id, symbol)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    finally:
        if temp_exchange is not None:
            await temp_exchange.close()


@router.post("/admin/user/{user_id}/reset-2fa")
async def reset_user_2fa(request: Request, user_id: int):
    """Admin recovery: clear a user's 2FA so they can re-enroll (lost device)."""
    from routes.admin import require_admin
    admin = await require_admin(request)

    from sqlalchemy import update
    from app.db.models import User
    async with db.get_session() as session:
        await session.execute(
            update(User).where(User.id == user_id).values(
                totp_secret_enc=None, totp_enabled=False, totp_backup_codes=None)
        )
        await session.commit()

    log.info("Admin %d reset 2FA for user %d", admin.id, user_id)
    await db.log_event(f"Admin reset 2FA for user #{user_id}", level="warn", category="system")
    return JSONResponse({"ok": True})


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
            update = {}

            # Reconcile entry from real fills (entry is a regular market order).
            if trade.entry_order_id:
                entry_fills = await _get_fills(client, trade.symbol, int(trade.entry_order_id))
                if entry_fills:
                    total_qty = sum(float(f["qty"]) for f in entry_fills)
                    avg_price = sum(float(f["price"]) * float(f["qty"]) for f in entry_fills) / total_qty if total_qty else 0
                    if avg_price > 0 and abs((trade.entry_price or 0) - avg_price) > 0.01:
                        update["entry_price"] = avg_price
                    if total_qty > 0 and abs((trade.quantity or 0) - total_qty) > 0.0001:
                        update["quantity"] = total_qty
                    et = fills_time(entry_fills)
                    if et and (trade.entry_time is None or abs((trade.entry_time - et).total_seconds()) > 2):
                        update["entry_time"] = et

            # Resolve the real exit from account fills. SL/TP are conditional/algo
            # orders whose ids can't be resolved with futures_get_order, so verify
            # win/loss from the actual closing fills (realizedPnl) instead.
            exit_info = await resolve_trade_exit(
                client, trade.symbol, trade.direction,
                trade.entry_order_id, trade.entry_time,
                update.get("quantity", trade.quantity),
                trade.sl_price, trade.tp_price,
            )
            if exit_info:
                exit_p = exit_info["exit_price"]
                entry_p = update.get("entry_price", trade.entry_price) or 0
                if exit_p > 0 and abs((trade.exit_price or 0) - exit_p) > 0.01:
                    update["exit_price"] = exit_p
                correct_result = "loss" if exit_info["is_sl"] else "win"
                if trade.result != correct_result:
                    update["result"] = correct_result
                    update["r_value"] = r_value_for_exit(exit_info["is_sl"], entry_p, trade.sl_price, trade.tp_price, trade.target_rr)
                xt = exit_info.get("exit_time")
                if xt and (trade.exit_time is None or abs((trade.exit_time - xt).total_seconds()) > 2):
                    update["exit_time"] = xt

            # Net PnL + fees from Binance's income ledger (verified numbers).
            breakdown = await position_pnl_breakdown(client, trade.symbol, trade.entry_time, trade.exit_time)
            if breakdown is not None:
                if trade.pnl_usdt is None or abs((trade.pnl_usdt or 0) - breakdown["net_pnl"]) > 0.005:
                    update["pnl_usdt"] = breakdown["net_pnl"]
                if abs((trade.commission or 0) - breakdown["commission"]) > 0.0001:
                    update["commission"] = breakdown["commission"]
                if abs((trade.funding_fee or 0) - breakdown["funding_fee"]) > 0.0001:
                    update["funding_fee"] = breakdown["funding_fee"]

            if update:
                await db.update_trade(trade.id, update)
                fixed.append({"trade_id": trade.id, "updates": {k: round(v, 4) if isinstance(v, float) else v for k, v in update.items()}})
                log.info("Reconciled trade #%d for user %d: %s", trade.id, user_id, update)

        return JSONResponse({"ok": True, "fixed": fixed, "total_checked": len(closed)})

    finally:
        await client.close_connection()


async def _get_fills(client: AsyncClient, symbol: str, order_id: int) -> list[dict]:
    try:
        trades = await client.futures_account_trades(symbol=symbol)
        return [t for t in trades if int(t.get("orderId", 0)) == order_id]
    except Exception:
        return []
