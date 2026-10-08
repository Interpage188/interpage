#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

# ============================================================
# MULTI TF COINBEACON B — FIX 12 + ATR PERCENTIL + NR7
# ============================================================

SYMBOLS = [
    "ARB", "UNI", "LTC", "LINK", "BNB", "ENA",
    "RAY", "ETH", "HYPE", "ZEC", "DOGE", "STX", "DASH",
]

BTC_SYMBOL = "BTC"
TIMEFRAMES = ["15m", "1h"]

MAX_HISTORY_HOURS = 48

SCORE_MIN_INDECISO = 90.0
SCORE_MIN_REBOTE   = 80.0
SALTO_15M          = 5.0
RSI15_ALTO         = 45.0
RSI15_BAJO         = 55.0
RSI1_ALTO          = 48.0
RSI1_BAJO          = 52.0
DELTA_CRUCE        = 1.50
RSI15_TECHO_ENTRADA = 65.0
RSI15_SUELO_ENTRADA = 35.0

PD_VENTANA_MIN = 10
PD_MIN_PCT = 2.0

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

STATE_FILE = DATA_DIR / "multi_tf_coinbeaconB_state.json"
CSV_FILE = DATA_DIR / "multi_tf_coinbeaconB.csv"
HISTORICO_CSV_FILE = DATA_DIR / "historial_lineas_B.csv"
CORRELACION_CSV_FILE = DATA_DIR / "correlacion_btc_alt_B.csv"
DIAG_CSV_FILE = DATA_DIR / "diagnostico_filtros_B.csv"

THROTTLE_FILE = DATA_DIR / "multi_tf_coinbeaconB_throttle.json"
THROTTLE_REMOTE = (
    "https://raw.githubusercontent.com/Interpage188/"
    "interpage/main/data/multi_tf_coinbeaconB_throttle.json"
)

LIMA_OFFSET = timedelta(hours=-5)
HORA_INICIO = 0
HORA_FIN = 24

# ============================================================
# FILTRO 1 — COMPRESIÓN → EXPANSIÓN BTC
# ============================================================
COMP_VENTANA          = 4
COMP_MIN_VELAS        = 12
COMP_RATIO_COMPRESION = 0.70      # legacy (ya no se usa para compresión)
COMP_FACTOR_EXPANSION = 3.0
COMP_HORAS_RECIENTE   = 2
COMP_MODO_FILTRO      = "hard"

COMP_THROTTLE_MIN     = 30

# ============================================================
# NUEVA COMPRESIÓN — ATR PERCENTIL + NR7
# ============================================================
ATR_PERIOD            = 14
ATR_VENTANA           = 100     # últimas 100 muestras de ATR
ATR_UMBRAL_COMPRESION = 20.0    # < 20 = compresión
NR7_PERIOD            = 7       # NR7 (patrón Toby Crabel)

# ============================================================
# CONFIGURACIÓN SQUEEZE MOMENTUM (LazyBear)
# ============================================================
SQZ_BB_LENGTH = 20
SQZ_BB_MULT   = 2.0
SQZ_KC_LENGTH = 20
SQZ_KC_MULT   = 1.5

# ============================================================
# CONFIGURACIÓN ADX
# ============================================================
ADX_LENGTH = 14
ADX_UMBRAL = 23.0

# ============================================================
# CONTADOR DE DIAGNÓSTICO
# ============================================================
CONTADOR_FILTROS = {
    "EDAD": 0,
    "MOMENTUM": 0,
    "ADX": 0,
    "DI": 0,
    "PASA": 0,
    "COMPRESION": 0,
    "SIN_EXPANSION": 0,
}


def hora_permite_envio():
    now_lima = datetime.now(timezone.utc) + LIMA_OFFSET
    return HORA_INICIO <= now_lima.hour < HORA_FIN


COINBEACON_TRENDLINES_URL = "https://api.coinbeacon.io/detectors/trendlines"
COINBEACON_VOLUME_URL = "https://api.coinbeacon.io/detectors/volume"
COINBEACON_PUMPING_URL = "https://api.coinbeacon.io/pumping/events"

COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY")
COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"

COINGECKO_IDS = {
    "BTC": "bitcoin", "ARB": "arbitrum", "UNI": "uniswap",
    "LTC": "litecoin", "INJ": "injective-protocol", "LINK": "chainlink",
    "BNB": "binancecoin", "ENA": "ethena", "SUSHI": "sushi",
    "RAY": "raydium", "ETH": "ethereum", "SOL": "solana",
    "HYPE": "hyperliquid", "ZEC": "zcash", "DOGE": "dogecoin",
    "STX": "blockstack", "DASH": "dash",
}

CACHE_REMOTE_BASE = (
    "https://raw.githubusercontent.com/Interpage188/"
    "interpage/main/data/cache"
)
CACHE_MAX_EDAD_MIN = 40


# ============================================================
# CACHE REMOTO
# ============================================================

def leer_cache_remoto(symbol):
    url = f"{CACHE_REMOTE_BASE}/{symbol}.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"   ⚠️ cache remoto {symbol}: {str(e)[:60]}", flush=True)
        return None

    pulso = data.get("pulso", [])
    if not pulso:
        return None

    ultimo = pulso[-1]
    ts = ultimo.get("ts")
    if not ts:
        return None

    ahora = datetime.now(timezone.utc).timestamp()
    edad_min = (ahora - ts) / 60
    if edad_min > CACHE_MAX_EDAD_MIN:
        print(f"   ⚠️ cache {symbol} viejo ({edad_min:.0f} min)", flush=True)
        return None

    return {
        "symbol": symbol,
        "price": ultimo.get("price"),
        "rsi15": ultimo.get("rsi15"),
        "rsi1h": ultimo.get("rsi1h"),
        "rsi4h": ultimo.get("rsi4h"),
        "dir15": ultimo.get("dir15"),
        "dir1h": ultimo.get("dir1h"),
        "dir4h": ultimo.get("dir4h"),
        "atr_pct15": ultimo.get("atr_pct15"),
        "edad_min": edad_min,
        "n_muestras": len(pulso),
        "pulso": pulso,
        "velas_5m": data.get("velas_5m", []),
    }


def extraer_rsi_del_cache(cache, incluir_4h=False):
    if not cache:
        return {}
    datos = {}
    if cache.get("rsi15") is not None:
        datos["15m"] = {"price": cache.get("price"), "rsi14": cache["rsi15"],
                        "tendencia": cache.get("dir15", "?")}
    if cache.get("rsi1h") is not None:
        datos["1h"] = {"price": cache.get("price"), "rsi14": cache["rsi1h"],
                       "tendencia": cache.get("dir1h", "?")}
    if incluir_4h and cache.get("rsi4h") is not None:
        datos["4h"] = {"price": cache.get("price"), "rsi14": cache["rsi4h"],
                       "tendencia": cache.get("dir4h", "?")}
    return datos


def _media(xs):
    return sum(xs) / len(xs) if xs else 0.0


# ============================================================
# TRADUCCIÓN DE COLORES A TÉRMINOS FÁCILES
# ============================================================

def traducir_color_momentum(color_interno):
    mapa = {
        "lime":   ("SUBE FUERTE",   "🟢", "Alcista confirmado"),
        "green":  ("PIERDE FUERZA", "🟡", "Alcista agotándose"),
        "red":    ("CAE FUERTE",    "🔴", "Bajista confirmado"),
        "maroon": ("GIRA AL ALZA",  "🟠", "Reversión alcista temprana"),
    }
    return mapa.get(color_interno, ("DESCONOCIDO", "⚪", "Sin señal"))


# ============================================================
# DIAGNÓSTICO
# ============================================================

