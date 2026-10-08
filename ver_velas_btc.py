#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Muestra las últimas velas de BTC del cache."""

import json
import urllib.request
from datetime import datetime, timezone

URL = "https://raw.githubusercontent.com/Interpage188/interpage/main/data/cache/BTC.json"

print("=" * 70)
print("📊 ÚLTIMAS VELAS DE BTC (5m)")
print("=" * 70)

try:
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
except Exception as e:
    print(f"❌ Error: {e}")
    exit(1)

velas_5m = data.get("velas_5m", [])
print(f"\n📊 Total velas_5m: {len(velas_5m)}")
print(f"📅 Última actualización: {data.get('updated_at', 'N/A')}")

if len(velas_5m) < 12:
    print("⚠️ Muy pocas velas")
    exit(1)

# Últimas 15 velas
print(f"\n📈 ÚLTIMAS 15 VELAS (5m):")
print(f"{'FECHA UTC':<20} {'OPEN':>10} {'HIGH':>10} {'LOW':>10} {'CLOSE':>10} {'VOL':>10}")
print("-" * 80)

for v in velas_5m[-15:]:
    ts = datetime.fromtimestamp(v["ts"] / 1000, tz=timezone.utc)
    print(f"{ts.strftime('%m-%d %H:%M'):<20} "
          f"{v['o']:>10.1f} {v['h']:>10.1f} {v['l']:>10.1f} {v['c']:>10.1f} "
          f"{v['v']:>10.4f}")

# Calcular cambio 1h (últimas 12 velas)
p_actual = velas_5m[-1]["c"]
p_1h_atras = velas_5m[-13]["c"] if len(velas_5m) >= 13 else velas_5m[0]["c"]
cambio_1h = ((p_actual - p_1h_atras) / p_1h_atras) * 100

# Volumen reciente vs previo
vols_recientes = [v["v"] for v in velas_5m[-12:]]
vols_previos = [v["v"] for v in velas_5m[-24:-12]] if len(velas_5m) >= 24 else []
vol_ratio = (sum(vols_recientes) / sum(vols_previos)) if vols_previos and sum(vols_previos) > 0 else 1.0

print(f"\n{'='*70}")
print(f"📊 ANÁLISIS ÚLTIMA HORA")
print(f"{'='*70}")
print(f"  Precio actual:  ${p_actual:,.1f}")
print(f"  Precio 1h atrás: ${p_1h_atras:,.1f}")
print(f"  Cambio 1h:      {cambio_1h:+.2f}%")
print(f"  Vol ratio 1h:   {vol_ratio:.2f}x")

# Chequeo del filtro con los UMBRALES NUEVOS
print(f"\n{'='*70}")
print(f"🔍 CHEQUEO DEL FILTRO")
print(f"{'='*70}")

# Bloque 1: caída/subida fuerte (-1.3% + vol 1.3x)
dispara_por_caida = cambio_1h < -1.3 and vol_ratio >= 1.3
dispara_por_subida = cambio_1h > 1.3 and vol_ratio >= 1.3

# Bloque 2: volumen masivo (3x+ sin importar cambio)
dispara_por_vol = vol_ratio >= 3.0 and abs(cambio_1h) >= 0.3

if dispara_por_caida:
    print(f"  🔴 SÍ → Dispararía por CAÍDA FUERTE")
    print(f"     Condición: cambio_1h ({cambio_1h:+.2f}%) < -1.3  ✅")
    print(f"     Condición: vol_ratio ({vol_ratio:.2f}x) >= 1.3    ✅")
elif dispara_por_subida:
    print(f"  🟢 SÍ → Dispararía por SUBIDA FUERTE")
    print(f"     Condición: cambio_1h ({cambio_1h:+.2f}%) > +1.3  ✅")
    print(f"     Condición: vol_ratio ({vol_ratio:.2f}x) >= 1.3    ✅")
elif dispara_por_vol:
    direccion = "SHORT" if cambio_1h < 0 else "LONG"
    print(f"  🔥 SÍ → Dispararía por VOLUMEN MASIVO → {direccion}")
    print(f"     Condición: vol_ratio ({vol_ratio:.2f}x) >= 3.0    ✅")
    print(f"     Condición: |cambio| ({abs(cambio_1h):.2f}%) >= 0.3  ✅")
else:
    print(f"  ❌ NO → El bot está correcto en no disparar")
    print(f"\n  Chequeos:")
    print(f"    Bloque 1 (caída):   -1.3% requiere {abs(-1.3 - cambio_1h):.2f}% más de caída"
          if cambio_1h > -1.3 else f"    Bloque 1 (caída):   ✅ cumple")
    print(f"    Bloque 2 (subida):  +1.3% requiere {abs(1.3 - cambio_1h):.2f}% más de subida"
          if cambio_1h < 1.3 else f"    Bloque 2 (subida):  ✅ cumple")
    print(f"    Bloque 3 (volumen): 3.0x requiere {3.0 - vol_ratio:.2f}x más de volumen"
          if vol_ratio < 3.0 else f"    Bloque 3 (volumen): ✅ cumple")

# Chequeo del volumen acumulado
print(f"\n{'='*70}")
print(f"📊 VOLUMEN")
print(f"{'='*70}")
print(f"  Promedio últimas 12 velas: {sum(vols_recientes)/len(vols_recientes):.4f}")
if vols_previos:
    print(f"  Promedio previas 12 velas:  {sum(vols_previos)/len(vols_previos):.4f}")
    print(f"  Ratio:                      {vol_ratio:.2f}x")

# Resumen del estado
print(f"\n{'='*70}")
print(f"🎯 RESUMEN")
print(f"{'='*70}")
print(f"  Cambio 1h:  {cambio_1h:+.2f}%")
print(f"  Vol ratio:  {vol_ratio:.2f}x")
print(f"  Estado BTC: {'EXPANDIENDO' if (dispara_por_caida or dispara_por_subida or dispara_por_vol) else 'NEUTRAL'}")
