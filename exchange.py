"""
Binance Futures API wrapper.

Always connects to LIVE Binance. Paper trading is handled by PaperExchange.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone
from binance import AsyncClient, BinanceSocketManager

from config import SYMBOLS, LEVERAGE, LOT_SIZE

log = logging.getLogger(__name__)


class BinanceExchange:
    def __init__(self, api_key: str, api_secret: str, leverage: int | None = None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.client: AsyncClient | None = None
        self.bsm: BinanceSocketManager | None = None
        self.hedge_mode: bool = False
        # Per-user leverage; falls back to the global default when unset.
        self.leverage: int = int(leverage) if leverage else LEVERAGE

    async def connect(self):
        self.client = await AsyncClient.create(
            api_key=self.api_key,
            api_secret=self.api_secret,
        )
        log.info("Connected to Binance LIVE")

        self.bsm = BinanceSocketManager(self.client)

        await self._detect_position_mode()
        await self._set_leverage()

    async def _detect_position_mode(self):
        try:
            result = await self.client.futures_get_position_mode()
            self.hedge_mode = result.get("dualSidePosition", False)
            log.info("Position mode: %s", "hedge" if self.hedge_mode else "one-way")
        except Exception as e:
            self.hedge_mode = False
            log.warning("Failed to detect position mode, assuming one-way: %s", e)

    async def _set_leverage(self):
        for symbol in SYMBOLS:
            try:
                await self.client.futures_change_leverage(symbol=symbol, leverage=self.leverage)
                log.info("Set %s leverage to %dx", symbol, self.leverage)
            except Exception as e:
                log.warning("Failed to set leverage for %s: %s", symbol, e)

    async def connect_minimal(self):
        """Connect for a one-off action (e.g. an admin force-close) without
        starting a socket manager or touching leverage — a stopped user's bot
        must not have its exchange-side settings changed as a side effect."""
        self.client = await AsyncClient.create(
            api_key=self.api_key,
            api_secret=self.api_secret,
        )
        await self._detect_position_mode()

    async def set_leverage(self, leverage: int):
        """Update leverage and re-apply on the exchange (used when settings change)."""
        self.leverage = int(leverage) if leverage else LEVERAGE
        if self.client is not None:
            await self._set_leverage()

    async def close(self):
        if self.client:
            await self.client.close_connection()

    # --- Trading operations ---

    async def get_balance(self) -> float:
        balances = await self.client.futures_account_balance()
        for b in balances:
            if b["asset"] == "USDT":
                return float(b["balance"])
        return 0.0

    # Binance Futures income history returns only the last 7 DAYS when no startTime
    # is given, so we must pass an explicit far-back start and paginate forward to
    # capture the ORIGINAL funding (missing it makes the drawdown curve start after
    # the trades → garbage). Default ≈ Binance Futures launch.
    _INCOME_HISTORY_START_MS = 1567296000000  # 2019-09-01

    async def get_transfers(self, start_time: int | None = None) -> list[dict]:
        """Capital in/out of the USDⓈ-M futures wallet, from income history.
        `income` > 0 = money moved IN (deposit/funding), < 0 = OUT (withdrawal).
        Returns [{"time": ms, "amount": float}] sorted ascending, over the account's
        FULL history (paginated 1000/page from a far-back start)."""
        out: list[dict] = []
        # Capital-movement income types (spot↔futures, internal, bonuses).
        for itype in ("TRANSFER", "INTERNAL_TRANSFER", "WELCOME_BONUS"):
            start = start_time if start_time is not None else self._INCOME_HISTORY_START_MS
            while True:
                recs = await self.client.futures_income_history(
                    incomeType=itype, startTime=start, limit=1000)
                if not recs:
                    break
                for r in recs:
                    amt = float(r.get("income") or 0)
                    if amt:
                        out.append({"time": int(r["time"]), "amount": amt})
                if len(recs) < 1000:
                    break
                start = int(recs[-1]["time"]) + 1
        out.sort(key=lambda x: x["time"])
        return out

    async def get_daily_wallet_balances(self, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
        """Real daily FUTURES USDT wallet balance from Binance's account snapshot —
        Binance's ACTUAL asset value (the data behind the wallet chart), not a
        reconstruction. Only ~the last 30 days are available, and the range must span
        < 30 days. Returns [(updateTime_ms, usdt_wallet_balance)] ascending."""
        snap = await self.client.get_account_snapshot(
            type="FUTURES", startTime=int(start_ms), endTime=int(end_ms), limit=30)
        out: list[tuple[int, float]] = []
        for v in snap.get("snapshotVos") or []:
            usdt = next((a for a in v.get("data", {}).get("assets", []) if a.get("asset") == "USDT"), None)
            if usdt is not None:
                out.append((int(v["updateTime"]), float(usdt.get("walletBalance", 0) or 0)))
        out.sort(key=lambda x: x[0])
        return out

    async def get_wallet_balance_on(self, target_ms: int) -> float | None:
        """Real FUTURES USDT balance just BEFORE `target_ms` — the latest daily
        snapshot (snapshot `updateTime` is END-of-day) that is strictly earlier than
        the first bot trade, i.e. the account's balance before it started trading.
        Used to seed the drawdown base for existing accounts.

        Returns None when NO snapshot predates `target_ms` — the target is older than
        the ~30-day snapshot window, or there's a gap with no earlier day. Never
        returns a post-trade balance or an unrelated recent one (a wrong base would
        ship a wrong drawdown, so we say 'unknown' instead)."""
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        day = 86_400_000
        start = max(int(target_ms) - 3 * day, now_ms - 29 * day)
        rows = await self.get_daily_wallet_balances(start, now_ms)
        before = [bal for ts, bal in rows if ts < int(target_ms)]
        return before[-1] if before else None

    async def get_position(self, symbol: str) -> dict | None:
        positions = await self.client.futures_position_information(symbol=symbol)
        for p in positions:
            amt = float(p["positionAmt"])
            if amt != 0:
                return {
                    "symbol": p["symbol"],
                    "side": "long" if amt > 0 else "short",
                    "quantity": abs(amt),
                    "entry_price": float(p["entryPrice"]),
                    "unrealized_pnl": float(p["unRealizedProfit"]),
                }
        return None

    async def get_all_positions(self) -> list[dict]:
        positions = await self.client.futures_position_information()
        result = []
        for p in positions:
            amt = float(p["positionAmt"])
            if amt != 0:
                result.append({
                    "symbol": p["symbol"],
                    "side": "long" if amt > 0 else "short",
                    "quantity": abs(amt),
                    "entry_price": float(p["entryPrice"]),
                    "unrealized_pnl": float(p["unRealizedProfit"]),
                })
        return result

    async def get_open_orders(self, symbol: str) -> list[dict]:
        return await self.client.futures_get_open_orders(symbol=symbol)

    async def get_conditional_orders(self, symbol: str, strict: bool = False) -> list[dict]:
        """Open conditional/algo orders (STOP_MARKET / TAKE_PROFIT_MARKET).

        python-binance auto-routes STOP/TP orders to Binance's algo endpoint,
        so SL/TP live in this separate bucket — invisible to get_open_orders().

        strict=True re-raises on error so a caller making a safety-critical
        decision (e.g. force-close) can tell "no SL exists" apart from
        "couldn't fetch". Default swallows errors and returns [] (best-effort).
        """
        try:
            res = await self.client.futures_get_open_orders(symbol=symbol, conditional=True)
            return res or []
        except Exception as e:
            if strict:
                raise
            log.warning("Failed to get conditional orders on %s: %s", symbol, e)
            return []

    async def place_market_order(self, symbol: str, side: str, quantity: float,
                                 position_side: str | None = None,
                                 reduce_only: bool = False) -> dict:
        """`reduce_only=True` for a CLOSING order: if the position is already
        flat (an SL/TP filled in the gap since we read it), Binance rejects the
        order instead of opening a fresh position in the opposite direction.
        Only meaningful in one-way mode — hedge mode forbids reduceOnly, but
        there a side+positionSide order can only ever reduce that leg anyway."""
        params = dict(
            symbol=symbol,
            side=side.upper(),
            type="MARKET",
            quantity=self._format_qty(symbol, quantity),
            newOrderRespType="RESULT",
        )
        if self.hedge_mode:
            ps = position_side.upper() if position_side else ("LONG" if side.upper() == "BUY" else "SHORT")
            params["positionSide"] = ps
        elif reduce_only:
            params["reduceOnly"] = "true"
        order = await self.client.futures_create_order(**params)
        log.info("Market %s %s %.4f — order %s (avg %.2f)", side, symbol, quantity, order["orderId"], float(order.get("avgPrice", 0)))
        return order

    async def place_stop_loss(self, symbol: str, side: str, quantity: float, stop_price: float) -> dict:
        params = dict(
            symbol=symbol,
            side=side.upper(),
            type="STOP_MARKET",
            stopPrice=self._format_price(symbol, stop_price),
            quantity=self._format_qty(symbol, quantity),
        )
        if self.hedge_mode:
            params["positionSide"] = "LONG" if side.upper() == "SELL" else "SHORT"
        else:
            params["reduceOnly"] = "true"
        order = await self.client.futures_create_order(**params)
        # STOP_MARKET is routed to the algo endpoint, whose response has algoId (no orderId)
        log.info("SL %s %s @ %.2f — order %s", side, symbol, stop_price, order.get("algoId") or order.get("orderId"))
        return order

    async def place_take_profit(self, symbol: str, side: str, quantity: float, price: float) -> dict:
        params = dict(
            symbol=symbol,
            side=side.upper(),
            type="TAKE_PROFIT_MARKET",
            stopPrice=self._format_price(symbol, price),
            quantity=self._format_qty(symbol, quantity),
        )
        if self.hedge_mode:
            params["positionSide"] = "LONG" if side.upper() == "SELL" else "SHORT"
        else:
            params["reduceOnly"] = "true"
        order = await self.client.futures_create_order(**params)
        # TAKE_PROFIT_MARKET is routed to the algo endpoint, whose response has algoId (no orderId)
        log.info("TP %s %s @ %.2f — order %s", side, symbol, price, order.get("algoId") or order.get("orderId"))
        return order

    async def get_order(self, symbol: str, order_id: int) -> dict | None:
        try:
            return await self.client.futures_get_order(symbol=symbol, orderId=order_id)
        except Exception as e:
            log.warning("Failed to get order %s on %s: %s", order_id, symbol, e)
            return None

    async def get_trades_for_order(self, symbol: str, order_id: int) -> list[dict]:
        try:
            trades = await self.client.futures_account_trades(symbol=symbol)
            return [t for t in trades if int(t.get("orderId", 0)) == order_id]
        except Exception as e:
            log.warning("Failed to get trades for order %s: %s", order_id, e)
            return []

    async def cancel_order(self, symbol: str, order_id: int):
        try:
            await self.client.futures_cancel_order(symbol=symbol, orderId=order_id)
            log.info("Cancelled order %s on %s", order_id, symbol)
        except Exception as e:
            log.warning("Failed to cancel order %s: %s", order_id, e)

    async def cancel_all_orders(self, symbol: str):
        # Two separate Binance buckets: regular orders AND conditional/algo orders
        # (SL/TP live in the algo bucket). Both must be cleared.
        try:
            await self.client.futures_cancel_all_open_orders(symbol=symbol)
        except Exception as e:
            log.warning("Failed to cancel regular orders on %s: %s", symbol, e)
        try:
            await self.client.futures_cancel_all_open_orders(symbol=symbol, conditional=True)
        except Exception as e:
            # Benign when there are no conditional orders to cancel
            log.warning("Failed to cancel conditional orders on %s: %s", symbol, e)
        log.info("Cancelled all open orders (regular + conditional) on %s", symbol)

    # --- Market data operations ---

    async def get_klines(self, symbol: str, interval: str, limit: int = 500) -> list[dict]:
        raw = await self.client.futures_klines(symbol=symbol, interval=interval, limit=limit)
        candles = []
        for k in raw:
            candles.append({
                "timestamp": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })
        return candles

    async def start_kline_socket(self, symbols: list[str], interval: str, callback):
        streams = [f"{s.lower()}@kline_{interval}" for s in symbols]
        socket = self.bsm.futures_multiplex_socket(streams=streams)
        async with socket as stream:
            while True:
                msg = await stream.recv()
                if msg and "data" in msg:
                    await callback(msg["data"])

    async def start_user_socket(self, callback):
        socket = self.bsm.futures_user_socket()
        async with socket as stream:
            while True:
                msg = await stream.recv()
                if msg:
                    await callback(msg)

    # --- Helpers ---

    def _format_qty(self, symbol: str, qty: float) -> str:
        step = LOT_SIZE.get(symbol, 0.01)
        qty = math.floor(qty / step) * step
        decimals = max(0, -int(math.floor(math.log10(step))))
        return f"{qty:.{decimals}f}"

    def _format_price(self, symbol: str, price: float) -> str:
        tick = {"BTCUSDT": 0.10, "ETHUSDT": 0.01, "SOLUSDT": 0.01}.get(symbol, 0.01)
        rounded = round(price / tick) * tick
        if tick >= 0.1:
            return f"{rounded:.1f}"
        return f"{rounded:.2f}"


async def validate_api_key(api_key: str, api_secret: str) -> dict:
    """Validate a Binance Futures API key before saving it.

    Returns ``{ok, permissions, error}``. ``ok`` is True only if the key works,
    Futures is enabled, and withdrawals are NOT enabled (a security red flag).
    ``permissions`` carries the readable flags (reading/futures/withdrawals/
    ip_restricted) when available; some keys can't read their own restrictions,
    in which case it's empty but the Futures check still gates ``ok``.
    """
    try:
        client = await AsyncClient.create(api_key=api_key, api_secret=api_secret)
    except Exception:
        return {"ok": False, "permissions": {}, "error": "Could not connect to Binance. Check the key and secret."}

    try:
        # 1) Proves the key/secret work AND that Futures access is enabled.
        try:
            await client.futures_account_balance()
        except Exception as e:
            msg = str(e)
            if "-2015" in msg:
                err = "Invalid key, IP not whitelisted, or Futures trading not enabled on this key."
            elif "-2014" in msg or "-1022" in msg or "signature" in msg.lower():
                err = "Invalid API key or secret."
            else:
                err = "Could not access Binance Futures with this key."
            return {"ok": False, "permissions": {}, "error": err}

        # 2) Read the permission flags (best-effort — not all keys can read this).
        perms: dict = {}
        try:
            r = await client.get_account_api_permissions()
            perms = {
                "reading": bool(r.get("enableReading", True)),
                "futures": bool(r.get("enableFutures", True)),
                "withdrawals": bool(r.get("enableWithdrawals", False)),
                "ip_restricted": bool(r.get("ipRestrict", False)),
            }
            if perms["withdrawals"]:
                return {
                    "ok": False,
                    "permissions": perms,
                    "error": "This key has Withdrawals enabled. Disable it on Binance for safety, then reconnect.",
                }
        except Exception:
            perms = {}  # unknown — Futures access is already confirmed above

        return {"ok": True, "permissions": perms, "error": None}
    finally:
        try:
            await client.close_connection()
        except Exception:
            pass


def r_value_for_exit(
    is_sl: bool,
    entry_price: float | None,
    sl_price: float | None,
    tp_price: float | None,
    fallback_r: float | None = None,
) -> float:
    """Static R multiple for a resolved trade.

    Loss = -1R. Win = the trade's planned target RR (``fallback_r``, e.g. 2 or 3).
    We deliberately do NOT derive a win's R from the tp/sl geometry
    (``|tp-entry| / |entry-sl|``): the SL is often widened to the manipulation
    wick, which would skew the ratio and make wins show odd values like 1.7R/2.4R.
    Geometry is only a last-resort estimate for legacy rows with no target RR.
    Guards against the ``-1.0 or 2.0`` truthiness trap.
    """
    if is_sl:
        return -1.0
    if fallback_r and fallback_r > 0:
        return float(fallback_r)
    try:
        sl_dist = abs((entry_price or 0) - (sl_price or 0))
        tp_dist = abs((tp_price or 0) - (entry_price or 0))
        if sl_dist > 0 and tp_dist > 0:
            return round(tp_dist / sl_dist, 2)
    except Exception:
        pass
    return 2.0


async def resolve_trade_exit(
    client: "AsyncClient",
    symbol: str,
    direction: str,
    entry_order_id: str | int | None,
    entry_time,
    entry_quantity: float | None,
    sl_price: float | None,
    tp_price: float | None,
) -> dict | None:
    """Determine how a closed position actually exited, straight from Binance fills.

    SL/TP are placed on Binance's conditional/algo endpoint, so the stored
    sl_order_id / tp_order_id are ``algoId``s. When such an order triggers it
    produces a brand-new *regular* order/trade with its own orderId — so a
    ``futures_get_order(algoId)`` lookup fails with ``-2013`` and the bot never
    learns the real outcome. This reads the account's own trade fills
    (``/fapi/v1/userTrades``, which carries ``realizedPnl``) and isolates the
    closing fills for this position, independent of any order id.

    Returns ``{exit_price, exit_commission, realized_pnl, is_sl, exit_qty}`` or
    ``None`` if the closing fills could not be found (caller should fall back).
    """
    if not entry_time:
        return None
    start_ms = int(entry_time.timestamp() * 1000) - 1000  # small buffer
    try:
        fills = await client.futures_account_trades(symbol=symbol, startTime=start_ms, limit=1000)
    except Exception as e:
        log.warning("resolve_trade_exit: could not fetch fills for %s: %s", symbol, e)
        return None
    if not fills:
        return None

    entry_oid = int(entry_order_id) if entry_order_id else None
    # Entry side is the side that opened the position; the exit is the opposite.
    exit_side = "BUY" if direction == "short" else "SELL"
    # In hedge mode the closing fill carries the position's own side; tolerate
    # one-way mode ("BOTH") and any account where it's absent.
    expected_ps = "SHORT" if direction == "short" else "LONG"

    def _ps_ok(f) -> bool:
        ps = f.get("positionSide")
        return ps is None or ps in ("BOTH", expected_ps)

    # Closing fills: opposite side, this position's side, not the entry order,
    # in chronological order.
    candidates = sorted(
        (
            f for f in fills
            if f.get("side") == exit_side
            and _ps_ok(f)
            and (entry_oid is None or int(f.get("orderId", 0)) != entry_oid)
        ),
        key=lambda f: f.get("time", 0),
    )
    if not candidates:
        return None

    # Accumulate only up to this position's size so we don't absorb a later
    # trade's fills on the same symbol (one trade per asset at a time).
    target_qty = float(entry_quantity) if entry_quantity else None
    selected = []
    acc = 0.0
    for f in candidates:
        selected.append(f)
        acc += float(f["qty"])
        if target_qty and acc >= target_qty - 1e-9:
            break

    total_qty = sum(float(f["qty"]) for f in selected)
    if total_qty <= 0:
        return None

    exit_price = sum(float(f["price"]) * float(f["qty"]) for f in selected) / total_qty
    exit_comm = sum(float(f.get("commission", 0)) for f in selected)
    realized = sum(float(f.get("realizedPnl", 0)) for f in selected)
    exit_ms = max((int(f.get("time", 0)) for f in selected), default=0)

    # Decide SL vs TP by which target the real exit price is closest to. This
    # matches the strategy's semantics (TP hit = win) even if fees nudge a
    # marginal win negative. Fall back to realized-PnL sign if a level is unset.
    if sl_price and tp_price:
        is_sl = abs(exit_price - sl_price) <= abs(exit_price - tp_price)
    else:
        is_sl = realized < 0

    return {
        "exit_price": exit_price,
        "exit_commission": exit_comm,
        "realized_pnl": realized,
        "is_sl": is_sl,
        "exit_qty": total_qty,
        # Actual close moment on Binance (last closing fill), for an exact exit_time.
        "exit_time": datetime.fromtimestamp(exit_ms / 1000, timezone.utc) if exit_ms else None,
    }


def fills_time(fills) -> "datetime | None":
    """The actual fill moment (latest fill timestamp) as a tz-aware datetime, or None."""
    ms = max((int(f.get("time", 0)) for f in (fills or [])), default=0)
    return datetime.fromtimestamp(ms / 1000, timezone.utc) if ms else None


async def _asset_usdt_price(client: "AsyncClient", asset: str, time_ms: int, cache: dict) -> float:
    """Price of `asset` in USDT at `time_ms` (1m kline close), cached per asset+minute.

    Used to convert fees paid in a non-USDT asset (e.g. BNB fee discount) into the
    USDT value Binance shows — at the fee's own timestamp, so it matches the ledger.
    """
    if asset == "USDT":
        return 1.0
    bucket = (asset, time_ms // 60000)
    if bucket in cache:
        return cache[bucket]
    px = 0.0
    try:
        kl = await client.futures_klines(
            symbol=f"{asset}USDT", interval="1m",
            startTime=time_ms - 60000, endTime=time_ms + 60000, limit=2,
        )
        if kl:
            px = float(kl[-1][4])
    except Exception as e:
        log.warning("_asset_usdt_price: %sUSDT price fetch failed: %s", asset, e)
    cache[bucket] = px
    return px


async def position_pnl_breakdown(
    client: "AsyncClient",
    symbol: str,
    entry_time,
    exit_time,
) -> dict | None:
    """Net realized PnL + breakdown straight from Binance's income ledger.

    This is exactly what Binance's *Position History* shows — never computed from
    prices. Sums the income entries over the position's lifetime:

        net = REALIZED_PNL + FUNDING_FEE + COMMISSION

    COMMISSION is a cost (negative income); when it's paid in a non-USDT asset
    (BNB fee discount) it's converted to USDT via that asset's price at the fee
    time, matching Binance's display to the cent.

    Returns ``{realized_pnl, funding_fee, commission, net_pnl}`` (``commission`` is
    a positive USDT cost) or ``None`` if the ledger couldn't be read or is empty.
    """
    if not entry_time:
        return None
    # This position's income is all at/before exit_time (a close is detected after
    # the fill), so a tight window avoids catching the next same-symbol trade's fees.
    start_ms = int(entry_time.timestamp() * 1000) - 1000
    end_dt = exit_time or datetime.now(timezone.utc)
    end_ms = int(end_dt.timestamp() * 1000) + 5000
    try:
        rows = await client.futures_income_history(symbol=symbol, startTime=start_ms, endTime=end_ms, limit=1000)
    except Exception as e:
        log.warning("position_pnl_breakdown: income fetch failed for %s: %s", symbol, e)
        return None
    if not rows:
        return None

    realized = 0.0
    funding = 0.0
    commission = 0.0  # positive USDT cost
    price_cache: dict = {}
    saw_realized = False
    for r in rows:
        itype = r.get("incomeType")
        amt = float(r.get("income", 0) or 0)
        asset = r.get("asset") or "USDT"
        if itype == "REALIZED_PNL":
            realized += amt
            saw_realized = True
        elif itype == "FUNDING_FEE":
            funding += amt
        elif itype == "COMMISSION":
            if asset == "USDT":
                commission += -amt
            else:
                px = await _asset_usdt_price(client, asset, int(r.get("time", 0)), price_cache)
                if px <= 0:
                    # Can't convert the fee to USDT — don't store an inaccurate net;
                    # the scanner re-runs and will fix it once the price is readable.
                    log.warning("position_pnl_breakdown: no %s/USDT price for %s — skipping", asset, symbol)
                    return None
                commission += -amt * px

    # Only commission/funding and no realized PnL = not a resolved close; let the
    # caller keep its value rather than store a partial.
    if not saw_realized:
        return None

    return {
        "realized_pnl": realized,
        "funding_fee": funding,
        "commission": commission,
        "net_pnl": realized + funding - commission,
    }
