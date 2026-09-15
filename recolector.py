#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ============================================================
# RECOLECTOR — Fase 1.5
#   9 monedas representativas
#   3 niveles de frecuencia según RSI 15m:
#     - extremo      (RSI <30 o >70):  cada 5 min
#     - recuperación (RSI 30-40, 60-70): cada 10 min
#     - normal       (RSI 40-60):      cada 30 min
#   Fuente: OKX (velas OHLC reales, sin coste)
#   Retención: 24h de pulso
# ============================================================

SYMBOLS = [
    "BTC",
    "ETH",
    "BNB",
    "SOL",
    "ARB",
    "UNI",
    "LTC",
    "INJ",
    "LINK",
    "ENA",
    "SUSHI",
    "RAY",
    "HYPE",
    "ZEC",
    "DOGE",
    "STX",
    "DASH",
]

FREC_EXTREMO_MIN      = 5
FREC_RECUPERACION_MIN = 10
FREC_NORMAL_MIN       = 30

RSI_EXTREMO_BAJO = 30
RSI_EXTREMO_ALTO = 70
RSI_RECUP_BAJO   = 40
RSI_RECUP_ALTO   = 60

RETENCION_PULSO_H = 24
OKX_LIMIT_VELAS = 100

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)


OKX_INTERVALOS = {
    "15m": "15m",
    "1h":  "1H",
    "4h":  "4H",
}


