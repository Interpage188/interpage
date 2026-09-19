#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
============================================================
STRUCTURE BOT V2.1
============================================================

Versión corregida del V2 con los siguientes cambios:

    [BUG 1] El contexto 15m/1h viaja DENTRO de cada entrada.
            Antes se calculaba global y se mezclaba entre símbolos.

    [BUG 2] vela_nueva_confirmada usa '>' en vez de '>='.
            Evita aceptar la misma vela como nueva.

    [BUG 3] max()/min() en calcular_poc() protegidos contra
            listas vacías. Cache corrupto ya no tumba el bot.

    [MACD]  Cruce del entry ahora es MACD/0 (como el script
            original de TradingView), no MACD/Signal.

    [DOC]   Comentario explícito sobre:
            distancia_ruptura ≠ distancia_POC
            Son filtros independientes en momentos distintos.

IMPORTANTE:
- NO recolecta datos nuevos.
- Lee data/cache/*.json del RECOLECTOR.
- Mantiene SU PROPIO estado en data/structure_v2_state.json
- CHoCH/BOS NO se envían a Telegram.
- Solo las entradas reales van a Telegram.

============================================================
"""

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path


BOT_VERSION = "V2.1"

SYMBOLS = [
    "BTC", "ETH", "BNB", "SOL", "ARB", "UNI", "LTC", "INJ",
    "LINK", "ENA", "SUSHI", "RAY", "HYPE", "ZEC", "DOGE", "STX", "DASH",
]

TIMEFRAMES = ["15m", "1h"]

DATA_DIR = Path("data")
CACHE_DIR = DATA_DIR / "cache"
DATA_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)

STATE_FILE = DATA_DIR / "structure_v2_state.json"

# ============================================================
# MACD
# ============================================================
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# ============================================================
# ESTRUCTURA
# ============================================================
# [FILTRO 1] Distancia máxima del PRECIO al SWING ROTO.
#   Se aplica en el momento del CHoCH/BOS.
#   Ejemplo: precio a 4.73% del swing → PASA (límite 5.0)
#   Ejemplo: precio a 6.20% del swing → NO PASA
#
MIN_SWING_PCT = 0.15
MAX_BREAKOUT_DISTANCE_PCT = 5.0

# ============================================================
# VOLUME PROFILE / POC
# ============================================================
POC_BINS = 24

# [FILTRO 2] Distancia máxima del PRECIO al POC.
#   Se aplica al verificar el pending.
#   Ejemplo: precio a 0.23% del POC → PASA (límite 1.50)
#   Ejemplo: precio a 2.80% del POC → NO PASA
#
# NOTA: Este filtro es INDEPENDIENTE del filtro de ruptura.
#       Un setup puede tener ruptura de 4.73% (OK) y
#       distancia al POC de 0.23% (OK). Ambos pasan.
#
ENTRY_MAX_DIST_POC_PCT = 1.50
MAX_POC_DISTANCE_PCT = 5.0   # Distancia máxima del POC al precio
                              # en el momento de crear el pending.

# ============================================================
# ENTRADA
# ============================================================
ENTRY_MACD_WINDOW = 3
ENTRY_EXPIRA_HORAS = 4

# ============================================================
# FILTROS DE CONTEXTO
# ============================================================
MIN_RVOL_15M = 0.80
PERMITIR_CONTEXT_NEUTRAL = True

# ============================================================
# FIBO TIME
# ============================================================
FIBO_MULTIPLOS = [3, 5, 8, 13, 21, 34]
FIBO_TOLERANCIA_VELAS = 1

# ============================================================
# ESTADO
# ============================================================
MAX_NIVELES_GUARDADOS = 50
MAX_ENTRADAS_GUARDADAS = 100

# ============================================================
# TELEGRAM
# ============================================================
LIMA_OFFSET = timedelta(hours=-5)
HORA_INICIO = 0
HORA_FIN = 24


# ============================================================
# UTILIDADES
# ============================================================
def ahora_utc():
    return datetime.now(timezone.utc)


def hora_permite_envio():
    ahora_lima = ahora_utc() + LIMA_OFFSET
    return HORA_INICIO <= ahora_lima.hour < HORA_FIN


def precio_valido(valor):
    try:
        return valor is not None and float(valor) > 0
    except Exception:
        return False


def pct_distancia(a, b):
    if not precio_valido(a) or not precio_valido(b):
        return None
    try:
        return abs(float(a) - float(b)) / float(b) * 100
    except Exception:
        return None


# ============================================================
# CACHE
# ============================================================
def cache_path(symbol):
    return CACHE_DIR / f"{symbol}.json"


def leer_cache_local(symbol):
    path = cache_path(symbol)
    if not path.exists():
        print(f"   ⚠️ {symbol}: cache no existe", flush=True)
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            print(f"   ⚠️ {symbol}: cache inválido", flush=True)
            return None
        return data
    except Exception as e:
        print(f"   ⚠️ {symbol}: error leyendo cache: {str(e)[:100]}", flush=True)
        return None


# ============================================================
# EMA / MACD
# ============================================================
def ema(valores, span):
    if not valores:
        return []
    if span <= 0:
        return []
    k = 2.0 / (span + 1)
    out = [float(valores[0])]
    for valor in valores[1:]:
        valor = float(valor)
        out.append(valor * k + out[-1] * (1 - k))
    return out


def calcular_macd(cierres):
    """Devuelve (macd, signal, hist)."""
    if len(cierres) < MACD_SLOW + MACD_SIGNAL:
        return [], [], []
    ef = ema(cierres, MACD_FAST)
    es = ema(cierres, MACD_SLOW)
    macd = [f - s for f, s in zip(ef, es)]
    signal = ema(macd, MACD_SIGNAL)
    hist = [m - s for m, s in zip(macd, signal)]
    return macd, signal, hist


def detectar_cruce_macd_cero(macd, ventana=ENTRY_MACD_WINDOW):
    """
    [V2.1] Cruce MACD contra CERO (como el script original).

    up:   MACD pasa de <=0 a >0
    down: MACD pasa de >=0 a <0
    """
    if len(macd) < 2:
        return None
    inicio = max(1, len(macd) - ventana)
    for i in range(inicio, len(macd)):
        mp = macd[i - 1]
        mn = macd[i]
        if mp <= 0 and mn > 0:
            return "up"
        if mp >= 0 and mn < 0:
            return "down"
    return None


# ============================================================
# RSI
# ============================================================
def calcular_rsi(prices, period=14):
    if not prices or len(prices) < period + 1:
        return None
    cambios = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    ganancias = [max(c, 0) for c in cambios]
    perdidas = [max(-c, 0) for c in cambios]

    avg_gain = sum(ganancias[:period]) / period
    avg_loss = sum(perdidas[:period]) / period

    for i in range(period, len(cambios)):
        avg_gain = (avg_gain * (period - 1) + ganancias[i]) / period
        avg_loss = (avg_loss * (period - 1) + perdidas[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ============================================================
# VOLUME PROFILE / POC
# ============================================================
def calcular_poc(velas, idx_inicio, idx_fin, bins=POC_BINS):
    """
    [BUG 3 FIX] Protegido contra listas vacías.
    Si el rango no tiene highs/lows válidos, retorna None.
    """
    if not velas:
        return None
    if idx_inicio < 0:
        return None
    if idx_fin >= len(velas):
        return None
    if idx_inicio >= idx_fin:
        return None

    rango = velas[idx_inicio:idx_fin + 1]
    if not rango:
        return None

    # [BUG 3] Listas seguras en vez de max()/min() directos
    validos_h = [float(v["h"]) for v in rango if precio_valido(v.get("h"))]
    validos_l = [float(v["l"]) for v in rango if precio_valido(v.get("l"))]

    if not validos_h or not validos_l:
        return None

    top = max(validos_h)
    bottom = min(validos_l)

    if top <= bottom:
        return None

    paso = (top - bottom) / bins
    if paso <= 0:
        return None

    volumen = [0.0 for _ in range(bins)]

    for vela in rango:
        try:
            high = float(vela["h"])
            low = float(vela["l"])
            vol = float(vela.get("v", 0))
        except Exception:
            continue

        if vol <= 0:
            continue

        ib = int((low - bottom) / paso)
        it = int((high - bottom) / paso)
        ib = max(0, min(bins - 1, ib))
        it = max(0, min(bins - 1, it))

        if it < ib:
            it = ib

        cantidad_bins = it - ib + 1
        if cantidad_bins <= 0:
            continue

        vol_por_bin = vol / cantidad_bins
        for b in range(ib, it + 1):
            volumen[b] += vol_por_bin

    if not any(volumen):
        return None

    indice_max = max(range(bins), key=lambda i: volumen[i])

    poc_btm = bottom + indice_max * paso
    poc_top = poc_btm + paso
    poc_precio = (poc_btm + poc_top) / 2

    return {
        "poc_precio": poc_precio,
        "poc_btm": poc_btm,
        "poc_top": poc_top,
        "rango_top": top,
        "rango_btm": bottom,
        "poc_volumen": volumen[indice_max],
    }


# ============================================================
# FIBO TIME
# ============================================================
def calcular_fibo_zones(idx_evento, idx_inicio_swing):
    distancia = idx_evento - idx_inicio_swing
    if distancia <= 0:
        return []
    zonas = []
    for mult in FIBO_MULTIPLOS:
        distancia_futura = distancia * mult
        zonas.append({
            "mult": mult,
            "distancia_velas": distancia_futura,
            "idx": idx_evento + distancia_futura,
        })
    return zonas


def fibo_time_activo(pending, idx_actual):
    if not pending:
        return None
    zonas = pending.get("fibo_zones", [])
    if not zonas:
        return None

    idx_evento = pending.get("idx_evento", 0)
    velas_desde = idx_actual - idx_evento

    if velas_desde < 0:
        return None

    for zona in zonas:
        target = zona["distancia_velas"]
        if abs(velas_desde - target) <= FIBO_TOLERANCIA_VELAS:
            return zona["mult"]
    return None


# ============================================================
# DEDUP
# ============================================================
def nivel_ya_alertado(nivel, niveles):
    if not precio_valido(nivel):
        return False
    for n in niveles:
        if not precio_valido(n):
            continue
        distancia = pct_distancia(nivel, n)
        if distancia is not None and distancia < 0.10:
            return True
    return False


def agregar_nivel(nivel, niveles):
    if not precio_valido(nivel):
        return niveles
    niveles = list(niveles or [])
    niveles.append(float(nivel))
    return niveles[-MAX_NIVELES_GUARDADOS:]


# ============================================================
# CONTEXTO MULTI-TF
# ============================================================
def contexto_tf(velas):
    if not velas:
        return {
            "direccion": "?", "rsi": None, "rvol": None,
            "macd": None, "signal": None, "hist": None,
        }

    cierres = [float(v["c"]) for v in velas]

    direccion = "?"
    if len(cierres) >= 2:
        if cierres[-1] > cierres[-2]:
            direccion = "up"
        elif cierres[-1] < cierres[-2]:
            direccion = "down"
        else:
            direccion = "flat"

    rsi = calcular_rsi(cierres)
    macd, signal, hist = calcular_macd(cierres)

    rvol = None
    if len(velas) >= 21:
        volumen_actual = float(velas[-1].get("v", 0))
        anteriores = [
            float(v.get("v", 0))
            for v in velas[-21:-1]
            if float(v.get("v", 0)) > 0
        ]
        if anteriores:
            promedio = sum(anteriores) / len(anteriores)
            if promedio > 0:
                rvol = volumen_actual / promedio

    return {
        "direccion": direccion,
        "rsi": rsi,
        "rvol": rvol,
        "macd": macd[-1] if macd else None,
        "signal": signal[-1] if signal else None,
        "hist": hist[-1] if hist else None,
    }


def contexto_confirma(direccion, contexto_15m, contexto_1h):
    d15 = contexto_15m.get("direccion")
    d1h = contexto_1h.get("direccion")

    if direccion == "up":
        if d15 == "down" and d1h == "down":
            return False
        return True

    if direccion == "down":
        if d15 == "up" and d1h == "up":
            return False
        return True

    return False


def volumen_confirma(contexto_15m):
    rvol = contexto_15m.get("rvol")
    if rvol is None:
        return True
    return rvol >= MIN_RVOL_15M


# ============================================================
# DETECCIÓN DE ESTRUCTURA
# ============================================================
def analizar_timeframe(velas, estado_tf, contexto_15m, contexto_1h):
    if not velas:
        return None, None, estado_tf
    if len(velas) < 40:
        return None, None, estado_tf

    ultimo_ts = estado_tf.get("ultimo_ts", 0)
    velas_nuevas = [
        v for v in velas
        if int(v.get("ts", 0)) > int(ultimo_ts)
    ]

    if not velas_nuevas and ultimo_ts != 0:
        return None, None, estado_tf

    cierres = [float(v["c"]) for v in velas]
    highs = [float(v["h"]) for v in velas]
    lows = [float(v["l"]) for v in velas]

    macd, signal, hist = calcular_macd(cierres)
    if len(macd) < 10:
        return None, None, estado_tf

    swing_high = estado_tf.get("swing_high")
    swing_low = estado_tf.get("swing_low")
    sh_idx = estado_tf.get("swing_high_idx")
    sl_idx = estado_tf.get("swing_low_idx")
    trend = estado_tf.get("trend", 0)
    niveles = estado_tf.get("niveles_alertados", [])

    # Detectar swings por cruce MACD vs 0
    for i in range(1, len(macd)):
        mp = macd[i - 1]
        mn = macd[i]

        if mp <= 0 and mn > 0:
            idx = i - 1
            if 0 <= idx < len(lows):
                nuevo_low = lows[idx]
                if (swing_low is None
                        or pct_distancia(nuevo_low, swing_low) >= MIN_SWING_PCT):
                    swing_low = nuevo_low
                    sl_idx = idx

        if mp >= 0 and mn < 0:
            idx = i - 1
            if 0 <= idx < len(highs):
                nuevo_high = highs[idx]
                if (swing_high is None
                        or pct_distancia(nuevo_high, swing_high) >= MIN_SWING_PCT):
                    swing_high = nuevo_high
                    sh_idx = idx

    cierre = cierres[-1]
    ts_actual = int(velas[-1]["ts"])

    evento = None
    pending = None

    # [BUG 2 FIX] Estrictamente mayor → solo velas nuevas
    vela_nueva_confirmada = (
        ts_actual > int(ultimo_ts or 0)
    )

    # ========================================================
    # RUPTURA ALCISTA
    # ========================================================
    if (swing_high is not None
            and cierre > swing_high
            and vela_nueva_confirmada):

        distancia = (cierre - swing_high) / swing_high * 100

        if (not nivel_ya_alertado(swing_high, niveles)
                and distancia <= MAX_BREAKOUT_DISTANCE_PCT):

            tipo = "BOS ALCISTA" if trend == 1 else "CHoCH ALCISTA"

            evento = {
                "tipo": tipo,
                "direccion": "up",
                "nivel_roto": swing_high,
                "precio": cierre,
                "distancia_pct": distancia,
                "ts": ts_actual,
            }

            trend = 1

            if sl_idx is not None and sl_idx < len(velas) - 1:
                poc = calcular_poc(velas, sl_idx, len(velas) - 1)

                if poc:
                    poc_dist = pct_distancia(poc["poc_precio"], cierre)

                    # LONG: POC debe estar DEBAJO del precio
                    if (poc["poc_precio"] < cierre
                            and (poc_dist is None
                                 or poc_dist <= MAX_POC_DISTANCE_PCT)):

                        fibo_zones = calcular_fibo_zones(
                            len(velas) - 1, sl_idx
                        )

                        pending = {
                            "version": BOT_VERSION,
                            "tipo": tipo,
                            "direccion": "up",
                            "creado_ts": ts_actual,
                            "expira_ts": ts_actual + ENTRY_EXPIRA_HORAS * 3600 * 1000,
                            "idx_evento": len(velas) - 1,
                            "idx_swing": sl_idx,
                            "nivel_roto": swing_high,
                            "fibo_zones": fibo_zones,
                            **poc,
                        }

            niveles = agregar_nivel(swing_high, niveles)
            swing_high = None
            sh_idx = None

    # ========================================================
    # RUPTURA BAJISTA
    # ========================================================
    elif (swing_low is not None
            and cierre < swing_low
            and vela_nueva_confirmada):

        distancia = (swing_low - cierre) / swing_low * 100

        if (not nivel_ya_alertado(swing_low, niveles)
                and distancia <= MAX_BREAKOUT_DISTANCE_PCT):

            tipo = "BOS BAJISTA" if trend == -1 else "CHoCH BAJISTA"

            evento = {
                "tipo": tipo,
                "direccion": "down",
                "nivel_roto": swing_low,
                "precio": cierre,
                "distancia_pct": distancia,
                "ts": ts_actual,
            }

            trend = -1

            if sh_idx is not None and sh_idx < len(velas) - 1:
                poc = calcular_poc(velas, sh_idx, len(velas) - 1)

                if poc:
                    poc_dist = pct_distancia(poc["poc_precio"], cierre)

                    # SHORT: POC debe estar ARRIBA del precio
                    if (poc["poc_precio"] > cierre
                            and (poc_dist is None
                                 or poc_dist <= MAX_POC_DISTANCE_PCT)):

                        fibo_zones = calcular_fibo_zones(
                            len(velas) - 1, sh_idx
                        )

                        pending = {
                            "version": BOT_VERSION,
                            "tipo": tipo,
                            "direccion": "down",
                            "creado_ts": ts_actual,
                            "expira_ts": ts_actual + ENTRY_EXPIRA_HORAS * 3600 * 1000,
                            "idx_evento": len(velas) - 1,
                            "idx_swing": sh_idx,
                            "nivel_roto": swing_low,
                            "fibo_zones": fibo_zones,
                            **poc,
                        }

            niveles = agregar_nivel(swing_low, niveles)
            swing_low = None
            sl_idx = None

    nuevo_estado = {
        "version": BOT_VERSION,
        "trend": trend,
        "swing_high": swing_high,
        "swing_low": swing_low,
        "swing_high_idx": sh_idx,
        "swing_low_idx": sl_idx,
        "niveles_alertados": niveles,
        "ultimo_evento": (
            evento["tipo"] if evento
            else estado_tf.get("ultimo_evento")
        ),
        "ultimo_ts": ts_actual,
        "macd_actual": round(macd[-1], 8) if macd else None,
        "macd_signal": round(signal[-1], 8) if signal else None,
        "macd_hist": round(hist[-1], 8) if hist else None,
        "high_5": max(highs[-5:]) if len(highs) >= 5 else None,
        "low_5": min(lows[-5:]) if len(lows) >= 5 else None,
        "actualizado": ahora_utc().isoformat(),
    }

    return evento, pending, nuevo_estado


# ============================================================
# VERIFICAR PENDING
# ============================================================
def verificar_pending(pending, velas_tf, velas_5m, contexto_15m, contexto_1h):
    if not pending:
        return None
    if not velas_tf:
        return None

    ts_actual = int(velas_tf[-1]["ts"])
    expira = int(pending.get("expira_ts", 0))

    if expira > 0 and ts_actual > expira:
        return "EXPIRADO"

    precio_actual = float(velas_tf[-1]["c"])
    poc_btm = float(pending["poc_btm"])
    poc_top = float(pending["poc_top"])
    poc_precio = float(pending["poc_precio"])

    # [FILTRO POC] Distancia actual al POC (independiente de ruptura)
    distancia_poc = pct_distancia(precio_actual, poc_precio)
    if (distancia_poc is not None
            and distancia_poc > ENTRY_MAX_DIST_POC_PCT):
        return None

    fuente = (
        velas_5m if velas_5m and len(velas_5m) >= 10
        else velas_tf
    )

    if not fuente:
        return None

    toque = False
    for vela in fuente[-(ENTRY_MACD_WINDOW + 2):]:
        try:
            low = float(vela["l"])
            high = float(vela["h"])
        except Exception:
            continue
        if low <= poc_top and high >= poc_btm:
            toque = True
            break

    if not toque:
        return None

    direccion = pending.get("direccion")

    if not contexto_confirma(direccion, contexto_15m, contexto_1h):
        return None

    if not volumen_confirma(contexto_15m):
        return None

    # [V2.1] Cruce MACD 5m contra CERO
    cierres_5m = [float(v["c"]) for v in fuente]
    macd, signal, hist = calcular_macd(cierres_5m)

    if not macd:
        return None

    cruce = detectar_cruce_macd_cero(macd, ENTRY_MACD_WINDOW)

    if cruce is None:
        return None

    if direccion == "up" and cruce == "up":
        return "ENTRY LONG"
    if direccion == "down" and cruce == "down":
        return "ENTRY SHORT"

    return None


# ============================================================
# TELEGRAM
# ============================================================
def enviar_telegram(msg):
    if not hora_permite_envio():
        print("   ⏰ Telegram: fuera de horario.", flush=True)
        return False

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("   ⚠️ Telegram no configurado.", flush=True)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = (
        "chat_id=" + urllib.parse.quote(str(chat_id))
        + "&text=" + urllib.parse.quote(msg)
    ).encode("utf-8")

    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            resultado = json.loads(response.read().decode("utf-8"))
            return resultado.get("ok", False)
    except Exception as e:
        print(f"   ⚠️ Telegram: {str(e)[:100]}", flush=True)
        return False


# ============================================================
# ESTADO
# ============================================================
def cargar_estado():
    if not STATE_FILE.exists():
        return {}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception as e:
        print(f"⚠️ Estado V2 inválido: {str(e)[:100]}", flush=True)
        return {}


def guardar_estado(estado):
    temporal = STATE_FILE.with_suffix(".tmp")
    try:
        with temporal.open("w", encoding="utf-8") as f:
            json.dump(estado, f, indent=2, ensure_ascii=False)
        temporal.replace(STATE_FILE)
    except Exception as e:
        print(f"❌ Error guardando estado V2: {e}", flush=True)


# ============================================================
# CONTEXTOS
# ============================================================
def obtener_contextos(cache):
    velas_15m = cache.get("velas_15m", [])
    velas_1h = cache.get("velas_1h", [])
    return (
        contexto_tf(velas_15m),
        contexto_tf(velas_1h),
    )


# ============================================================
# MENSAJES
# ============================================================
def construir_mensaje_estructura(symbol, tf, evento, pending):
    icono = "🟢" if evento["direccion"] == "up" else "🔴"

    msg = (
        "🏗️ STRUCTURE BOT V2.1\n"
        f"{icono} {evento['tipo']}\n"
        f"🪙 {symbol}\n"
        f"⏱️ TF: {tf}\n"
        f"📈 Precio: ${evento['precio']:.8f}\n"
        f"🎯 Nivel roto: ${evento['nivel_roto']:.8f}\n"
        f"📏 Ruptura: {evento['distancia_pct']:.2f}%\n"
    )

    if pending:
        msg += (
            "\n📊 VOLUME PROFILE\n"
            f"POC: ${pending['poc_precio']:.8f}\n"
            f"Zona: ${pending['poc_btm']:.8f} - "
            f"${pending['poc_top']:.8f}\n"
        )
        fibo = pending.get("fibo_zones", [])
        if fibo:
            fibo_txt = ", ".join(f"{x['mult']}×" for x in fibo[:4])
            msg += f"📅 Fibo Time: {fibo_txt}\n"

        msg += (
            "\n⏳ SETUP ARMADO\n"
            "Esperando:\n"
            "• Retroceso al POC\n"
            "• Confirmación MACD 5m\n"
            "• Contexto multi-TF\n"
        )
    else:
        msg += (
            "\n⚠️ Sin setup POC válido.\n"
            "El evento se registra, pero no se arma entrada.\n"
        )

    ahora_lima = (ahora_utc() + LIMA_OFFSET).strftime("%Y-%m-%d %H:%M:%S")
    msg += f"\n🕐 Lima: {ahora_lima}\n━━━━━━━━━━━━━━━━━━"
    return msg


def construir_mensaje_entrada(symbol, tf, señal, pending, precio,
                                fibo_mult, contexto_15m, contexto_1h):
    es_long = (señal == "ENTRY LONG")
    icono = "🟢" if es_long else "🔴"
    accion = "COMPRA" if es_long else "VENTA"

    rvol = contexto_15m.get("rvol")
    rsi15 = contexto_15m.get("rsi")
    rsi1h = contexto_1h.get("rsi")

    msg = (
        "🎯 STRUCTURE BOT V2.1\n"
        f"{icono} ¡{accion} {symbol}!\n"
        f"📌 {señal}\n"
        f"⏱️ TF origen: {tf}\n\n"
        f"📈 Precio actual: ${precio:.8f}\n"
        f"🎯 POC: ${pending['poc_precio']:.8f}\n"
        f"📊 Zona POC: ${pending['poc_btm']:.8f} - "
        f"${pending['poc_top']:.8f}\n"
        f"🏗️ Origen: {pending['tipo']}\n\n"
        "✅ CONDICIONES CONFIRMADAS\n"
        "• Retroceso al POC\n"
        "• MACD 5m cruzó 0\n"
        "• Contexto multi-TF válido\n"
    )

    if rvol is not None:
        msg += f"• RVOL 15m: {rvol:.2f}\n"
    if rsi15 is not None:
        msg += f"• RSI 15m: {rsi15:.1f}\n"
    if rsi1h is not None:
        msg += f"• RSI 1h: {rsi1h:.1f}\n"
    if fibo_mult is not None:
        msg += f"📅 Fibo Time: {fibo_mult}× ACTIVO\n"

    ahora_lima = (ahora_utc() + LIMA_OFFSET).strftime("%Y-%m-%d %H:%M:%S")
    msg += (
        "\n⚠️ Señal algorítmica. No implica garantía de resultado.\n"
        f"🕐 Lima: {ahora_lima}\n"
        "━━━━━━━━━━━━━━━━━━"
    )
    return msg


# ============================================================
# MAIN
# ============================================================
def main():
    print("\n" + "=" * 70, flush=True)
    print("🏗️ STRUCTURE BOT V2.1", flush=True)
    print(f"   {len(SYMBOLS)} monedas | TF: {', '.join(TIMEFRAMES)}", flush=True)
    print(f"   POC bins: {POC_BINS} | Expira: {ENTRY_EXPIRA_HORAS}h", flush=True)
    print(f"   MAX distancia POC: {ENTRY_MAX_DIST_POC_PCT}%", flush=True)
    print(f"   MAX distancia ruptura: {MAX_BREAKOUT_DISTANCE_PCT}%", flush=True)
    print(f"   MIN RVOL 15m: {MIN_RVOL_15M}", flush=True)
    print(f"   Entry: MACD 5m cruce / 0", flush=True)
    print(f"   Estado: {STATE_FILE}", flush=True)
    print("=" * 70, flush=True)
    print(f"\nUTC: {ahora_utc().isoformat()}", flush=True)

    estado_global = cargar_estado()
    eventos = []
    entradas = []
    expirados = []

    for symbol in SYMBOLS:
        print(f"\n🔍 {symbol}", flush=True)
        cache = leer_cache_local(symbol)
        if not cache:
            continue

        velas_5m = cache.get("velas_5m", [])
        velas_15m = cache.get("velas_15m", [])
        velas_1h = cache.get("velas_1h", [])

        if len(velas_15m) < 40:
            print(f"   ⚠️ 15m insuficiente: {len(velas_15m)}", flush=True)
            continue

        # Contexto del símbolo actual
        contexto_15m, contexto_1h = obtener_contextos(cache)

        for tf in TIMEFRAMES:
            velas = cache.get(f"velas_{tf}", [])

            if len(velas) < 40:
                print(f"   ⚠️ {tf}: {len(velas)} velas", flush=True)
                continue

            clave = f"{symbol}_{tf}"
            estado_tf = estado_global.get(
                clave,
                {"version": BOT_VERSION, "niveles_alertados": []},
            )

            # =================================================
            # PENDING EXISTENTE
            # =================================================
            pending_actual = estado_tf.get("pending")

            if pending_actual:
                señal = verificar_pending(
                    pending_actual,
                    velas,
                    velas_5m,
                    contexto_15m,
                    contexto_1h,
                )

                if señal in ("ENTRY LONG", "ENTRY SHORT"):
                    precio = float(velas[-1]["c"])
                    fibo_mult = fibo_time_activo(
                        pending_actual, len(velas) - 1
                    )

                    # [BUG 1 FIX] Contexto viaja dentro de la entrada
                    entrada = {
                        "symbol": symbol,
                        "tf": tf,
                        "señal": señal,
                        "pending": pending_actual,
                        "precio": precio,
                        "fibo_mult": fibo_mult,
                        "ts": velas[-1]["ts"],
                        "contexto_15m": contexto_15m,   # ← NUEVO
                        "contexto_1h": contexto_1h,     # ← NUEVO
                    }

                    entradas.append(entrada)

                    historial = estado_tf.get("entradas", [])
                    historial.append({
                        "ts": velas[-1]["ts"],
                        "señal": señal,
                        "poc": pending_actual.get("poc_precio"),
                    })
                    estado_tf["entradas"] = historial[-MAX_ENTRADAS_GUARDADAS:]
                    estado_tf["pending"] = None

                    print(f"   🎯 {tf}: {señal}", flush=True)

                elif señal == "EXPIRADO":
                    expirados.append({"symbol": symbol, "tf": tf})
                    print(f"   ⌛ {tf}: pending expirado", flush=True)
                    estado_tf["pending"] = None

            # =================================================
            # NUEVO CHOCH / BOS
            # =================================================
            evento, pending_nuevo, nuevo_estado = analizar_timeframe(
                velas, estado_tf, contexto_15m, contexto_1h
            )

            if pending_nuevo:
                nuevo_estado["pending"] = pending_nuevo
            elif estado_tf.get("pending") is not None:
                nuevo_estado["pending"] = estado_tf.get("pending")

            estado_global[clave] = nuevo_estado

            trend = nuevo_estado.get("trend", 0)
            trend_icon = "↑" if trend == 1 else "↓" if trend == -1 else "→"
            sh = nuevo_estado.get("swing_high")
            sl = nuevo_estado.get("swing_low")
            pend = nuevo_estado.get("pending")

            sh_txt = f"SH={sh:.8f}" if sh is not None else "SH=—"
            sl_txt = f"SL={sl:.8f}" if sl is not None else "SL=—"

            pending_txt = ""
            if pend:
                pending_txt = (
                    f" | ⏳PEND {pend['direccion']} "
                    f"POC={pend['poc_precio']:.8f}"
                )

            print(
                f"   {trend_icon} {tf} | {sh_txt} | {sl_txt}{pending_txt}",
                flush=True,
            )

            if evento:
                print(
                    f"   ⚡ {tf} | {evento['tipo']} | "
                    f"nivel {evento['nivel_roto']:.8f} | "
                    f"+/-{evento['distancia_pct']:.2f}%",
                    flush=True,
                )

                if pending_nuevo:
                    print(
                        f"      → POC {pending_nuevo['poc_precio']:.8f}",
                        flush=True,
                    )
                    print(
                        f"      → Zona {pending_nuevo['poc_btm']:.8f}"
                        f" - {pending_nuevo['poc_top']:.8f}",
                        flush=True,
                    )
                else:
                    print(
                        "      → Sin pending: POC no válido para entrada",
                        flush=True,
                    )

                eventos.append({
                    "symbol": symbol,
                    "tf": tf,
                    "evento": evento,
                    "pending": pending_nuevo,
                })

    # ========================================================
    # RESUMEN
    # ========================================================
    print("\n" + "=" * 70, flush=True)
    print("📢 RESUMEN STRUCTURE BOT V2.1", flush=True)
    print("=" * 70, flush=True)

    # 1. Estructura (solo log, sin Telegram)
    for ev in eventos:
        msg = construir_mensaje_estructura(
            ev["symbol"], ev["tf"], ev["evento"], ev["pending"]
        )
        print("\n" + msg, flush=True)
        # enviar_telegram(msg)  # DESACTIVADO: solo log

    # 2. Entradas (SÍ Telegram)
    for entrada in entradas:
        msg = construir_mensaje_entrada(
            entrada["symbol"],
            entrada["tf"],
            entrada["señal"],
            entrada["pending"],
            entrada["precio"],
            entrada.get("fibo_mult"),
            entrada["contexto_15m"],   # [BUG 1] del propio símbolo
            entrada["contexto_1h"],    # [BUG 1] del propio símbolo
        )

        print("\n" + msg, flush=True)

        enviado = enviar_telegram(msg)
        if enviado:
            print("   ✅ Telegram V2.1 enviado", flush=True)
        else:
            print("   ⚠️ Telegram V2.1 NO enviado", flush=True)

    guardar_estado(estado_global)

    print("\n" + "=" * 70, flush=True)
    print("🏗️ STRUCTURE BOT V2.1 FINALIZADO", flush=True)
    print(f"CHoCH/BOS nuevos: {len(eventos)}", flush=True)
    print(f"🎯 ENTRADAS V2.1: {len(entradas)}", flush=True)
    print(f"⌛ Expirados: {len(expirados)}", flush=True)
    print(f"💾 Estado V2.1: {STATE_FILE}", flush=True)
    print("📦 Datos: data/cache/*.json", flush=True)
    print("🏁 PROGRAMA TERMINADO", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ ERROR STRUCTURE BOT V2.1: {str(e)}", flush=True)
        import traceback
        traceback.print_exc()
        raise
