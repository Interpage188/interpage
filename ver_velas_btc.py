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

# Chequeo del filtro
print(f"\n🔍 ¿Cumple filtro del fix? (caída -2% + vol 1.3x)")
if cambio_1h < -2.0 and vol_ratio >= 1.3:
    print(f"  ✅ SÍ → Debería disparar SHORT")
else:
    print(f"  ❌ NO → El bot está correcto en no disparar")
    if cambio_1h >= -2.0:
        print(f"     (falta {abs(-2.0 - cambio_1h):.2f}% para llegar al -2%)")
    if vol_ratio < 1.3:
        print(f"     (falta {1.3 - vol_ratio:.2f}x de volumen)")

# Chequeo del volumen acumulado
print(f"\n📊 VOLUMEN:")
print(f"  Promedio últimas 12 velas: {sum(vols_recientes)/len(vols_recientes):.4f}")
if vols_previos:
    print(f"  Promedio previas 12 velas:  {sum(vols_previos)/len(vols_previos):.4f}")