def fetch_okx_klines(symbol, intervalo, limite=OKX_LIMIT_VELAS):
    bar = OKX_INTERVALOS.get(intervalo)
    if not bar:
        return []
    inst_id = f"{symbol}-USDT"
    url = (
        f"https://www.okx.com/api/v5/market/candles"
        f"?instId={inst_id}&bar={bar}&limit={limite}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"   ⚠️ OKX {symbol} {intervalo}: {str(e)[:60]}", flush=True)
        return []

    if raw.get("code") != "0":
        print(f"   ⚠️ OKX {symbol} {intervalo}: code={raw.get('code')}", flush=True)
        return []

    data = raw.get("data", [])
    data.reverse()

    velas = []
    for k in data:
        try:
            velas.append({
                "ts": int(k[0]),
                "o":  float(k[1]),
                "h":  float(k[2]),
                "l":  float(k[3]),
                "c":  float(k[4]),
                "v":  float(k[5]),
            })
        except (ValueError, IndexError):
            continue
    return velas


def calcular_rsi(prices, period=14):
    if not prices or len(prices) < period + 1:
        return None
    changes = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [max(c, 0) for c in changes]
    losses = [max(-c, 0) for c in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def zona_rsi(rsi15):
    if rsi15 is None:
        return "desconocida"
    if rsi15 < RSI_EXTREMO_BAJO or rsi15 > RSI_EXTREMO_ALTO:
        return "extremo"
    if rsi15 < RSI_RECUP_BAJO or rsi15 > RSI_RECUP_ALTO:
        return "recuperacion"
    return "normal"


def frecuencia_para_zona(zona):
    if zona == "extremo":
        return FREC_EXTREMO_MIN
    if zona == "recuperacion":
        return FREC_RECUPERACION_MIN
    return FREC_NORMAL_MIN


def cache_path(symbol):
    return CACHE_DIR / f"{symbol}.json"


def cargar_cache(symbol):
    p = cache_path(symbol)
    if not p.exists():
        return {"symbol": symbol, "updated_at": None, "pulso": []}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"symbol": symbol, "updated_at": None, "pulso": []}
        return data
    except Exception as e:
        print(f"   ⚠️ cache {symbol} corrupto: {e}", flush=True)
        return {"symbol": symbol, "updated_at": None, "pulso": []}


def guardar_cache(symbol, data):
    with cache_path(symbol).open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def minutos_desde_ultima_muestra(cache, ahora_ts):
    pulso = cache.get("pulso", [])
    if not pulso:
        return 9999
    ultimo_ts = pulso[-1].get("ts")
    if not ultimo_ts:
        return 9999
    return (ahora_ts - ultimo_ts) / 60


def rsi_actual_del_cache(cache):
    pulso = cache.get("pulso", [])
    if not pulso:
        return None
    return pulso[-1].get("rsi15")


def actualizar_pulso(symbol, ahora):
    velas_15m = fetch_okx_klines(symbol, "15m", OKX_LIMIT_VELAS)
    velas_1h  = fetch_okx_klines(symbol, "1h",  OKX_LIMIT_VELAS)
    velas_4h  = fetch_okx_klines(symbol, "4h",  OKX_LIMIT_VELAS)

    if not velas_15m:
        print(f"   ❌ {symbol}: sin velas 15m. Se omite.", flush=True)
        return False

    rsi15 = calcular_rsi([v["c"] for v in velas_15m])
    rsi1h = calcular_rsi([v["c"] for v in velas_1h]) if velas_1h else None
    rsi4h = calcular_rsi([v["c"] for v in velas_4h]) if velas_4h else None

    price = velas_15m[-1]["c"]

    def direccion(velas):
        if len(velas) < 2:
            return "?"
        return "up" if velas[-1]["c"] > velas[-2]["c"] else "down"

    sample = {
        "ts":     int(ahora.timestamp()),
        "price":  round(price, 8),
        "rsi15":  round(rsi15, 2) if rsi15 is not None else None,
        "rsi1h":  round(rsi1h, 2) if rsi1h is not None else None,
        "rsi4h":  round(rsi4h, 2) if rsi4h is not None else None,
        "dir15":  direccion(velas_15m),
        "dir1h":  direccion(velas_1h) if velas_1h else "?",
    }

    cache = cargar_cache(symbol)
    pulso = cache.get("pulso", [])

    if pulso and pulso[-1].get("ts") == sample["ts"]:
        return False

    pulso.append(sample)

    limite_ts = ahora.timestamp() - RETENCION_PULSO_H * 3600
    pulso = [p for p in pulso if p.get("ts", 0) >= limite_ts]

    cache["symbol"] = symbol
    cache["updated_at"] = ahora.isoformat()
    cache["pulso"] = pulso

    guardar_cache(symbol, cache)

    zona = zona_rsi(rsi15)
    rsi15_str = f"{rsi15:.1f}" if rsi15 is not None else "N/A"
    rsi1h_str = f"{rsi1h:.1f}" if rsi1h is not None else "N/A"
    rsi4h_str = f"{rsi4h:.1f}" if rsi4h is not None else "N/A"

    icono_zona = {
        "extremo": "🔴",
        "recuperacion": "🟡",
        "normal": "🟢",
        "desconocida": "⚪",
    }.get(zona, "⚪")

    print(
        f"   {icono_zona} {symbol} [{zona}]: ${price:.6f} | "
        f"RSI15={rsi15_str} {sample['dir15']} | "
        f"RSI1h={rsi1h_str} | RSI4h={rsi4h_str} | "
        f"pulso={len(pulso)}",
        flush=True
    )
    return True


def main():
    ahora = datetime.now(timezone.utc)
    ahora_ts = ahora.timestamp()

    print("\n" + "=" * 70, flush=True)
    print("📦 RECOLECTOR — Fase 1.5 (3 niveles)", flush=True)
    print(f"   {len(SYMBOLS)} monedas representativas", flush=True)
    print(f"   Extremo       (RSI<{RSI_EXTREMO_BAJO} o >{RSI_EXTREMO_ALTO}):   cada {FREC_EXTREMO_MIN} min", flush=True)
    print(f"   Recuperación  (RSI<{RSI_RECUP_BAJO} o >{RSI_RECUP_ALTO}):    cada {FREC_RECUPERACION_MIN} min", flush=True)
    print(f"   Normal        (RSI {RSI_RECUP_BAJO}-{RSI_RECUP_ALTO}):        cada {FREC_NORMAL_MIN} min", flush=True)
    print(f"   Retención pulso: {RETENCION_PULSO_H}h", flush=True)
    print("=" * 70, flush=True)
    print(f"\nHora UTC: {ahora.isoformat()}", flush=True)

    guardados = 0
    skipped = 0
    fallidos = 0

    for symbol in SYMBOLS:
        cache = cargar_cache(symbol)
        rsi15_previo = rsi_actual_del_cache(cache)
        zona = zona_rsi(rsi15_previo)
        intervalo = frecuencia_para_zona(zona)
        minutos = minutos_desde_ultima_muestra(cache, ahora_ts)

        if minutos < intervalo:
            print(
                f"⏭️ {symbol} [{zona}]: hace {minutos:.0f} min, "
                f"toca cada {intervalo} → skip",
                flush=True
            )
            skipped += 1
            continue

        print(f"\n🔄 {symbol} [{zona}]: hace {minutos:.0f} min → actualizando", flush=True)
        ok = actualizar_pulso(symbol, ahora)
        if ok:
            guardados += 1
        else:
            fallidos += 1

    print("\n" + "=" * 70, flush=True)
    print("📢 RESULTADO", flush=True)
    print("=" * 70, flush=True)
    print(f"Guardados:  {guardados}", flush=True)
    print(f"Skip:       {skipped}", flush=True)
    print(f"Fallidos:   {fallidos}", flush=True)
    print(f"\n💾 Cache dir: {CACHE_DIR}", flush=True)
    print("🏁 PROGRAMA TERMINADO", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("\n❌ ERROR GENERAL:", str(e), flush=True)
        import traceback
        traceback.print_exc()
        raise
