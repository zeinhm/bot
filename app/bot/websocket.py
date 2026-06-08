from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import app.db as db

log = logging.getLogger(__name__)

router = APIRouter()

_latest_prices: dict[str, dict] = {}


class ConnectionManager:
    def __init__(self):
        self.connections: dict[int, set[WebSocket]] = {}

    async def connect(self, user_id: int, ws: WebSocket):
        await ws.accept()
        self.connections.setdefault(user_id, set()).add(ws)
        log.info("WS connected: user=%d (total=%d)", user_id, len(self.connections.get(user_id, set())))

    def disconnect(self, user_id: int, ws: WebSocket):
        if user_id in self.connections:
            self.connections[user_id].discard(ws)
            log.info("WS disconnected: user=%d (total=%d)", user_id, len(self.connections[user_id]))

    async def send_to_user(self, user_id: int, data: dict):
        conns = self.connections.get(user_id, set())
        if not conns:
            return
        msg = json.dumps(data)
        dead = set()
        for ws in conns:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        conns -= dead

    async def broadcast_all(self, data: dict):
        for user_id in list(self.connections.keys()):
            await self.send_to_user(user_id, data)

    def has_connections(self) -> bool:
        return any(len(conns) > 0 for conns in self.connections.values())


ws_manager = ConnectionManager()


@router.websocket("/ws/{user_id}")
async def websocket_endpoint(ws: WebSocket, user_id: int):
    has_session = "session" in ws.scope
    session_data = dict(ws.scope.get("session", {})) if has_session else {}
    session_user = session_data.get("user_id")
    cookie_header = next((v.decode() for k, v in ws.scope.get("headers", []) if k == b"cookie"), "")
    has_session_cookie = "session" in cookie_header.lower()
    if session_user is None or session_user != user_id:
        log.warning("WS auth rejected: path=/ws/%d session_user=%s has_session_scope=%s has_cookie=%s session_keys=%s",
                     user_id, session_user, has_session, has_session_cookie, list(session_data.keys()))
        await ws.close(code=4003)
        return

    await ws_manager.connect(user_id, ws)
    try:
        while True:
            text = await ws.receive_text()
            try:
                msg = json.loads(text)
                if msg.get("type") == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(user_id, ws)


# --- Background push tasks ---

async def start_ws_tasks(bot_manager, shared_market=None) -> list[asyncio.Task]:
    return [
        asyncio.create_task(_price_stream(bot_manager, shared_market)),
        asyncio.create_task(_heartbeat_loop(bot_manager)),
        asyncio.create_task(_position_poll(bot_manager)),
        asyncio.create_task(_balance_poll(bot_manager)),
        asyncio.create_task(_orderbook_stream(bot_manager, shared_market)),
        asyncio.create_task(_agg_trade_stream(bot_manager, shared_market)),
        asyncio.create_task(_trade_anomaly_scanner(bot_manager)),
    ]


async def _price_stream(bot_manager, shared_market=None):
    while True:
        try:
            if shared_market is None or shared_market.market_bsm is None:
                await asyncio.sleep(2)
                continue

            from config import SYMBOLS
            bsm = shared_market.market_bsm
            streams = [f"{s.lower()}@ticker" for s in SYMBOLS]
            socket = bsm.futures_multiplex_socket(streams=streams)

            async with socket as stream:
                while True:
                    msg = await stream.recv()
                    if not msg or "data" not in msg:
                        continue

                    d = msg["data"]
                    symbol = d.get("s", "")
                    price = float(d.get("c", 0))
                    change_24h = float(d.get("p", 0))
                    change_pct = float(d.get("P", 0))

                    high_24h = float(d.get("h", 0))
                    low_24h = float(d.get("l", 0))
                    vol_base = float(d.get("v", 0))
                    vol_quote = float(d.get("q", 0))

                    _latest_prices[symbol] = {
                        "price": price,
                        "change_24h": change_24h,
                        "change_pct_24h": change_pct,
                    }

                    shared_market.update_price(symbol, price)

                    if not ws_manager.has_connections():
                        continue

                    await ws_manager.broadcast_all({
                        "type": "price",
                        "symbol": symbol,
                        "price": price,
                        "change_24h": change_24h,
                        "change_pct_24h": change_pct,
                        "high_24h": high_24h,
                        "low_24h": low_24h,
                        "vol_base": round(vol_base, 2),
                        "vol_quote": round(vol_quote, 2),
                    })

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("Price stream error: %s", e)
            await asyncio.sleep(5)


