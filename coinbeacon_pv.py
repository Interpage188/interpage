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
import time

# ============================================================
# COINBEACON PV — 1d con TRIPLE CONFIRMACIÓN
#
#   Capa 1: BTC (FIX 8 — delta también en zonas 35-48 y 52-65)
#   Capa 2: Impulso de la moneda (solo si hay cache)
#   Capa 3: Ruptura (prioridad broken > near > retest)
#
#   Score mínimo:
#     - Indeciso normal: 90
#     - Rebote en zona saludable (delta fuerte): 80
# ============================================================

TIMEFRAMES = ["1d"]

MIN_TOUCHES = 8
MAX_HISTORY_HOURS = 48

SCORE_MIN_INDECISO = 90.0
SCORE_MIN_REBOTE = 80.0

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

STATE_FILE = DATA_DIR / "coinbeacon_pv_state.json"
CSV_FILE = DATA_DIR / "coinbeacon_pv.csv"

LIMA_OFFSET = timedelta(hours=-5)
HORA_INICIO = 0
HORA_FIN = 24


def hora_permite_envio():
    now_lima = datetime.now(timezone.utc) + LIMA_OFFSET
    hora = now_lima.hour
    return HORA_INICIO <= hora < HORA_FIN


# ============================================================
# CACHE DEL RECOLECTOR
# ============================================================

CACHE_REMOTE_BASE = (
    "https://raw.githubusercontent.com/Interpage188/"
    "interpage/main/data/cache"
)
CACHE_MAX_EDAD_MIN = 40

SUPERTREND_STATE_URL = (
    "https://raw.githubusercontent.com/Interpage188/"
    "interpage/main/data/supertrend_state.json"
)


def leer_cache_remoto(symbol):
    url = f"{CACHE_REMOTE_BASE}/{symbol}.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
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
    }


def rsi4_hace_n_horas(cache, horas):
    if not cache or not cache.get("pulso"):
        return None
    pulso = cache["pulso"]
    ahora = datetime.now(timezone.utc).timestamp()
    objetivo = ahora - horas * 3600
    mejor = None
    mejor_diff = 999999
    for m in pulso:
        ts = m.get("ts")
        if ts is None:
            continue
        diff = abs(ts - objetivo)
        if diff < mejor_diff:
            mejor_diff = diff
            mejor = m
    if mejor is None or mejor_diff > 3600:
        return None
    return mejor.get("rsi4h")


def extraer_rsi_del_cache(cache):
    if not cache:
        return {}
    resultado = {}
    if cache.get("rsi15") is not None:
        resultado["15m"] = cache["rsi15"]
    if cache.get("rsi1h") is not None:
        resultado["1h"] = cache["rsi1h"]
    if cache.get("rsi4h") is not None:
        resultado["4h"] = cache["rsi4h"]
    return resultado


def obtener_rsi_btc():
    cache = leer_cache_remoto("BTC")
    if cache:
        print(f"   📦 Cache BTC: RSI15={cache['rsi15']} | RSI1h={cache['rsi1h']} | RSI4h={cache['rsi4h']} | hace {cache['edad_min']:.1f} min", flush=True)
        return extraer_rsi_del_cache(cache)
    print("   ⚠️ Sin cache BTC → CoinGecko fallback", flush=True)
    return obtener_rsi_btc_coingecko()


