from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Awaitable, TYPE_CHECKING

from config import ACC_RANGE_MODE
from strategy import check_signal
from exchange import r_value_for_exit
import app.db as db

if TYPE_CHECKING:
    from app.bot.shared_market import SharedMarketData

log = logging.getLogger(__name__)


@dataclass
class BotConfig:
    api_key: str
    api_secret: str
    symbols: list[str]
    strategy_params: dict
    leverage: int
    commission_pct: float
    slippage_ticks: int
    tick_size: dict[str, float]
    lot_size: dict[str, float]
    candle_buffer_size: int
    telegram_chat_id: str = ""
    telegram_owner_id: str = ""
    loss_streak_threshold: int = 4
    reduced_risk_pct: float = 0.25
    wins_to_recover: int = 2
    is_paper: bool = False
    paper_balance: float = 10000.0


class BaseWorker:
    """Mode-agnostic trading worker: candle → signal → execute → monitor.

    Everything mode-specific (Binance vs paper) is delegated to hook methods that
    LiveWorker / PaperWorker override, so the live (real-money) path and the paper
    path are fully separated with no `is_paper` branching in the shared logic.
    """

    # Overridden by subclasses; drives _mode_label and the is_paper flag.
    MODE: str = "live"

    def __init__(
        self,
        user_id: int,
        config: BotConfig,
        broadcast_fn: Callable[[dict], Awaitable[None]] | None = None,
        shared_market: SharedMarketData | None = None,
    ):
        self.user_id = user_id
        self.config = config
        self.is_paper = self.MODE == "paper"

        self._shared_market = shared_market

        self.exchange = self._create_exchange()
        self.running = False
        self.status: str = "stopped"
        self.started_at: float | None = None
        self.last_error: str | None = None
        self.last_error_time: float | None = None
        # Per-asset open trades: symbol -> trade_id. One open trade per symbol
        # (BTC/ETH/SOL can run concurrently); no global cap.
        self._active_trades: dict[str, int] = {}
        # Per-symbol SL/TP recovery timers
        self._sltp_missing_since: dict[str, float] = {}
        self._sltp_last_alert: dict[str, float] = {}
        self._broadcast_fn = broadcast_fn

        self.current_streak: int = 0
        self.adaptive_active: bool = False
        self.consecutive_wins: int = 0

        self._tasks: list[asyncio.Task] = []

    def _mode_label(self) -> str:
        return self.MODE

    async def start(self):
        self.status = "starting"
        log.info("BotWorker[user=%d/%s] starting...", self.user_id, self._mode_label())
        await self.exchange.connect()

        # Apply the user's chosen leverage (falls back to the config default when unset).
        lev = await db.get_state("leverage", self.config.leverage, user_id=self.user_id, is_paper=self.is_paper)
        try:
            await self.exchange.set_leverage(lev)
        except Exception as e:
            log.warning("BotWorker[user=%d/%s] failed to apply leverage %s: %s", self.user_id, self._mode_label(), lev, e)

        enabled = await db.get_state("bot_enabled", True, user_id=self.user_id, is_paper=self.is_paper)
        if not enabled:
            log.info("BotWorker[user=%d/%s] disabled in settings, monitor-only mode", self.user_id, self._mode_label())

        await self._crash_recovery()

        self.running = True
        self.status = "running"
        self.started_at = time.time()
        self.last_error = None
        self.last_error_time = None
        log.info("BotWorker[user=%d/%s] started — listening for candles", self.user_id, self._mode_label())
        await db.log_event("Bot started", category="system", user_id=self.user_id, is_paper=self.is_paper)
        await self._alert_started()

        if self._shared_market:
            self._shared_market.on_candle_close(self._on_shared_candle)

        self._tasks = [
            asyncio.create_task(self._run_user_stream()),
            asyncio.create_task(self._run_position_poll()),
        ]
        await asyncio.gather(*self._tasks)

    async def stop(self):
        self.running = False
        self.status = "stopped"
        if self._shared_market:
            self._shared_market.remove_candle_callback(self._on_shared_candle)
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        await db.log_event("Bot stopped", category="system", user_id=self.user_id, is_paper=self.is_paper)
        await self._alert_stopped("shutdown")
        await self.exchange.close()
        log.info("BotWorker[user=%d/%s] stopped", self.user_id, self._mode_label())

    def _get_effective_risk(self) -> float:
        if self.adaptive_active:
            return self.config.reduced_risk_pct
        return None

    def _on_trade_result(self, won: bool):
        if not won:
            self.current_streak += 1
            self.consecutive_wins = 0
            if self.current_streak >= self.config.loss_streak_threshold:
                if not self.adaptive_active:
                    self.adaptive_active = True
                    log.info(
                        "BotWorker[user=%d/%s] adaptive sizing ACTIVATED after %d consecutive losses",
                        self.user_id, self._mode_label(), self.current_streak,
                    )
                    asyncio.create_task(self._broadcast({
                        "type": "adaptive_sizing",
                        "active": True,
                        "streak": self.current_streak,
                        "wins_needed": self.config.wins_to_recover,
                    }))
        else:
            self.current_streak = 0
            if self.adaptive_active:
                self.consecutive_wins += 1
                if self.consecutive_wins >= self.config.wins_to_recover:
                    self.adaptive_active = False
                    self.consecutive_wins = 0
                    log.info(
                        "BotWorker[user=%d/%s] adaptive sizing DEACTIVATED after %d wins",
                        self.user_id, self._mode_label(), self.config.wins_to_recover,
                    )
                    asyncio.create_task(self._broadcast({
                        "type": "adaptive_sizing",
                        "active": False,
                        "streak": 0,
                        "wins_needed": 0,
                    }))

    async def _broadcast(self, data: dict):
        if self._broadcast_fn:
            await self._broadcast_fn(data)

    # --- Mode hooks (overridden by LiveWorker / PaperWorker) ---
    # Data hooks: each subclass MUST implement these.
    def _create_exchange(self):
        raise NotImplementedError

    async def _resolve_exit(self, trade) -> tuple[float, bool, float] | None:
        """How a closed position exited: (exit_price, is_sl, exit_commission) or None."""
        raise NotImplementedError

    async def _count_active_sltp(self, trade) -> tuple[int, int] | None:
        """(sl_count, tp_count) of live SL/TP orders, or None if unreadable."""
        raise NotImplementedError

    # Value hooks: default = simple computed values (paper); live reads real fills.
    async def _entry_commission(self, symbol, order, fill_price, fill_qty) -> float:
        return fill_price * fill_qty * self.config.commission_pct

    async def _exit_fill(self, symbol, order_id, fill_price, qty) -> tuple[float, float]:
        return fill_price, fill_price * qty * self.config.commission_pct

    # Side-effect hooks: default = no-op (paper); live overrides with the real work.
    async def _alert_started(self):
        pass

    async def _alert_stopped(self, reason: str):
        pass

    async def _alert_entry(self, symbol, signal, fill_price, fill_qty, balance):
        pass

    async def _on_trade_closed(self, trade, result, exit_price, pnl, r_value, balance):
        pass

    async def _sweep_orphan_positions(self, open_trades):
        pass

    async def _prepare_entry(self, symbol):
        pass

    async def _before_place_sl_tp(self, symbol):
        pass

    async def _poll_open_position(self, trade, pos):
        pass

    # --- Startup ---

    async def _crash_recovery(self):
        log.info("Running crash recovery check...")

        open_trades = await db.get_open_trades(self.user_id, self.is_paper)
        self._active_trades = {t.symbol: t.id for t in open_trades}

        for open_trade in open_trades:
            log.info("Found open trade #%d %s %s in DB", open_trade.id, open_trade.symbol, open_trade.direction)
            try:
                await self._recover_one_trade(open_trade)
            except Exception as e:
                log.error("Crash recovery failed for %s #%d: %s", open_trade.symbol, open_trade.id, e)

        # Sweep orphan positions on symbols that have no DB trade at all.
        # Use the original open-trades snapshot (NOT self._active_trades, which
        # _recover_one_trade may have popped on a transient get_position miss) —
        # otherwise a glitchy read could wrongly orphan-close a live position.
        await self._sweep_orphan_positions(open_trades)

        log.info("Crash recovery complete — %d open trade(s) recovered", len(self._active_trades))

    async def _recover_one_trade(self, open_trade):
        symbol = open_trade.symbol
        pos = await self.exchange.get_position(symbol)
        if pos is None:
            log.warning("Trade #%d is open in DB but no position — marking closed", open_trade.id)

            exit_info = await self._resolve_exit(open_trade)
            if exit_info is not None:
                exit_price, is_sl, exit_comm = exit_info
            else:
                exit_price, is_sl, exit_comm = 0.0, True, 0.0

            result = "loss" if is_sl else "win"
            rr = self.config.strategy_params.get("rrr", 2.0)
            r_value = r_value_for_exit(is_sl, open_trade.entry_price, open_trade.sl_price, open_trade.tp_price, rr)
            total_comm = (open_trade.commission or 0) + exit_comm

            if exit_price > 0:
                if open_trade.direction == "long":
                    raw_pnl = (exit_price - open_trade.entry_price) * open_trade.quantity
                else:
                    raw_pnl = (open_trade.entry_price - exit_price) * open_trade.quantity
                pnl = raw_pnl - total_comm
            else:
                pnl = 0.0

            await db.log_event(
                f"Orphan trade #{open_trade.id} closed during recovery",
                level="warn", category="system",
                user_id=self.user_id, is_paper=self.is_paper,
            )
            await db.update_trade(open_trade.id, {
                "result": result,
                "r_value": r_value,
                "exit_time": datetime.now(timezone.utc),
                "exit_price": exit_price if exit_price > 0 else None,
                "pnl_usdt": pnl,
                "commission": total_comm,
            })
            self._active_trades.pop(symbol, None)
        else:
            counts = await self._count_active_sltp(open_trade)
            if counts is None:
                log.warning("Crash recovery: could not read orders for #%d — leaving as-is", open_trade.id)
                return
            sl_count, tp_count = counts
            if sl_count != 1 or tp_count != 1:
                log.warning("Trade #%d has %d SL / %d TP orders (need 1/1) — cancelling all and re-placing",
                            open_trade.id, sl_count, tp_count)
                await self.exchange.cancel_all_orders(symbol)
                await self._place_sl_tp(
                    symbol, open_trade.direction,
                    pos["quantity"], open_trade.sl_price, open_trade.tp_price,
                    open_trade.id,
                )

    # --- Candle handling (shared) ---

    async def _on_shared_candle(self, symbol: str, candle: dict):
        if not self.running:
            return
        if symbol not in self.config.symbols:
            return
        await self._process_candle(symbol)

    async def _process_candle(self, symbol: str):
        now = datetime.now(timezone.utc)

        # Seasonal events are per-user, independently toggleable (default on = today's
        # behavior): "Sell in May" → skip May; "US tax deadline" → skip the April tax weeks.
        skip_may = await db.get_state("skip_may", True, user_id=self.user_id, is_paper=self.is_paper)
        skip_tax = await db.get_state("skip_tax_deadline", True, user_id=self.user_id, is_paper=self.is_paper)
        skip_months = self.config.strategy_params.get("skip_months", []) if skip_may else []
        skip_weeks = self.config.strategy_params.get("skip_weeks", {}) if skip_tax else {}

        if now.month in skip_months:
            return

        bot_enabled = await db.get_state("bot_enabled", True, user_id=self.user_id, is_paper=self.is_paper)
        if not bot_enabled:
            return

        active_symbols = await db.get_state("active_symbols", self.config.symbols, user_id=self.user_id, is_paper=self.is_paper)
        if symbol not in active_symbols:
            return

        # One open trade per asset: block only if THIS symbol already has one.
        # Other symbols can still open (BTC/ETH/SOL run concurrently). Checked
        # against the DB (source of truth) to avoid any in-memory race.
        if symbol in self._active_trades:
            return
        if await db.get_open_trade_for_symbol(self.user_id, symbol, self.is_paper) is not None:
            return

        # No daily trade cap — adaptive sizing handles drawdown/loss-streaks.

        rr = await db.get_state("rr_ratio", self.config.strategy_params["rrr"], user_id=self.user_id, is_paper=self.is_paper)
        sessions = await db.get_state("active_sessions", self.config.strategy_params["sessions"], user_id=self.user_id, is_paper=self.is_paper)
        params = {
            **self.config.strategy_params,
            "rrr": rr,
            "sessions": sessions,
            "skip_months": skip_months,
            "skip_weeks": skip_weeks,
            "acc_range_mode": ACC_RANGE_MODE.get(symbol, "wick"),
        }

        candles = self._shared_market.get_candles(symbol) if self._shared_market else []
        signal = check_signal(candles, params)

        if signal is None:
            return

        log.info("[%s] SIGNAL: %s %s entry=%.2f sl=%.2f tp=%.2f",
                 self._mode_label(), signal["direction"], symbol, signal["entry_price"], signal["sl"], signal["tp"])

        await self._execute_trade(symbol, signal)

    # --- Trade Execution ---

    async def _execute_trade(self, symbol: str, signal: dict):
        try:
            risk_mode = await db.get_state("risk_mode", "static", user_id=self.user_id, is_paper=self.is_paper)
            risk_value = await db.get_state("risk_value", 10.0, user_id=self.user_id, is_paper=self.is_paper)

            effective_risk = self._get_effective_risk()

            if risk_mode == "dynamic":
                balance = await self.exchange.get_balance()
                if effective_risk is not None:
                    risk_amt = balance * (effective_risk / 100.0)
                else:
                    risk_amt = balance * (risk_value / 100.0)
            else:
                risk_amt = float(risk_value)

            sl_dist = signal["sl_distance"]
            if sl_dist <= 0:
                log.warning("Invalid SL distance %.4f — skipping", sl_dist)
                return

            tick = self.config.tick_size.get(symbol, 0.01)
            slip = self.config.slippage_ticks * tick
            if signal["direction"] == "long":
                entry_adj = signal["entry_price"] + slip
            else:
                entry_adj = signal["entry_price"] - slip

            quantity = risk_amt / sl_dist
            min_qty = self.config.lot_size.get(symbol, 0.01)
            quantity = max(quantity, min_qty)

            await self._prepare_entry(symbol)

            side = "BUY" if signal["direction"] == "long" else "SELL"
            order = await self.exchange.place_market_order(symbol, side, quantity)

            fill_price = float(order.get("avgPrice") or 0) or signal["entry_price"]
            fill_qty = float(order.get("executedQty") or 0) or quantity

            entry_comm = await self._entry_commission(symbol, order, fill_price, fill_qty)

            trade = await db.create_trade({
                "user_id": self.user_id,
                "is_paper": self.is_paper,
                "symbol": symbol,
                "direction": signal["direction"],
                "entry_time": datetime.now(timezone.utc),
                "entry_price": fill_price,
                "sl_price": signal["sl"],
                "tp_price": signal["tp"],
                "quantity": fill_qty,
                "result": "open",
                "commission": entry_comm,
                "entry_order_id": str(order.get("orderId", "")),
            })

            self._active_trades[symbol] = trade.id

            await self._place_sl_tp(symbol, signal["direction"], fill_qty, signal["sl"], signal["tp"], trade.id)

            await self._broadcast({
                "type": "trade_opened",
                "trade": {
                    "id": trade.id,
                    "symbol": symbol,
                    "direction": signal["direction"],
                    "entry_price": fill_price,
                    "sl": signal["sl"],
                    "tp": signal["tp"],
                    "quantity": fill_qty,
                },
            })

            balance = await self.exchange.get_balance()
            await db.log_event(
                f"Opened {signal['direction'].upper()} {symbol} @ ${fill_price:.2f}",
                category="trade",
                details=f"SL: ${signal['sl']:.2f} | TP: ${signal['tp']:.2f} | Qty: {fill_qty}",
                user_id=self.user_id, is_paper=self.is_paper,
            )
            await self._alert_entry(symbol, signal, fill_price, fill_qty, balance)

        except Exception as e:
            log.error("Failed to execute trade: %s", e, exc_info=True)
            await db.log_event(f"Trade execution failed: {e}", level="error", category="trade",
                               user_id=self.user_id, is_paper=self.is_paper)

    async def _place_sl_tp(self, symbol: str, direction: str, quantity: float, sl: float, tp: float, trade_id: int):
        close_side = "SELL" if direction == "long" else "BUY"

        await self._before_place_sl_tp(symbol)

        sl_order = None
        tp_order = None
        for attempt in range(3):
            try:
                if sl_order is None:
                    sl_order = await self.exchange.place_stop_loss(symbol, close_side, quantity, sl)
                if tp_order is None:
                    tp_order = await self.exchange.place_take_profit(symbol, close_side, quantity, tp)
                break
            except Exception as e:
                log.warning("SL/TP placement attempt %d failed: %s", attempt + 1, e)
                if attempt < 2:
                    await asyncio.sleep(1)

        update = {}
        if sl_order:
            update["sl_order_id"] = str(sl_order.get("algoId") or sl_order.get("orderId") or "")
        if tp_order:
            update["tp_order_id"] = str(tp_order.get("algoId") or tp_order.get("orderId") or "")
        if update:
            await db.update_trade(trade_id, update)

        return sl_order is not None, tp_order is not None

    # --- Order Fill Monitoring ---

    async def _run_user_stream(self):
        while self.running:
            try:
                await self.exchange.start_user_socket(self._on_user_event)
            except Exception as e:
                log.error("User WebSocket error: %s", e)
                await asyncio.sleep(5)

    async def _on_user_event(self, data: dict):
        if data.get("e") != "ORDER_TRADE_UPDATE":
            return

        order = data.get("o", {})
        status = order.get("X", "")
        if status != "FILLED":
            return

        order_id = str(order.get("i", ""))
        symbol = order.get("s", "")
        order_type = order.get("ot", "")
        fill_price = float(order.get("ap", 0))

        if order_type not in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
            return

        trade = await db.get_open_trade_for_symbol(self.user_id, symbol, self.is_paper)
        if trade is None:
            return

        is_sl = order_type == "STOP_MARKET"
        result = "loss" if is_sl else "win"
        rr = await db.get_state("rr_ratio", self.config.strategy_params["rrr"],
                                user_id=self.user_id, is_paper=self.is_paper)
        r_value = -1.0 if is_sl else rr

        fill_price, exit_comm = await self._exit_fill(symbol, order_id, fill_price, trade.quantity)

        total_comm = (trade.commission or 0) + exit_comm

        if trade.direction == "long":
            raw_pnl = (fill_price - trade.entry_price) * trade.quantity
        else:
            raw_pnl = (trade.entry_price - fill_price) * trade.quantity
        pnl = raw_pnl - total_comm

        await db.update_trade(trade.id, {
            "exit_time": datetime.now(timezone.utc),
            "exit_price": fill_price,
            "result": result,
            "r_value": r_value,
            "pnl_usdt": pnl,
            "commission": total_comm,
        })

        await self.exchange.cancel_all_orders(symbol)

        self._active_trades.pop(symbol, None)
        self._sltp_missing_since.pop(symbol, None)
        self._sltp_last_alert.pop(symbol, None)

        self._on_trade_result(result == "win")

        log.info("[%s] Trade #%d closed: %s (%.2f USDT)", self._mode_label(), trade.id, result, pnl)

        await db.log_event(
            f"Closed {trade.direction.upper()} {symbol}: {result.upper()} ${pnl:+.2f}",
            level="info" if result == "win" else "warn",
            category="trade",
            details=f"Entry: ${trade.entry_price:.2f} → Exit: ${fill_price:.2f} | {r_value:+.1f}R",
            user_id=self.user_id, is_paper=self.is_paper,
        )

        await self._broadcast({
            "type": "trade_closed",
            "trade": {
                "id": trade.id,
                "symbol": symbol,
                "result": result,
                "pnl": pnl,
                "r_value": r_value,
            },
        })

        balance = await self.exchange.get_balance()
        await self._on_trade_closed(trade, result, fill_price, pnl, r_value, balance)

    # --- Fallback Position Poll ---

    async def _run_position_poll(self):
        while self.running:
            poll_interval = 10 if (self._active_trades and self._sltp_missing_since) else 30
            await asyncio.sleep(poll_interval)
            try:
                open_trades = await db.get_open_trades(self.user_id, self.is_paper)
                open_symbols = {t.symbol for t in open_trades}

                # Sync in-memory state to what's actually open in the DB
                self._active_trades = {t.symbol: t.id for t in open_trades}
                for sym in list(self._sltp_missing_since):
                    if sym not in open_symbols:
                        self._sltp_missing_since.pop(sym, None)
                for sym in list(self._sltp_last_alert):
                    if sym not in open_symbols:
                        self._sltp_last_alert.pop(sym, None)

                for trade in open_trades:
                    try:
                        await self._poll_one_trade(trade)
                    except Exception as e:
                        log.error("Position poll error for %s #%d: %s", trade.symbol, trade.id, e)
            except Exception as e:
                log.error("Position poll error: %s", e)

    async def _poll_one_trade(self, trade):
        symbol = trade.symbol
        pos = await self.exchange.get_position(symbol)
        if pos is None:
            log.info("Position poll: no position found for trade #%d — checking orders", trade.id)

            exit_info = await self._resolve_exit(trade)
            if exit_info is not None:
                exit_price, is_sl, exit_comm = exit_info
            else:
                exit_price, is_sl, exit_comm = None, None, 0.0

            if exit_price is None or exit_price == 0:
                # Last resort (fills unavailable): infer from price proximity to
                # SL vs TP — de-biased, never assume SL.
                sm_candles = self._shared_market.get_candles(symbol) if self._shared_market else []
                recent = sm_candles[-3:] if sm_candles else []
                if not recent:
                    return
                last_close = recent[-1]["close"]
                is_sl = abs(last_close - trade.sl_price) <= abs(last_close - trade.tp_price)
                exit_price = trade.sl_price if is_sl else trade.tp_price
                exit_comm = exit_price * trade.quantity * self.config.commission_pct

            result = "loss" if is_sl else "win"
            rr = await db.get_state("rr_ratio", self.config.strategy_params["rrr"],
                                    user_id=self.user_id, is_paper=self.is_paper)
            r_value = r_value_for_exit(is_sl, trade.entry_price, trade.sl_price, trade.tp_price, rr)

            total_comm = (trade.commission or 0) + exit_comm

            if trade.direction == "long":
                raw_pnl = (exit_price - trade.entry_price) * trade.quantity
            else:
                raw_pnl = (trade.entry_price - exit_price) * trade.quantity
            pnl = raw_pnl - total_comm

            await db.update_trade(trade.id, {
                "exit_time": datetime.now(timezone.utc),
                "exit_price": exit_price,
                "result": result,
                "r_value": r_value,
                "pnl_usdt": pnl,
                "commission": total_comm,
            })

            await self.exchange.cancel_all_orders(symbol)
            self._active_trades.pop(symbol, None)
            self._sltp_missing_since.pop(symbol, None)
            self._sltp_last_alert.pop(symbol, None)

            self._on_trade_result(result == "win")

            log.info("Position poll: trade #%d resolved as %s", trade.id, result)

            await self._broadcast({
                "type": "trade_closed",
                "trade": {
                    "id": trade.id,
                    "symbol": symbol,
                    "result": result,
                    "pnl": pnl,
                },
            })

            balance = await self.exchange.get_balance()
            await self._on_trade_closed(trade, result, exit_price, pnl, r_value, balance)
            return

        # Position still open — mode-specific verification (live verifies SL/TP and
        # runs the force-close-breach safety net; paper is a no-op).
        await self._poll_open_position(trade, pos)



# Backwards-compatible alias for type hints / legacy imports. Concrete workers
# are LiveWorker (worker_live.py) and PaperWorker (worker_paper.py); construct
# them via make_worker() below or the manager.
BotWorker = BaseWorker


def make_worker(user_id, config, broadcast_fn=None, shared_market=None):
    """Build the right worker for the config's mode."""
    if config.is_paper:
        from app.bot.worker_paper import PaperWorker
        cls = PaperWorker
    else:
        from app.bot.worker_live import LiveWorker
        cls = LiveWorker
    return cls(user_id, config, broadcast_fn=broadcast_fn, shared_market=shared_market)
