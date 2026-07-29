"""
build_pokemon_search_index.py — builds pokemon_search_index.json for /api/pokemon-search.

Self-contained: iterates CardsDB/pokemon/*/profile.json directly (same pattern as
rebuild_identifier_lookup in app.py — does NOT depend on sku_game_map.json freshness,
so it is order-independent within the scheduler chain).

JP cards with images in images.db are included; all others are skipped.

Entry shape (one per English Pokémon SKU):
    {
      "sku":       "base1-4",    # CardsDB folder name
      "name":      "Charizard",  # profile.name
      "number":    "4",          # profile.card_number, VERBATIM (unpadded)
      "set_id":    "base1",      # profile.set_id
      "set_name":  "Base",       # profile.set_name (falls back to set_id)
      "set_total": "102"         # printed_total from set_metadata.json by set_id (or null)
    }

Output path: /modal_data/pokemon_search_index.json on Modal, else local
pokemon_search_index.json (mirrors IDENTIFIER_LOOKUP_PATH convention in app.py).

Callable as build_pokemon_search_index() from the scheduler (set_scheduler.py),
or run directly as a script for a manual local rebuild against C:\\CardsDB.
"""
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed


def build_pokemon_search_index(data_root="."):
    """Build the English-Pokémon search index. Returns a summary dict.

    data_root is the directory that contains CardsDB/ and receives the output
    file: "/modal_data" on Modal (rebuild_remote passes it explicitly), "." for
    local runs.
    """
    pokemon_dir = os.path.join(data_root, "CardsDB", "pokemon")
    out_path = os.path.join(data_root, "pokemon_search_index.json")
    if not os.path.isdir(pokemon_dir):
        print(f"[PKM-SEARCH] pokemon dir not found: {pokemon_dir} — nothing built", flush=True)
        return {"total": 0, "skipped": 0, "out_path": out_path}

    # set_total is NOT in per-card profiles — it lives in set_metadata.json keyed
    # by set_id. Use printed_total (the on-card denominator, e.g. "/102"), falling
    # back to total. Map is read-only after build, safe to share across threads.
    set_totals = {}
    _meta = {}
    set_meta_path = os.path.join(data_root, "set_metadata.json")
    try:
        with open(set_meta_path, "r", encoding="utf-8") as f:
            _meta = json.load(f)
        for _sid, _entry in _meta.items():
            if isinstance(_entry, dict):
                _pt = _entry.get("printed_total")
                if _pt is None:
                    _pt = _entry.get("total")
                if _pt is not None:
                    set_totals[_sid] = str(_pt)
        print(f"[PKM-SEARCH] loaded set totals for {len(set_totals)} sets", flush=True)
    except Exception as e:
        print(f"[PKM-SEARCH] WARN no set_metadata.json ({e}) — set_total will be null", flush=True)

    # JP set-id normalisation: profile stores "SV4a", metadata key is "jpn-sv4a"
    jp_setid_map = {}   # suffix.lower() → full jpn-... key
    jp_setnames  = {}   # jpn-... key   → English name from set_metadata if present
    for _sid, _entry in _meta.items():
        if _sid.startswith("jpn-"):
            _suffix = _sid[4:].lower()
            jp_setid_map[_suffix] = _sid
            if isinstance(_entry, dict):
                _n = _entry.get("name", "")
                if _n and _n != _sid:
                    jp_setnames[_sid] = _n
    print(f"[PKM-SEARCH] JP set-id map: {len(jp_setid_map)} entries", flush=True)

    # JP image filter: only include JP cards present in images.db (~4,294 with R2 thumbnails)
    imaged_jp_skus = set()
    images_db_path = os.path.join(data_root, "MatchITv2_ProductMatch_Data", "cards", "images.db")
    try:
        with sqlite3.connect(images_db_path) as _conn:
            _rows = _conn.execute("SELECT sku FROM images WHERE sku LIKE 'jpn-%'").fetchall()
            imaged_jp_skus = {r[0] for r in _rows}
        print(f"[PKM-SEARCH] {len(imaged_jp_skus)} imaged JP SKUs from images.db", flush=True)
    except Exception as e:
        print(f"[PKM-SEARCH] WARN could not load images.db ({e}) — JP cards will be excluded", flush=True)

    def _read_one(d):
        sku_dir = d.name
        if sku_dir.startswith("_"):
            return None
        is_jp = sku_dir.startswith("jpn-")
        if is_jp and sku_dir not in imaged_jp_skus:
            return None   # JP card with no R2 image — skip
        profile_path = os.path.join(d.path, "profile.json")
        if not os.path.isfile(profile_path):
            return None
        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                p = json.load(f)
        except Exception as e:
            print(f"[PKM-SEARCH] WARN could not read {profile_path}: {e}", flush=True)
            return None

        if str(p.get("category") or "").upper().strip() != "POKEMON":
            return None

        name = str(p.get("name") or "").strip()
        number = str(p.get("card_number") or "").strip()   # verbatim, unpadded
        # Strip leading zeros for consistency with EN (so "001" → "1", matching normalizeQuery)
        if number.isdigit():
            number = str(int(number))
        set_id = str(p.get("set_id") or "").strip()
        lang = str(p.get("lang") or "en").lower()

        # JP profiles store "SV4a"; normalise to the "jpn-sv4a" key used everywhere else
        meta_set_id = jp_setid_map.get(set_id.lower(), "jpn-" + set_id.lower()) if is_jp else set_id
        set_name_raw = str(p.get("set_name") or "").strip()
        # Prefer English name from set_metadata, fall back to profile value, then set code
        set_name = (jp_setnames.get(meta_set_id) if is_jp else None) or set_name_raw or meta_set_id

        if not name or not number:
            return None

        prices = p.get("prices") or {}
        price_val = None
        price_currency = None

        # Try cardmarket avg_sell (EUR) first
        cm = prices.get("cardmarket") or {}
        if cm.get("avg_sell"):
            try:
                price_val = float(cm["avg_sell"])
                price_currency = "EUR"
            except (TypeError, ValueError):
                pass

        # Fallback: tcgplayer holofoil market (USD)
        if price_val is None:
            tcp = prices.get("tcgplayer") or {}
            for variant in tcp.values():
                if isinstance(variant, dict) and variant.get("market"):
                    try:
                        price_val = float(variant["market"])
                        price_currency = "USD"
                    except (TypeError, ValueError):
                        pass
                    break

        return {
            "sku":       sku_dir,
            "name":      name,
            "number":    number,
            "set_id":    meta_set_id,
            "set_name":  set_name,
            "set_total": set_totals.get(meta_set_id),
            "lang":      lang,
            "price":     price_val,
            "currency":  price_currency,
            "img":       str(p.get("image_url_small") or "").strip() or None,
        }

    with os.scandir(pokemon_dir) as _it:
        dirs = [d for d in _it if d.is_dir()]

    index = []
    with ThreadPoolExecutor(max_workers=32) as executor:
        futures = {executor.submit(_read_one, d): d.name for d in dirs}
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            if result:
                index.append(result)
            if i % 1000 == 0:
                print(f"[PKM-SEARCH] ...{i} processed so far", flush=True)

    seen = len(dirs)
    skipped = seen - len(index)

    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, out_path)

    en_count = sum(1 for e in index if e.get("lang", "en") == "en")
    jp_count  = sum(1 for e in index if e.get("lang") == "ja")
    print(f"[PKM-SEARCH] Built {len(index)} entries (EN={en_count} JP={jp_count}) "
          f"scanned={seen} skipped={skipped} -> {out_path}", flush=True)

    return {"total": len(index), "skipped": skipped, "out_path": out_path}