def leer_supertrend_state():
    try:
        req = urllib.request.Request(
            SUPERTREND_STATE_URL, headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data.get("symbols", {})
    except Exception:
        return {}


# ============================================================
# COINGECKO (fallback BTC)
# ============================================================

COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY")
COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
COINGECKO_IDS = {"BTC": "bitcoin"}


def obtener_precios_coingecko(symbol, days=7):
    coin_id = COINGECKO_IDS.get(symbol)
    if not coin_id:
        raise ValueError(f"Símbolo {symbol} no mapeado")
    headers = {"Accept": "application/json"}
    if COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_API_KEY
    params = {"vs_currency": "usd", "days": days}
    url = COINGECKO_BASE_URL.format(coin_id=coin_id)
    response = requests.get(url, headers=headers, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()
    return data.get("prices", [])


def agrupar_precios(prices, intervalo_segundos):
    if not prices:
        return []
    buckets = {}
    for ts_ms, price in prices:
        ts = int(ts_ms / 1000)
        bucket = ts - (ts % intervalo_segundos)
        buckets[bucket] = price
    return [buckets[key] for key in sorted(buckets.keys())]


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
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def obtener_rsi_btc_coingecko():
    try:
        prices = obtener_precios_coingecko("BTC", days=7)
    except Exception as e:
        print(f"   ⚠️ Error RSI BTC CoinGecko: {e}", flush=True)
        return {}
    intervalos = {"15m": 15 * 60, "1h": 60 * 60, "4h": 4 * 60 * 60}
    resultado = {}
    for tf, segs in intervalos.items():
        agrupados = agrupar_precios(prices, segs)
        if len(agrupados) < 15:
            continue
        rsi = calcular_rsi(agrupados)
        if rsi is None:
            continue
        resultado[tf] = rsi
    return resultado


# ============================================================
# COINBEACON
# ============================================================

COINBEACON_TRENDLINES_URL = "https://api.coinbeacon.io/detectors/trendlines"
COINBEACON_VOLUME_URL = "https://api.coinbeacon.io/detectors/volume"


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
        raw = response.read().decode("utf-8")
    return json.loads(raw)


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
            "symbol": item_symbol,
            "price": numero(item.get("price")),
            "bias": item.get("bias"),
            "confidence": numero(item.get("confidence", 0)),
            "type": item_data.get("type"),
            "currentLevel": numero(item_data.get("currentLevel")),
            "status": item_data.get("status"),
            "touchCount": touch_count,
            "fallingOrRising": item_data.get("fallingOrRising", "flat"),
            "computedAt": item.get("computedAt"),
            "timeframe": timeframe,
        })
    return lineas


def consultar_volume_coinbeacon():
    result = consultar_coinbeacon(COINBEACON_VOLUME_URL, "4h", 500)
    items = result.get("items", [])
    volume_data = []
    for item in items:
        volume_data.append({
            "symbol": item.get("symbol"),
            "price": numero(item.get("price")),
            "volumeTrend": numero(item.get("volumeTrend")) or 0.0,
            "volume24h": numero(item.get("volume24h")) or 0.0,
            "rvoll": numero(item.get("rvoll")) or 0.0,
            "direction": item.get("direction") or "unknown",
            "status": item.get("status") or "",
            "spottedAt": item.get("spottedAt"),
        })
    return volume_data


# ============================================================
# SCORING
# ============================================================

def clasificar_estructura(touches):
    if touches >= 11: return "VERY_STRONG"
    if touches >= 8:  return "STRONG"
    if touches >= 6:  return "VALID"
    if touches >= 4:  return "WEAK"
    return "IGNORE"


def obtener_touch_score(touches):
    if touches < 4: return 0
    if touches == 4: return 8
    if touches == 5: return 12
    if touches == 6: return 16
    if touches == 7: return 20
    if touches == 8: return 25
    if touches == 9: return 28
    if touches == 10: return 31
    if touches >= 11: return 35 + min((touches - 11) * 2, 15)
    return 0


def obtener_proximidad_score(distance_pct, timeframe):
    if distance_pct is None or distance_pct <= 0:
        return 0
    max_dist = 1.5
    if distance_pct > max_dist:
        return 0
    ratio = distance_pct / max_dist
    return 35 - (ratio * 30)


def obtener_timeframe_score(timeframe):
    return {"1d": 27, "4h": 21, "1h": 19, "15m": 15}.get(timeframe, 0)


def obtener_status_score(status):
    status_lower = str(status).lower()
    score = 0
    if "near breakout" in status_lower:  score += 20
    if "near breakdown" in status_lower: score += 20
    if "retest" in status_lower:         score += 15
    if "broken" in status_lower:         score -= 30
    if "confirmed" in status_lower:      score += 10
    return score


def obtener_confidence_score(confidence):
    if confidence is None: return 0
    if confidence <= 1:   normalized = confidence * 100
    elif confidence <= 10: normalized = confidence * 10
    else:                  normalized = confidence
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
    return {
        "structure_quality": estructura,
        "structure_score": structure_score,
        "proximity_score": proximity_score,
        "timeframe_score": timeframe_score,
        "status_score": status_score,
        "confidence_score": confidence_score,
        "smart_score": smart_score,
        "bonus_score": bonus,
        "total_score": total_score,
    }