async def _heartbeat_loop(bot_manager):
    first = True
    while True:
        try:
            if first:
                await asyncio.sleep(2)
                first = False
            else:
                await asyncio.sleep(15)
            if not ws_manager.has_connections():
                continue

            session = _get_current_session()

            for user_id, conns in list(ws_manager.connections.items()):
                if not conns:
                    continue

                for mode in ("paper", "live"):
                    worker = bot_manager.get_worker(user_id, mode)
                    is_paper = (mode == "paper")

                    risk_mode = await db.get_state("risk_mode", "static", user_id=user_id, is_paper=is_paper)
                    risk_value = await db.get_state("risk_value", 10.0, user_id=user_id, is_paper=is_paper)

                    last_trade_ts = None
                    recent = await db.get_recent_trades(1, user_id=user_id, is_paper=is_paper)
                    if recent and recent[0].exit_time:
                        last_trade_ts = int(recent[0].exit_time.timestamp())

                    last_candle_age = None
                    bot_status = "stopped"
                    uptime_secs = None
                    api_status = "disconnected"

                    if worker:
                        bot_status = worker.status
                        if worker.exchange.client is not None:
                            api_status = "connected"
                        if worker.started_at:
                            uptime_secs = int(time.time() - worker.started_at)
                        if worker._shared_market:
                            for symbol in worker.config.symbols:
                                buf = worker._shared_market.get_candles(symbol)
                                if buf:
                                    last_ts = buf[-1].get("timestamp", 0) / 1000
                                    age = int(time.time() - last_ts)
                                    if last_candle_age is None or age < last_candle_age:
                                        last_candle_age = age

                    await ws_manager.send_to_user(user_id, {
                        "type": "heartbeat",
                        "mode": mode,
                        "timestamp": int(time.time()),
                        "last_candle_age_secs": last_candle_age,
                        "session": session,
                        "bot_status": bot_status,
                        "api_status": api_status,
                        "uptime_secs": uptime_secs,
                        "risk_mode": risk_mode,
                        "risk_value": risk_value,
                        "last_trade_ts": last_trade_ts,
                    })

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("Heartbeat error: %s", e)


async def _position_poll(bot_manager):
    last_sent: dict[tuple, str] = {}

    while True:
        try:
            await asyncio.sleep(2)
            if not ws_manager.has_connections():
                continue

            for (uid, mode), worker in list(bot_manager.workers.items()):
                if not worker.running:
                    continue
                conns = ws_manager.connections.get(uid)
                if not conns:
                    continue

                is_paper = (mode == "paper")

                try:
                    all_positions = await worker.exchange.get_all_positions()
                except Exception:
                    continue

                bot_trade = await db.get_open_trade(uid, is_paper)

                positions = []
                for pos in all_positions:
                    current_price = _latest_prices.get(pos["symbol"], {}).get("price", pos["entry_price"])

                    is_bot = (
                        bot_trade is not None
                        and bot_trade.symbol == pos["symbol"]
                        and bot_trade.direction == pos["side"]
                    )

                    p = {
                        "symbol": pos["symbol"],
                        "direction": pos["side"],
                        "entry_price": pos["entry_price"],
                        "current_price": round(current_price, 2),
                        "quantity": pos["quantity"],
                        "unrealised_pnl_usdt": round(pos["unrealized_pnl"], 2),
                        "source": "bot" if is_bot else "manual",
                    }

                    if is_bot:
                        sl_dist = abs(bot_trade.entry_price - bot_trade.sl_price)
                        risk_amt = sl_dist * bot_trade.quantity
                        p["unrealised_r"] = round(pos["unrealized_pnl"] / risk_amt, 2) if risk_amt > 0 else 0.0
                        p["sl_price"] = bot_trade.sl_price
                        p["tp_price"] = bot_trade.tp_price
                        if bot_trade.entry_time:
                            delta = datetime.now(timezone.utc) - bot_trade.entry_time
                            p["time_in_trade_secs"] = int(delta.total_seconds())

                    positions.append(p)

                data = json.dumps({"type": "positions", "mode": mode, "positions": positions})
                cache_key = (uid, mode)

                if data != last_sent.get(cache_key):
                    await ws_manager.send_to_user(uid, {"type": "positions", "mode": mode, "positions": positions})
                    last_sent[cache_key] = data

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("Position poll error: %s", e)
            await asyncio.sleep(5)


