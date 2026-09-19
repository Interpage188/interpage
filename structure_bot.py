#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STRUCTURE BOT — VERSIÓN COMPLETA (Fases 1-5)
- Fase 1: CHoCH / BOS (MACD sobre velas)
- Fase 2: POC (Volume Profile del impulso)
- Fase 3: Entry Sniper (retroceso al POC + cruce MACD)
- Fase 4: Fibo Time (proyecciones temporales)
- Fase 5: Multi-TF (contexto 15m y 1h)

Fixes:
- Solo LONG si POC < precio actual (no comprar en el techo)
- Solo SHORT si POC > precio actual (no vender en el piso)
- Dedup por nivel alertado
- Ventana de 3 velas para eventos nuevos
- Filtro distancia máxima
"""

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ============================================================
# CONFIGURACIÓN
# ============================================================

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
MAX_NIVELES_GUARDADOS = 20

# POC
POC_BINS = 24
ENTRY_MACD_VENTANA = 3
ENTRY_EXPIRA_HORAS = 4

# Fibo Time
FIBO_MULTIPLOS = [3, 5, 8, 13, 21, 34]
FIBO_TOLERANCIA_VELAS = 1

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "structure_state.json"

LIMA_OFFSET = timedelta(hours=-5)
HORA_INICIO = 0
HORA_FIN = 24


def hora_permite_envio():
    now_lima = datetime.now(timezone.utc) + LIMA_OFFSET
    return HORA_INICIO <= now_lima.hour < HORA_FIN


# ============================================================
# CACHE
# ============================================================

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


# ============================================================
# INDICADORES
# ============================================================

def ema(valores, span):
    if not valores:
        return []
    k = 2.0 / (span + 1)
    out = [valores[0]]
    for v in valores[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def calcular_macd(cierres):
    """Devuelve (macd, signal) como listas."""
    if len(cierres) < MACD_SLOW + 5:
        return [], []
    ef = ema(cierres, MACD_FAST)
    es = ema(cierres, MACD_SLOW)
    macd = [f - s for f, s in zip(ef, es)]
    signal = ema(macd, MACD_SIGNAL)
    return macd, signal


def cruce_macd_reciente(macd, ventana=ENTRY_MACD_VENTANA):
    if len(macd) < 2:
        return None
    ini = max(1, len(macd) - ventana)
    for i in range(ini, len(macd)):
        mp, mn = macd[i - 1], macd[i]
        if mp <= 0 and mn > 0:
            return "up"
        if mp >= 0 and mn < 0:
            return "down"
    return None


# ============================================================
# POC (Volume Profile)
# ============================================================

def calcular_poc(velas, idx_inicio, idx_fin, bins=POC_BINS):
    """Calcula el POC del rango [idx_inicio, idx_fin]."""
    if idx_inicio < 0 or idx_fin >= len(velas) or idx_inicio >= idx_fin:
        return None
    rango = velas[idx_inicio:idx_fin + 1]
    if not rango:
        return None
    top = max(v["h"] for v in rango)
    bottom = min(v["l"] for v in rango)
    if top <= bottom:
        return None
    paso = (top - bottom) / bins
    if paso <= 0:
        return None
    vol = [0.0] * bins
    for v in rango:
        h, l, vv = v["h"], v["l"], v.get("v", 0)
        if vv <= 0:
            continue
        ib = max(0, min(bins - 1, int((l - bottom) / paso)))
        it = max(0, min(bins - 1, int((h - bottom) / paso)))
        n = it - ib + 1
        if n <= 0:
            continue
        for b in range(ib, it + 1):
            vol[b] += vv / n
    mx = max(range(bins), key=lambda i: vol[i])
    poc_b = bottom + mx * paso
    poc_t = poc_b + paso
    return {
        "poc_top": poc_t,
        "poc_btm": poc_b,
        "poc_precio": (poc_b + poc_t) / 2,
        "rango_top": top,
        "rango_btm": bottom,
    }


# ============================================================
# FIBO TIME
# ============================================================

def calcular_fibo_zones(idx_choch, idx_inicio_swing, ts_actual):
    """
    Calcula los pivotes temporales futuros.
    Devuelve lista de dicts con idx proyectado, multiplicador y timestamp.
    """
    dist = idx_choch - idx_inicio_swing
    if dist <= 0:
        return []
    zonas = []
    for mult in FIBO_MULTIPLOS:
        idx_proyectado = idx_choch + (dist * mult)
        # Aproximar ts: asumimos que cada vela dura (ts_actual - ts_inicio) / dist
        zonas.append({
            "mult": mult,
            "idx": idx_proyectado,
            "distancia_velas": dist * mult,
        })
    return zonas


def fibo_time_activo(velas, fibo_zones, velas_desde_choch):
    """
    Verifica si estamos en una zona Fibo Time activa.
    velas_desde_choch = cuántas velas han pasado desde el CHoCH.
    """
    if not fibo_zones:
        return None
    for fz in fibo_zones:
        target = fz["distancia_velas"]
        if abs(velas_desde_choch - target) <= FIBO_TOLERANCIA_VELAS:
            return fz["mult"]
    return None


# ============================================================
# DETECCIÓN DE ESTRUCTURA
# ============================================================

def ya_fue_alertado(nivel, niveles):
    for n in niveles:
        try:
            if abs(nivel - n) / n * 100 < 0.1:
                return True
        except ZeroDivisionError:
            continue
    return False


def analizar_timeframe(velas, estado_tf):
    """Detecta CHoCH/BOS y calcula POC + Fibo zones."""
    if not velas or len(velas) < 40:
        return None, None, estado_tf

    ultimo_ts = estado_tf.get("ultimo_ts", 0)
    velas_nuevas = [v for v in velas if v["ts"] > ultimo_ts]
    if not velas_nuevas and ultimo_ts != 0:
        return None, None, estado_tf

    if ultimo_ts == 0:
        min_ts = velas[-VENTANA_VELAS_NUEVAS]["ts"] if len(velas) >= VENTANA_VELAS_NUEVAS else 0
    else:
        min_ts = velas_nuevas[0]["ts"]

    cierres = [v["c"] for v in velas]
    highs = [v["h"] for v in velas]
    lows = [v["l"] for v in velas]
    ts = [v["ts"] for v in velas]

    macd, signal_line = calcular_macd(cierres)
    if len(macd) < 10:
        return None, None, estado_tf

    swing_high = estado_tf.get("swing_high")
    swing_low = estado_tf.get("swing_low")
    sh_idx = estado_tf.get("swing_high_idx")
    sl_idx = estado_tf.get("swing_low_idx")
    trend = estado_tf.get("trend", 0)
    niveles = estado_tf.get("niveles_alertados", [])

    # Detectar swings
    for i in range(1, len(macd)):
        mp, mn = macd[i - 1], macd[i]
        if mp <= 0 and mn > 0:
            idx = i - 1
            if 0 <= idx < len(lows):
                nl = lows[idx]
                if swing_low is None or abs(nl - swing_low) / swing_low * 100 >= MIN_SWING_PCT:
                    swing_low = nl
                    sl_idx = idx
        if mp >= 0 and mn < 0:
            idx = i - 1
            if 0 <= idx < len(highs):
                nh = highs[idx]
                if swing_high is None or abs(nh - swing_high) / swing_high * 100 >= MIN_SWING_PCT:
                    swing_high = nh
                    sh_idx = idx

    cierre = cierres[-1]
    ts_act = ts[-1]
    evento = None
    pending = None

    # CHoCH/BOS ALCISTA
    if swing_high is not None and cierre > swing_high:
        dist = ((cierre - swing_high) / swing_high) * 100
        if not ya_fue_alertado(swing_high, niveles) and ts_act >= min_ts and dist <= MAX_DISTANCIA_PCT:
            tipo = "BOS ALCISTA" if trend == 1 else "CHoCH ALCISTA"
            evento = {
                "tipo": tipo, "direccion": "up",
                "nivel_roto": swing_high, "precio": cierre,
                "distancia_pct": dist,
            }
            trend = 1
            # Calcular POC del impulso
            if sl_idx is not None and sl_idx < len(velas) - 1:
                poc = calcular_poc(velas, sl_idx, len(velas) - 1)
                if poc:
                    # FIX: solo crear pending si POC < precio actual (retroceso)
                    if poc["poc_precio"] < cierre:
                        # Calcular Fibo zones
                        fibo_zones = calcular_fibo_zones(len(velas) - 1, sl_idx, ts_act)
                        pending = {
                            "tipo": tipo, "direccion": "up",
                            "creado_ts": ts_act,
                            "expira_ts": ts_act + ENTRY_EXPIRA_HORAS * 3600 * 1000,
                            "idx_choch": len(velas) - 1,
                            "fibo_zones": fibo_zones,
                            **poc,
                        }
                    else:
                        print(f"      ⚠️ POC ({poc['poc_precio']:.6f}) >= precio ({cierre:.6f}) → no es zona de compra", flush=True)
            niveles.append(swing_high)
            niveles = niveles[-MAX_NIVELES_GUARDADOS:]
            swing_high = None
            sh_idx = None

    # CHoCH/BOS BAJISTA
    elif swing_low is not None and cierre < swing_low:
        dist = ((swing_low - cierre) / swing_low) * 100
        if not ya_fue_alertado(swing_low, niveles) and ts_act >= min_ts and dist <= MAX_DISTANCIA_PCT:
            tipo = "BOS BAJISTA" if trend == -1 else "CHoCH BAJISTA"
            evento = {
                "tipo": tipo, "direccion": "down",
                "nivel_roto": swing_low, "precio": cierre,
                "distancia_pct": dist,
            }
            trend = -1
            if sh_idx is not None and sh_idx < len(velas) - 1:
                poc = calcular_poc(velas, sh_idx, len(velas) - 1)
                if poc:
                    # FIX: solo SHORT si POC > precio actual
                    if poc["poc_precio"] > cierre:
                        fibo_zones = calcular_fibo_zones(len(velas) - 1, sh_idx, ts_act)
                        pending = {
                            "tipo": tipo, "direccion": "down",
                            "creado_ts": ts_act,
                            "expira_ts": ts_act + ENTRY_EXPIRA_HORAS * 3600 * 1000,
                            "idx_choch": len(velas) - 1,
                            "fibo_zones": fibo_zones,
                            **poc,
                        }
                    else:
                        print(f"      ⚠️ POC ({poc['poc_precio']:.6f}) <= precio ({cierre:.6f}) → no es zona de venta", flush=True)
            niveles.append(swing_low)
            niveles = niveles[-MAX_NIVELES_GUARDADOS:]
            swing_low = None
            sl_idx = None

    nuevo_estado = {
        "trend": trend,
        "swing_high": swing_high,
        "swing_low": swing_low,
        "swing_high_idx": sh_idx,
        "swing_low_idx": sl_idx,
        "niveles_alertados": niveles,
        "ultimo_evento": evento["tipo"] if evento else estado_tf.get("ultimo_evento"),
        "ultimo_ts": ts_act,
        "macd_actual": round(macd[-1], 4) if macd else None,
        "macd_signal": round(signal_line[-1], 4) if signal_line else None,
        "macd_hist": round(macd[-1] - signal_line[-1], 4) if signal_line else None,
        "high_5": max(v["h"] for v in velas[-5:]) if len(velas) >= 5 else None,
        "low_5": min(v["l"] for v in velas[-5:]) if len(velas) >= 5 else None,
        "actualizado": datetime.now(timezone.utc).isoformat(),
    }
    return evento, pending, nuevo_estado


# ============================================================
# VERIFICACIÓN DE ENTRY
# ============================================================

def verificar_pending(pending, velas_tf, velas_5m):
    """
    Verifica si el precio volvió al POC + cruce MACD en 5m → entrada.

    [FIX MULTI-TF] Bloquea el entry si el 5m ya cambió de dirección
    contra el TF que detectó el CHoCH. Evita entrar cuando el 5m
    ya está en la dirección contraria al pending.
    """
    if not pending or not velas_tf:
        return None
    if velas_tf[-1]["ts"] > pending.get("expira_ts", 0):
        return "EXPIRADO"

    precio = velas_tf[-1]["c"]
    pb, pt = pending["poc_btm"], pending["poc_top"]

    # ¿Tocó la zona POC? Usamos velas de 5m para máxima precisión
    fuente = velas_5m if velas_5m and len(velas_5m) >= 10 else velas_tf
    en_zona = False
    for v in fuente[-ENTRY_MACD_VENTANA - 1:]:
        if v["l"] <= pt and v["h"] >= pb:
            en_zona = True
            break
    if not en_zona:
        return None

    # [FIX MULTI-TF] Verificar dirección del 5m
    if velas_5m and len(velas_5m) >= 2:
        cierre_5m_actual = float(velas_5m[-1]["c"])
        cierre_5m_previo = float(velas_5m[-2]["c"])

        if cierre_5m_actual > cierre_5m_previo:
            direccion_5m = "up"
        elif cierre_5m_actual < cierre_5m_previo:
            direccion_5m = "down"
        else:
            direccion_5m = "flat"

        d = pending["direccion"]

        # Conflicto: pending SHORT pero el 5m ya está subiendo
        if d == "down" and direccion_5m == "up":
            print(f"      ⏭️ Bloqueado: pending SHORT pero 5m ya está UP", flush=True)
            return None

        # Conflicto: pending LONG pero el 5m ya está bajando
        if d == "up" and direccion_5m == "down":
            print(f"      ⏭️ Bloqueado: pending LONG pero 5m ya está DOWN", flush=True)
            return None

    # Cruce MACD en 5m (timing fino)
    cierres = [v["c"] for v in fuente]
    macd, _ = calcular_macd(cierres)
    cruce = cruce_macd_reciente(macd, ENTRY_MACD_VENTANA)
    if cruce is None:
        return None

    d = pending["direccion"]
    if d == "up" and cruce == "up":
        return "ENTRY LONG"
    if d == "down" and cruce == "down":
        return "ENTRY SHORT"
    return None


def fibo_activo_en_pending(pending, velas):
    """Devuelve el multiplicador Fibo activo, si lo hay."""
    if not pending or "fibo_zones" not in pending:
        return None
    idx_choch = pending.get("idx_choch", 0)
    idx_actual = len(velas) - 1
    velas_desde = idx_actual - idx_choch
    if velas_desde < 0:
        return None
    return fibo_time_activo(velas, pending["fibo_zones"], velas_desde)

# ============================================================
# ESTADO
# ============================================================

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


# ============================================================
# MAIN
# ============================================================

def main():
    print("\n" + "=" * 70, flush=True)
    print("🏗️  STRUCTURE BOT — FASES 1-5", flush=True)
    print(f"   {len(SYMBOLS)} monedas | TF: {', '.join(TIMEFRAMES)}", flush=True)
    print(f"   POC bins={POC_BINS} | Entry ventana={ENTRY_MACD_VENTANA} | Expira={ENTRY_EXPIRA_HORAS}h", flush=True)
    print(f"   Fibo múltiplos: {FIBO_MULTIPLOS}", flush=True)
    print("=" * 70, flush=True)
    print(f"\nHora UTC: {datetime.now(timezone.utc).isoformat()}", flush=True)

    estado_global = cargar_estado()
    eventos = []
    entries = []
    expirados = []

    for symbol in SYMBOLS:
        print(f"\n🔍 {symbol}", flush=True)
        cache = leer_cache_local(symbol)
        if not cache:
            continue

        for tf in TIMEFRAMES:
            velas = cache.get(f"velas_{tf}", [])
            if not velas or len(velas) < 40:
                print(f"   ⚠️ {tf}: sin velas ({len(velas)})", flush=True)
                continue

            clave = f"{symbol}_{tf}"
            estado_tf = estado_global.get(clave, {})

            pend_actual = estado_tf.get("pending")
            if pend_actual:
                velas_5m = cache.get("velas_5m", [])
                señal = verificar_pending(pend_actual, velas, velas_5m)
                if señal in ("ENTRY LONG", "ENTRY SHORT"):
                    fibo_mult = fibo_activo_en_pending(pend_actual, velas)
                    entries.append({
                        "symbol": symbol, "tf": tf,
                        "señal": señal, "pending": pend_actual,
                        "precio": velas[-1]["c"],
                        "fibo_mult": fibo_mult,
                    })
                    estado_tf["pending"] = None
                elif señal == "EXPIRADO":
                    expirados.append({"symbol": symbol, "tf": tf})
                    print(f"   ⌛ {tf}: pending expirado", flush=True)
                    estado_tf["pending"] = None

            # 2. Detectar CHoCH/BOS nuevos
            evento, pending_nuevo, nuevo_estado = analizar_timeframe(velas, estado_tf)

            if pending_nuevo:
                nuevo_estado["pending"] = pending_nuevo
            elif estado_tf.get("pending"):
                nuevo_estado["pending"] = estado_tf["pending"]

            estado_global[clave] = nuevo_estado

            trend = nuevo_estado.get("trend", 0)
            t_str = "↑" if trend == 1 else "↓" if trend == -1 else "→"
            sh = nuevo_estado.get("swing_high")
            sl = nuevo_estado.get("swing_low")
            pend = nuevo_estado.get("pending")
            sh_s = f"SH={sh:.6f}" if sh else "SH=—"
            sl_s = f"SL={sl:.6f}" if sl else "SL=—"
            pend_s = ""
            if pend:
                pend_s = f" | ⏳PEND[{pend['direccion']} POC={pend['poc_precio']:.6f}]"

            if evento:
                print(f"   ⚡ {tf} | {evento['tipo']} en {evento['nivel_roto']:.6f} "
                      f"(+{evento['distancia_pct']:.2f}%)", flush=True)
                if pending_nuevo:
                    print(f"      → POC: {pending_nuevo['poc_precio']:.6f} "
                          f"[{pending_nuevo['poc_btm']:.6f}–{pending_nuevo['poc_top']:.6f}]", flush=True)
                    fibo_txt = ", ".join([f"{fz['mult']}×({fz['distancia_velas']}v)" for fz in pending_nuevo.get("fibo_zones", [])[:3]])
                    print(f"      → Fibo zones: {fibo_txt}", flush=True)
                eventos.append({"symbol": symbol, "tf": tf, "evento": evento,
                                "pending": pending_nuevo})
            else:
                print(f"   {t_str} {tf} | {sh_s} | {sl_s}{pend_s}", flush=True)

    # --- Alertas ---
    print("\n" + "=" * 70, flush=True)
    print("📢 RESUMEN", flush=True)
    print("=" * 70, flush=True)

    now_lima = (datetime.now(timezone.utc) + LIMA_OFFSET).strftime("%Y-%m-%d %H:%M:%S")

    # 1. CHoCH/BOS — SOLO log, NO Telegram
    for ev in eventos:
        s, tf, e = ev["symbol"], ev["tf"], ev["evento"]
        icono = "🟢" if e["direccion"] == "up" else "🔴"
        msg = (f"🏗️ ESTRUCTURA — {e['tipo']}\n"
               f"{icono} {s}\n"
               f"📈 Precio: ${e['precio']:.6f}\n"
               f"🎯 Nivel roto: ${e['nivel_roto']:.6f} (+{e['distancia_pct']:.2f}%)\n")
        if ev["pending"]:
            p = ev["pending"]
            msg += (f"📊 POC: ${p['poc_precio']:.6f}\n"
                    f"   Zona: ${p['poc_btm']:.6f} – ${p['poc_top']:.6f}\n")
            fibo_list = p.get("fibo_zones", [])
            if fibo_list:
                fibo_txt = ", ".join([f"{fz['mult']}×" for fz in fibo_list[:3]])
                msg += f"📅 Fibo Time: {fibo_txt}\n"
            msg += f"   ⏳ Esperando retroceso + MACD\n"
        msg += f"⏱️ {tf}\n🕐 {now_lima}\n━━━━━━━━━━━━━━━━━━━"
        print(f"\n{msg}", flush=True)
        # enviar_telegram(msg)  # ← DESACTIVADO: solo log, no Telegram

    # 2. ENTRADAS (señal real) — SÍ Telegram
    for en in entries:
        s, tf = en["symbol"], en["tf"]
        p, precio, tipo = en["pending"], en["precio"], en["señal"]
        icono = "🟢" if tipo == "ENTRY LONG" else "🔴"
        accion = "COMPRA" if tipo == "ENTRY LONG" else "VENTA"
        msg = (f"🎯 STRUCTURE BOT V1\n"
               f"¡{accion} {s}!\n"
               f"{icono} {tipo}\n"
               f"📈 Precio actual: ${precio:.6f}\n"
               f"🎯 POC: ${p['poc_precio']:.6f}\n"
               f"   Zona: ${p['poc_btm']:.6f} – ${p['poc_top']:.6f}\n"
               f"📊 Origen: {p['tipo']} ({tf})\n")
        if en.get("fibo_mult"):
            msg += f"📅 Fibo Time: {en['fibo_mult']}× ACTIVO\n"
        msg += (f"✅ Retroceso al POC + MACD confirmado\n"
                f"🕐 {now_lima}\n━━━━━━━━━━━━━━━━━━━")
        print(f"\n{msg}", flush=True)
        if enviar_telegram(msg):
            print("   ✅ Enviado", flush=True)

    guardar_estado(estado_global)

    print("\n" + "=" * 70, flush=True)
    print(f"CHoCH/BOS nuevos: {len(eventos)}", flush=True)
    print(f"🎯 ENTRADAS: {len(entries)}", flush=True)
    print(f"⌛ Expirados: {len(expirados)}", flush=True)
    print(f"💾 Estado: {STATE_FILE.name}", flush=True)
    print("🏁 PROGRAMA TERMINADO", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