def analizar_coinbeacon(symbol, timeframe="1d"):
    print(f"\n📈 COINBEACON — {symbol} ({timeframe})", flush=True)
    print("=" * 70, flush=True)
    try:
        lineas = consultar_coinbeacon_trendlines(symbol, timeframe)
    except Exception as e:
        print(f"   ❌ {timeframe}: {str(e)[:60]}", flush=True)
        return {"price": None, "lines": []}

    if not lineas:
        return {"price": None, "lines": []}

    print(f"   {timeframe}: {len(lineas)} líneas", flush=True)

    precio = None
    for linea in lineas:
        if linea.get("price") is not None:
            precio = linea["price"]
            break

    if precio is not None:
        print(f"💰 Precio detectado: ${precio:.6f}", flush=True)
    else:
        print("💰 Precio: N/A", flush=True)

    for linea in lineas:
        linea["distance_pct"] = distancia_porcentual(precio, linea.get("currentLevel"))
        if linea["distance_pct"] is not None:
            score_data = calcular_score_linea(linea)
            linea.update(score_data)
        else:
            linea["structure_quality"] = "UNKNOWN"
            linea["total_score"] = 0

    print(f"\n⭐ LÍNEAS VÁLIDAS (6+ toques)", flush=True)
    validas = [l for l in lineas if l["structure_quality"] in ("VALID", "STRONG", "VERY_STRONG")]
    if not validas:
        print("   Ninguna.", flush=True)
    else:
        for linea in validas:
            print(f"   {linea['timeframe']} | {linea['type']} | ${linea['currentLevel']:.6f} | {linea['distance_pct']:+.2f}% | {linea['touchCount']}T | {linea.get('status', '')} | Score: {linea['total_score']:.1f}", flush=True)

    return {"price": precio, "lines": lineas}


# ============================================================
# [CAPA 1] BTC CONTEXTO
# ============================================================

