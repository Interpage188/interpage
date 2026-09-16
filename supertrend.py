#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# SUPERTREND (Pivot Point) — port fiel del Pine de LonesomeTheBlue
#
# NO decide. NO alerta. Solo escribe data/supertrend_state.json
# para que multi.py lo lea como señal de confirmación.
#
# Replica:
#   - pivothigh(prd, prd) / pivotlow(prd, prd)
#   - center = (center * 2 + lastpp) / 3
#   - Up/Dn = center ± Factor * ATR(Pd)
#   - TUp/TDown stateful + Trend
#
# Vive en el repo público (Interpage188/interpage) junto al
# recolector y el watcher.
# ============================================================

import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ===== CONFIG (idéntica al Pine) =====
PRD = 2            # Pivot Point Period
FACTOR = 3.0       # ATR Factor
ATR_PERIOD = 10    # ATR Period

# ===== OKX =====
OKX_BASE = "https://www.okx.com/api/v5/market/candles"
OKX_BAR = "15m"
OKX_LIMIT = 200    # ~50h de velas

# Mismo set que el recolector (17 monedas, incluye BTC)
SYMBOLS = [
    "BTC", "ETH", "BNB", "SOL", "ARB", "UNI", "LTC", "INJ", "LINK",
    "ENA", "SUSHI", "RAY", "HYPE", "ZEC", "DOGE", "STX", "DASH",
]

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
ST_STATE_FILE = DATA_DIR / "supertrend_state.json"


# ============================================================
# OKX
# ============================================================

def fetch_okx_candles(symbol, bar="15m", limit=200):
    pair = f"{symbol}-USDT"
    url = f"{OKX_BASE}?instId={pair}&bar={bar}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    if data.get("code") != "0":
        raise RuntimeError(f"OKX error: {data.get('msg')}")
    items = data.get("data", [])
    items = list(reversed(items))  # OKX devuelve más reciente primero
    candles = []
    for it in items:
        candles.append({
            "ts": int(it[0]),
            "open": float(it[1]),
            "high": float(it[2]),
            "low": float(it[3]),
            "close": float(it[4]),
        })
    return candles


# ============================================================
# ATR (Wilder's RMA — igual que Pine)
# ============================================================

def true_range(prev_close, high, low):
    if prev_close is None:
        return high - low
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr_wilder(candles, period=10):
    n = len(candles)
    atr = [None] * n
    if n < period + 1:
        return atr
    trs = [None] * n
    for i in range(n):
        prev_close = candles[i - 1]["close"] if i > 0 else None
        trs[i] = true_range(prev_close, candles[i]["high"], candles[i]["low"])
    first_idx = period  # último índice del primer SMA
    if first_idx >= n:
        return atr
    atr[first_idx] = sum(trs[1:1 + period]) / period
    for i in range(first_idx + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + trs[i]) / period
    return atr


# ============================================================
# PIVOT HIGH / LOW (semántica TradingView)
# ============================================================

def pivothigh(candles, left, right):
    n = len(candles)
    result = [None] * n
    for i in range(n):
        pivot_idx = i - right
        if pivot_idx - left < 0 or pivot_idx + right >= n:
            continue
        pivot_val = candles[pivot_idx]["high"]
        is_pivot = True
        for k in range(1, left + 1):
            if candles[pivot_idx - k]["high"] >= pivot_val:
                is_pivot = False
                break
        if not is_pivot:
            continue
        for k in range(1, right + 1):
            if candles[pivot_idx + k]["high"] >= pivot_val:
                is_pivot = False
                break
        if is_pivot:
            result[i] = pivot_val
    return result


def pivotlow(candles, left, right):
    n = len(candles)
    result = [None] * n
    for i in range(n):
        pivot_idx = i - right
        if pivot_idx - left < 0 or pivot_idx + right >= n:
            continue
        pivot_val = candles[pivot_idx]["low"]
        is_pivot = True
        for k in range(1, left + 1):
            if candles[pivot_idx - k]["low"] <= pivot_val:
                is_pivot = False
                break
        if not is_pivot:
            continue
        for k in range(1, right + 1):
            if candles[pivot_idx + k]["low"] <= pivot_val:
                is_pivot = False
                break
        if is_pivot:
            result[i] = pivot_val
    return result


# ============================================================
# SUPERTREND — port del Pine
# ============================================================