async def _balance_poll(bot_manager):
    while True:
        try:
            await asyncio.sleep(30)
            if not ws_manager.has_connections():
                continue

            for (uid, mode), worker in list(bot_manager.workers.items()):
                if not worker.running:
                    continue
                conns = ws_manager.connections.get(uid)
                if not conns:
                    continue

                try:
                    balance = await worker.exchange.get_balance()
                    await ws_manager.send_to_user(uid, {
                        "type": "balance",
                        "mode": mode,
                        "usdt_balance": round(balance, 2),
                    })
                    await db.set_state("last_balance", round(balance, 2), user_id=uid, is_paper=(mode == "paper"))
                except Exception as e:
                    log.error("Balance poll error for user %d/%s: %s", uid, mode, e)

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("Balance poll error: %s", e)


async def _orderbook_stream(bot_manager, shared_market=None):
    while True:
        try:
            if shared_market is None or shared_market.market_bsm is None:
                await asyncio.sleep(2)
                continue

            from config import SYMBOLS
            bsm = shared_market.market_bsm
            symbols = SYMBOLS
            streams = [f"{s.lower()}@depth20" for s in symbols]
            log.info("Orderbook: connecting depth stream for %s", symbols)
            socket = bsm.futures_multiplex_socket(streams=streams, category="public")

            async with socket as stream:
                log.info("Orderbook: stream connected")
                last_send: dict[str, float] = {}
                while True:
                    msg = await stream.recv()
                    if not msg:
                        continue

                    d = msg.get("data", msg)
                    symbol = d.get("s", "")

                    now = time.time()
                    if now - last_send.get(symbol, 0) < 0.5:
                        continue
                    last_send[symbol] = now

                    if not ws_manager.has_connections():
                        continue

                    bids = [[float(p), float(q)] for p, q in d.get("b", d.get("bids", []))[:20]]
                    asks = [[float(p), float(q)] for p, q in d.get("a", d.get("asks", []))[:20]]

                    await ws_manager.broadcast_all({
                        "type": "orderbook",
                        "symbol": symbol,
                        "bids": bids,
                        "asks": asks,
                    })

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("Orderbook stream error: %s", e, exc_info=True)
            await asyncio.sleep(5)


async def _agg_trade_stream(bot_manager, shared_market=None):
    while True:
        try:
            if shared_market is None or shared_market.market_bsm is None:
                await asyncio.sleep(2)
                continue

            from config import SYMBOLS
            bsm = shared_market.market_bsm
            symbols = SYMBOLS
            streams = [f"{s.lower()}@aggTrade" for s in symbols]
            socket = bsm.futures_multiplex_socket(streams=streams)

            async with socket as stream:
                while True:
                    msg = await stream.recv()
                    if not msg or "data" not in msg:
                        continue

                    if not ws_manager.has_connections():
                        continue

                    d = msg["data"]
                    await ws_manager.broadcast_all({
                        "type": "agg_trade",
                        "symbol": d.get("s", ""),
                        "price": float(d.get("p", 0)),
                        "qty": float(d.get("q", 0)),
                        "side": "sell" if d.get("m", False) else "buy",
                        "time": d.get("T", 0),
                    })

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("AggTrade stream error: %s", e)
            await asyncio.sleep(5)


def _get_current_session() -> str:
    from zoneinfo import ZoneInfo
    ny_hour = datetime.now(ZoneInfo("America/New_York")).hour

    sessions = []
    if ny_hour >= 17 or ny_hour < 2:
        sessions.append("sydney")
    if ny_hour >= 19 or ny_hour < 4:
        sessions.append("tokyo")
    if 3 <= ny_hour < 12:
        sessions.append("london")
    if 8 <= ny_hour < 17:
        sessions.append("ny")

    return ",".join(sessions) if sessions else "off"


