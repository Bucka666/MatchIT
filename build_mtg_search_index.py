"""
build_mtg_search_index.py — builds mtg_search_index.json for /api/card-search.

Self-contained: iterates CardsDB/mtg/*/profile.json directly (same pattern as
build_pokemon_search_index.py — does NOT depend on sku_game_map.json
freshness).

Unlike Pokémon/One Piece, MTG profile.json carries NO image URL field at all
(confirmed live, 2026-08-25 — no image_url_small, img_url, image_url, or
image key on any sampled MTG profile). img is R2-native from day one: joined
by SKU against images.db's image_id, null if no image_id exists. There is no
external URL to fall back to for this game.

MTG also needs the per-(set_id, name) dedup _build_set_card_list's MTG
branch already applies for the Browse Sets page (app.py) — multiple SKUs can
share a card name within one set (foil/frame/variant printings), and without
collapsing them a search for a common card name would return several
near-identical entries from the same set. Replicated here at build time
(group by (set_id, name.lower()), keep the lowest card_number) rather than
imported from app.py, matching every other builder's self-contained-script
convention.

Entry shape (one per MTG SKU, post-dedup):
    {
      "sku":       "mtg-10e-1",           # CardsDB folder name (lowest card_number survivor)
      "name":      "Ancestor's Chosen",   # profile.name
      "number":    "1",                   # profile.card_number, VERBATIM (unpadded)
      "set_id":    "10e",                 # profile.set_id
      "set_name":  "Tenth Edition",       # profile.set_name (falls back to set_id)
      "set_total": "383",                 # printed_total from set_metadata.json by set_id (or null)
      "img":       "https://images.grailsweep.com/<image_id>.jpg"  # or null if no image_id
    }

Output path: /modal_data/mtg_search_index.json on Modal, else local
mtg_search_index.json.

Callable as build_mtg_search_index() from the scheduler (set_scheduler.py),
or run directly as a script for a manual local rebuild against C:\\CardsDB.
"""
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed


