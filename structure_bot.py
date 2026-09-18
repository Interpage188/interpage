#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STRUCTURE BOT — BOS / CHoCH
Detecta cambios de estructura usando MACD sobre velas del cache del recolector.

[FIX] Solo alerta eventos NUEVOS (últimas 3 velas desde última corrida).
[FIX] Dedup: no repite el mismo nivel roto.
[FIX] Filtro distancia máxima 5%.
"""

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

SYMBOLS = [
    "BTC", "ETH", "BNB", "SOL", "ARB", "UNI", "LTC", "INJ",
    "LINK", "ENA", "SUSHI", "RAY", "HYPE", "ZEC", "DOGE", "STX", "DASH",
]

CACHE_DIR = Path("data/cache")

TIMEFRAMES = ["15m", "1h"]

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

MIN_SWING_PCT = 0.15
MAX_DISTANCIA_PCT = 5.0
VENTANA_VELAS_NUEVAS = 3

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "structure_state.json"

LIMA_OFFSET = timedelta(hours=-5)
HORA_INICIO = 0
HORA_FIN = 24


def hora_permite_envio():
    now_lima = datetime.now(timezone.utc) + LIMA_OFFSET
    return HORA_INICIO <= now_lima.hour < HORA_FIN


def leer_cache_local(symbol):
    path = CACHE_DIR / f"{symbol}.json"
    if not path.exists():
        print(f"   ⚠️ cache {symbol}: no existe", flush=True)
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"   ⚠️ cache {symbol}: {str(e)[:60]}", flush=True)
        return None


def ema(valores, span):
    if not valores:
        return []
    k = 2.0 / (span + 1)
    ema_vals = [valores[0]]
    for v in valores[1:]:
        ema_vals.append(v * k + ema_vals[-1] * (1 - k))
    return ema_vals


def calcular_macd(cierres):
    if len(cierres) < MACD_SLOW + 5:
        return []
    ema_fast = ema(cierres, MACD_FAST)
    ema_slow = ema(cierres, MACD_SLOW)
    macd = [f - s for f, s in zip(ema_fast, ema_slow)]
    return macd


def analizar_timeframe(velas, estado_tf):
    if not velas or len(velas) < 40:
        return None, estado_tf

    ultimo_ts = estado_tf.get("ultimo_ts", 0)
    velas_nuevas = [v for v in velas if v["ts"] > ultimo_ts]

    if not velas_nuevas and ultimo_ts != 0:
        return None, estado_tf

    min_ts_valido = 0
    if ultimo_ts == 0:
        # Primera corrida: solo alertar si rompió en las últimas N velas
        if len(velas) >= VENTANA_VELAS_NUEVAS:
            min_ts_valido = velas[-VENTANA_VELAS_NUEVAS]["ts"]
    else:
        min_ts_valido = velas_nuevas[0]["ts"]

    cierres = [v["c"] for v in velas]
    highs = [v["h"] for v in velas]
    lows = [v["l"] for v in velas]
    timestamps = [v["ts"] for v in velas]

    macd = calcular_macd(cierres)
    if len(macd) < 10:
        return None, estado_tf

    swing_high = estado_tf.get("swing_high")
    swing_low = estado_tf.get("swing_low")
    swing_high_ts = estado_tf.get("swing_high_ts", 0)
    swing_low_ts = estado_tf.get("swing_low_ts", 0)
    trend = estado_tf.get("trend", 0)

    for i in range(1, len(macd)):
        m_prev = macd[i - 1]
        m_now = macd[i]

        if m_prev <= 0 and m_now > 0:
            idx = i - 1
            if 0 <= idx < len(lows):
                nuevo_low = lows[idx]
                ts_low = timestamps[idx]
                if swing_low is None or abs(nuevo_low - swing_low) / swing_low * 100 >= MIN_SWING_PCT:
                    swing_low = nuevo_low
                    swing_low_ts = ts_low

        if m_prev >= 0 and m_now < 0:
            idx = i - 1
            if 0 <= idx < len(highs):
                nuevo_high = highs[idx]
                ts_high = timestamps[idx]
                if swing_high is None or abs(nuevo_high - swing_high) / swing_high * 100 >= MIN_SWING_PCT:
                    swing_high = nuevo_high
                    swing_high_ts = ts_high

    cierre_actual = cierres[-1]
    ts_actual = timestamps[-1]
    evento = None

    if swing_high is not None and cierre_actual > swing_high:
        dist_pct = ((cierre_actual - swing_high) / swing_high) * 100
        # Solo alertar si:
        # 1. La ruptura ocurrió DESPUÉS de min_ts_valido
        # 2. Distancia dentro del máximo
        if ts_actual >= min_ts_valido and dist_pct <= MAX_DISTANCIA_PCT:
            if trend == 1:
                tipo = "BOS ALCISTA"
            else:
                tipo = "CHoCH ALCISTA"
            evento = {
                "tipo": tipo,
                "direccion": "up",
                "nivel_roto": swing_high,
                "precio": cierre_actual,
                "distancia_pct": dist_pct,
            }
            trend = 1
            swing_high = None
            swing_high_ts = 0

    elif swing_low is not None and cierre_actual < swing_low:
        dist_pct = ((swing_low - cierre_actual) / swing_low) * 100
        if ts_actual >= min_ts_valido and dist_pct <= MAX_DISTANCIA_PCT:
            if trend == -1:
                tipo = "BOS BAJISTA"
            else:
                tipo = "CHoCH BAJISTA"
            evento = {
                "tipo": tipo,
                "direccion": "down",
                "nivel_roto": swing_low,
                "precio": cierre_actual,
                "distancia_pct": dist_pct,
            }
            trend = -1
            swing_low = None
            swing_low_ts = 0

    nuevo_estado = {
        "trend": trend,
        "swing_high": swing_high,
        "swing_low": swing_low,
        "swing_high_ts": swing_high_ts,
        "swing_low_ts": swing_low_ts,
        "ultimo_evento": evento["tipo"] if evento else estado_tf.get("ultimo_evento"),
        "ultimo_ts": ts_actual,
        "actualizado": datetime.now(timezone.utc).isoformat(),
    }
    return evento, nuevo_estado


def enviar_telegram(msg):
    if not hora_permite_envio():
        print("   ⏰ Fuera de horario. No se envía.", flush=True)
        return False
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("   ⚠️ Telegram no configurado.", flush=True)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = f"chat_id={urllib.parse.quote(str(chat_id))}&text={urllib.parse.quote(msg)}".encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.loads(r.read().decode("utf-8"))
        return res.get("ok", False)
    except Exception as e:
        print(f"   ⚠️ Telegram error: {str(e)[:60]}", flush=True)
        return False


def cargar_estado():
    if not STATE_FILE.exists():
        return {}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def guardar_estado(estado):
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(estado, f, indent=2)


def main():
    print("\n" + "=" * 70, flush=True)
    print("🏗️  STRUCTURE BOT — BOS / CHoCH", flush=True)
    print(f"   {len(SYMBOLS)} monedas | TF: {', '.join(TIMEFRAMES)}", flush=True)
    print(f"   Ventana: últimas {VENTANA_VELAS_NUEVAS} velas | Max dist: {MAX_DISTANCIA_PCT}%", flush=True)
    print("=" * 70, flush=True)
    print(f"\nHora UTC: {datetime.now(timezone.utc).isoformat()}", flush=True)

    estado_global = cargar_estado()
    eventos = []

    for symbol in SYMBOLS:
        print(f"\n🔍 {symbol}", flush=True)
        cache = leer_cache_local(symbol)
        if not cache:
            continue

        for tf in TIMEFRAMES:
            velas = cache.get(f"velas_{tf}", [])
            if not velas or len(velas) < 40:
                print(f"   ⚠️ {tf}: sin velas suficientes ({len(velas)})", flush=True)
                continue

            clave = f"{symbol}_{tf}"
            estado_tf = estado_global.get(clave, {})

            evento, nuevo_estado = analizar_timeframe(velas, estado_tf)
            estado_global[clave] = nuevo_estado

            trend = nuevo_estado.get("trend", 0)
            trend_str = "↑" if trend == 1 else "↓" if trend == -1 else "→"
            sh = nuevo_estado.get("swing_high")
            sl = nuevo_estado.get("swing_low")
            sh_str = f"SH={sh:.6f}" if sh else "SH=—"
            sl_str = f"SL={sl:.6f}" if sl else "SL=—"

            if evento:
                print(f"   ⚡ {tf} | {evento['tipo']} en {evento['nivel_roto']:.6f} "
                      f"(precio {evento['precio']:.6f}, +{evento['distancia_pct']:.2f}%)", flush=True)
                eventos.append({
                    "symbol": symbol,
                    "tf": tf,
                    "evento": evento,
                })
            else:
                print(f"   {trend_str} {tf} | {sh_str} | {sl_str}", flush=True)

    print("\n" + "=" * 70, flush=True)
    print("📢 EVENTOS DETECTADOS", flush=True)
    print("=" * 70, flush=True)

    if not eventos:
        print("Sin eventos nuevos en este ciclo.", flush=True)
    else:
        for ev in eventos:
            symbol = ev["symbol"]
            tf = ev["tf"]
            e = ev["evento"]
            icono = "🟢" if e["direccion"] == "up" else "🔴"
            now_lima = (datetime.now(timezone.utc) + LIMA_OFFSET).strftime("%Y-%m-%d %H:%M:%S")

            msg = (
                f"🏗️ STRUCTURE BOT — {e['tipo']}\n"
                f"{icono} {symbol}\n"
                f"📈 Precio: ${e['precio']:.6f}\n"
                f"🎯 Nivel roto: ${e['nivel_roto']:.6f}\n"
                f"📊 Distancia: +{e['distancia_pct']:.2f}%\n"
                f"⏱️ Timeframe: {tf}\n"
                f"🕐 Hora Lima: {now_lima}\n"
                f"━━━━━━━━━━━━━━━━━━━"
            )
            print(f"\n{msg}", flush=True)

            if enviar_telegram(msg):
                print("   ✅ Enviado a Telegram", flush=True)

    guardar_estado(estado_global)

    print("\n" + "=" * 70, flush=True)
    print(f"Total eventos: {len(eventos)}", flush=True)
    print(f"💾 Estado: {STATE_FILE.name}", flush=True)
    print("🏁 PROGRAMA TERMINADO", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