def analizar_contexto_btc(rsi_btc, rsi4_anterior=None):
    rsi4 = rsi_btc.get("4h")
    rsi1 = rsi_btc.get("1h")
    rsi15 = rsi_btc.get("15m")

    if rsi4 is None or rsi1 is None or rsi15 is None:
        return {
            "btc_dir": "flat", "btc_modo": "neutro",
            "razon_ventana": "sin datos", "delta_2h": None,
            "rsi4": rsi4, "rsi1": rsi1, "rsi15": rsi15,
            "score_min_requerido": 0.0,
        }

    delta_2h = (rsi4 - rsi4_anterior) if rsi4_anterior is not None else None

    RSI_SOBREVENTA = 35.0
    RSI_BAJA_SALUDABLE = 48.0
    RSI_CENTRAL = 52.0
    RSI_SOBRECOMPRA = 65.0

    D_UP_FUERTE = 0.34
    D_UP_INDECISO = 0.10
    D_DOWN_INDECISO = -0.10
    D_DOWN_FUERTE = -0.34
    D_CRUCE = 1.50

    btc_dir = "flat"
    btc_modo = "neutro"
    razon = "neutro"
    score_min_req = 0.0

    if rsi4 >= RSI_SOBRECOMPRA:
        if delta_2h is None:
            razon = "sobrecompra sin delta"
        elif delta_2h > D_UP_FUERTE:
            btc_dir, btc_modo, razon = "up", "fuerte", f"sobrecompra Δ{delta_2h:+.2f} sigue"
        elif delta_2h > D_UP_INDECISO:
            btc_dir, btc_modo, razon = "up", "indeciso", f"sobrecompra Δ{delta_2h:+.2f}"
            score_min_req = SCORE_MIN_INDECISO
        elif delta_2h < D_DOWN_FUERTE:
            btc_dir, btc_modo, razon = "down", "fuerte", f"sobrecompra Δ{delta_2h:+.2f} gira"
        elif delta_2h < D_DOWN_INDECISO:
            btc_dir, btc_modo, razon = "down", "indeciso", f"sobrecompra Δ{delta_2h:+.2f}"
            score_min_req = SCORE_MIN_INDECISO
        else:
            razon = f"sobrecompra Δ{delta_2h:+.2f} neutro"

    elif rsi4 >= RSI_CENTRAL:
        # 52-65 Alta saludable → LONG directo, salvo delta muy negativo
        if delta_2h is not None and delta_2h < -D_CRUCE:
            btc_dir, btc_modo, razon = "down", "indeciso", f"alta saludable Δ{delta_2h:+.2f} gira DOWN"
            score_min_req = SCORE_MIN_REBOTE
        else:
            btc_dir, btc_modo, razon = "up", "fuerte", "alta saludable"

    elif rsi4 >= RSI_BAJA_SALUDABLE:
        # 48-52 Central → delta normal
        if delta_2h is None:
            razon = "central sin delta"
        elif delta_2h > D_UP_FUERTE:
            btc_dir, btc_modo, razon = "up", "fuerte", f"central Δ{delta_2h:+.2f}"
        elif delta_2h > D_UP_INDECISO:
            btc_dir, btc_modo, razon = "up", "indeciso", f"central Δ{delta_2h:+.2f}"
            score_min_req = SCORE_MIN_INDECISO
        elif delta_2h < D_DOWN_FUERTE:
            btc_dir, btc_modo, razon = "down", "fuerte", f"central Δ{delta_2h:+.2f}"
        elif delta_2h < D_DOWN_INDECISO:
            btc_dir, btc_modo, razon = "down", "indeciso", f"central Δ{delta_2h:+.2f}"
            score_min_req = SCORE_MIN_INDECISO
        else:
            razon = f"central Δ{delta_2h:+.2f} neutro"

    elif rsi4 >= RSI_SOBREVENTA:
        # 35-48 Baja saludable → SHORT directo, salvo delta muy positivo
        if delta_2h is not None and delta_2h > D_CRUCE:
            btc_dir, btc_modo, razon = "up", "indeciso", f"baja saludable Δ{delta_2h:+.2f} gira UP"
            score_min_req = SCORE_MIN_REBOTE
        else:
            btc_dir, btc_modo, razon = "down", "fuerte", "baja saludable"

    else:
        # <35 Sobreventa
        if delta_2h is None:
            razon = "sobreventa sin delta"
        elif delta_2h < D_DOWN_FUERTE:
            btc_dir, btc_modo, razon = "down", "fuerte", f"sobreventa Δ{delta_2h:+.2f} sigue"
        elif delta_2h < D_DOWN_INDECISO:
            btc_dir, btc_modo, razon = "down", "indeciso", f"sobreventa Δ{delta_2h:+.2f}"
            score_min_req = SCORE_MIN_INDECISO
        elif delta_2h > D_UP_FUERTE:
            btc_dir, btc_modo, razon = "up", "fuerte", f"sobreventa Δ{delta_2h:+.2f} gira"
        elif delta_2h > D_UP_INDECISO:
            btc_dir, btc_modo, razon = "up", "indeciso", f"sobreventa Δ{delta_2h:+.2f}"
            score_min_req = SCORE_MIN_INDECISO
        else:
            razon = f"sobreventa Δ{delta_2h:+.2f} neutro"

    return {
        "btc_dir": btc_dir,
        "btc_modo": btc_modo,
        "razon_ventana": razon,
        "delta_2h": delta_2h,
        "rsi4": rsi4, "rsi1": rsi1, "rsi15": rsi15,
        "score_min_requerido": score_min_req,
    }


# ============================================================
# [CAPA 2] IMPULSO DE LA MONEDA
# ============================================================

def evaluar_impulso_moneda(cache):
    if not cache:
        return {"rsi4h": None, "rsi4h_delta": None, "impulso": "sin_cache"}

    rsi4h = cache.get("rsi4h")
    rsi4h_6h = rsi4_hace_n_horas(cache, 6)

    delta_6h = None
    if rsi4h is not None and rsi4h_6h is not None:
        delta_6h = rsi4h - rsi4h_6h

    impulso = "flat"
    if rsi4h is not None and rsi4h >= 55:
        if delta_6h is not None and delta_6h > 0.5:
            impulso = "up"
        elif rsi4h >= 65:
            impulso = "up"

    if rsi4h is not None and rsi4h <= 45:
        if delta_6h is not None and delta_6h < -0.5:
            impulso = "down"
        elif rsi4h <= 35:
            impulso = "down"

    return {
        "rsi4h": rsi4h,
        "rsi4h_delta": delta_6h,
        "impulso": impulso,
    }


# ============================================================
# TELEGRAM / CSV / ESTADO
# ============================================================

def send_telegram_message(message):
    if not hora_permite_envio():
        print("Fuera de horario. Mensaje no enviado.", flush=True)
        return False
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram no configurado.", flush=True)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = f"chat_id={urllib.parse.quote(str(chat_id))}&text={urllib.parse.quote(message)}".encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        if result.get("ok"):
            print("Mensaje de Telegram enviado.", flush=True)
            return True
        print(f"Telegram devolvió: {result}", flush=True)
        return False
    except Exception as e:
        print(f"Error al enviar Telegram: {e}", flush=True)
        return False