def registrar_diagnostico(fila):
    fieldnames = [
        "ts_lima", "direccion", "edad_h", "fuerza_x",
        "mom_nombre", "mom_valor", "mom_etiqueta",
        "adx", "di_plus", "di_minus",
        "resultado", "razon",
    ]
    try:
        if not DIAG_CSV_FILE.exists():
            with DIAG_CSV_FILE.open("w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=fieldnames).writeheader()
        with DIAG_CSV_FILE.open("a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=fieldnames).writerow(fila)
    except Exception as e:
        print(f"   ⚠️ No se pudo escribir diagnóstico: {e}", flush=True)


# ============================================================
# CONSTRUCCIÓN DE VELAS
# ============================================================

def construir_velas_de_cache(cache):
    if not cache:
        return []

    velas_raw = cache.get("velas_5m") or []

    if velas_raw:
        velas = []
        for v in velas_raw:
            try:
                o = float(v["o"])
                c = float(v["c"])
                h = float(v.get("h", max(o, c)))
                l = float(v.get("l", min(o, c)))
                vol = v.get("v")
            except (TypeError, ValueError, KeyError):
                continue
            velas.append({
                "timestamp": v["ts"] / 1000,
                "open":  o,
                "close": c,
                "high":  h,
                "low":   l,
                "volumen": vol,
                "rango": abs(c - o),
            })
        if velas:
            return velas

    pulso = cache.get("pulso") or []
    datos = []
    for p in pulso:
        try:
            datos.append((float(p.get("ts")), float(p.get("price"))))
        except (TypeError, ValueError):
            continue
    if len(datos) < 2:
        return []
    datos.sort(key=lambda x: x[0])
    return [
        {
            "timestamp": datos[i][0],
            "open":  datos[i - 1][1],
            "close": datos[i][1],
            "high":  max(datos[i - 1][1], datos[i][1]),
            "low":   min(datos[i - 1][1], datos[i][1]),
            "volumen": None,
            "rango": abs(datos[i][1] - datos[i - 1][1]),
        }
        for i in range(1, len(datos))
    ]


# ============================================================
# ATR PERCENTIL — Detección de compresión adaptativa
# ============================================================

def calcular_atr_percentile(velas, period=14, ventana=100):
    """
    Percentil del ATR actual respecto a los últimos `ventana` ATRs.
    0 = ATR más bajo del histórico (compresión máxima)
    100 = ATR más alto del histórico (expansión máxima)
    """
    if len(velas) < period + ventana + 1:
        return None

    # --- Paso 1: True Ranges ---
    trs = []
    for i in range(1, len(velas)):
        high = velas[i]["high"]
        low  = velas[i]["low"]
        pc   = velas[i-1]["close"]
        tr = max(high - low, abs(high - pc), abs(low - pc))
        trs.append(tr)

    if len(trs) < period + ventana:
        return None

    # --- Paso 2: ATRs con suma móvil O(n) ---
    atrs = []
    suma = sum(trs[:period])
    atrs.append(suma / period)
    for i in range(period, len(trs)):
        suma = suma - trs[i - period] + trs[i]
        atrs.append(suma / period)

    if len(atrs) < ventana:
        return None

    # --- Paso 3: Percentil ---
    actual = atrs[-1]
    historico = atrs[-ventana:]
    menores = sum(1 for x in historico if x <= actual)
    return round((menores / len(historico)) * 100, 2)


def es_nr7(velas, period=7):
    """NR7: la vela actual tiene el rango más estrecho de las últimas 7 velas."""
    if len(velas) < period:
        return False
    rangos = [v["rango"] for v in velas[-period:]]
    return rangos[-1] == min(rangos)


# ============================================================
# SQUEEZE MOMENTUM (LazyBear)
# ============================================================

def _sma(serie, length):
    if len(serie) < length:
        return None
    return sum(serie[-length:]) / length


def _stdev(serie, length):
    if len(serie) < length:
        return None
    ventana = serie[-length:]
    m = sum(ventana) / length
    return (sum((x - m) ** 2 for x in ventana) / length) ** 0.5


def _linreg_value(y):
    n = len(y)
    if n < 2:
        return y[-1] if y else 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(y) / n
    num = sum((i - x_mean) * (y[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    if den == 0:
        return y[-1]
    slope = num / den
    return y_mean + slope * ((n - 1) - x_mean)


def calcular_squeeze_momentum(velas, length=20, mult=2.0,
                              lengthKC=20, multKC=1.5):
    if len(velas) < 2 * lengthKC:
        return None

    highs  = [v["high"]  for v in velas]
    lows   = [v["low"]   for v in velas]
    closes = [v["close"] for v in velas]
    n = lengthKC

    basis = _sma(closes, length)
    dev   = _stdev(closes, length)
    if basis is None or dev is None:
        return None
    dev *= mult
    upperBB, lowerBB = basis + dev, basis - dev

    ma = _sma(closes, n)
    if ma is None:
        return None
    trs = []
    for i in range(len(closes)):
        if i == 0:
            trs.append(highs[i] - lows[i])
        else:
            pc = closes[i - 1]
            trs.append(max(highs[i] - lows[i],
                           abs(highs[i] - pc),
                           abs(lows[i] - pc)))
    rangema = _sma(trs, n)
    if rangema is None:
        return None
    upperKC = ma + rangema * multKC
    lowerKC = ma - rangema * multKC

    squeeze_on  = (lowerBB > lowerKC) and (upperBB < upperKC)
    squeeze_off = (lowerBB < lowerKC) and (upperBB > upperKC)

    serie_mom = []
    for i in range(n - 1, len(closes)):
        hh = max(highs[i - n + 1:i + 1])
        ll = min(lows[i - n + 1:i + 1])
        sma_c = sum(closes[i - n + 1:i + 1]) / n
        ref = 0.25 * (hh + ll) + 0.5 * sma_c
        serie_mom.append(closes[i] - ref)

    if len(serie_mom) < n + 1:
        return None

    m_actual = _linreg_value(serie_mom[-n:])
    m_prev   = _linreg_value(serie_mom[-n - 1:-1])

    if m_actual > 0:
        color = "lime" if m_actual > m_prev else "green"
    else:
        color = "red"  if m_actual < m_prev else "maroon"

    return {
        "squeeze_on":    squeeze_on,
        "squeeze_off":   squeeze_off,
        "momentum":      m_actual,
        "momentum_prev": m_prev,
        "color":         color,
    }


# ============================================================
# ADX
# ============================================================

def calcular_adx(velas, length=14):
    n = len(velas)
    if n < length * 2:
        return None

    highs = [v["high"] for v in velas]
    lows = [v["low"] for v in velas]
    closes = [v["close"] for v in velas]

    tr_list, plus_dm_list, minus_dm_list = [], [], []
    for i in range(1, n):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i-1]),
                 abs(lows[i] - closes[i-1]))
        tr_list.append(tr)

        up_move = highs[i] - highs[i-1]
        down_move = lows[i-1] - lows[i]

        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0

        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)

    def smooth(data, period):
        smoothed = [sum(data[:period])]
        for i in range(period, len(data)):
            smoothed.append(smoothed[-1] - (smoothed[-1] / period) + data[i])
        return smoothed

    atr_smooth = smooth(tr_list, length)
    plus_dm_smooth = smooth(plus_dm_list, length)
    minus_dm_smooth = smooth(minus_dm_list, length)

    di_plus_list, di_minus_list, dx_list = [], [], []
    for i in range(len(atr_smooth)):
        if atr_smooth[i] == 0:
            continue
        di_plus = (plus_dm_smooth[i] / atr_smooth[i]) * 100
        di_minus = (minus_dm_smooth[i] / atr_smooth[i]) * 100
        di_plus_list.append(di_plus)
        di_minus_list.append(di_minus)

        di_sum = di_plus + di_minus
        if di_sum != 0:
            dx_list.append(abs(di_plus - di_minus) / di_sum * 100)

    if len(dx_list) < length:
        return None

    adx = sum(dx_list[-length:]) / length

    return {
        "adx": adx,
        "di_plus": di_plus_list[-1] if di_plus_list else None,
        "di_minus": di_minus_list[-1] if di_minus_list else None,
    }


# ============================================================
# ANÁLISIS DE PATRÓN BTC
# ============================================================

def analizar_patron_btc(btc_cache):
    global CONTADOR_FILTROS

    if not btc_cache:
        CONTADOR_FILTROS["SIN_EXPANSION"] += 1
        return {"pasa": False, "estado": "sin_datos", "detalle": "sin cache BTC"}

    velas = construir_velas_de_cache(btc_cache)
    n = len(velas)

    if n < COMP_MIN_VELAS:
        CONTADOR_FILTROS["SIN_EXPANSION"] += 1
        return {"pasa": False, "estado": "sin_datos", "detalle": f"solo {n} velas"}

    ahora = datetime.now(timezone.utc).timestamp()
    ts_lima = (datetime.now(timezone.utc) + LIMA_OFFSET).strftime("%Y-%m-%d %H:%M")

    # --- Nuevas métricas de compresión ---
    atr_pct = calcular_atr_percentile(velas, ATR_PERIOD, ATR_VENTANA)
    nr7 = es_nr7(velas, NR7_PERIOD)

    # --- Squeeze Momentum ---
    sqz = calcular_squeeze_momentum(velas, SQZ_BB_LENGTH, SQZ_BB_MULT,
                                    SQZ_KC_LENGTH, SQZ_KC_MULT)
    if sqz is None:
        CONTADOR_FILTROS["SIN_EXPANSION"] += 1
        return {"pasa": False, "estado": "neutral",
                "detalle": "faltan velas para momentum"}

    mom_color = sqz["color"]
    mom_val   = sqz["momentum"]
    mom_nombre, mom_emoji, _ = traducir_color_momentum(mom_color)

    # --- ADX ---
    adx_data = calcular_adx(velas, ADX_LENGTH)
    if adx_data is None:
        CONTADOR_FILTROS["SIN_EXPANSION"] += 1
        return {"pasa": False, "estado": "neutral",
                "detalle": "faltan velas para ADX"}

    adx_val = adx_data["adx"]
    di_plus = adx_data["di_plus"]
    di_minus = adx_data["di_minus"]

    # ═══════════════════════════════════════════════════════════
    # BÚSQUEDA DE EXPANSIÓN
    # ═══════════════════════════════════════════════════════════
    hay_expansion = False

    for k in range(max(0, n - 8), n):
        vela_actual = velas[k]["rango"]
        anteriores = [velas[i]["rango"] for i in range(max(0, k-6), k)]
        if not anteriores:
            continue
        prom_previo = _media(anteriores)
        if not (prom_previo > 0 and vela_actual > prom_previo * COMP_FACTOR_EXPANSION):
            continue

        hay_expansion = True
        fuerza_x = vela_actual / prom_previo
        edad_h = (ahora - velas[k]["timestamp"]) / 3600
        d = "up" if velas[k]["close"] > velas[k]["open"] else "down"

        base_diag = {
            "ts_lima": ts_lima,
            "direccion": d.upper(),
            "edad_h": f"{edad_h:.2f}",
            "fuerza_x": f"{fuerza_x:.2f}",
            "mom_nombre": mom_nombre,
            "mom_valor": f"{mom_val:+.4f}",
            "mom_etiqueta": "",
            "adx": f"{adx_val:.2f}",
            "di_plus": f"{di_plus:.2f}" if di_plus is not None else "N/A",
            "di_minus": f"{di_minus:.2f}" if di_minus is not None else "N/A",
        }

        if edad_h > COMP_HORAS_RECIENTE:
            base_diag["resultado"] = "RECHAZA"
            base_diag["razon"] = f"EDAD ({edad_h:.1f}h > {COMP_HORAS_RECIENTE}h)"
            registrar_diagnostico(base_diag)
            CONTADOR_FILTROS["EDAD"] += 1
            print(f"   ⏭️ Expansión {d.upper()} rechazada por EDAD "
                  f"({edad_h:.1f}h > {COMP_HORAS_RECIENTE}h)", flush=True)
            continue

        etiqueta = ""

        if d == "up":
            if mom_color == "maroon":
                etiqueta = "TEMPRANO"
            elif mom_color == "lime":
                etiqueta = "CONFIRMADO"
            else:
                base_diag["resultado"] = "RECHAZA"
                base_diag["razon"] = f"MOMENTUM ({mom_nombre})"
                registrar_diagnostico(base_diag)
                CONTADOR_FILTROS["MOMENTUM"] += 1
                print(f"   ⏭️ Expansión UP rechazada por MOMENTUM "
                      f"({mom_nombre}, {mom_val:+.4f})", flush=True)
                continue
        else:
            if mom_color == "green":
                etiqueta = "TEMPRANO"
            elif mom_color == "red":
                etiqueta = "CONFIRMADO"
            else:
                base_diag["resultado"] = "RECHAZA"
                base_diag["razon"] = f"MOMENTUM ({mom_nombre})"
                registrar_diagnostico(base_diag)
                CONTADOR_FILTROS["MOMENTUM"] += 1
                print(f"   ⏭️ Expansión DOWN rechazada por MOMENTUM "
                      f"({mom_nombre}, {mom_val:+.4f})", flush=True)
                continue

        base_diag["mom_etiqueta"] = etiqueta

        if adx_val < ADX_UMBRAL:
            base_diag["resultado"] = "RECHAZA"
            base_diag["razon"] = f"ADX ({adx_val:.1f} < {ADX_UMBRAL})"
            registrar_diagnostico(base_diag)
            CONTADOR_FILTROS["ADX"] += 1
            print(f"   ⏭️ Expansión {d.upper()} rechazada por ADX "
                  f"({adx_val:.1f} < {ADX_UMBRAL})", flush=True)
            continue

        if d == "up" and (di_plus is None or di_minus is None or di_plus <= di_minus):
            base_diag["resultado"] = "RECHAZA"
            base_diag["razon"] = f"DI ({di_plus} <= {di_minus})"
            registrar_diagnostico(base_diag)
            CONTADOR_FILTROS["DI"] += 1
            print(f"   ⏭️ Expansión UP rechazada por DI", flush=True)
            continue
        if d == "down" and (di_plus is None or di_minus is None or di_minus <= di_plus):
            base_diag["resultado"] = "RECHAZA"
            base_diag["razon"] = f"DI ({di_minus} <= {di_plus})"
            registrar_diagnostico(base_diag)
            CONTADOR_FILTROS["DI"] += 1
            print(f"   ⏭️ Expansión DOWN rechazada por DI", flush=True)
            continue

        base_diag["resultado"] = "PASA"
        base_diag["razon"] = f"OK [{etiqueta}]"
        registrar_diagnostico(base_diag)
        CONTADOR_FILTROS["PASA"] += 1

        print(f"   ✅ EXPANSIÓN {d.upper()} CONFIRMADA — "
              f"{mom_nombre} [{etiqueta}] | ADX {adx_val:.1f} | "
              f"ATR% {atr_pct if atr_pct is not None else 'N/A'}", flush=True)

        return {
            "pasa": True,
            "estado": "expandiendo",
            "direccion": d,
            "precio": velas[k]["close"],
            "fuerza": fuerza_x,
            "edad_h": edad_h,
            "momentum":        mom_val,
            "momentum_prev":   sqz["momentum_prev"],
            "momentum_color":  mom_color,
            "momentum_nombre": mom_nombre,
            "momentum_emoji":  mom_emoji,
            "momentum_etiqueta": etiqueta,
            "squeeze_on":      sqz["squeeze_on"],
            "adx": adx_val,
            "di_plus": di_plus,
            "di_minus": di_minus,
            "atr_pct": atr_pct,
            "nr7": nr7,
            "detalle": (f"expansión {d.upper()} hace {edad_h:.1f}h "
                        f"({fuerza_x:.1f}x) | "
                        f"mom {mom_nombre} [{etiqueta}] {mom_val:+.4f} | "
                        f"ADX {adx_val:.1f} | "
                        f"ATR% {atr_pct if atr_pct is not None else 'N/A'}")
        }

    # ═══════════════════════════════════════════════════════════
    # SIN EXPANSIÓN — EVALUAR COMPRESIÓN POR ATR PERCENTIL
    # ═══════════════════════════════════════════════════════════

    if not hay_expansion:
        CONTADOR_FILTROS["SIN_EXPANSION"] += 1

    # Compresión: ATR percentil bajo
    if atr_pct is not None and atr_pct < ATR_UMBRAL_COMPRESION:
        CONTADOR_FILTROS["COMPRESION"] += 1
        nr7_txt = " | NR7 ✅" if nr7 else ""
        detalle = f"compresión ATR%={atr_pct:.1f} (<{ATR_UMBRAL_COMPRESION}){nr7_txt}"
        print(f"   🌀 COMPRESIÓN detectada — {detalle}", flush=True)
        return {
            "pasa": False,
            "estado": "comprimiendo",
            "atr_pct": atr_pct,
            "nr7": nr7,
            "detalle": detalle,
        }

    # ═══════════════════════════════════════════════════════════
    # ✅ NUEVO: Detectar caída/subida fuerte DISTRIBUIDA
    # (aunque no haya vela explosiva única)
    # ═══════════════════════════════════════════════════════════
    if len(velas) >= 24:
        # Cambio en las últimas 12 velas (= 1h si velas son de 5m)
        vela_ini = velas[-12]
        p_actual = velas[-1]["close"]
        p_ini = vela_ini["close"]

        if p_ini > 0:
            cambio_reciente = ((p_actual - p_ini) / p_ini) * 100

            # Volumen reciente vs previo
            vols_recientes = [v.get("volumen") for v in velas[-12:] if v.get("volumen")]
            vols_previos = [v.get("volumen") for v in velas[-24:-12] if v.get("volumen")]

            vol_ratio = 1.0
            if vols_recientes and vols_previos:
                prom_rec = sum(vols_recientes) / len(vols_recientes)
                prom_prev = sum(vols_previos) / len(vols_previos)
                if prom_prev > 0:
                    vol_ratio = prom_rec / prom_prev

            # ═══ CAÍDA FUERTE DISTRIBUIDA ═══
            if cambio_reciente < -1.3 and vol_ratio >= 1.3:
                print(f"   🔴 CAÍDA FUERTE DISTRIBUIDA: {cambio_reciente:+.2f}% | vol {vol_ratio:.2f}x", flush=True)
                CONTADOR_FILTROS["PASA"] += 1

                return {
                    "pasa": True,
                    "estado": "expandiendo",
                    "direccion": "down",
                    "precio": p_actual,
                    "fuerza": vol_ratio,
                    "edad_h": 1.0,
                    "momentum":        mom_val,
                    "momentum_prev":   sqz["momentum_prev"],
                    "momentum_color":  mom_color,
                    "momentum_nombre": mom_nombre,
                    "momentum_emoji":  mom_emoji,
                    "momentum_etiqueta": "CONFIRMADO",
                    "squeeze_on":      sqz["squeeze_on"],
                    "adx": adx_val,
                    "di_plus": di_plus,
                    "di_minus": di_minus,
                    "atr_pct": atr_pct,
                    "nr7": nr7,
                    "detalle": (f"caída distribuida {cambio_reciente:+.2f}% | "
                                f"vol {vol_ratio:.2f}x | ADX {adx_val:.1f}"),
                }

            # ═══ VOLUMEN MASIVO (cambio pequeño pero volumen 3x+) ═══
            if vol_ratio >= 3.0 and abs(cambio_reciente) >= 0.3:
                direccion = "down" if cambio_reciente < 0 else "up"
                emoji = "🔴" if direccion == "down" else "🟢"
                print(f"   {emoji} VOLUMEN MASIVO BTC: {cambio_reciente:+.2f}% | vol {vol_ratio:.2f}x", flush=True)
                CONTADOR_FILTROS["PASA"] += 1

                return {
                    "pasa": True,
                    "estado": "expandiendo",
                    "direccion": direccion,
                    "precio": p_actual,
                    "fuerza": vol_ratio,
                    "edad_h": 1.0,
                    "momentum":        mom_val,
                    "momentum_prev":   sqz["momentum_prev"],
                    "momentum_color":  mom_color,
                    "momentum_nombre": mom_nombre,
                    "momentum_emoji":  mom_emoji,
                    "momentum_etiqueta": "CONFIRMADO",
                    "squeeze_on":      sqz["squeeze_on"],
                    "adx": adx_val,
                    "di_plus": di_plus,
                    "di_minus": di_minus,
                    "atr_pct": atr_pct,
                    "nr7": nr7,
                    "detalle": (f"volumen masivo {vol_ratio:.2f}x | "
                                f"cambio {cambio_reciente:+.2f}% | ADX {adx_val:.1f}"),
                }

            # ═══ SUBIDA FUERTE DISTRIBUIDA ═══
            if cambio_reciente > 1.3 and vol_ratio >= 1.3:
                print(f"   🟢 SUBIDA FUERTE DISTRIBUIDA: {cambio_reciente:+.2f}% | vol {vol_ratio:.2f}x", flush=True)
                CONTADOR_FILTROS["PASA"] += 1

                return {
                    "pasa": True,
                    "estado": "expandiendo",
                    "direccion": "up",
                    "precio": p_actual,
                    "fuerza": vol_ratio,
                    "edad_h": 1.0,
                    "momentum":        mom_val,
                    "momentum_prev":   sqz["momentum_prev"],
                    "momentum_color":  mom_color,
                    "momentum_nombre": mom_nombre,
                    "momentum_emoji":  mom_emoji,
                    "momentum_etiqueta": "CONFIRMADO",
                    "squeeze_on":      sqz["squeeze_on"],
                    "adx": adx_val,
                    "di_plus": di_plus,
                    "di_minus": di_minus,
                    "atr_pct": atr_pct,
                    "nr7": nr7,
                    "detalle": (f"subida distribuida {cambio_reciente:+.2f}% | "
                                f"vol {vol_ratio:.2f}x | ADX {adx_val:.1f}"),
                }

    # ═══════════════════════════════════════════════════════════
    # RETURN FINAL — Estado neutral (sin oportunidad)
    # ═══════════════════════════════════════════════════════════
    return {"pasa": False, "estado": "neutral",
            "detalle": f"rango normal (ATR% {atr_pct if atr_pct is not None else 'N/A'})"}
# ============================================================
# THROTTLE
# ============================================================

def cargar_throttle():
    try:
        req = urllib.request.Request(
            THROTTLE_REMOTE, headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        return {
            "ok": True,
            "estado": data.get("estado"),
            "ts": data.get("ts", 0),
        }
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"ok": True, "estado": None, "ts": 0}
        print(f"   ⚠️ throttle HTTP error {e.code}", flush=True)
        return {"ok": False, "estado": None, "ts": 0}
    except Exception as e:
        print(f"   ⚠️ throttle remoto: {str(e)[:60]}", flush=True)
        return {"ok": False, "estado": None, "ts": 0}


def guardar_throttle(estado):
    try:
        with THROTTLE_FILE.open("w", encoding="utf-8") as f:
            json.dump({
                "estado": estado,
                "ts": datetime.now(timezone.utc).timestamp(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, f, indent=2)
    except Exception as e:
        print(f"⚠️ No se pudo guardar throttle: {e}", flush=True)


# ============================================================
# PUMP/DUMP
# ============================================================

def consultar_pumping_events():
    token = os.environ.get("COINBEACON_TOKEN")
    if not token:
        return []
    types = "pump_5m,dump_5m"
    url = f"{COINBEACON_PUMPING_URL}?exchange=binance&types={types}&pair=USDT&limit=500"
    headers = {"User-Agent": "Mozilla/5.0", "Cookie": f"access_token={token}",
               "Accept": "application/json"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data.get("items", [])
    except Exception as e:
        print(f"   ⚠️ pump/dump: {str(e)[:80]}", flush=True)
        return []


def indexar_pumping_events(eventos):
    index = {}
    for ev in eventos:
        symbol_base = ev.get("symbol", "").replace("USDT", "")
        tipo = ev.get("type", "")
        spotted_at = ev.get("spottedAt", 0)
        if symbol_base not in index:
            index[symbol_base] = {}
        actual = index[symbol_base].get(tipo)
        if actual is None or spotted_at > actual.get("spottedAt", 0):
            index[symbol_base][tipo] = ev
    return index


def pd_para_symbol(symbol, pd_index):
    resultado = {"activo": False, "tipo": None, "direccion": None,
                 "pct": 0.0, "rvol": 0.0, "vol_conf": False,
                 "edad_min": 0.0, "reversal": False, "continuation": False}
    if symbol not in pd_index:
        return resultado
    ahora_ts = datetime.now(timezone.utc).timestamp()
    mejor, mejor_ts = None, 0
    for tipo, ev in pd_index[symbol].items():
        spotted_at = ev.get("spottedAt", 0)
        if spotted_at > mejor_ts:
            mejor, mejor_ts = ev, spotted_at
    if not mejor:
        return resultado
    edad_min = (ahora_ts * 1000 - mejor_ts) / 60000
    resultado["tipo"] = mejor.get("type")
    resultado["direccion"] = "up" if "pump" in str(mejor.get("type", "")) else "down"
    resultado["pct"] = mejor.get("pct", 0.0)
    resultado["rvol"] = mejor.get("rvol", 0.0)
    resultado["vol_conf"] = mejor.get("volConfirmed", False)
    resultado["edad_min"] = edad_min
    resultado["reversal"] = str(mejor.get("classification", "")).lower() == "reversal"
    resultado["continuation"] = str(mejor.get("classification", "")).lower() == "continuation"
    if edad_min <= PD_VENTANA_MIN and abs(resultado["pct"]) >= PD_MIN_PCT:
        resultado["activo"] = True
    return resultado


# ============================================================
# HELPERS
# ============================================================

def numero(valor):
    if valor is None:
        return None
    try:
        return float(valor)
    except (ValueError, TypeError):
        return None


def distancia_porcentual(precio, nivel):
    if precio is None or nivel is None or precio == 0:
        return None
    return ((nivel - precio) / precio) * 100


def consultar_coinbeacon(endpoint, timeframe, limit, symbol=None):
    token = os.environ.get("COINBEACON_TOKEN")
    if not token:
        raise RuntimeError("Falta COINBEACON_TOKEN")
    url = f"{endpoint}?exchange=binance&timeframe={timeframe}&quote=USDT&limit={limit}"
    if symbol:
        url += f"&symbol={symbol}"
    headers = {"User-Agent": "Mozilla/5.0", "Cookie": f"access_token={token}"}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def consultar_coinbeacon_trendlines(symbol, timeframe):
    pair = symbol + "USDT"
    result = consultar_coinbeacon(COINBEACON_TRENDLINES_URL, timeframe, 200, symbol=pair)
    items = result.get("items", [])
    lineas = []
    for item in items:
        item_symbol = str(item.get("symbol", "")).upper()
        if item_symbol != pair:
            continue
        item_data = item.get("item", {})
        touch_count = int(numero(item_data.get("touchCount", 0)) or 0)
        lineas.append({
            "symbol": item_symbol, "price": numero(item.get("price")),
            "bias": item.get("bias"), "confidence": numero(item.get("confidence", 0)),
            "type": item_data.get("type"),
            "currentLevel": numero(item_data.get("currentLevel")),
            "status": item_data.get("status"), "touchCount": touch_count,
            "fallingOrRising": item_data.get("fallingOrRising", "flat"),
            "computedAt": item.get("computedAt"), "timeframe": timeframe,
        })
    return lineas


def consultar_volume_coinbeacon():
    result = consultar_coinbeacon(COINBEACON_VOLUME_URL, "4h", 500)
    items = result.get("items", [])
    return [{
        "symbol": item.get("symbol"), "price": numero(item.get("price")),
        "volumeTrend": numero(item.get("volumeTrend")) or 0.0,
        "volume24h": numero(item.get("volume24h")) or 0.0,
        "rvoll": numero(item.get("rvoll")) or 0.0,
        "direction": item.get("direction") or "unknown",
        "status": item.get("status") or "", "spottedAt": item.get("spottedAt"),
    } for item in items]


def clasificar_estructura(touches):
    if touches >= 11: return "VERY_STRONG"
    if touches >= 8:  return "STRONG"
    if touches >= 6:  return "VALID"
    if touches >= 4:  return "WEAK"
    return "IGNORE"


def obtener_touch_score(touches):
    if touches < 4: return 0
    tabla = {4:8, 5:12, 6:16, 7:20, 8:25, 9:28, 10:31}
    if touches >= 11: return 35 + min((touches - 11) * 2, 15)
    return tabla.get(touches, 0)


def obtener_proximidad_score(distance_pct, timeframe):
    if distance_pct is None or distance_pct <= 0:
        return 0
    max_dist = 2.0 if timeframe == "1h" else 1.5
    if distance_pct > max_dist:
        return 0
    return 35 - ((distance_pct / max_dist) * 30)


def obtener_timeframe_score(timeframe):
    return {"1h": 19, "15m": 15}.get(timeframe, 0)


def obtener_status_score(status):
    s = str(status).lower()
    score = 0
    if "near breakout" in s: score += 20
    if "near breakdown" in s: score += 20
    if "retest" in s: score += 15
    if "broken" in s: score -= 30
    if "confirmed" in s: score += 10
    return score


def obtener_confidence_score(confidence):
    if confidence is None:
        return 0
    if confidence <= 1: normalized = confidence * 100
    elif confidence <= 10: normalized = confidence * 10
    else: normalized = confidence
    normalized = max(0, min(100, normalized))
    return (normalized / 100) * 20


def obtener_smart_score(line):
    smart = line.get("smartSetup") or line.get("smartSetupScore") or 0
    if smart:
        return (smart / 10) * 15 if smart <= 10 else 15
    return 0


def calcular_score_linea(linea):
    touches = linea.get("touchCount", 0)
    estructura = clasificar_estructura(touches)
    structure_score = obtener_touch_score(touches)
    proximity_score = obtener_proximidad_score(linea.get("distance_pct"), linea.get("timeframe"))
    timeframe_score = obtener_timeframe_score(linea.get("timeframe"))
    status_score = obtener_status_score(linea.get("status"))
    confidence_score = obtener_confidence_score(linea.get("confidence"))
    smart_score = obtener_smart_score(linea)
    bonus = 0
    if linea.get("type") == "support" and "rising" in str(linea.get("fallingOrRising", "")).lower():
        bonus += 10
    if linea.get("type") == "resistance" and "falling" in str(linea.get("fallingOrRising", "")).lower():
        bonus += 10
    total_score = (structure_score + proximity_score + timeframe_score
                   + status_score + confidence_score + smart_score + bonus)
    return {"structure_quality": estructura, "structure_score": structure_score,
            "proximity_score": proximity_score, "timeframe_score": timeframe_score,
            "status_score": status_score, "confidence_score": confidence_score,
            "smart_score": smart_score, "bonus_score": bonus,
            "total_score": total_score}


def analizar_coinbeacon(symbol):
    print(f"\n📈 COINBEACON — {symbol}", flush=True)
    print("=" * 70, flush=True)
    todas = []
    for timeframe in TIMEFRAMES:
        try:
            lineas = consultar_coinbeacon_trendlines(symbol, timeframe)
            print(f"   {timeframe}: {len(lineas)} líneas", flush=True)
            todas.extend(lineas)
        except Exception as e:
            print(f"   ❌ {timeframe}: {e}", flush=True)

    if not todas:
        return {"price": None, "lines": []}

    precio = None
    for linea in todas:
        if linea.get("price") is not None:
            precio = linea["price"]
            break

    if precio is not None:
        print(f"💰 Precio: ${precio:.6f}", flush=True)

    for linea in todas:
        linea["distance_pct"] = distancia_porcentual(precio, linea.get("currentLevel"))
        if linea["distance_pct"] is not None:
            linea.update(calcular_score_linea(linea))
        else:
            linea["structure_quality"] = "UNKNOWN"
            linea["total_score"] = 0

    return {"price": precio, "lines": todas}


# ============================================================
# COINGECKO FALLBACK
# ============================================================

def obtener_precios_coingecko(symbol, days=7):
    coin_id = COINGECKO_IDS.get(symbol)
    if not coin_id:
        raise ValueError(f"Sin mapeo CG: {symbol}")
    headers = {"Accept": "application/json"}
    if COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_API_KEY
    url = COINGECKO_BASE_URL.format(coin_id=coin_id)
    r = requests.get(url, headers=headers, params={"vs_currency": "usd", "days": days}, timeout=30)
    r.raise_for_status()
    prices = r.json().get("prices", [])
    if not prices:
        raise ValueError("Sin precios CG")
    return prices


def agrupar_precios(prices, intervalo_segundos):
    if not prices:
        return []
    buckets = {}
    for ts_ms, price in prices:
        ts = int(ts_ms / 1000)
        bucket = ts - (ts % intervalo_segundos)
        buckets[bucket] = price
    return [buckets[k] for k in sorted(buckets)]


def calcular_rsi(prices, period=14):
    if len(prices) < period + 1:
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
    return 100 - (100 / (1 + avg_gain / avg_loss))


def analizar_rsi_coingecko(symbol, incluir_4h=False):
    cache = leer_cache_remoto(symbol)
    if cache:
        return extraer_rsi_del_cache(cache, incluir_4h)

    try:
        prices = obtener_precios_coingecko(symbol, days=7)
    except Exception:
        return {}
    if not prices:
        return {}
    intervalos = {"15m": 15*60, "1h": 60*60}
    if incluir_4h:
        intervalos["4h"] = 4*60*60
    datos = {}
    for tf, segs in intervalos.items():
        agrupados = agrupar_precios(prices, segs)
        if len(agrupados) < 15:
            continue
        rsi = calcular_rsi(agrupados)
        if rsi is None:
            continue
        precio_tf, precio_ant = agrupados[-1], agrupados[-2]
        tendencia = "up" if precio_tf > precio_ant else "down"
        datos[tf] = {"price": precio_tf, "rsi14": rsi, "tendencia": tendencia}
    return datos


def evaluar_rsi(datos, mostrar=True):
    if not datos:
        return False, "N/A"
    d15, d1h = datos.get("15m"), datos.get("1h")
    if not d15 or not d1h:
        return False, "Incompleto"
    rsi15, rsi1 = d15.get("rsi14"), d1h.get("rsi14")
    if rsi15 is None or rsi1 is None:
        return False, "Incompleto"
    cumple_1h = rsi1 > 30
    cumple_15m = rsi15 > 30
    return (cumple_1h and cumple_15m), f"1h={rsi1:.2f} | 15m={rsi15:.2f}"


# ============================================================
# BTC CONTEXTO
# ============================================================

def evaluar_btc_rsi(btc_rsi_data, rsi4_anterior=None, mostrar=True):
    d4, d1, d15 = btc_rsi_data.get("4h"), btc_rsi_data.get("1h"), btc_rsi_data.get("15m")
    if not d4 or not d1 or not d15:
        return False, "incompleto"
    rsi4, rsi1, rsi15 = d4.get("rsi14"), d1.get("rsi14"), d15.get("rsi14")
    if rsi4 is None or rsi1 is None or rsi15 is None:
        return False, "incompleto"
    cumple_4h = rsi4 > 50
    cumple_1h = rsi1 > 30
    cumple_15m = rsi15 > 30
    tendencia_alcista = rsi4_anterior is not None and rsi4 > rsi4_anterior
    btc_ok = (cumple_4h and cumple_1h and cumple_15m) or (tendencia_alcista and cumple_1h and cumple_15m)
    return (True, "favorable") if btc_ok else (False, "desfavorable")


def analizar_contexto_btc(btc_coin_data, btc_rsi_data, rsi4_anterior=None,
                          btc_cache=None, prev_btc=None):
    precio_btc = None
    for tf in ["4h", "1h", "15m"]:
        d = btc_rsi_data.get(tf)
        if d and d.get("price") is not None:
            precio_btc = d["price"]
            break
    if precio_btc is None:
        precio_btc = btc_coin_data.get("price")

    rsi15 = btc_rsi_data.get("15m", {}).get("rsi14") if btc_rsi_data.get("15m") else None
    rsi1  = btc_rsi_data.get("1h", {}).get("rsi14")  if btc_rsi_data.get("1h")  else None
    rsi4  = btc_rsi_data.get("4h", {}).get("rsi14")  if btc_rsi_data.get("4h")  else None

    btc_rsi_ok, estado = evaluar_btc_rsi(btc_rsi_data, rsi4_anterior, mostrar=False)

    delta_2h = None
    if rsi4 is not None and rsi4_anterior is not None:
        delta_2h = rsi4 - rsi4_anterior

    prev_btc = prev_btc or {}
    rsi15_prev = prev_btc.get("rsi15")

    impulso_up_corto = False
    impulso_down_corto = False

    if rsi15 is not None and rsi1 is not None:
        subida_15m = (rsi15_prev is not None and (rsi15 - rsi15_prev) >= SALTO_15M)
        bajada_15m = (rsi15_prev is not None and (rsi15_prev - rsi15) >= SALTO_15M)
        if (rsi15 >= RSI15_ALTO and rsi1 >= RSI1_ALTO) or subida_15m:
            impulso_up_corto = True
        if (rsi15 <= RSI15_BAJO and rsi1 <= RSI1_BAJO) or bajada_15m:
            impulso_down_corto = True
        if impulso_up_corto and rsi15 > RSI15_TECHO_ENTRADA:
            impulso_up_corto = False
        if impulso_down_corto and rsi15 < RSI15_SUELO_ENTRADA:
            impulso_down_corto = False

    RSI_SOBREVENTA = 35.0
    RSI_BAJA_SALUDABLE = 48.0
    RSI_CENTRAL = 52.0
    RSI_SOBRECOMPRA = 65.0
    D_UP_FUERTE, D_UP_INDECISO = 0.34, 0.10
    D_DOWN_INDECISO, D_DOWN_FUERTE = -0.10, -0.34

    btc_dir, btc_modo, razon = "flat", "neutro", "neutro"
    score_min_req = 0.0

    if rsi4 is None:
        razon = "RSI4h N/A"
    elif rsi4 >= RSI_SOBRECOMPRA:
        if impulso_down_corto:
            btc_dir, btc_modo = "down", "indeciso"
            razon = "sobrecompra + impulso DOWN"
            score_min_req = SCORE_MIN_INDECISO
        elif delta_2h is None:
            razon = "sobrecompra sin delta"
        elif delta_2h > D_UP_FUERTE:
            btc_dir, btc_modo = "up", "fuerte"; razon = f"sobrecompra Δ{delta_2h:+.2f}"
        elif delta_2h > D_UP_INDECISO:
            btc_dir, btc_modo = "up", "indeciso"; razon = f"sobrecompra Δ{delta_2h:+.2f}"
            score_min_req = SCORE_MIN_INDECISO
        elif delta_2h < D_DOWN_FUERTE:
            btc_dir, btc_modo = "down", "fuerte"; razon = f"sobrecompra Δ{delta_2h:+.2f}"
        elif delta_2h < D_DOWN_INDECISO:
            btc_dir, btc_modo = "down", "indeciso"; razon = f"sobrecompra Δ{delta_2h:+.2f}"
            score_min_req = SCORE_MIN_INDECISO
        else:
            razon = "sobrecompra neutro"
    elif rsi4 >= RSI_CENTRAL:
        if impulso_down_corto:
            btc_dir, btc_modo = "down", "indeciso"
            razon = "alta saludable + impulso DOWN"
            score_min_req = SCORE_MIN_REBOTE
        elif delta_2h is not None and delta_2h < -DELTA_CRUCE:
            btc_dir, btc_modo = "down", "indeciso"
            razon = f"alta Δ{delta_2h:+.2f} gira DOWN"
            score_min_req = SCORE_MIN_REBOTE
        else:
            btc_dir, btc_modo = "up", "fuerte"
            razon = f"RSI4h {rsi4:.2f} alta saludable"
    elif rsi4 >= RSI_BAJA_SALUDABLE:
        if impulso_up_corto:
            btc_dir, btc_modo = "up", "indeciso"
            razon = "central + impulso UP"
            score_min_req = SCORE_MIN_INDECISO
        elif impulso_down_corto:
            btc_dir, btc_modo = "down", "indeciso"
            razon = "central + impulso DOWN"
            score_min_req = SCORE_MIN_INDECISO
        elif delta_2h is None:
            razon = "central sin delta"
        elif delta_2h > D_UP_FUERTE:
            btc_dir, btc_modo = "up", "fuerte"; razon = f"central Δ{delta_2h:+.2f}"
        elif delta_2h > D_UP_INDECISO:
            btc_dir, btc_modo = "up", "indeciso"; razon = f"central Δ{delta_2h:+.2f}"
            score_min_req = SCORE_MIN_INDECISO
        elif delta_2h < D_DOWN_FUERTE:
            btc_dir, btc_modo = "down", "fuerte"; razon = f"central Δ{delta_2h:+.2f}"
        elif delta_2h < D_DOWN_INDECISO:
            btc_dir, btc_modo = "down", "indeciso"; razon = f"central Δ{delta_2h:+.2f}"
            score_min_req = SCORE_MIN_INDECISO
        else:
            razon = "central neutro"
    elif rsi4 >= RSI_SOBREVENTA:
        if impulso_up_corto:
            btc_dir, btc_modo = "up", "indeciso"
            razon = "baja saludable + impulso UP"
            score_min_req = SCORE_MIN_REBOTE
        elif delta_2h is not None and delta_2h > DELTA_CRUCE:
            btc_dir, btc_modo = "up", "indeciso"
            razon = f"baja Δ{delta_2h:+.2f} gira UP"
            score_min_req = SCORE_MIN_REBOTE
        else:
            btc_dir, btc_modo = "down", "fuerte"
            razon = f"RSI4h {rsi4:.2f} baja saludable"
    else:
        if impulso_up_corto:
            btc_dir, btc_modo = "up", "indeciso"
            razon = "sobreventa + impulso UP"
            score_min_req = SCORE_MIN_INDECISO
        elif delta_2h is None:
            razon = "sobreventa sin delta"
        elif delta_2h < D_DOWN_FUERTE:
            btc_dir, btc_modo = "down", "fuerte"; razon = f"sobreventa Δ{delta_2h:+.2f}"
        elif delta_2h < D_DOWN_INDECISO:
            btc_dir, btc_modo = "down", "indeciso"; razon = f"sobreventa Δ{delta_2h:+.2f}"
            score_min_req = SCORE_MIN_INDECISO
        elif delta_2h > D_UP_FUERTE:
            btc_dir, btc_modo = "up", "fuerte"; razon = f"sobreventa Δ{delta_2h:+.2f}"
        elif delta_2h > D_UP_INDECISO:
            btc_dir, btc_modo = "up", "indeciso"; razon = f"sobreventa Δ{delta_2h:+.2f}"
            score_min_req = SCORE_MIN_INDECISO
        else:
            razon = "sobreventa neutro"

    estado_btc = "FAVORABLE" if btc_rsi_ok else "DESFAVORABLE"

    return {
        "price": precio_btc, "rsi_ok": btc_rsi_ok, "estado": estado_btc,
        "rsi15": rsi15, "rsi1": rsi1, "rsi4": rsi4,
        "btc_dir": btc_dir, "btc_modo": btc_modo, "razon_ventana": razon,
        "delta_2h": delta_2h, "rsi4_delta": delta_2h if delta_2h is not None else 0.0,
        "impulso_up_corto": impulso_up_corto,
        "impulso_down_corto": impulso_down_corto,
        "score_min_requerido": score_min_req,
    }


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram_message(message):
    if not hora_permite_envio():
        return False
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = f"chat_id={urllib.parse.quote(str(chat_id))}&text={urllib.parse.quote(message)}".encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        if result.get("ok"):
            print("   📢 Telegram enviado", flush=True)
            return True
        return False
    except Exception as e:
        print(f"   ⚠️ Error Telegram: {e}", flush=True)
        return False


# ============================================================
# CSV, ESTADO
# ============================================================

def guardar_en_csv(alert_data):
    fieldnames = [
        "hora_lima", "symbol", "bias", "type", "timeframe",
        "currentLevel", "touchCount", "confidence", "rvoll", "volumeTrend",
        "score", "status", "fallingOrRising", "price", "tipo_efectivo",
        "rsi1h", "rsi15m", "tendencia",
        "btc_rsi4h", "btc_rsi1h", "btc_rsi15m", "btc_estado",
        "btc_dir", "btc_modo", "btc_razon",
        "pd_tipo", "pd_pct", "pd_rvol", "pd_conf",
        "structure_quality", "total_score"
    ]
    if not CSV_FILE.exists():
        with CSV_FILE.open("w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=fieldnames).writeheader()
    with CSV_FILE.open("a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=fieldnames).writerow(alert_data)


def cargar_estado():
    if not STATE_FILE.exists():
        return []
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            estado = json.load(f)
        if isinstance(estado, list):
            return estado
    except Exception:
        pass
    return []


def limpiar_estado(previous_state, now_ts):
    resultado = []
    for item in previous_state:
        da = item.get("detected_at")
        if da is None:
            resultado.append(item)
            continue
        try:
            if (now_ts - float(da)) / 3600 < MAX_HISTORY_HOURS:
                resultado.append(item)
        except (ValueError, TypeError):
            continue
    return resultado


def guardar_estado(estado):
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(estado, f, indent=2)


# ============================================================
# CONFLUENCIA
# ============================================================

def _prioridad_patron(linea):
    s = str(linea.get("status", "")).lower()
    if "near" in s: return 2
    if "retest" in s: return 1
    return 0


def analizar_confluencia(symbol, coin_data, rsi_data, volume_by_symbol, btc_context, pd_index=None):
    precio = coin_data.get("price")
    lineas = coin_data.get("lines", [])
    if precio is None:
        return []

    btc_dir = btc_context.get("btc_dir", "flat")
    btc_modo = btc_context.get("btc_modo", "neutro")
    razon_ventana = btc_context.get("razon_ventana", "neutro")
    btc_rsi4 = btc_context.get("rsi4")
    btc_rsi1 = btc_context.get("rsi1")
    btc_rsi15 = btc_context.get("rsi15")
    btc_estado = btc_context.get("estado", "DESFAVORABLE")

    tendencia_15m = rsi_data.get("15m", {}).get("tendencia") if rsi_data else None
    tendencia_1h = rsi_data.get("1h", {}).get("tendencia") if rsi_data else None

    if btc_modo == "neutro":
        return []

    operacion_permitida = "LONG" if btc_dir == "up" else "SHORT"
    score_min_req = btc_context.get("score_min_requerido", 0.0)
    pd = pd_para_symbol(symbol, pd_index or {})

    volume = volume_by_symbol.get(symbol + "USDT") or volume_by_symbol.get(symbol)
    if volume:
        vol_status = str(volume.get("status", "")).lower()
        is_spike = "spike" in vol_status
        is_dry_up = "dry" in vol_status or "squeeze" in vol_status
        is_exhaustion = "exhaustion" in vol_status
    else:
        is_spike = is_dry_up = is_exhaustion = False

    candidates_by_tf = {}
    for linea in lineas:
        distancia = linea.get("distance_pct")
        if distancia is None:
            continue
        status = str(linea.get("status", "")).lower()
        if "broken" in status and "retest" not in status:
            continue
        quality = linea.get("structure_quality", "IGNORE")
        if quality in ("IGNORE", "WEAK"):
            continue
        tf = linea.get("timeframe")
        max_dist = 2.0 if tf == "1h" else 1.5
        tendencia = tendencia_1h if tf == "1h" else tendencia_15m
        if abs(distancia) > max_dist:
            continue
        if tendencia == "up":
            nivel_esperado = "resistance"
        elif tendencia == "down":
            nivel_esperado = "support"
        else:
            continue
        tipo_efectivo = linea.get("type")
        if tipo_efectivo == "resistance" and distancia < 0:
            tipo_efectivo = "support"
        elif tipo_efectivo == "support" and distancia > 0:
            tipo_efectivo = "resistance"
        if tipo_efectivo != nivel_esperado:
            continue
        operacion = operacion_permitida
        total_score = linea.get("total_score", 0)

        if pd["activo"]:
            if operacion == "LONG" and pd["direccion"] == "down" and not pd["reversal"]:
                continue
            if operacion == "SHORT" and pd["direccion"] == "up" and not pd["reversal"]:
                continue

        pd_confirmado = False
        if pd["activo"]:
            if operacion == "LONG" and pd["direccion"] == "up":
                pd_confirmado = True
            if operacion == "SHORT" and pd["direccion"] == "down":
                pd_confirmado = True

        if score_min_req > 0 and total_score < score_min_req and not pd_confirmado:
            continue

        candidates_by_tf.setdefault(tf, []).append({
            "line": linea, "operacion": operacion, "score": total_score,
            "tendencia": tendencia, "tipo_efectivo": tipo_efectivo,
            "pd_confirmado": pd_confirmado,
        })

    final_candidates = []
    for tf, items in candidates_by_tf.items():
        if items:
            final_candidates.append(max(items, key=lambda x: x["score"]))
    final_candidates.sort(key=lambda x: x["score"], reverse=True)

    seen = set()
    filtered = []
    for cand in final_candidates:
        s = cand["line"].get("symbol")
        if s in seen:
            continue
        seen.add(s)
        filtered.append(cand)

    alerts = []
    for cand in filtered:
        linea = cand["line"]
        alerts.append({
            "symbol": symbol, "line": linea,
            "volume": volume if volume else {},
            "score": cand["score"],
            "is_spike": is_spike, "is_dry_up": is_dry_up,
            "is_exhaustion": is_exhaustion,
            "is_near_breakout": "near breakout" in str(linea.get("status", "")).lower(),
            "is_near_breakdown": "near breakdown" in str(linea.get("status", "")).lower(),
            "is_retest": "retest" in str(linea.get("status", "")).lower(),
            "bias": cand["operacion"],
            "tipo_efectivo": cand.get("tipo_efectivo"),
            "rsi1h": rsi_data.get("1h", {}).get("rsi14") if rsi_data else None,
            "rsi15m": rsi_data.get("15m", {}).get("rsi14") if rsi_data else None,
            "tendencia": cand.get("tendencia"),
            "btc_rsi4h": btc_rsi4, "btc_rsi1h": btc_rsi1, "btc_rsi15m": btc_rsi15,
            "btc_estado": btc_estado,
            "btc_dir": btc_dir, "btc_modo": btc_modo, "btc_razon": razon_ventana,
            "structure_quality": linea.get("structure_quality"),
            "total_score": cand["score"],
            "pd_confirmado": cand.get("pd_confirmado", False),
            "pd_activo": pd["activo"], "pd_tipo": pd["tipo"], "pd_pct": pd["pct"],
            "pd_rvol": pd["rvol"], "pd_vol_conf": pd["vol_conf"], "pd_reversal": pd["reversal"],
        })
    return alerts


# ============================================================
# PROCESAR ALERTAS
# ============================================================

def procesar_alertas(alerts, filtered_previous, btc_context, pd_index):
    sent_count = 0
    long_count = 0
    short_count = 0
    new_state = list(filtered_previous)
    now_ts = datetime.now(timezone.utc).timestamp()
    now_lima = (datetime.now(timezone.utc) + LIMA_OFFSET).strftime("%Y-%m-%d %H:%M:%S")

    for alert in alerts:
        symbol = alert["symbol"]
        line = alert["line"]
        vol = alert["volume"]
        operacion = alert["bias"]

        key = f"{symbol}_{line['type']}_{line['timeframe']}_{line['currentLevel']}"
        if any(f"{it.get('symbol')}_{it.get('type')}_{it.get('timeframe')}_{it.get('currentLevel')}" == key
               for it in new_state):
            continue

        emoji = "🟢" if operacion == "LONG" else "🔴" if operacion == "SHORT" else "⚪"
        if operacion == "LONG":
            long_count += 1
        elif operacion == "SHORT":
            short_count += 1

        tipo_linea = (line.get("type") or "?").upper()
        tipo_efectivo = (alert.get("tipo_efectivo") or line.get("type") or "").upper()
        inclinacion = (line.get("fallingOrRising", "flat")).upper()
        precio_actual = line.get("price") or 0.0
        nivel_linea = line.get("currentLevel") or 0.0
        tendencia = alert.get("tendencia") or "?"
        flecha = "↑" if tendencia == "up" else "↓" if tendencia == "down" else "?"
        flip_text = " [FLIP]" if tipo_efectivo != tipo_linea else ""

        tags = []
        if alert.get("is_near_breakout"): tags.append("🔥 NEAR BREAKOUT")
        if alert.get("is_near_breakdown"): tags.append("💀 NEAR BREAKDOWN")
        if alert.get("is_retest"): tags.append("🔄 RETEST")
        if alert.get("is_spike"): tags.append("🚀 VOL SPIKE")
        if alert.get("is_dry_up"): tags.append("💧 DRY-UP")
        if alert.get("is_exhaustion"): tags.append("🪫 EXHAUSTION")
        if alert.get("pd_confirmado"): tags.append("🔥 PD CONFIRMADO")
        tag_text = " ".join(tags) if tags else ""

        rsi1h_str = f"{alert.get('rsi1h'):.2f}" if alert.get('rsi1h') is not None else "N/A"
        rsi15m_str = f"{alert.get('rsi15m'):.2f}" if alert.get('rsi15m') is not None else "N/A"
        btc_rsi4h_str = f"{alert.get('btc_rsi4h'):.2f}" if alert.get('btc_rsi4h') is not None else "N/A"

        btc_dir_str = alert.get("btc_dir", "flat").upper()
        btc_modo_str = alert.get("btc_modo", "neutro").upper()
        btc_razon = alert.get("btc_razon", "neutro")
        delta_2h_val = btc_context.get("delta_2h")
        delta_2h_txt = f"{delta_2h_val:+.2f}" if delta_2h_val is not None else "N/A"

        pd_tipo = alert.get("pd_tipo")
        pd_pct = alert.get("pd_pct", 0.0)
        pd_rvol = alert.get("pd_rvol", 0.0)
        pd_vol_conf = alert.get("pd_vol_conf", False)
        pd_reversal = alert.get("pd_reversal", False)
        pd_confirmado = alert.get("pd_confirmado", False)

        if pd_tipo:
            flecha_pd = "🚀" if "pump" in pd_tipo else "💥"
            tipo_pd_str = "PUMP 5m" if "pump" in pd_tipo else "DUMP 5m"
            if pd_reversal:
                tipo_pd_str += " REVERSAL"
            vol_mark = " ⚡" if pd_vol_conf else ""
            confirm = " ✅ CONFIRMA" if pd_confirmado else ""
            pd_linea = f"📡 PD: {flecha_pd} {tipo_pd_str} {pd_pct:+.2f}% (rvol {pd_rvol:.2f}){vol_mark}{confirm}"
        else:
            pd_linea = "📡 PD: sin movimiento"

        pat = btc_context.get("patron_btc") or {}
        pat_estado = pat.get("estado", "?").upper()
        pat_det = pat.get("detalle", "")
        flecha_pat = "🔥" if pat_estado == "EXPANDIENDO" else "🌀" if pat_estado == "COMPRIMIENDO" else "⚪"
        patron_linea = f"{flecha_pat} Patrón: {pat_estado} — {pat_det}"

        mom_line = ""
        if pat.get("momentum") is not None:
            mom_color_interno = (pat.get("momentum_color") or "").lower()
            mom_nombre, mom_emoji, mom_signif = traducir_color_momentum(mom_color_interno)
            mom_val = pat.get("momentum")
            mom_etq = pat.get("momentum_etiqueta", "")

            if mom_etq == "TEMPRANO":
                badge = "🟠 TEMPRANO"
            elif mom_etq == "CONFIRMADO":
                badge = "🟢 CONFIRMADO"
            else:
                badge = ""

            mom_line = (f"📈 Momentum: {mom_emoji} {mom_nombre} "
                        f"{mom_val:+.4f} — {badge}\n")
            mom_line += f"   ({mom_signif})\n"

        if pat.get("adx") is not None:
            adx_val = pat.get("adx")
            adx_emoji = "✅" if adx_val >= ADX_UMBRAL else "⚠️"
            mom_line += f"📊 ADX: {adx_emoji} {adx_val:.1f} (umbral {ADX_UMBRAL})\n"

        sqz_txt = ""
        if pat.get("squeeze_on") is not None:
            if pat.get("squeeze_on"):
                sqz_txt = "🔵 Mercado COMPRIMIDO — resorte cargando\n"
            else:
                sqz_txt = "⚪ Mercado LIBERADO — volatilidad activa\n"

        msg = (
            f"🧠 MULTI TF\n"
            f"{emoji} {operacion} {symbol}\n"
            f"📈 Precio: ${precio_actual:.6f}\n"
            f"📉 {tipo_linea}{flip_text} ({inclinacion})\n"
            f"   • TF: {line.get('timeframe', '')}\n"
            f"   • Nivel: ${nivel_linea:.6f}\n"
            f"   • Toques: {line.get('touchCount', 0)} ({alert['structure_quality']})\n"
            f"🎯 Score: {alert['score']:.1f}\n"
            f"📈 Tendencia: {flecha} {tendencia}\n"
            f"{mom_line}"
            f"{sqz_txt}"
            f"📊 BTC: {btc_dir_str} {btc_modo_str} (RSI4H {btc_rsi4h_str} | Δ{delta_2h_txt})\n"
            f"{pd_linea}\n"
            f"{patron_linea}\n"
            f"🕐 {now_lima}\n"
        )
        if tag_text:
            msg += f"{tag_text}\n"
        msg += "━━━━━━━━━━━━━━━━━━━"

        if send_telegram_message(msg):
            sent_count += 1
            guardar_en_csv({
                "hora_lima": now_lima, "symbol": symbol, "bias": operacion,
                "type": line.get("type", ""), "timeframe": line.get("timeframe", ""),
                "currentLevel": str(nivel_linea),
                "touchCount": str(line.get("touchCount", 0)),
                "confidence": str(line.get("confidence", 0)),
                "rvoll": f"{vol.get('rvoll', 0):.2f}",
                "volumeTrend": f"{vol.get('volumeTrend', 0):.1f}",
                "score": f"{alert['score']:.1f}", "status": line.get("status", ""),
                "fallingOrRising": line.get("fallingOrRising", "flat"),
                "price": str(precio_actual), "tipo_efectivo": tipo_efectivo,
                "rsi1h": rsi1h_str, "rsi15m": rsi15m_str, "tendencia": tendencia,
                "btc_rsi4h": btc_rsi4h_str, "btc_rsi1h": "N/A", "btc_rsi15m": "N/A",
                "btc_estado": alert.get("btc_estado", ""),
                "btc_dir": btc_dir_str, "btc_modo": btc_modo_str, "btc_razon": btc_razon,
                "pd_tipo": pd_tipo or "",
                "pd_pct": f"{pd_pct:+.2f}" if pd_tipo else "",
                "pd_rvol": f"{pd_rvol:.2f}" if pd_tipo else "",
                "pd_conf": "1" if pd_vol_conf else "0",
                "structure_quality": alert['structure_quality'],
                "total_score": f"{alert['score']:.1f}",
            })
            new_state.append({
                "symbol": symbol, "type": line.get("type"),
                "timeframe": line.get("timeframe"),
                "currentLevel": nivel_linea, "detected_at": now_ts,
            })

    return sent_count, long_count, short_count, new_state


def analizar_moneda(symbol, volume_by_symbol, btc_context, btc_rsi_data, hora_lima, pd_index=None):
    coin_data = analizar_coinbeacon(symbol)
    rsi_data = analizar_rsi_coingecko(symbol, incluir_4h=False)
    alerts = analizar_confluencia(symbol, coin_data, rsi_data, volume_by_symbol, btc_context, pd_index)
    return alerts, coin_data, rsi_data


# ============================================================
# RESUMEN DIAGNÓSTICO
# ============================================================

def imprimir_resumen_diagnostico():
    total = sum(CONTADOR_FILTROS.values())
    print("\n" + "=" * 70, flush=True)
    print("🔬 DIAGNÓSTICO — ¿Qué detuvo cada señal en este run?", flush=True)
    print("=" * 70, flush=True)

    if total == 0:
        print("   (Sin evaluaciones registradas en este ciclo)", flush=True)
        return

    orden = sorted(CONTADOR_FILTROS.items(), key=lambda x: -x[1])
    for nombre, count in orden:
        if count == 0:
            continue
        pct = (count / total) * 100
        barra = "█" * int(pct / 3)
        print(f"   {nombre:15s} {count:3d}  ({pct:5.1f}%)  {barra}", flush=True)

    print("-" * 70, flush=True)
    print(f"   TOTAL evaluaciones: {total}", flush=True)

    print("\n   💡 Sugerencia automática:", flush=True)
    if CONTADOR_FILTROS["MOMENTUM"] > total * 0.5:
        print("      ⚠️ MOMENTUM rechaza >50% → considerar aceptar 'green'/'maroon'", flush=True)
    if CONTADOR_FILTROS["ADX"] > total * 0.4:
        print("      ⚠️ ADX rechaza >40% → considerar bajar umbral a 20", flush=True)
    if CONTADOR_FILTROS["EDAD"] > total * 0.3:
        print("      ⚠️ EDAD rechaza >30% → considerar subir COMP_HORAS_RECIENTE a 3-4h", flush=True)
    if CONTADOR_FILTROS["PASA"] == 0 and total > 5:
        print("      ⚠️ 0 señales pasaron → sistema sobre-filtrado", flush=True)
    if 0 < CONTADOR_FILTROS["PASA"] <= 3:
        print("      ✅ Balance saludable — pocas señales pero pasan las buenas", flush=True)


def main():
    global CONTADOR_FILTROS
    CONTADOR_FILTROS = {k: 0 for k in CONTADOR_FILTROS}

    # ✅ NUEVO: Forzar creación del CSV de diagnóstico
    DIAG_CSV_FILE.parent.mkdir(exist_ok=True)
    if not DIAG_CSV_FILE.exists():
        fieldnames = [
            "ts_lima", "direccion", "edad_h", "fuerza_x",
            "mom_nombre", "mom_valor", "mom_etiqueta",
            "adx", "di_plus", "di_minus",
            "resultado", "razon",
        ]
        with DIAG_CSV_FILE.open("w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=fieldnames).writeheader()

    print("\n" + "=" * 70, flush=True)
    print("🚀 MULTI TF COINBEACON B — FIX 12 + ATR PERCENTIL + NR7", flush=True)
    print("=" * 70, flush=True)
    print(f"\nHora UTC: {datetime.now(timezone.utc).isoformat()}", flush=True)

    print("\n🔍 FILTRO 1: compresión→expansión BTC...", flush=True)
    btc_cache_full = leer_cache_remoto(BTC_SYMBOL)
    patron_btc = analizar_patron_btc(btc_cache_full)
    print(f"   Estado:  {patron_btc['estado'].upper()}", flush=True)
    print(f"   Detalle: {patron_btc['detalle']}", flush=True)

    estado_actual = patron_btc["estado"]
    ahora_ts = datetime.now(timezone.utc).timestamp()

    lectura = cargar_throttle()

    if not lectura["ok"]:
        print("   ⚠️ No se pudo leer el throttle remoto → NO se enviará aviso", flush=True)
        debe_avisar = False
    else:
        estado_anterior = lectura["estado"]
        ts_anterior = lectura["ts"]

        if estado_anterior is None:
            print("   📭 Throttle no existe → primera vez, se enviará", flush=True)
            debe_avisar = True
        else:
            cambio_estado = (estado_actual != estado_anterior)
            reintentar = (ahora_ts - ts_anterior) > (COMP_THROTTLE_MIN * 60)
            debe_avisar = cambio_estado or reintentar
            print(f"   📖 Anterior: {estado_anterior} | Actual: {estado_actual} | "
                  f"Δts: {(ahora_ts - ts_anterior)/60:.1f} min | "
                  f"avisar: {debe_avisar}", flush=True)

    if debe_avisar:
        guardar_throttle(estado_actual)

        # ═══════════════════════════════════════════════════════════
        # SOLO se envía Telegram cuando hay expansión CONFIRMADA.
        # Estados "comprimiendo" y "neutral" quedan en SILENCIO.
        # ═══════════════════════════════════════════════════════════
        if estado_actual == "expandiendo":
            direccion = patron_btc.get("direccion", "?")
            operacion = "LONG" if direccion == "up" else "SHORT"
            emoji_op = "🟢" if direccion == "up" else "🔴"
            precio_actual = patron_btc.get("precio", 0)
            fuerza = patron_btc.get("fuerza", 0)
            edad_h = patron_btc.get("edad_h", 0)

            mom_color_interno = (patron_btc.get("momentum_color") or "").lower()
            mom_nombre, mom_emoji, mom_signif = traducir_color_momentum(mom_color_interno)
            mom_val = patron_btc.get("momentum")
            mom_etq = patron_btc.get("momentum_etiqueta", "")

            if mom_etq == "TEMPRANO":
                badge = "🟠 TEMPRANO"
            elif mom_etq == "CONFIRMADO":
                badge = "🟢 CONFIRMADO"
            else:
                badge = ""

            mom_val_txt = f"{mom_val:+.4f}" if mom_val is not None else "N/A"

            if patron_btc.get("squeeze_on"):
                sqz_txt = "🔵 Mercado COMPRIMIDO — resorte cargando"
            else:
                sqz_txt = "⚪ Mercado LIBERADO — volatilidad activa"

            adx_val = patron_btc.get("adx")
            adx_txt = f"{adx_val:.1f}" if adx_val is not None else "N/A"
            adx_emoji = "✅" if adx_val and adx_val >= ADX_UMBRAL else "⚠️"

            atr_pct_txt = patron_btc.get("atr_pct")
            atr_line = f"📊 ATR%: {atr_pct_txt:.1f}\n" if atr_pct_txt is not None else ""

            ahora_lima_str = (datetime.now(timezone.utc) + LIMA_OFFSET).strftime("%Y-%m-%d %H:%M")

            send_telegram_message(
                f"🧠 MULTI TF\n"
                f"🔥 EXPANSIÓN {direccion.upper()} — {emoji_op} {operacion} BTC\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"📍 Precio: ${precio_actual:,.2f}\n"
                f"📊 Fuerza: {fuerza:.1f}x hace {edad_h:.1f}h\n"
                f"📈 Momentum: {mom_emoji} {mom_nombre} {mom_val_txt} — {badge}\n"
                f"   ({mom_signif})\n"
                f"{sqz_txt}\n"
                f"📊 ADX: {adx_emoji} {adx_txt} (umbral {ADX_UMBRAL})\n"
                f"{atr_line}"
                f"🎯 Dirección: {operacion}\n"
                f"✅ Filtro pasa → analizando monedas...\n"
                f"🕐 {ahora_lima_str} (Lima)"
            )
        else:
            print(f"   🔇 Estado {estado_actual.upper()} — sin envío (solo expansión)", flush=True)
    else:
        print("   🔇 Throttle activo — sin envío", flush=True)

    if COMP_MODO_FILTRO == "hard" and not patron_btc["pasa"]:
        print(f"\n⏸️ Filtro no pasó ({estado_actual.upper()}) — abortando en silencio", flush=True)
        imprimir_resumen_diagnostico()
        return

    print("✅ Filtro pasó\n", flush=True)

    previous_state = cargar_estado()
    rsi4_anterior = None
    prev_btc = {}
    for item in previous_state:
        if item.get("type") == "btc_rsi":
            rsi4_anterior = item.get("rsi4")
            prev_btc = {"rsi4": item.get("rsi4"), "rsi1": item.get("rsi1"),
                        "rsi15": item.get("rsi15")}
            break

    btc_coin_data = analizar_coinbeacon(BTC_SYMBOL)
    btc_rsi_data = (extraer_rsi_del_cache(btc_cache_full, incluir_4h=True)
                    if btc_cache_full
                    else analizar_rsi_coingecko(BTC_SYMBOL, incluir_4h=True))
    btc_context = analizar_contexto_btc(btc_coin_data, btc_rsi_data, rsi4_anterior,
                                        btc_cache_full, prev_btc=prev_btc)
    btc_context["patron_btc"] = patron_btc

    pd_eventos = consultar_pumping_events()
    pd_index = indexar_pumping_events(pd_eventos)

    try:
        volume_data = consultar_volume_coinbeacon()
    except Exception as e:
        print(f"❌ Volumen: {e}", flush=True)
        volume_data = []
    volume_by_symbol = {str(i.get("symbol", "")).upper(): i for i in volume_data}

    now_ts = datetime.now(timezone.utc).timestamp()
    filtered_previous = limpiar_estado(previous_state, now_ts)
    alertas_previas = [it for it in filtered_previous if it.get("type") != "btc_rsi"]
    hora_lima = (datetime.now(timezone.utc) + LIMA_OFFSET).strftime("%Y-%m-%d %H:%M:%S")

    all_alerts = []
    for symbol in SYMBOLS:
        alerts, _, _ = analizar_moneda(symbol, volume_by_symbol, btc_context,
                                       btc_rsi_data, hora_lima, pd_index)
        all_alerts.extend(alerts)

    all_alerts.sort(key=lambda x: (0 if x["bias"] == "LONG" else 1, -x["score"]))

    sent_count, long_count, short_count, new_state = procesar_alertas(
        all_alerts, alertas_previas, btc_context, pd_index
    )

    new_state.append({
        "type": "btc_rsi",
        "rsi4": btc_context.get("rsi4"),
        "rsi1": btc_context.get("rsi1"),
        "rsi15": btc_context.get("rsi15"),
        "detected_at": now_ts,
    })
    guardar_estado(new_state)

    imprimir_resumen_diagnostico()

    print("\n" + "=" * 70, flush=True)
    print("📢 RESULTADO FINAL", flush=True)
    print("=" * 70, flush=True)
    print(f"Alertas: {sent_count} | LONG: {long_count} | SHORT: {short_count}", flush=True)
    print(f"Patrón: {patron_btc['estado'].upper()}", flush=True)
    print(f"📄 Diagnóstico guardado en: {DIAG_CSV_FILE}", flush=True)
    print("\n🏁 PROGRAMA TERMINADO", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ ERROR GENERAL: {e}", flush=True)
        import traceback
        traceback.print_exc()
        raise
