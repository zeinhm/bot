from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import database as db

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
    session_user = ws.session.get("user_id") if hasattr(ws, "session") else None
    if session_user is not None and session_user != user_id:
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

async def start_ws_tasks(bot_manager) -> list[asyncio.Task]:
    return [
        asyncio.create_task(_price_stream(bot_manager)),
        asyncio.create_task(_heartbeat_loop(bot_manager)),
        asyncio.create_task(_position_poll(bot_manager)),
        asyncio.create_task(_balance_poll(bot_manager)),
        asyncio.create_task(_orderbook_stream(bot_manager)),
        asyncio.create_task(_agg_trade_stream(bot_manager)),
    ]


def _any_worker(bot_manager):
    for w in bot_manager.workers.values():
        if w.exchange.market_bsm is not None:
            return w
    return None


async def _price_stream(bot_manager):
    while True:
        try:
            worker = _any_worker(bot_manager)
            if worker is None:
                await asyncio.sleep(2)
                continue

            bsm = worker.exchange.market_bsm
            streams = [f"{s.lower()}@ticker" for s in worker.config.symbols]
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
            risk_mode = await db.get_state("risk_mode", "static")
            risk_value = await db.get_state("risk_value", 10.0)

            last_trade_ts = None
            recent = await db.get_recent_trades(1)
            if recent and recent[0].exit_time:
                last_trade_ts = int(recent[0].exit_time.timestamp())

            for user_id, conns in list(ws_manager.connections.items()):
                if not conns:
                    continue

                worker = bot_manager.get_worker(user_id)

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
                    for symbol in worker.config.symbols:
                        buf = worker.candle_buffers.get(symbol, [])
                        if buf:
                            last_ts = buf[-1].get("timestamp", 0) / 1000
                            age = int(time.time() - last_ts)
                            if last_candle_age is None or age < last_candle_age:
                                last_candle_age = age

                await ws_manager.send_to_user(user_id, {
                    "type": "heartbeat",
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
    last_sent: dict[int, str] = {}

    while True:
        try:
            await asyncio.sleep(2)
            if not ws_manager.has_connections():
                continue

            for user_id, conns in list(ws_manager.connections.items()):
                if not conns:
                    continue

                worker = bot_manager.get_worker(user_id)
                if worker is None or not worker.running:
                    continue

                try:
                    all_positions = await worker.exchange.get_all_positions()
                except Exception:
                    continue

                bot_trade = await db.get_open_trade()

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

                data = json.dumps({"type": "positions", "positions": positions})

                if data != last_sent.get(user_id):
                    await ws_manager.send_to_user(user_id, {"type": "positions", "positions": positions})
                    last_sent[user_id] = data

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

            for user_id, conns in list(ws_manager.connections.items()):
                if not conns:
                    continue

                worker = bot_manager.get_worker(user_id)
                if worker is None or not worker.running:
                    continue

                try:
                    balance = await worker.exchange.get_balance()
                    await ws_manager.send_to_user(user_id, {
                        "type": "balance",
                        "usdt_balance": round(balance, 2),
                    })
                except Exception as e:
                    log.error("Balance poll error for user %d: %s", user_id, e)

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("Balance poll error: %s", e)


async def _orderbook_stream(bot_manager):
    while True:
        try:
            worker = _any_worker(bot_manager)
            if worker is None:
                await asyncio.sleep(2)
                continue

            bsm = worker.exchange.market_bsm
            symbols = worker.config.symbols
            streams = [f"{s.lower()}@depth10" for s in symbols]
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

                    bids = [[float(p), float(q)] for p, q in d.get("b", d.get("bids", []))[:10]]
                    asks = [[float(p), float(q)] for p, q in d.get("a", d.get("asks", []))[:10]]

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


async def _agg_trade_stream(bot_manager):
    while True:
        try:
            worker = _any_worker(bot_manager)
            if worker is None:
                await asyncio.sleep(2)
                continue

            bsm = worker.exchange.market_bsm
            symbols = worker.config.symbols
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
    now = datetime.now(timezone.utc)
    est_hour = (now.hour - 5) % 24

    sessions = []
    if est_hour >= 17 or est_hour < 2:
        sessions.append("sydney")
    if est_hour >= 19 or est_hour < 4:
        sessions.append("tokyo")
    if 3 <= est_hour < 12:
        sessions.append("london")
    if 8 <= est_hour < 17:
        sessions.append("ny")

    return ",".join(sessions) if sessions else "off"