def guardar_en_csv(alert_data):
    fieldnames = [
        "hora_lima", "symbol", "bias", "type", "timeframe",
        "currentLevel", "touchCount", "confidence", "rvoll", "volumeTrend",
        "score", "status", "fallingOrRising", "price", "razon_patron",
        "btc_dir", "btc_modo", "btc_razon", "btc_rsi4", "btc_delta2h",
        "moneda_rsi4h", "moneda_delta6h", "moneda_impulso",
        "structure_quality", "total_score"
    ]
    if not CSV_FILE.exists():
        with CSV_FILE.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
    existing_rows = []
    with CSV_FILE.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            existing_rows.append(row)
    for row in existing_rows:
        if (row.get("symbol") == alert_data["symbol"] and
            row.get("type") == alert_data["type"] and
            row.get("currentLevel") == alert_data["currentLevel"] and
            row.get("hora_lima", "").split()[0] == alert_data["hora_lima"].split()[0]):
            return
    with CSV_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writerow(alert_data)
    print(f"✅ Alerta guardada en CSV para {alert_data['symbol']}", flush=True)


def cargar_estado():
    if not STATE_FILE.exists():
        return []
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            estado = json.load(f)
        if isinstance(estado, list):
            return estado
    except Exception as e:
        print(f"⚠️ No se pudo leer {STATE_FILE.name}: {e}", flush=True)
    return []


def limpiar_estado(previous_state, now_ts):
    resultado = []
    for item in previous_state:
        detected_at = item.get("detected_at")
        if detected_at is None:
            resultado.append(item)
            continue
        try:
            age_hours = (now_ts - float(detected_at)) / 3600
        except (ValueError, TypeError):
            continue
        if age_hours < MAX_HISTORY_HOURS:
            resultado.append(item)
    return resultado


def guardar_estado(estado):
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(estado, f, indent=2)


# ============================================================
# ANÁLISIS DE CONFLUENCIA — triple confirmación
# ============================================================

