#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

# ============================================================
# MULTI TF COINBEACON B — FIX 9 + PD + FILTRO COMPRESIÓN
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
PATRON_STATE_FILE = DATA_DIR / "patron_btc_ultimo_estado.json"

LIMA_OFFSET = timedelta(hours=-5)
HORA_INICIO = 0
HORA_FIN = 24

# ============================================================
# FILTRO 1 — COMPRESIÓN → EXPANSIÓN BTC
# ============================================================
COMP_VENTANA          = 4
COMP_MIN_VELAS        = 12
COMP_RATIO_COMPRESION = 0.70
COMP_FACTOR_EXPANSION = 3.0
COMP_HORAS_RECIENTE   = 6
COMP_MODO_FILTRO      = "hard"


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


# ============================================================
# FILTRO 1 — COMPRESIÓN → EXPANSIÓN (funciones)
# ============================================================

def _media(xs):
    return sum(xs) / len(xs) if xs else 0.0


def construir_velas_de_cache(cache):
    if not cache:
        return []
    velas_raw = cache.get("velas_5m") or []
    if velas_raw:
        velas = []
        for v in velas_raw:
            try:
                o, c = float(v.get("o")), float(v.get("c"))
            except (TypeError, ValueError):
                continue
            velas.append({
                "timestamp": v["ts"] / 1000,
                "open":  o,
                "close": c,
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
            "rango": abs(datos[i][1] - datos[i - 1][1]),
        }
        for i in range(1, len(datos))
    ]


def analizar_patron_btc(btc_cache):
    if not btc_cache:
        return {"pasa": False, "estado": "sin_datos", "detalle": "sin cache BTC"}

    velas = construir_velas_de_cache(btc_cache)
    n = len(velas)

    if n < COMP_MIN_VELAS:
        return {"pasa": False, "estado": "sin_datos",
                "detalle": f"solo {n} velas"}

    v = COMP_VENTANA
    r_ult = _media([x["rango"] for x in velas[-v:]])
    r_pre = _media([x["rango"] for x in velas[-2 * v:-v]])

    if r_pre <= 0:
        return {"pasa": False, "estado": "neutral", "detalle": "sin rango previo"}

    ratio = r_ult / r_pre
    ahora = datetime.now(timezone.utc).timestamp()

    for k in range(max(0, n - 3), n):
        rango_v = velas[k]["rango"]
        if r_ult > 0 and rango_v > r_ult * COMP_FACTOR_EXPANSION:
            edad_h = (ahora - velas[k]["timestamp"]) / 3600
            if edad_h <= COMP_HORAS_RECIENTE:
                d = "up" if velas[k]["close"] > velas[k]["open"] else "down"
                return {"pasa": True, "estado": "expandiendo",
                        "detalle": f"expansión {d.upper()} hace {edad_h:.1f}h "
                                   f"({rango_v / r_ult:.1f}x)"}

    if ratio < COMP_RATIO_COMPRESION:
        return {"pasa": True, "estado": "comprimiendo",
                "detalle": f"comprimiendo {ratio:.2f}x"}

    return {"pasa": False, "estado": "neutral",
            "detalle": f"rango normal ({ratio:.2f}x)"}


def cargar_ultimo_patron():
    if not PATRON_STATE_FILE.exists():
        return None
    try:
        with PATRON_STATE_FILE.open("r", encoding="utf-8") as f:
            return json.load(f).get("estado")
    except Exception:
        return None


def guardar_ultimo_patron(estado):
    try:
        with PATRON_STATE_FILE.open("w", encoding="utf-8") as f:
            json.dump({"estado": estado,
                       "updated_at": datetime.now(timezone.utc).isoformat()}, f)
    except Exception as e:
        print(f"⚠️ No se pudo guardar estado patrón: {e}", flush=True)


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

        msg = (
            f"📊 MULTI TF COINBEACON B\n"
            f"{emoji} {operacion} {symbol}\n"
            f"📈 Precio: ${precio_actual:.6f}\n"
            f"📉 {tipo_linea}{flip_text} ({inclinacion})\n"
            f"   • TF: {line.get('timeframe', '')}\n"
            f"   • Nivel: ${nivel_linea:.6f}\n"
            f"   • Toques: {line.get('touchCount', 0)} ({alert['structure_quality']})\n"
            f"🎯 Score: {alert['score']:.1f}\n"
            f"📈 Momentum: {flecha} {tendencia}\n"
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


def main():
    print("\n" + "=" * 70, flush=True)
    print("🚀 MULTI TF COINBEACON B — FIX 9 + PD + FILTRO COMPRESIÓN", flush=True)
    print("=" * 70, flush=True)
    print(f"\nHora UTC: {datetime.now(timezone.utc).isoformat()}", flush=True)

    # ============================================================
    # FILTRO 1
    # ============================================================
    print("\n🔍 FILTRO 1: compresión→expansión BTC...", flush=True)
    btc_cache_full = leer_cache_remoto(BTC_SYMBOL)
    patron_btc = analizar_patron_btc(btc_cache_full)
    print(f"   Estado:  {patron_btc['estado'].upper()}", flush=True)
    print(f"   Detalle: {patron_btc['detalle']}", flush=True)

    # ============================================================
    # AVISO INTELIGENTE: solo cuando CAMBIA el estado
    # ============================================================
    estado_actual = patron_btc["estado"]
    estado_anterior = cargar_ultimo_patron()
    cambio_estado = (estado_actual != estado_anterior)
    guardar_ultimo_patron(estado_actual)

    if cambio_estado:
        ahora_lima_str = (datetime.now(timezone.utc) + LIMA_OFFSET).strftime("%Y-%m-%d %H:%M")
        if estado_actual == "comprimiendo":
            send_telegram_message(
                f"🌀 COMPRESIÓN BTC DETECTADA\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"   {patron_btc['detalle']}\n"
                f"⏳ Esperando ruptura (UP o DOWN)\n"
                f"🕐 {ahora_lima_str} (Lima)"
            )
        elif estado_actual == "expandiendo":
            send_telegram_message(
                f"🔥 EXPANSIÓN BTC DETECTADA\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"   {patron_btc['detalle']}\n"
                f"✅ Filtro pasa → analizando monedas...\n"
                f"🕐 {ahora_lima_str} (Lima)"
            )

    if COMP_MODO_FILTRO == "hard" and not patron_btc["pasa"]:
        print(f"\n⏸️ Filtro no pasó ({estado_actual.upper()}) — abortando en silencio", flush=True)
        return

    print("✅ Filtro pasó\n", flush=True)

    # ============================================================
    # FLUJO NORMAL
    # ============================================================
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

    print("\n" + "=" * 70, flush=True)
    print("📢 RESULTADO FINAL", flush=True)
    print("=" * 70, flush=True)
    print(f"Alertas: {sent_count} | LONG: {long_count} | SHORT: {short_count}", flush=True)
    print(f"Patrón: {patron_btc['estado'].upper()}", flush=True)
    print("\n🏁 PROGRAMA TERMINADO", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ ERROR GENERAL: {e}", flush=True)
        import traceback
        traceback.print_exc()
        raise
