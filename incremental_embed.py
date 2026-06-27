"""
incremental_embed.py — Append new card embeddings without full matrix rebuild
=============================================================================
Loads the existing front/back matrix + info cache, finds any image_ids in
images.db that are not yet embedded, embeds only those, and appends them.

Usage (standalone):
    python incremental_embed.py

Usage (from scheduler / app code):
    from incremental_embed import run_incremental_embed
    result = run_incremental_embed()

Drop into C:\\MatchIT\\ alongside app.py.
"""

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Helpers — mirror app.py path logic
# ─────────────────────────────────────────────────────────────

def _get_data_dir() -> Path:
    """Mirror of app.py get_data_dir() — works on Windows + Modal."""
    localappdata = os.environ.get("LOCALAPPDATA", "/modal_data")
    return Path(localappdata) / "MatchITv2_ProductMatch_Data" / "cards"


def _get_npy_cache_dir() -> Path:
    return _get_data_dir() / "npy_cache"


def _get_images_db_path() -> Path:
    return _get_data_dir() / "images.db"


# ─────────────────────────────────────────────────────────────
# Load existing cache
# ─────────────────────────────────────────────────────────────

def _load_existing_cache() -> Tuple[
    Optional[np.ndarray],   # front_matrix
    Optional[np.ndarray],   # back_matrix
    List[tuple],            # front_info
    List[tuple],            # back_info
    Dict,                   # extra dicts from cache_data.json
]:
    cache_dir = _get_npy_cache_dir()
    front_path = cache_dir / "front_matrix.npy"
    back_path  = cache_dir / "back_matrix.npy"
    data_path  = cache_dir / "cache_data.json"

    front_matrix = None
    back_matrix  = None
    front_info   = []
    back_info    = []
    extra        = {}

    if front_path.exists():
        front_matrix = np.load(str(front_path))
        logger.info(f"[INCR] Loaded existing front_matrix: {front_matrix.shape}")

    if back_path.exists():
        back_matrix = np.load(str(back_path))
        logger.info(f"[INCR] Loaded existing back_matrix: {back_matrix.shape}")

    if data_path.exists():
        with open(data_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Reconstruct tuples from JSON arrays
        front_info = [tuple(row) for row in data.get("front_info", [])]
        back_info  = [tuple(row) for row in data.get("back_info",  [])]

        # Carry over auxiliary dicts
        for key in ("desc_by_id", "orig_by_id", "view_by_id", "path_by_id"):
            if key in data:
                extra[key] = data[key]

        logger.info(
            f"[INCR] Loaded cache_data.json: "
            f"{len(front_info)} front, {len(back_info)} back entries"
        )

    return front_matrix, back_matrix, front_info, back_info, extra


# ─────────────────────────────────────────────────────────────
# Find new images not yet embedded
# ─────────────────────────────────────────────────────────────

def _find_new_images(
    front_info: List[tuple],
    back_info:  List[tuple],
    category_filter: Optional[str] = None,
) -> Tuple[List[dict], List[dict]]:
    """
    Query images.db for rows whose image_id is not in the existing cache.
    Returns (new_front_rows, new_back_rows) as lists of dicts.
    Note: this DB has no view/category columns — all images are FRONT.
    """
    db_path = _get_images_db_path()
    if not db_path.exists():
        logger.error(f"[INCR] images.db not found at {db_path}")
        return [], []

    # Build set of already-embedded image_ids
    embedded_front = {row[0] for row in front_info}

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Filter by category via SKU prefix if requested
    query = """
        SELECT image_id, sku, path, original_filename, description
        FROM images
        WHERE path IS NOT NULL
    """
    params = []
    if category_filter:
        cat = category_filter.upper()
        if cat == "POKEMON":
            query += " AND (sku LIKE 'base%' OR sku LIKE 'sv%' OR sku LIKE 'swsh%' OR sku LIKE 'sm%' OR sku LIKE 'xy%' OR sku LIKE 'bw%' OR sku LIKE 'dp%' OR sku LIKE 'ex%' OR sku LIKE 'neo%' OR sku LIKE 'gym%' OR sku LIKE 'jpn-%')"
        elif cat == "MTG":
            query += " AND sku LIKE 'mtg-%'"
        elif cat == "YUGIOH":
            query += " AND sku LIKE 'ygo-%'"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    new_front = []
    for row in rows:
        image_id = row["image_id"]
        path     = row["path"]
        if not path or not os.path.exists(path):
            continue
        if image_id not in embedded_front:
            new_front.append({
                "image_id":          image_id,
                "sku":               row["sku"] or "",
                "path":              path,
                "original_filename": row["original_filename"] or "",
                "description":       row["description"] or "",
            })

    logger.info(
        f"[INCR] Found {len(new_front)} new FRONT images to embed "
        f"(category_filter={category_filter})"
    )
    return new_front, []  # no BACK images in this DB


# ─────────────────────────────────────────────────────────────
# Embed new images
# ─────────────────────────────────────────────────────────────

def _embed_new_images(
    new_rows: List[dict],
    emb,
    batch_size: int = 50,
) -> Tuple[np.ndarray, List[tuple], int]:
    """
    Embed a list of new image rows using the provided embedder.
    Returns (matrix, info_list, sqlite_written) where matrix is float32 (N, dim).
    Also writes each embedding to the images.db embedding column so SQLite
    stays in sync with the .npy cache.
    """
    from app import _embed_one_query
    from vertical_loader import get_vertical

    v = get_vertical()
    use_multi_crop  = v.get("multi_crop",   False)
    use_suppress_bg = v.get("suppress_bg",  False)

    vecs = []
    info = []
    failed = 0
    sqlite_written = 0

    db_path = _get_images_db_path()
    db_conn = sqlite3.connect(str(db_path)) if db_path.exists() else None

    total = len(new_rows)
    for i, row in enumerate(new_rows):
        if i % batch_size == 0:
            logger.info(f"[INCR] Embedding {i}/{total}...")

        try:
            vec = _embed_one_query(
                emb,
                row["path"],
                multi_crop=use_multi_crop,
                suppress_bg=use_suppress_bg,
                max_side=512,
            )
            # L2 normalise (mirror of app.py:1023)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            vec = vec.astype(np.float32)
            vecs.append(vec)
            info.append((
                row["image_id"],
                row["sku"],
                row["original_filename"],
                row["description"],
            ))

            # Persist embedding to SQLite so DB stays in sync with .npy cache
            if db_conn is not None:
                try:
                    db_conn.execute(
                        "UPDATE images SET embedding = ? WHERE image_id = ?",
                        (vec.tobytes(), row["image_id"]),
                    )
                    sqlite_written += 1
                    logger.info(f"[INCR] Persisted embedding to SQLite: {row['sku']}")
                except Exception as db_e:
                    logger.warning(f"[INCR] SQLite write failed for {row['sku']}: {db_e}")

        except Exception as e:
            logger.warning(f"[INCR] Failed to embed {row['path']}: {e}")
            failed += 1
            continue

    if db_conn is not None:
        db_conn.commit()
        db_conn.close()

    if not vecs:
        logger.warning("[INCR] No vectors produced")
        return np.empty((0,), dtype=np.float32), [], 0

    matrix = np.stack(vecs).astype(np.float32)
    logger.info(
        f"[INCR] Embedded {len(vecs)}/{total} images "
        f"({failed} failed) → shape {matrix.shape}"
    )
    logger.info(f"[INCR] SQLite embeddings written: {sqlite_written}")
    return matrix, info, sqlite_written


# ─────────────────────────────────────────────────────────────
# Save updated cache
# ─────────────────────────────────────────────────────────────

def _save_updated_cache(
    front_matrix: np.ndarray,
    back_matrix:  Optional[np.ndarray],
    front_info:   List[tuple],
    back_info:    List[tuple],
    extra:        Dict,
) -> None:
    cache_dir = _get_npy_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    front_path = cache_dir / "front_matrix.npy"
    back_path  = cache_dir / "back_matrix.npy"
    data_path  = cache_dir / "cache_data.json"
    meta_path  = cache_dir / "cache_meta.json"

    # Save matrices
    np.save(str(front_path), front_matrix)
    logger.info(f"[INCR] Saved front_matrix.npy: {front_matrix.shape}")

    if back_matrix is not None and back_matrix.shape[0] > 0:
        np.save(str(back_path), back_matrix)
        logger.info(f"[INCR] Saved back_matrix.npy: {back_matrix.shape}")

    # Save cache_data.json
    data = {
        "front_info": [list(row) for row in front_info],
        "back_info":  [list(row) for row in back_info],
    }
    data.update(extra)

    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    logger.info(f"[INCR] Saved cache_data.json")

    # Save cache_meta.json
    meta = {
        "loaded_at":   time.time(),
        "front_rows":  len(front_info),
        "back_rows":   len(back_info),
        "incremental": True,
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f)
    logger.info(f"[INCR] Saved cache_meta.json")


# ─────────────────────────────────────────────────────────────
# Reload in-memory matrices after update
# ─────────────────────────────────────────────────────────────

def _reload_app_matrices(
    front_matrix: np.ndarray,
    back_matrix:  Optional[np.ndarray],
    front_info:   List[tuple],
    back_info:    List[tuple],
) -> None:
    """
    Hot-reload the global matrices in app.py without restarting Flask.
    """
    try:
        import app as _app
        _app.FRONT_MATRIX = front_matrix
        _app.FRONT_INFO   = front_info
        if back_matrix is not None and back_matrix.shape[0] > 0:
            _app.BACK_MATRIX = back_matrix
            _app.BACK_INFO   = back_info
        logger.info(
            f"[INCR] Hot-reloaded matrices: "
            f"front={front_matrix.shape[0]} rows, "
            f"back={back_matrix.shape[0] if back_matrix is not None else 0} rows"
        )
    except Exception as e:
        logger.warning(
            f"[INCR] Could not hot-reload app matrices: {e} "
            f"— restart Flask to pick up new embeddings"
        )


# ─────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────

def run_incremental_embed(
    category_filter: Optional[str] = None,
    hot_reload:      bool = True,
) -> Dict:
    """
    Find and embed any new images not yet in the matrix cache.

    Args:
        category_filter: Optional TCG filter e.g. 'POKEMON', 'MTG', 'YUGIOH'
        hot_reload:      If True, immediately update the in-memory matrices

    Returns:
        Summary dict with counts and timing.
    """
    t_start = time.time()
    summary = {
        "new_front":   0,
        "new_back":    0,
        "failed":      0,
        "total_front": 0,
        "total_back":  0,
        "elapsed_s":   0,
        "status":      "ok",
    }

    logger.info("[INCR] Starting incremental embed run...")

    # 1. Load existing cache
    front_matrix, back_matrix, front_info, back_info, extra = _load_existing_cache()

    # GUARDRAIL: cold/missing matrix must HARD-STOP here, never fall through
    # to a full re-embed.
    if front_matrix is None:
        logger.error("[INCR] No existing front_matrix.npy found — run full embed first")
        summary["status"] = "error_no_existing_matrix"
        return summary

    # 2. Find new images
    new_front_rows, new_back_rows = _find_new_images(
        front_info, back_info, category_filter
    )

    if not new_front_rows and not new_back_rows:
        logger.info("[INCR] No new images to embed — cache is up to date")
        summary["status"]      = "up_to_date"
        summary["total_front"] = front_matrix.shape[0]
        summary["total_back"]  = back_matrix.shape[0] if back_matrix is not None else 0
        summary["elapsed_s"]   = round(time.time() - t_start, 1)
        return summary

    # 3. Get embedder
    try:
        from app import get_embedder
        emb = get_embedder()
    except Exception as e:
        logger.error(f"[INCR] Could not get embedder: {e}")
        summary["status"] = f"error_embedder: {e}"
        return summary

    sqlite_total = 0

    # 4. Embed new FRONT images
    if new_front_rows:
        new_front_matrix, new_front_info, n_written = _embed_new_images(new_front_rows, emb)
        sqlite_total += n_written
        if new_front_matrix.shape[0] > 0:
            front_matrix = np.concatenate(
                [front_matrix, new_front_matrix], axis=0
            )
            front_info.extend(new_front_info)
            summary["new_front"] = new_front_matrix.shape[0]

    # 5. Embed new BACK images
    if new_back_rows:
        new_back_matrix, new_back_info, n_written = _embed_new_images(new_back_rows, emb)
        sqlite_total += n_written
        if new_back_matrix.shape[0] > 0:
            if back_matrix is not None and back_matrix.shape[0] > 0:
                back_matrix = np.concatenate(
                    [back_matrix, new_back_matrix], axis=0
                )
            else:
                back_matrix = new_back_matrix
            back_info.extend(new_back_info)
            summary["new_back"] = new_back_matrix.shape[0]

    summary["sqlite_written"] = sqlite_total

    # 6. Save updated cache
    _save_updated_cache(
        front_matrix, back_matrix,
        front_info,   back_info,
        extra,
    )

    # 7. Hot-reload in-memory matrices
    if hot_reload:
        _reload_app_matrices(
            front_matrix, back_matrix,
            front_info,   back_info,
        )

    summary["total_front"] = front_matrix.shape[0]
    summary["total_back"]  = back_matrix.shape[0] if back_matrix is not None else 0
    summary["elapsed_s"]   = round(time.time() - t_start, 1)

    logger.info(
        f"[INCR] Done — added {summary['new_front']} front, "
        f"{summary['new_back']} back embeddings in {summary['elapsed_s']}s. "
        f"Total: {summary['total_front']} front, {summary['total_back']} back."
    )
    return summary


# ─────────────────────────────────────────────────────────────
# Standalone run
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    import sys
    category = sys.argv[1].upper() if len(sys.argv) > 1 else None
    result = run_incremental_embed(category_filter=category, hot_reload=False)
    print("\n=== Incremental Embed Summary ===")
    for k, v in result.items():
        print(f"  {k}: {v}")


# ─────────────────────────────────────────────────────────────
# Modal entry point — modal run incremental_embed.py
# ─────────────────────────────────────────────────────────────

import sys
sys.path.insert(0, "/app")
import modal
from modal_config import image, vol

embed_app = modal.App("grailsweep-incremental-embed")


@embed_app.function(
    image=image,
    gpu="T4",
    volumes={"/modal_data": vol},
    secrets=[
        modal.Secret.from_name("app-credentials"),
        modal.Secret.from_name("stripe-credentials"),
        modal.Secret.from_name("google-vision-credentials"),
        modal.Secret.from_name("vapid-credentials"),
        modal.Secret.from_name("external-api-credentials"),
        modal.Secret.from_name("cf-proxy-secret"),
    ],
    timeout=1800,
)
def run_embed():
    import os, sys
    os.chdir("/app")
    sys.path.insert(0, "/app")
    os.environ["LOCALAPPDATA"] = "/modal_data"
    from incremental_embed import run_incremental_embed as _run
    result = _run(hot_reload=False)
    print("\n=== Incremental Embed Summary ===", flush=True)
    for k, v in result.items():
        print(f"  {k}: {v}", flush=True)
    return result


@embed_app.local_entrypoint()
def main():
    run_embed.remote()