def analizar_confluencia(symbol, coin_data, volume_by_symbol, btc_context, st_state, cache_moneda):
    precio = coin_data.get("price")
    lineas = coin_data.get("lines", [])
    if precio is None:
        return []

    btc_dir = btc_context.get("btc_dir", "flat")
    btc_modo = btc_context.get("btc_modo", "neutro")
    razon = btc_context.get("razon_ventana", "neutro")
    btc_rsi4 = btc_context.get("rsi4")
    delta_2h = btc_context.get("delta_2h")

    # ===== Capa 1: BTC (interno) =====
    if btc_modo == "neutro":
        return []

    operacion_permitida = "LONG" if btc_dir == "up" else "SHORT"
    score_min = btc_context.get("score_min_requerido", 0.0)

    # ===== Capa 2: Impulso moneda (solo si hay cache) =====
    impulso_data = evaluar_impulso_moneda(cache_moneda)
    imp = impulso_data["impulso"]
    rsi4h_m = impulso_data["rsi4h"]
    delta_6h_m = impulso_data["rsi4h_delta"]

    if cache_moneda is not None:
        rsi4h_txt = f"{rsi4h_m:.1f}" if rsi4h_m is not None else "N/A"
        delta6h_txt = f"{delta_6h_m:+.2f}" if delta_6h_m is not None else "N/A"
        if operacion_permitida == "LONG" and imp != "up":
            print(f"   ⏭️ CAPA 2: Impulso NO alcista ({imp}, RSI4h {rsi4h_txt}, Δ6h {delta6h_txt}) → sin LONG", flush=True)
            return []
        if operacion_permitida == "SHORT" and imp != "down":
            print(f"   ⏭️ CAPA 2: Impulso NO bajista ({imp}, RSI4h {rsi4h_txt}, Δ6h {delta6h_txt}) → sin SHORT", flush=True)
            return []
        print(f"   ✅ CAPA 2: Impulso {imp.upper()} (RSI4h {rsi4h_txt}, Δ6h {delta6h_txt})", flush=True)
    else:
        imp = "sin_cache"

    # ===== Volumen =====
    volume = volume_by_symbol.get(symbol + "USDT") or volume_by_symbol.get(symbol)
    if volume:
        rvoll = volume.get("rvoll", 0)
        volume_trend = volume.get("volumeTrend", 0)
        vol_status = str(volume.get("status", "")).lower()
        is_spike = "spike" in vol_status
        is_dry_up = "dry" in vol_status or "squeeze" in vol_status or "dry-up" in vol_status
        is_exhaustion = "exhaustion" in vol_status
    else:
        rvoll = volume_trend = 0
        is_spike = is_dry_up = is_exhaustion = False

    # ===== Capa 3: Ruptura (prioridad broken > near > retest) =====
    candidates = []
    for linea in lineas:
        tipo = linea.get("type")
        distancia = linea.get("distance_pct")
        if distancia is None:
            continue

        status = str(linea.get("status", "")).lower()
        es_broken = "broken" in status and "retest" not in status

        quality = linea.get("structure_quality", "IGNORE")
        if quality in ("IGNORE", "WEAK"):
            continue

        if not es_broken and abs(distancia) > 2.0:
            continue
        if es_broken and abs(distancia) > 5.0:
            continue

        operacion = None
        razon_patron = ""
        prioridad_patron = 0

        # PRIORIDAD 3: Ruptura confirmada (broken)
        if es_broken and linea.get("touchCount", 0) >= 8:
            dist_abs = abs(distancia)
            if 0.5 <= dist_abs <= 5.0:
                if tipo == "resistance" and distancia < 0:
                    operacion = "LONG"
                    razon_patron = f"ruptura confirmada ({distancia:+.2f}%)"
                    prioridad_patron = 3
                elif tipo == "support" and distancia > 0:
                    operacion = "SHORT"
                    razon_patron = f"ruptura confirmada ({distancia:+.2f}%)"
                    prioridad_patron = 3

        # PRIORIDAD 2: near breakout / near breakdown
        if operacion is None and tipo == "resistance" and "near breakout" in status:
            operacion = "LONG"
            razon_patron = "near breakout"
            prioridad_patron = 2

        if operacion is None and tipo == "support" and "near breakdown" in status:
            operacion = "SHORT"
            razon_patron = "near breakdown"
            prioridad_patron = 2

        # PRIORIDAD 1: retest
        if operacion is None and tipo == "support" and "retest" in status and distancia < 0:
            operacion = "LONG"
            razon_patron = "retest soporte"
            prioridad_patron = 1

        if operacion is None and tipo == "resistance" and "retest" in status and distancia > 0:
            operacion = "SHORT"
            razon_patron = "retest resistencia"
            prioridad_patron = 1

        if operacion is None:
            continue

        if operacion != operacion_permitida:
            continue

        total_score = linea.get("total_score", 0)
        if score_min > 0 and total_score < score_min:
            print(f"   ⏭️ CAPA 3: {symbol} score {total_score:.1f} < {score_min:.0f} → sin alerta", flush=True)
            continue

        candidates.append({
            "line": linea,
            "operacion": operacion,
            "score": total_score,
            "razon_patron": razon_patron,
            "prioridad_patron": prioridad_patron,
        })

    if not candidates:
        return []

    candidates.sort(key=lambda x: (x["prioridad_patron"], x["score"]), reverse=True)
    best = candidates[0]

    linea = best["line"]
    operacion = best["operacion"]
    total_score = best["score"]
    razon_patron = best["razon_patron"]

    st_sym = st_state.get(symbol, {}).get("trend", "N/A")
    st_str = st_sym.upper() if st_sym not in ("N/A", None) else "N/A"

    alert = {
        "symbol": symbol,
        "line": linea,
        "volume": volume if volume else {},
        "score": total_score,
        "bias": operacion,
        "razon_patron": razon_patron,
        "prioridad_patron": best["prioridad_patron"],
        "is_spike": is_spike,
        "is_dry_up": is_dry_up,
        "is_exhaustion": is_exhaustion,
        "btc_dir": btc_dir,
        "btc_modo": btc_modo,
        "btc_razon": razon,
        "btc_rsi4": btc_rsi4,
        "btc_delta2h": delta_2h,
        "rsi4h_moneda": rsi4h_m,
        "delta_6h_moneda": delta_6h_m,
        "impulso": imp,
        "st_sym": st_str,
        "structure_quality": linea.get("structure_quality"),
        "total_score": total_score,
    }
    return [alert]


# ============================================================
# PROCESAR ALERTAS
# ============================================================