# ── Modal wrapper ─────────────────────────────────────────────────────────────
# Lets `modal run build_pokemon_search_index.py` populate the index on the LIVE
# volume without waiting for a full scheduler run. Mirrors upload_to_modal.py and
# sync_profiles.py (each defines its own modal.App). The pure-Python
# build_pokemon_search_index() above is what set_scheduler.py imports — this block
# only adds the remote entrypoint and is a no-op for local `python` runs.
import modal

# vol + image inlined here (no shared-config import dependency) — same pattern as
# upload_to_modal.py defining vol inline. Kept byte-identical to the shared Modal
# config so Modal reuses the same cached image and the same live volume.
_VOLUME_NAME = "matchit-data-v2"
_vol = modal.Volume.from_name(_VOLUME_NAME, version=2)
_image = (
    modal.Image.debian_slim(python_version="3.11")
    .run_commands(
        "apt-get update && apt-get install -y libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*"
    )
    .pip_install(
        "flask==3.0.0",
        "flask-cors==4.0.0",
        "Pillow>=10.0.0",
        "numpy>=1.24.0",
        "requests>=2.31.0",
        "open_clip_torch>=2.24.0",
        "torch>=2.1.0",
        "torchvision>=0.16.0",
        "transformers>=4.36.0",
        "timm>=0.9.12",
        "huggingface_hub>=0.20.0",
        "stripe>=7.0.0",
        "anthropic>=0.25.0",
        "pywebpush>=2.3.0",
        "boto3",
        "tcgdex-sdk==2.3.0",
    )
    .run_commands(
        "python -c \"import open_clip; open_clip.create_model_and_transforms('ViT-L-14', pretrained='laion2b_s32b_b82k')\"",
        "python -c \"from transformers import AutoModel, AutoImageProcessor; AutoModel.from_pretrained('facebook/dinov2-large'); AutoImageProcessor.from_pretrained('facebook/dinov2-large')\"",
    )
    .env({
        "HF_HUB_OFFLINE": "1",
        "PYTHONWARNINGS": "ignore::UserWarning",
    })
    .add_local_dir(
        "C:/MatchIT",
        remote_path="/app",
        ignore=[
            # existing
            "*.txt", "*.md", "__pycache__", "*.pyc", ".git", ".venv", "*.log",
            # P-3: scratch/dev/test dirs — recon-confirmed unreferenced at runtime
            ".cache_mobileclip_test",
            ".wrangler",
            ".claude",
            "fp16_drift_run",
            "ondevice_index_v1_test",
            "regression_queries",
            "test_queries",
            "_snapshots",
            "ondevice_index_v1",
            "web_spike",
            "*_pre_*",
            "*.bak*",
        ],
    )
)

_modal_app = modal.App("matchit-pokemon-search-index")


@_modal_app.function(image=_image, volumes={"/modal_data": _vol}, timeout=1800)
def rebuild_remote():
    import sys
    os.chdir("/app")
    sys.path.insert(0, "/app")
    _vol.reload()
    result = build_pokemon_search_index(data_root="/modal_data")   # reads /modal_data/CardsDB, writes /modal_data/pokemon_search_index.json
    _vol.commit()
    print(f"[PKM-SEARCH] volume committed: {result}", flush=True)
    return result


@_modal_app.local_entrypoint()
def main():
    print(rebuild_remote.remote())


if __name__ == "__main__":
    build_pokemon_search_index()
