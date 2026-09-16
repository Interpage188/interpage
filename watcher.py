#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ============================================================
# WATCHER — solo alarma de lo que multi.py publicó
#
# No analiza. No consulta OKX. No decide nada.
# Solo:
#   1. Lee data/pending_levels.json (lo que multi.py publicó)
#   2. Lee el precio del cache del recolector
#   3. Compara y avisa por Telegram:
#      - POR TOCAR: precio se acercó (≤0.5%)
#      - TOCÓ: precio llegó (≤0.15% o mecha)
#   4. Expira si se aleja >1.5% (respecto a la distancia de emisión)
#      o pasa 24h
# ============================================================

DATA_DIR = Path("data")
CACHE_DIR = DATA_DIR / "cache"
PENDING_FILE = DATA_DIR / "pending_levels.json"

TOQUE_PCT = 0.15              # para "tocó"
CERCA_PCT = 0.50              # para "por tocar"
EXPIRACION_PCT = 1.5          # alejamiento que expira
MAX_HORAS_VIGENCIA = 24       # vida máxima del nivel

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


def precio_del_cache(symbol):
    """
    Lee del cache del recolector. Devuelve (price, high15, low15).
    Es solo lectura de números. No es análisis.
    """
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


def enviar_telegram(msg):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
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
        return result.get("ok", False)
    except Exception:
        return False


def main():
    ahora = ahora_utc()
    hora_lima_dt = datetime.fromtimestamp(
        ahora.timestamp() + LIMA_OFFSET_HORAS * 3600,
        tz=timezone.utc
    )

    print("=" * 70, flush=True)
    print("🎯 WATCHER — solo alarma", flush=True)
    print(f"   {ahora.isoformat()}", flush=True)
    print("=" * 70, flush=True)

    levels = leer_json(PENDING_FILE)
    if not levels:
        print("⏭️ Sin niveles pendientes → salida rápida", flush=True)
        return

    print(f"📋 {len(levels)} niveles pendientes", flush=True)

    tocados = 0
    por_tocar = 0
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
        score = item.get("score", 0)
        touch = item.get("touchCount", 0)
        tf = item.get("timeframe", "?")
        distancia_emision = item.get("distancia_emision", 0)

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

        precio, high15, low15 = precio_del_cache(symbol)
        if precio is None:
            print(f"⚠️ {symbol}: sin precio en cache", flush=True)
            esperando += 1
            continue

        distancia_pct = ((precio - nivel) / nivel) * 100
        dist_abs = abs(distancia_pct)

        # ===== TOCÓ (close) =====
        toco_close = dist_abs <= TOQUE_PCT

        # ===== TOCÓ (mecha) =====
        toco_mecha = False
        mecha_info = ""
        if direccion == "SHORT" and high15 is not None and high15 >= nivel:
            toco_mecha = True
            mecha_info = f"high=${high15:.6f}"
        elif direccion == "LONG" and low15 is not None and low15 <= nivel:
            toco_mecha = True
            mecha_info = f"low=${low15:.6f}"

        if toco_close or toco_mecha:
            motivo = "Nivel alcanzado" if toco_close else "Mecha tocó el nivel"
            print(f"🎯 {symbol}: TOCÓ — {motivo}", flush=True)

            emoji = "🟢" if direccion == "LONG" else "🔴"
            accion = "COMPRA" if direccion == "LONG" else "VENDE"

            msg = (
                f"{emoji} {accion} {symbol}\n"
                f"📈 Precio: ${precio:.6f}\n"
                f"📐 Nivel: ${nivel:.6f} ({tf})\n"
                f"🎯 Score: {score:.1f} | {touch}T\n"
                f"✅ {motivo}\n"
                f"🕐 {hora_lima_dt.strftime('%H:%M')} Lima"
            )
            if toco_mecha and not toco_close:
                msg += f"\n📏 {mecha_info}"

            enviar_telegram(msg)

            item["estado"] = "tocado"
            item["tocado_en"] = ahora.isoformat()
            item["precio_en_toque"] = precio
            tocados += 1
            continue

        # ===== POR TOCAR =====
        if (dist_abs <= CERCA_PCT
            and distancia_emision > CERCA_PCT
            and not item.get("aviso_por_tocar")):
            print(f"🟡 {symbol}: POR TOCAR — {dist_abs:.2f}% del nivel", flush=True)

            emoji = "🟢" if direccion == "LONG" else "🔴"
            accion = "COMPRA" if direccion == "LONG" else "VENDE"

            msg = (
                f"{emoji} {accion} {symbol} ⚠️ POR TOCAR\n"
                f"📈 Precio: ${precio:.6f}\n"
                f"📐 Nivel: ${nivel:.6f} ({tf})\n"
                f"🎯 Score: {score:.1f} | {touch}T\n"
                f"📊 Distancia: {dist_abs:.2f}%\n"
                f"💡 Prepara entrada\n"
                f"🕐 {hora_lima_dt.strftime('%H:%M')} Lima"
            )
            enviar_telegram(msg)

            item["aviso_por_tocar"] = True
            item["aviso_por_tocar_en"] = ahora.isoformat()
            por_tocar += 1
            continue

        # ===== ALEJAMIENTO =====
        # Solo expira si el nivel estaba dentro de rango al emitirse
        # (≤ EXPIRACION_PCT) y ahora se alejó más allá.
        # Niveles emitidos ya lejos (>EXPIRACION_PCT) siguen vivos
        # hasta acercarse o expirar por tiempo.
        if dist_abs > EXPIRACION_PCT and distancia_emision <= EXPIRACION_PCT:
            item["estado"] = "expirado"
            item["expirado_motivo"] = f"alejado {distancia_pct:+.2f}%"
            expirados += 1
            print(f"⏰ {symbol}: expirado por alejamiento", flush=True)
            continue

        esperando += 1
        print(f"👀 {symbol} {direccion}: esperando ({distancia_pct:+.2f}%)", flush=True)

    guardar_json(PENDING_FILE, levels)

    print("=" * 70, flush=True)
    print(f"📊 RESULTADO: {esperando} activos | {tocados} tocados | {por_tocar} por tocar | {expirados} expirados", flush=True)
    print("🏁 WATCHER TERMINADO", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
        raise