def procesar_alertas(alerts, filtered_previous):
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
        exists = any(
            f"{item.get('symbol')}_{item.get('type')}_{item.get('timeframe')}_{item.get('currentLevel')}" == key
            for item in new_state
        )
        if exists:
            continue

        emoji = "🟢" if operacion == "LONG" else "🔴" if operacion == "SHORT" else "⚪"
        if operacion == "LONG": long_count += 1
        elif operacion == "SHORT": short_count += 1

        tipo_linea = (line.get("type") or "desconocido").upper()
        inclinacion = (line.get("fallingOrRising", "flat")).upper()
        precio_actual = line.get("price") or 0.0
        nivel_linea = line.get("currentLevel") or 0.0
        razon_patron = alert.get("razon_patron", "")

        tags = []
        if alert.get("is_spike"):      tags.append("🚀 VOLUME SPIKE")
        if alert.get("is_dry_up"):     tags.append("💧 DRY-UP")
        if alert.get("is_exhaustion"): tags.append("🪫 EXHAUSTION")
        tag_text = " ".join(tags) if tags else ""

        # Línea BTC compacta
        btc_rsi4 = alert.get("btc_rsi4")
        btc_delta = alert.get("btc_delta2h")
        btc_rsi_txt = f"{btc_rsi4:.2f}" if btc_rsi4 is not None else "N/A"
        btc_delta_txt = f"{btc_delta:+.2f}" if btc_delta is not None else "N/A"

        if operacion == "LONG":
            btc_linea = f"📊 BTC UP (RSI4h {btc_rsi_txt} | Δ{btc_delta_txt})"
        elif operacion == "SHORT":
            btc_linea = f"📊 BTC DOWN (RSI4h {btc_rsi_txt} | Δ{btc_delta_txt})"
        else:
            btc_linea = f"📊 BTC (RSI4h {btc_rsi_txt} | Δ{btc_delta_txt})"

        msg = (
            f"📊 COINBEACON PV (1d)\n"
            f"{emoji} {operacion} {symbol}\n"
            f"📈 Precio: ${precio_actual:.6f}\n"
            f"📉 {tipo_linea} ({inclinacion})\n"
            f"   • {razon_patron}\n"
            f"   • Nivel: ${nivel_linea:.6f}\n"
            f"   • Toques: {line.get('touchCount', 0)} ({alert['structure_quality']})\n"
            f"🎯 Score: {alert['score']:.1f}\n"
            f"{btc_linea}\n"
            f"🔮 SuperTrend: {alert.get('st_sym', 'N/A')}\n"
            f"🕐 {now_lima}\n"
        )
        if tag_text:
            msg += f"{tag_text}\n"
        msg += "━━━━━━━━━━━━━━━━━━━"

        enviado = send_telegram_message(msg)
        if enviado:
            sent_count += 1
            rsi4h_m = alert.get("rsi4h_moneda")
            delta6h_m = alert.get("delta_6h_moneda")
            csv_data = {
                "hora_lima": now_lima, "symbol": symbol, "bias": operacion,
                "type": line.get("type", ""), "timeframe": line.get("timeframe", ""),
                "currentLevel": str(nivel_linea),
                "touchCount": str(line.get("touchCount", 0)),
                "confidence": str(line.get("confidence", 0)),
                "rvoll": f"{vol.get('rvoll', 0):.2f}",
                "volumeTrend": f"{vol.get('volumeTrend', 0):.1f}",
                "score": f"{alert['score']:.1f}", "status": line.get("status", ""),
                "fallingOrRising": line.get("fallingOrRising", "flat"),
                "price": str(precio_actual),
                "razon_patron": razon_patron,
                "btc_dir": alert.get("btc_dir", ""),
                "btc_modo": alert.get("btc_modo", ""),
                "btc_razon": alert.get("btc_razon", ""),
                "btc_rsi4": btc_rsi_txt,
                "btc_delta2h": btc_delta_txt,
                "moneda_rsi4h": f"{rsi4h_m:.1f}" if rsi4h_m is not None else "",
                "moneda_delta6h": f"{delta6h_m:+.2f}" if delta6h_m is not None else "",
                "moneda_impulso": alert.get("impulso", ""),
                "structure_quality": alert['structure_quality'],
                "total_score": f"{alert['score']:.1f}",
            }
            guardar_en_csv(csv_data)
            new_state.append({
                "symbol": symbol, "type": line.get("type"),
                "timeframe": line.get("timeframe"),
                "currentLevel": nivel_linea, "detected_at": now_ts,
            })

    return sent_count, long_count, short_count, new_state


