"""
build_onepiece_search_index.py — builds onepiece_search_index.json for
/api/card-search?game=onepiece.

Self-contained: iterates CardsDB/onepiece/*/profile.json directly (same
pattern as build_pokemon_search_index.py — does NOT depend on
sku_game_map.json freshness).

All One Piece cards are EN-only (no JP split, unlike Pokemon) so there is
no images.db imaged-JP filter here — every profile with a name and card
number is included, mirroring how build_pokemon_search_index.py includes
every EN Pokémon card unconditionally.

Entry shape (one per One Piece SKU):
    {
      "sku":       "op-op07-091",  # CardsDB folder name
      "name":      "Monkey.D.Luffy",
      "number":    "091",          # profile.card_number, VERBATIM (unpadded)
      "set_id":    "op07",         # profile.set_id
      "set_name":  "500 Years In The Future",  # profile.set_name (falls back to set_id)
      "set_total": "121"           # printed_total from set_metadata.json by set_id (or null)
    }

Two shape differences from the Pokemon build script, both confirmed live
against actual CardsDB/onepiece profiles:
  1. Image field is profile["img_url"], not profile["image_url_small"].
  2. Price is a FLAT dict written by backfill_onepiece_prices.py /
     refresh_onepiece_prices.py: profile["prices"]["tcgplayer"] = {"market": ...}.
     Pokemon's price reader iterates tcp.values() expecting nested
     {variant: {"market": ...}} dicts — reusing that verbatim here would
     silently always find no price, since a bare float has no .get().

Output path: /modal_data/onepiece_search_index.json on Modal, else local
onepiece_search_index.json (mirrors POKEMON_SEARCH_INDEX_PATH convention
in app.py).

Callable as build_onepiece_search_index() for a manual local rebuild
against C:\\CardsDB, or `modal run build_onepiece_search_index.py` for a
one-off remote rebuild against the live volume. Not yet wired into
set_scheduler.py's weekly chain — that's a separate decision.
"""
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed


def build_onepiece_search_index(data_root="."):
    """Build the One Piece search index. Returns a summary dict.

    data_root is the directory that contains CardsDB/ and receives the
    output file: "/modal_data" on Modal, "." for local runs.
    """
    onepiece_dir = os.path.join(data_root, "CardsDB", "onepiece")
    out_path = os.path.join(data_root, "onepiece_search_index.json")
    if not os.path.isdir(onepiece_dir):
        print(f"[OP-SEARCH] onepiece dir not found: {onepiece_dir} — nothing built", flush=True)
        return {"total": 0, "skipped": 0, "out_path": out_path}

    # sku -> image_id, for the R2 image-field fix below. img_url previously
    # pointed at Bandai's own CDN (en.onepiece-cardgame.com) verbatim for
    # every card, even the ones we already had a live R2 object for.
    sku_to_image_id = {}
    images_db_path = os.path.join(data_root, "MatchITv2_ProductMatch_Data", "cards", "images.db")
    try:
        with sqlite3.connect(images_db_path) as _conn:
            for _sku, _image_id in _conn.execute("SELECT sku, image_id FROM images WHERE sku LIKE 'op-%'"):
                sku_to_image_id[_sku] = _image_id
        print(f"[OP-SEARCH] {len(sku_to_image_id)} sku->image_id rows from images.db", flush=True)
    except Exception as e:
        print(f"[OP-SEARCH] WARN could not load images.db ({e}) — img falls back to Bandai CDN: {e}", flush=True)

    # set_total is NOT in per-card profiles — it lives in set_metadata.json
    # keyed by set_id (populated for ONEPIECE sets via CardsDB re-scan, see
    # build_set_metadata.py's build_onepiece_metadata()).
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
        print(f"[OP-SEARCH] loaded set totals for {len(set_totals)} sets", flush=True)
    except Exception as e:
        print(f"[OP-SEARCH] WARN no set_metadata.json ({e}) — set_total will be null", flush=True)

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
            print(f"[OP-SEARCH] WARN could not read {profile_path}: {e}", flush=True)
            return None

        if str(p.get("category") or "").upper().strip() != "ONEPIECE":
            return None

        name = str(p.get("name") or "").strip()
        number = str(p.get("card_number") or "").strip()   # verbatim, e.g. "001"
        set_id = str(p.get("set_id") or "").strip()
        set_name = str(p.get("set_name") or "").strip() or set_id

        if not name or not number or not set_id:
            return None

        # Flat shape: profile["prices"]["cardmarket"] = {"avg_sell": price} and/or
        # profile["prices"]["tcgplayer"] = {"market": price}. Cardmarket (EUR)
        # preferred for UK/EU pricing, same as every other game in this app —
        # only became available for One Piece once the dotgg.gg price source
        # was added (JustTCG never provided Cardmarket data at all).
        price_val = None
        price_currency = None
        prices = p.get("prices") or {}
        cm = prices.get("cardmarket") or {}
        if cm.get("avg_sell"):
            try:
                price_val = float(cm["avg_sell"])
                price_currency = "EUR"
            except (TypeError, ValueError):
                pass
        if price_val is None:
            tcp = prices.get("tcgplayer") or {}
            if tcp.get("market"):
                try:
                    price_val = float(tcp["market"])
                    price_currency = "USD"
                except (TypeError, ValueError):
                    pass

        # R2 from image_id, preferred -- non-regressive: fall back to Bandai's
        # CDN only if this sku has no image_id, else null.
        _image_id = sku_to_image_id.get(sku_dir)
        if _image_id:
            img = "https://images.grailsweep.com/" + _image_id + ".jpg"
        else:
            img = str(p.get("img_url") or "").strip() or None

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

    with os.scandir(onepiece_dir) as _it:
        dirs = [d for d in _it if d.is_dir()]

    index = []
    with ThreadPoolExecutor(max_workers=32) as executor:
        futures = {executor.submit(_read_one, d): d.name for d in dirs}
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            if result:
                index.append(result)
            if i % 1000 == 0:
                print(f"[OP-SEARCH] ...{i} processed so far", flush=True)

    seen = len(dirs)
    skipped = seen - len(index)

    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, out_path)

    print(f"[OP-SEARCH] Built {len(index)} entries scanned={seen} skipped={skipped} -> {out_path}", flush=True)

    return {"total": len(index), "skipped": skipped, "out_path": out_path}


# ── Modal wrapper ─────────────────────────────────────────────────────────────
# Lets `modal run build_onepiece_search_index.py` populate the index on the LIVE
# volume without waiting for a scheduler run. Image/volume block kept
# byte-identical to build_pokemon_search_index.py's so Modal reuses the same
# cached image and the same live volume, rather than building a fresh one.
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

_modal_app = modal.App("matchit-onepiece-search-index")


@_modal_app.function(image=_image, volumes={"/modal_data": _vol}, timeout=1800)
def rebuild_remote():
    import sys
    os.chdir("/app")
    sys.path.insert(0, "/app")
    _vol.reload()
    result = build_onepiece_search_index(data_root="/modal_data")   # reads /modal_data/CardsDB, writes /modal_data/onepiece_search_index.json
    _vol.commit()
    print(f"[OP-SEARCH] volume committed: {result}", flush=True)
    return result


@_modal_app.local_entrypoint()
def main():
    print(rebuild_remote.remote())


if __name__ == "__main__":
    build_onepiece_search_index()
