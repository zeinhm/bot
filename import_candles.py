"""
Import historical 1m candle CSVs into the database and resample to higher timeframes.

Usage: python import_candles.py [--symbols BTCUSDT ETHUSDT SOLUSDT] [--intervals 15m 1h]
"""

import argparse
import csv
import os
import sys
import time

import psycopg2
from psycopg2.extras import execute_values

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
DB_URL = os.getenv("DATABASE_URL", "postgresql://localhost:5432/tradingbot")

RESAMPLE_MAP = {
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
}


def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS historical_candles (
                id SERIAL PRIMARY KEY,
                symbol VARCHAR(20) NOT NULL,
                interval VARCHAR(5) NOT NULL,
                timestamp BIGINT NOT NULL,
                open DOUBLE PRECISION,
                high DOUBLE PRECISION,
                low DOUBLE PRECISION,
                close DOUBLE PRECISION,
                volume DOUBLE PRECISION,
                UNIQUE(symbol, interval, timestamp)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hist_sym_int_ts ON historical_candles(symbol, interval, timestamp)")
        conn.commit()


def import_1m(conn, symbol):
    csv_path = os.path.join(DATA_DIR, f"{symbol}_1m.csv")
    if not os.path.exists(csv_path):
        print(f"  {csv_path} not found, skipping")
        return 0

    print(f"  Reading {csv_path}...")
    rows = []
    with open(csv_path) as f:
        reader = csv.reader(f)
        next(reader)
        for row in reader:
            if not row:
                continue
            ts_ms = int(row[0])
            ts_sec = ts_ms // 1000
            rows.append((symbol, "1m", ts_sec, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])))

    print(f"  Inserting {len(rows)} 1m candles...")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM historical_candles WHERE symbol = %s AND interval = '1m'", (symbol,))
        batch_size = 10000
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            execute_values(
                cur,
                "INSERT INTO historical_candles (symbol, interval, timestamp, open, high, low, close, volume) VALUES %s",
                batch,
            )
            if (i // batch_size) % 10 == 0:
                print(f"    {i + len(batch)}/{len(rows)}")
        conn.commit()

    print(f"  Done: {len(rows)} 1m candles for {symbol}")
    return len(rows)


def resample(conn, symbol, interval):
    seconds = RESAMPLE_MAP.get(interval)
    if not seconds:
        print(f"  Unknown interval: {interval}")
        return 0

    print(f"  Resampling {symbol} 1m → {interval}...")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT timestamp, open, high, low, close, volume FROM historical_candles "
            "WHERE symbol = %s AND interval = '1m' ORDER BY timestamp",
            (symbol,),
        )
        raw = cur.fetchall()

    if not raw:
        print(f"  No 1m data for {symbol}")
        return 0

    buckets = {}
    for ts, o, h, l, c, v in raw:
        key = (ts // seconds) * seconds
        if key not in buckets:
            buckets[key] = {"o": o, "h": h, "l": l, "c": c, "v": v}
        else:
            b = buckets[key]
            b["h"] = max(b["h"], h)
            b["l"] = min(b["l"], l)
            b["c"] = c
            b["v"] += v

    rows = []
    for ts in sorted(buckets):
        b = buckets[ts]
        rows.append((symbol, interval, ts, round(b["o"], 8), round(b["h"], 8), round(b["l"], 8), round(b["c"], 8), round(b["v"], 8)))

    with conn.cursor() as cur:
        cur.execute("DELETE FROM historical_candles WHERE symbol = %s AND interval = %s", (symbol, interval))
        batch_size = 10000
        for i in range(0, len(rows), batch_size):
            execute_values(
                cur,
                "INSERT INTO historical_candles (symbol, interval, timestamp, open, high, low, close, volume) VALUES %s",
                rows[i:i + batch_size],
            )
        conn.commit()

    print(f"  Done: {len(rows)} {interval} candles for {symbol}")
    return len(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    parser.add_argument("--intervals", nargs="+", default=["15m"])
    args = parser.parse_args()

    conn = psycopg2.connect(DB_URL)
    ensure_table(conn)

    t0 = time.time()
    for sym in args.symbols:
        print(f"\n=== {sym} ===")
        import_1m(conn, sym)
        for iv in args.intervals:
            resample(conn, sym, iv)

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s")

    with conn.cursor() as cur:
        cur.execute("SELECT symbol, interval, COUNT(*) FROM historical_candles GROUP BY symbol, interval ORDER BY symbol, interval")
        print("\nSummary:")
        for row in cur.fetchall():
            print(f"  {row[0]} {row[1]}: {row[2]:,} candles")

    conn.close()


if __name__ == "__main__":
    main()
