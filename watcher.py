#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ============================================================
# WATCHER — vigila niveles pendientes y avisa al toque
# Corre junto al recolector cada 5 min
# ============================================================

DATA_DIR = Path("data")
CACHE_DIR = DATA_DIR / "cache"
PENDING_FILE = DATA_DIR / "pending_levels.json"

TOQUE_PCT = 0.15
EXPIRACION_PCT = 1.5
MAX_HORAS_VIGENCIA = 24

LIMA_OFFSET_HORAS = -5


def ahora_utc():
    return datetime.now(timezone.utc)


def leer_json(path):
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def guardar_json(path, data):
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def precio_actual(symbol):
    p = CACHE_DIR / f"{symbol}.json"
    data = leer_json(p)
    if not data:
        return None
    pulso = data.get("pulso", [])
    if not pulso:
        return None
    return pulso[-1].get("price")


def enviar_telegram(msg):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("⚠️ Telegram no configurado", flush=True)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = f"chat_id={urllib.parse.quote(str(chat_id))}&text={urllib.parse.quote(msg)}".encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            result = json.loads(r.read().decode("utf-8"))
        if result.get("ok"):
            print("📨 Telegram enviado", flush=True)
            return True
        print(f"⚠️ Telegram devolvió: {result}", flush=True)
        return False
    except Exception as e:
        print(f"⚠️ Error Telegram: {e}", flush=True)
        return False


def main():
    ahora = ahora_utc()
    hora_lima_dt = datetime.fromtimestamp(
        ahora.timestamp() + LIMA_OFFSET_HORAS * 3600,
        tz=timezone.utc
    )

    print("=" * 70, flush=True)
    print("🎯 WATCHER — vigila niveles pendientes", flush=True)
    print(f"   {ahora.isoformat()}", flush=True)
    print("=" * 70, flush=True)

    levels = leer_json(PENDING_FILE)
    if not levels:
        print("⏭️ Sin niveles pendientes → salida rápida", flush=True)
        return

    print(f"📋 {len(levels)} niveles pendientes", flush=True)

    tocados = 0
    expirados = 0
    esperando = 0

    for item in levels:
        if item.get("estado") != "esperando_toque":
            if item.get("estado") == "tocado":
                tocados += 1
            elif item.get("estado") == "expirado":
                expirados += 1
            continue

        symbol = item["symbol"]
        nivel = float(item["level"])
        direccion = item["direction"]

        # Antigüedad
        emitido = item.get("emitido_en")
        if emitido:
            try:
                dt_em = datetime.fromisoformat(emitido)
                if dt_em.tzinfo is None:
                    dt_em = dt_em.replace(tzinfo=timezone.utc)
                horas = (ahora - dt_em).total_seconds() / 3600
                if horas > MAX_HORAS_VIGENCIA:
                    item["estado"] = "expirado"
                    item["expirado_motivo"] = f"antigüedad {horas:.1f}h"
                    expirados += 1
                    print(f"⏰ {symbol}: expirado por antigüedad", flush=True)
                    continue
            except Exception:
                pass

        precio = precio_actual(symbol)
        if precio is None:
            print(f"⚠️ {symbol}: sin precio en cache", flush=True)
            esperando += 1
            continue

        distancia_pct = ((precio - nivel) / nivel) * 100

        # Toque detectado
        if abs(distancia_pct) <= TOQUE_PCT:
            print(f"🎯 {symbol}: TOQUE — nivel=${nivel:.6f} precio=${precio:.6f} ({distancia_pct:+.2f}%)", flush=True)

            emoji = "🟢" if direccion == "LONG" else "🔴"
            msg = (
                f"🎯 TOQUE DETECTADO\n"
                f"{emoji} {direccion} {symbol}\n"
                f"📈 Precio: ${precio:.6f}\n"
                f"📐 Nivel: ${nivel:.6f}\n"
                f"📊 Distancia: {distancia_pct:+.3f}%\n"
                f"🎯 Score original: {item.get('score', 0):.1f}\n"
                f"• Timeframe: {item.get('timeframe', '?')}\n"
                f"• Toques: {item.get('touchCount', 0)}\n"
                f"🕐 Hora Lima: {hora_lima_dt.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"⚠️ Evalúa rechazo/confirmación antes de operar."
            )
            enviar_telegram(msg)

            item["estado"] = "tocado"
            item["avisado"] = True
            item["tocado_en"] = ahora.isoformat()
            item["precio_en_toque"] = precio
            tocados += 1
            continue

        # Alejamiento
        if abs(distancia_pct) > EXPIRACION_PCT:
            item["estado"] = "expirado"
            item["expirado_motivo"] = f"precio alejado {distancia_pct:+.2f}%"
            expirados += 1
            print(f"⏰ {symbol}: expirado por alejamiento", flush=True)
            continue

        esperando += 1
        print(f"👀 {symbol} {direccion}: esperando ({distancia_pct:+.2f}%)", flush=True)

    guardar_json(PENDING_FILE, levels)

    print("=" * 70, flush=True)
    print(f"📊 RESULTADO: {esperando} activos | {tocados} tocados | {expirados} expirados", flush=True)
    print("🏁 WATCHER TERMINADO", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
        raise