def build_mtg_search_index(data_root="."):
    """Build the MTG search index. Returns a summary dict.

    data_root is the directory that contains CardsDB/ and receives the
    output file: "/modal_data" on Modal, "." for local runs.
    """
    mtg_dir = os.path.join(data_root, "CardsDB", "mtg")
    out_path = os.path.join(data_root, "mtg_search_index.json")
    if not os.path.isdir(mtg_dir):
        print(f"[MTG-SEARCH] mtg dir not found: {mtg_dir} — nothing built", flush=True)
        return {"total": 0, "skipped": 0, "out_path": out_path}

    set_totals = {}
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
        print(f"[MTG-SEARCH] loaded set totals for {len(set_totals)} sets", flush=True)
    except Exception as e:
        print(f"[MTG-SEARCH] WARN no set_metadata.json ({e}) — set_total will be null", flush=True)

    # sku -> image_id. No external field exists for MTG, so this is the ONLY
    # source of img -- a sku with no image_id gets img: null, full stop.
    sku_to_image_id = {}
    images_db_path = os.path.join(data_root, "MatchITv2_ProductMatch_Data", "cards", "images.db")
    try:
        with sqlite3.connect(images_db_path) as _conn:
            for _sku, _image_id in _conn.execute("SELECT sku, image_id FROM images WHERE sku LIKE 'mtg-%'"):
                sku_to_image_id[_sku] = _image_id
        print(f"[MTG-SEARCH] {len(sku_to_image_id)} sku->image_id rows from images.db", flush=True)
    except Exception as e:
        print(f"[MTG-SEARCH] WARN could not load images.db ({e}) — img will be null for every card: {e}", flush=True)

    def _extract_price(prof):
        # MTG cardmarket/tcgplayer are BOTH nested by variant (normal/foil),
        # unlike Pokémon/One Piece's flat cardmarket.avg_sell -- confirmed
        # live 2026-08-25 (mtg-10e-1 sample). Cardmarket (EUR) preferred,
        # same order every other builder uses.
        prices = prof.get("prices") or {}
        cm = prices.get("cardmarket") or {}
        for variant in cm.values():
            if isinstance(variant, dict) and variant.get("trend"):
                try:
                    return float(variant["trend"]), "EUR"
                except (TypeError, ValueError):
                    pass
        tcp = prices.get("tcgplayer") or {}
        for variant in tcp.values():
            if isinstance(variant, dict) and variant.get("market"):
                try:
                    return float(variant["market"]), "USD"
                except (TypeError, ValueError):
                    pass
        return None, None

    def _read_one(d):
        sku_dir = d.name
        if sku_dir.startswith("_"):
            return None
        profile_path = os.path.join(d.path, "profile.json")
        if not os.path.isfile(profile_path):
            return None
        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                p = json.load(f)
        except Exception as e:
            print(f"[MTG-SEARCH] WARN could not read {profile_path}: {e}", flush=True)
            return None

        if str(p.get("category") or "").upper().strip() != "MTG":
            return None

        name = str(p.get("name") or "").strip()
        number = str(p.get("card_number") or "").strip()
        set_id = str(p.get("set_id") or "").strip()
        set_name = str(p.get("set_name") or "").strip() or set_id

        if not name or not number or not set_id:
            return None

        price_val, price_currency = _extract_price(p)

        image_id = sku_to_image_id.get(sku_dir)
        img = "https://images.grailsweep.com/" + image_id + ".jpg" if image_id else None

        return {
            "sku":       sku_dir,
            "name":      name,
            "number":    number,
            "set_id":    set_id,
            "set_name":  set_name,
            "set_total": set_totals.get(set_id),
            "lang":      "en",
            "price":     price_val,
            "currency":  price_currency,
            "img":       img,
        }

    with os.scandir(mtg_dir) as _it:
        dirs = [d for d in _it if d.is_dir()]

    raw = []
    with ThreadPoolExecutor(max_workers=32) as executor:
        futures = {executor.submit(_read_one, d): d.name for d in dirs}
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            if result:
                raw.append(result)
            if i % 5000 == 0:
                print(f"[MTG-SEARCH] ...{i} processed so far", flush=True)

    # Per-(set_id, name) dedup -- same rule as _build_set_card_list's MTG
    # branch (app.py): keep the lowest card_number variant. Applied globally
    # here (batch context) rather than per-request (live-query context), but
    # it's the identical key and tie-break.
    seen = {}
    for entry in raw:
        key = (entry["set_id"], entry["name"].lower().strip())
        existing = seen.get(key)
        if existing is None:
            seen[key] = entry
            continue
        try:
            if int(entry["number"]) < int(existing["number"]):
                seen[key] = entry
        except (ValueError, TypeError):
            pass

    index = list(seen.values())
    scanned = len(dirs)
    skipped = scanned - len(index)

    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, out_path)

    print(f"[MTG-SEARCH] Built {len(index)} entries (raw={len(raw)} pre-dedup) "
          f"scanned={scanned} skipped={skipped} -> {out_path}", flush=True)

    return {"total": len(index), "raw_pre_dedup": len(raw), "skipped": skipped, "out_path": out_path}


# ── Modal wrapper ─────────────────────────────────────────────────────────────
# Lets `modal run build_mtg_search_index.py` populate the index on the LIVE
# volume without waiting for a full scheduler run. Mirrors
# build_pokemon_search_index.py / build_onepiece_search_index.py — each
# defines its own modal.App. The pure-Python build_mtg_search_index() above
# is what set_scheduler.py imports — this block only adds the remote
# entrypoint and is a no-op for local `python` runs.
import modal

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
            "*.txt", "*.md", "__pycache__", "*.pyc", ".git", ".venv", "*.log",
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

_modal_app = modal.App("matchit-mtg-search-index")


@_modal_app.function(image=_image, volumes={"/modal_data": _vol}, timeout=3600)
def rebuild_remote():
    import sys
    os.chdir("/app")
    sys.path.insert(0, "/app")
    _vol.reload()
    result = build_mtg_search_index(data_root="/modal_data")   # reads /modal_data/CardsDB, writes /modal_data/mtg_search_index.json
    _vol.commit()
    print(f"[MTG-SEARCH] volume committed: {result}", flush=True)
    return result


@_modal_app.local_entrypoint()
def main():
    print(rebuild_remote.remote())


if __name__ == "__main__":
    build_mtg_search_index()
