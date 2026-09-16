#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# BACKFILL SUPERTREND
# Añade el campo "super_trend" a los niveles activos en
# pending_levels.json que no lo tengan (publicados antes del
# fix de display).
#
# Ejecución manual única. No dejar en cron.
# ============================================================

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

GH_REPO = "Interpage188/interpage"
PENDING_PATH = "data/pending_levels.json"
ST_PATH = "data/supertrend_state.json"

PENDING_LOCAL = Path(PENDING_PATH)
ST_LOCAL = Path(ST_PATH)


def gh_get(path, pat):
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{path}"
    headers = {
        "Authorization": f"token {pat}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "backfill-supertrend",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
    content_b64 = data.get("content", "")
    sha = data.get("sha")
    if content_b64:
        raw = base64.b64decode(content_b64).decode("utf-8")
        return json.loads(raw), sha
    return None, sha


def gh_put(path, content, sha, message, pat):
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{path}"
    headers = {
        "Authorization": f"token {pat}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "backfill-supertrend",
        "Content-Type": "application/json",
    }
    body = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "sha": sha,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    pat = os.environ.get("GH_PUBLIC_PAT")
    if not pat:
        print("❌ Falta GH_PUBLIC_PAT", flush=True)
        return

    print("=" * 70, flush=True)
    print("🔧 BACKFILL SUPERTREND en pending_levels.json", flush=True)
    print("=" * 70, flush=True)

    # Leer ambos archivos
    try:
        pending, pending_sha = gh_get(PENDING_PATH, pat)
        st_data, _ = gh_get(ST_PATH, pat)
    except urllib.error.HTTPError as e:
        print(f"❌ Error leyendo: {e.code} {e.reason}", flush=True)
        return

    if not pending:
        print("⚠️ pending_levels.json vacío o no existe. Nada que hacer.", flush=True)
        return
    if not st_data:
        print("⚠️ supertrend_state.json vacío o no existe. Nada que hacer.", flush=True)
        return

    st_symbols = st_data.get("symbols", {})
    print(f"📋 pending_levels.json: {len(pending)} items", flush=True)
    print(f"🔮 supertrend_state.json: {len(st_symbols)} símbolos", flush=True)

    # Aplicar backfill a items activos sin super_trend
    modificados = 0
    for item in pending:
        if item.get("estado") != "esperando_toque":
            continue
        if "super_trend" in item:
            continue
        symbol = item.get("symbol")
        st_sym = st_symbols.get(symbol, {}).get("trend", "N/A")
        st_str = st_sym.upper() if st_sym not in ("N/A", None) else "N/A"
        item["super_trend"] = st_str
        modificados += 1
        print(f"   ✅ {symbol} {item.get('direction')} ${item.get('level')} → ST: {st_str}", flush=True)

    if modificados == 0:
        print("ℹ️ Nada que hacer. Todos los activos ya tienen super_trend.", flush=True)
        return

    # Guardar
    nuevo_content = json.dumps(pending, indent=2, ensure_ascii=False)
    try:
        gh_put(
            PENDING_PATH,
            nuevo_content,
            pending_sha,
            f"backfill: +super_trend en {modificados} niveles [skip ci]",
            pat,
        )
        print(f"\n✅ {modificados} items actualizados en {PENDING_PATH}", flush=True)
    except urllib.error.HTTPError as e:
        print(f"❌ Error escribiendo: {e.code} {e.reason}", flush=True)
        return

    print("🏁 TERMINADO", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
        raise