async def _trade_anomaly_scanner(bot_manager):
    await asyncio.sleep(60)
    while True:
        try:
            from datetime import timedelta
            from binance import AsyncClient
            from app.auth import decrypt
            from config import COMMISSION_PCT

            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            all_users = await db.get_all_approved_users()

            for user in all_users:
                trades = await db.get_all_trades(user.id, is_paper=False)
                recent_closed = [
                    t for t in trades
                    if t.result in ("win", "loss")
                    and t.exit_time and t.exit_time >= cutoff
                ]
                if not recent_closed:
                    continue

                cfg = await db.get_user_config(user.id)
                if not cfg or not cfg.binance_api_key_enc:
                    continue

                client = await AsyncClient.create(
                    api_key=decrypt(cfg.binance_api_key_enc),
                    api_secret=decrypt(cfg.binance_api_secret_enc),
                )
                try:
                    for trade in recent_closed:
                        try:
                            update = {}
                            binance_entry_price = None
                            binance_qty = None
                            binance_entry_comm = 0.0

                            if trade.entry_order_id:
                                fills = await client.futures_account_trades(symbol=trade.symbol)
                                entry_fills = [f for f in fills if int(f.get("orderId", 0)) == int(trade.entry_order_id)]
                                if entry_fills:
                                    binance_qty = sum(float(f["qty"]) for f in entry_fills)
                                    binance_entry_price = sum(float(f["price"]) * float(f["qty"]) for f in entry_fills) / binance_qty if binance_qty else 0
                                    binance_entry_comm = sum(float(f.get("commission", 0)) for f in entry_fills)

                                    if binance_entry_price > 0 and abs((trade.entry_price or 0) - binance_entry_price) > 0.01:
                                        update["entry_price"] = binance_entry_price
                                    if binance_qty > 0 and abs((trade.quantity or 0) - binance_qty) > 0.0001:
                                        update["quantity"] = binance_qty

                            binance_exit_price = None
                            binance_exit_comm = 0.0
                            binance_is_sl = None

                            for oid_str, is_sl in [(trade.sl_order_id, True), (trade.tp_order_id, False)]:
                                if not oid_str:
                                    continue
                                order_info = await client.futures_get_order(symbol=trade.symbol, orderId=int(oid_str))
                                if order_info and order_info.get("status") == "FILLED":
                                    exit_fills = await client.futures_account_trades(symbol=trade.symbol)
                                    exit_fills = [f for f in exit_fills if int(f.get("orderId", 0)) == int(oid_str)]
                                    if exit_fills:
                                        total_qty = sum(float(f["qty"]) for f in exit_fills)
                                        binance_exit_price = sum(float(f["price"]) * float(f["qty"]) for f in exit_fills) / total_qty if total_qty else 0
                                        binance_exit_comm = sum(float(f.get("commission", 0)) for f in exit_fills)
                                        binance_is_sl = is_sl
                                    break

                            if binance_exit_price and binance_exit_price > 0:
                                if abs((trade.exit_price or 0) - binance_exit_price) > 0.01:
                                    update["exit_price"] = binance_exit_price
                                correct_result = "loss" if binance_is_sl else "win"
                                if trade.result != correct_result:
                                    update["result"] = correct_result
                                    update["r_value"] = -1.0 if binance_is_sl else (trade.r_value or 2.0)

                            entry_p = update.get("entry_price", trade.entry_price) or (binance_entry_price or 0)
                            exit_p = update.get("exit_price", trade.exit_price) or (binance_exit_price or 0)
                            qty = update.get("quantity", trade.quantity) or (binance_qty or 0)
                            if entry_p > 0 and exit_p > 0 and qty > 0:
                                if trade.direction == "long":
                                    raw_pnl = (exit_p - entry_p) * qty
                                else:
                                    raw_pnl = (entry_p - exit_p) * qty
                                total_comm = (binance_entry_comm or trade.commission or 0) + binance_exit_comm
                                correct_pnl = raw_pnl - total_comm
                                if trade.pnl_usdt is None or abs((trade.pnl_usdt or 0) - correct_pnl) > 0.01:
                                    update["pnl_usdt"] = correct_pnl
                                    update["commission"] = total_comm

                            if update:
                                await db.update_trade(trade.id, update)
                                log.warning("Anomaly scanner: fixed trade #%d — %s", trade.id, update)
                                await db.log_event(
                                    f"Anomaly scanner fixed trade #{trade.id}: {', '.join(update.keys())}",
                                    level="warn", category="system",
                                    user_id=user.id, is_paper=False,
                                )
                        except Exception as e:
                            log.error("Anomaly scanner: failed on trade #%d: %s", trade.id, e)
                finally:
                    await client.close_connection()

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("Anomaly scanner error: %s", e)

        await asyncio.sleep(300)
