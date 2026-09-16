#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ============================================================
# WATCHER — vigila niveles pendientes y avisa al toque o aproximación
# Corre junto al recolector cada 5 min
#
# [FIX 2B] Detecta toques por close Y por mecha (high15 / low15)
# [FIX 3B] Detecta aproximación confirmada: el precio se acercó
#          al nivel y ya retrocedió (rechazo temprano)
# ============================================================

DATA_DIR = Path("data")
CACHE_DIR = DATA_DIR / "cache"
PENDING_FILE = DATA_DIR / "pending_levels.json"

# Umbrales de toque
TOQUE_PCT = 0.15              # distancia máxima para considerar "toque exacto"
EXPIRACION_PCT = 1.5          # si el precio se aleja > X% → expira
MAX_HORAS_VIGENCIA = 24       # niveles más viejos → expiran

# [FIX 3B] Aproximación confirmada
APROXIMACION_PCT = 0.8        # % máximo de acercamiento al nivel
APROXIMACION_HORAS = 3        # ventana de tiempo para mirar el cache
RETROCESO_MIN_PCT = 0.3       # % mínimo de retroceso desde el pico

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
    """Devuelve (precio_close, high15, low15) del último sample."""
    p = CACHE_DIR / f"{symbol}.json"
    data = leer_json(p)
    if not data:
        return None, None, None
    pulso = data.get("pulso", [])
    if not pulso:
        return None, None, None
    ultimo = pulso[-1]
    return (
        ultimo.get("price"),
        ultimo.get("high15"),
        ultimo.get("low15"),
    )


def aproximacion_confirmada(symbol, nivel, direccion, horas=APROXIMACION_HORAS):
    """
    Detecta si el precio se acercó al nivel y ya retrocedió.
    Devuelve (bool, pico_alcanzado, distancia_al_nivel_pct).
    """
    p = CACHE_DIR / f"{symbol}.json"
    data = leer_json(p)
    if not data:
        return False, None, None

    pulso = data.get("pulso", [])
    if not pulso:
        return False, None, None

    ahora_ts = ahora_utc().timestamp()
    limite_ts = ahora_ts - horas * 3600

    precio_actual = pulso[-1].get("price")
    if precio_actual is None:
        return False, None, None

    if direccion == "SHORT":
        highs = [m.get("high15") for m in pulso
                 if m.get("ts", 0) >= limite_ts and m.get("high15") is not None]
        if not highs:
            return False, None, None
        pico = max(highs)
        dist = ((nivel - pico) / nivel) * 100
        if 0 <= dist <= APROXIMACION_PCT:
            retroceso = ((pico - precio_actual) / pico) * 100
            if retroceso >= RETROCESO_MIN_PCT:
                return True, pico, dist
    elif direccion == "LONG":
        lows = [m.get("low15") for m in pulso
                if m.get("ts", 0) >= limite_ts and m.get("low15") is not None]
        if not lows:
            return False, None, None
        piso = min(lows)
        dist = ((piso - nivel) / nivel) * 100
        if 0 <= dist <= APROXIMACION_PCT:
            retroceso = ((precio_actual - piso) / piso) * 100
            if retroceso >= RETROCESO_MIN_PCT:
                return True, piso, dist

    return False, None, None


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

        precio, high15, low15 = precio_actual(symbol)
        if precio is None:
            print(f"⚠️ {symbol}: sin precio en cache", flush=True)
            esperando += 1
            continue

        distancia_pct = ((precio - nivel) / nivel) * 100

        # ===== Toque por close =====
        toque_close = abs(distancia_pct) <= TOQUE_PCT

        # ===== Toque por mecha =====
        toque_mecha = False
        mecha_info = ""
        if direccion == "SHORT" and high15 is not None and high15 >= nivel:
            toque_mecha = True
            mecha_info = f"high=${high15:.6f}"
        elif direccion == "LONG" and low15 is not None and low15 <= nivel:
            toque_mecha = True
            mecha_info = f"low=${low15:.6f}"

        # ===== Toque detectado =====
        if toque_close or toque_mecha:
            motivo = "Nivel alcanzado" if toque_close else "Mecha tocó el nivel"
            print(f"🎯 {symbol}: TOQUE ({motivo}) — nivel=${nivel:.6f} precio=${precio:.6f} ({distancia_pct:+.2f}%)", flush=True)

            emoji = "🟢" if direccion == "LONG" else "🔴"
            accion = "COMPRA" if direccion == "LONG" else "VENDE"

            msg = (
                f"{emoji} {accion} {symbol}\n"
                f"📈 Precio actual: ${precio:.6f}\n"
                f"📐 Nivel objetivo: ${nivel:.6f}\n"
                f"✅ {motivo}\n"
                f"🕐 {hora_lima_dt.strftime('%H:%M')} Lima"
            )
            if toque_mecha and not toque_close:
                msg += f"\n📏 {mecha_info}"

            enviar_telegram(msg)

            item["estado"] = "tocado"
            item["avisado"] = True
            item["tocado_en"] = ahora.isoformat()
            item["precio_en_toque"] = precio
            item["tipo_toque"] = "exacto"
            tocados += 1
            continue

        # ===== [FIX 3B] Aproximación confirmada =====
        aprox, pico, dist_aprox = aproximacion_confirmada(symbol, nivel, direccion)
        if aprox:
            print(f"🟡 {symbol}: APROXIMACIÓN — pico=${pico:.6f} ({dist_aprox:.2f}% antes del nivel)", flush=True)

            emoji = "🟢" if direccion == "LONG" else "🔴"
            accion = "COMPRA" if direccion == "LONG" else "VENDE"

            msg = (
                f"{emoji} {accion} {symbol}\n"
                f"📈 Precio actual: ${precio:.6f}\n"
                f"📐 Nivel objetivo: ${nivel:.6f}\n"
                f"⚠️ Se acercó a ${pico:.6f} y retrocedió\n"
                f"🕐 {hora_lima_dt.strftime('%H:%M')} Lima"
            )
            enviar_telegram(msg)

            item["estado"] = "tocado"
            item["avisado"] = True
            item["tocado_en"] = ahora.isoformat()
            item["precio_en_toque"] = precio
            item["tipo_toque"] = "aproximacion"
            item["pico_alcanzado"] = pico
            tocados += 1
            continue

        # ===== Alejamiento =====
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