# ============================================================
# MAIN
# ============================================================

def main():
    print("\n" + "=" * 70, flush=True)
    print("🚀 COINBEACON PV (1d) — TRIPLE CONFIRMACIÓN", flush=True)
    print("   Capa 1: BTC (delta en zonas 35-48, 48-52, 52-65)", flush=True)
    print("   Capa 2: Impulso moneda (si hay cache)", flush=True)
    print("   Capa 3: Ruptura (broken > near > retest)", flush=True)
    print("=" * 70, flush=True)

    print(f"\nHora UTC: {datetime.now(timezone.utc).isoformat()}", flush=True)

    previous_state = cargar_estado()
    rsi4_anterior = None
    for item in previous_state:
        if item.get("type") == "btc_rsi":
            rsi4_anterior = item.get("rsi4")
            break

    print("\n🌐 CAPA 1: BTC RSI", flush=True)
    rsi_btc = obtener_rsi_btc()
    btc_context = analizar_contexto_btc(rsi_btc, rsi4_anterior)

    delta_2h = btc_context.get("delta_2h")
    delta_txt = f"{delta_2h:+.2f}" if delta_2h is not None else "N/A"
    rsi4_val = btc_context.get('rsi4')
    rsi4_txt = f"{rsi4_val:.2f}" if rsi4_val is not None else "N/A"
    print(f"   🧭 BTC {btc_context.get('btc_dir', 'flat').upper()} {btc_context.get('btc_modo', 'neutro').upper()} "
          f"(RSI4h {rsi4_txt} | Δ2h {delta_txt}) → {btc_context.get('razon_ventana', '')}", flush=True)

    st_state = leer_supertrend_state()
    if st_state:
        print(f"🔮 SuperTrend state: {len(st_state)} símbolos", flush=True)

    print("\n📊 OBTENIENDO VOLUMEN COINBEACON", flush=True)
    try:
        volume_data = consultar_volume_coinbeacon()
        print(f"Datos de volumen: {len(volume_data)}", flush=True)
    except Exception as e:
        print(f"❌ Error volumen: {e}", flush=True)
        volume_data = []
    volume_by_symbol = {}
    for item in volume_data:
        symbol = str(item.get("symbol", "")).upper()
        volume_by_symbol[symbol] = item

    now_ts = datetime.now(timezone.utc).timestamp()
    filtered_previous = limpiar_estado(previous_state, now_ts)

    symbols = [s.replace("USDT", "") for s in volume_by_symbol.keys()]
    symbols = sorted(set(symbols))
    print(f"\n📋 Símbolos a analizar: {len(symbols)}", flush=True)

    all_alerts = []
    for symbol in symbols:
        cache_moneda = leer_cache_remoto(symbol)
        coin_data = analizar_coinbeacon(symbol, "1d")
        alerts = analizar_confluencia(symbol, coin_data, volume_by_symbol, btc_context, st_state, cache_moneda)
        all_alerts.extend(alerts)

    all_alerts.sort(key=lambda x: (0 if x["bias"] == "LONG" else 1, -x["score"]))

    sent_count, long_count, short_count, new_state = procesar_alertas(all_alerts, filtered_previous)

    new_state.append({
        "type": "btc_rsi",
        "rsi4": rsi_btc.get("4h"),
        "detected_at": now_ts,
    })
    guardar_estado(new_state)

    print("\n\n" + "=" * 70, flush=True)
    print("📢 RESULTADO FINAL", flush=True)
    print("=" * 70, flush=True)
    print(f"Alertas nuevas: {sent_count}", flush=True)
    print(f"🟢 LONG: {long_count}", flush=True)
    print(f"🔴 SHORT: {short_count}", flush=True)
    print(f"🧭 BTC: {btc_context.get('btc_dir', 'flat').upper()} {btc_context.get('btc_modo', 'neutro').upper()} ({btc_context.get('razon_ventana', 'neutro')})", flush=True)
    print(f"💾 CSV: {CSV_FILE}", flush=True)
    print(f"💾 Estado: {STATE_FILE}", flush=True)
    print("\n🏁 PROGRAMA TERMINADO", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("\n" + "=" * 70, flush=True)
        print("❌ ERROR GENERAL", flush=True)
        print("=" * 70, flush=True)
        print(str(e), flush=True)
        import traceback
        traceback.print_exc()
        raise