def calcular_supertrend(candles):
    n = len(candles)
    if n < ATR_PERIOD + PRD * 2 + 5:
        return None

    ph_arr = pivothigh(candles, PRD, PRD)
    pl_arr = pivotlow(candles, PRD, PRD)
    atr_arr = atr_wilder(candles, ATR_PERIOD)

    tup_arr = [None] * n
    tdown_arr = [None] * n
    trend_arr = [None] * n
    trail_arr = [None] * n

    center = None
    tup = None
    tdown = None
    trend = None

    for i in range(n):
        # center line
        lastpp = None
        if ph_arr[i] is not None:
            lastpp = ph_arr[i]
        elif pl_arr[i] is not None:
            lastpp = pl_arr[i]
        if lastpp is not None:
            center = lastpp if center is None else (center * 2 + lastpp) / 3

        atr_val = atr_arr[i]
        if center is not None and atr_val is not None:
            up = center - FACTOR * atr_val
            dn = center + FACTOR * atr_val
        else:
            up = dn = None

        prev_close = candles[i - 1]["close"] if i > 0 else None
        prev_tup = tup_arr[i - 1] if i > 0 else None
        prev_tdown = tdown_arr[i - 1] if i > 0 else None

        # TUp / TDown (stateful)
        if up is not None:
            if prev_close is not None and prev_tup is not None and prev_close > prev_tup:
                tup = max(up, prev_tup)
            else:
                tup = up
        tup_arr[i] = tup

        if dn is not None:
            if prev_close is not None and prev_tdown is not None and prev_close < prev_tdown:
                tdown = min(dn, prev_tdown)
            else:
                tdown = dn
        tdown_arr[i] = tdown

        # Trend
        close = candles[i]["close"]
        if prev_tdown is not None and close > prev_tdown:
            trend = 1
        elif prev_tup is not None and close < prev_tup:
            trend = -1
        elif trend is None:
            trend = 1  # nz(Trend[1], 1)
        trend_arr[i] = trend

        trail_arr[i] = tup if trend == 1 else tdown

    # Último flip
    ultimo_flip_idx = None
    for i in range(1, n):
        if trend_arr[i] is not None and trend_arr[i - 1] is not None:
            if trend_arr[i] != trend_arr[i - 1]:
                ultimo_flip_idx = i

    trend_actual = trend_arr[-1]
    señal = "NONE"
    if ultimo_flip_idx == n - 1:
        señal = "BUY" if trend_actual == 1 else "SELL"

    return {
        "trend": "buy" if trend_actual == 1 else "sell",
        "trend_num": trend_actual,
        "trailing_sl": trail_arr[-1],
        "ultimo_flip_idx": ultimo_flip_idx,
        "ultimo_flip_ts": candles[ultimo_flip_idx]["ts"] if ultimo_flip_idx is not None else None,
        "precio_flip": candles[ultimo_flip_idx]["close"] if ultimo_flip_idx is not None else None,
        "precio_actual": candles[-1]["close"],
        "señal_ultima_barra": señal,
    }


# ============================================================
# MAIN
# ============================================================

def ts_to_iso(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


def main():
    print("=" * 70, flush=True)
    print(f"🔮 SUPERTREND (Pivot Point) — PRD={PRD} Factor={FACTOR} ATR={ATR_PERIOD} @ {OKX_BAR}", flush=True)
    print(f"   {datetime.now(timezone.utc).isoformat()}", flush=True)
    print("=" * 70, flush=True)

    estado = {
        "generado_en": datetime.now(timezone.utc).isoformat(),
        "timeframe": OKX_BAR,
        "config": {"prd": PRD, "factor": FACTOR, "atr_period": ATR_PERIOD},
        "symbols": {},
    }

    for symbol in SYMBOLS:
        try:
            candles = fetch_okx_candles(symbol, OKX_BAR, OKX_LIMIT)
            st = calcular_supertrend(candles)
            if not st:
                print(f"   ⚠️ {symbol}: datos insuficientes", flush=True)
                estado["symbols"][symbol] = {"trend": "N/A", "error": "datos insuficientes"}
                continue

            estado["symbols"][symbol] = {
                "trend": st["trend"],
                "señal_ultima_barra": st["señal_ultima_barra"],
                "precio_actual": st["precio_actual"],
                "trailing_sl": st["trailing_sl"],
                "ultimo_flip_ts": ts_to_iso(st["ultimo_flip_ts"]) if st["ultimo_flip_ts"] else None,
                "precio_flip": st["precio_flip"],
            }

            flecha = "🟢" if st["trend"] == "buy" else "🔴"
            flip_txt = f"  ⚡ FLIP {st['señal_ultima_barra']}" if st["señal_ultima_barra"] != "NONE" else ""
            print(f"   {flecha} {symbol}: {st['trend'].upper()}{flip_txt}", flush=True)

        except Exception as e:
            print(f"   ❌ {symbol}: {str(e)[:80]}", flush=True)
            estado["symbols"][symbol] = {"trend": "N/A", "error": str(e)[:120]}
        time.sleep(0.3)

    with ST_STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(estado, f, indent=2)

    print("\n" + "=" * 70, flush=True)
    print(f"✅ {len(estado['symbols'])} símbolos guardados en {ST_STATE_FILE}", flush=True)
    print("🏁 TERMINADO", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
        raise
