"""
image_cleaner.py — Background removal for MatchIT query images
==============================================================
Uses rembg to remove background, composites onto white, tight-crops.
Falls back gracefully if rembg is unavailable or fails.

Usage (as module):
    from image_cleaner import clean_query_image
    cleaned_path = clean_query_image("/path/to/query.jpg")
    # Returns cleaned path, or original path on failure

Usage (CLI — batch clean DB images, same as your existing script):
    python image_cleaner.py --db images.db --imgdir image_db/
    python image_cleaner.py --db images.db --imgdir image_db/ --sku GC --force
"""

import os
import logging
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Lazy-load rembg (heavy import, optional dep)
# ─────────────────────────────────────────────
_rembg_session = None
_rembg_remove = None
_rembg_checked = False


def _ensure_rembg() -> bool:
    """Try to import rembg once. Returns True if available."""
    global _rembg_remove, _rembg_session, _rembg_checked
    if _rembg_checked:
        return _rembg_remove is not None
    _rembg_checked = True
    try:
        from rembg import remove, new_session
        _rembg_remove = remove
        # Pre-load the model (u2net is fastest & good enough for keys on white-ish bg)
        _rembg_session = new_session("u2net")
        logger.info("[CLEAN] rembg loaded OK (u2net session)")
        return True
    except ImportError:
        logger.warning("[CLEAN] rembg not installed — query cleaning disabled")
        return False
    except Exception as e:
        logger.warning(f"[CLEAN] rembg init failed: {e} — query cleaning disabled")
        return False


# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
PADDING = 20          # px around subject after crop
JPEG_QUALITY = 95


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

def clean_query_image(input_path: str, output_path: str = None) -> str:
    """
    Remove background from a query image using rembg.

    Args:
        input_path:  Path to the normalised query image (JPEG).
        output_path: Where to save cleaned result. If None, saves alongside
                     input with '_clean' suffix (e.g. abc.jpg → abc_clean.jpg).

    Returns:
        Path to the cleaned image on success.
        Original input_path if cleaning is unavailable or fails.
    """
    if output_path is None:
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}_clean{ext}"

    # Return cached result if it already exists
    if os.path.exists(output_path):
        return output_path

    if not _ensure_rembg():
        return input_path  # rembg not installed — use original

    # --- Open ---
    try:
        img = Image.open(input_path).convert("RGB")
    except Exception as e:
        logger.warning(f"[CLEAN] Could not open {input_path}: {e}")
        return input_path

    # --- Remove background ---
    try:
        result = _rembg_remove(img, session=_rembg_session)
    except Exception as e:
        logger.warning(f"[CLEAN] rembg failed on {input_path}: {e}")
        return input_path

    # --- Composite onto white + tight crop ---
    try:
        if result.mode != "RGBA":
            logger.warning("[CLEAN] rembg returned non-RGBA — using original")
            return input_path

        # White composite
        white = Image.new("RGB", result.size, (255, 255, 255))
        white.paste(result, mask=result.split()[3])

        # Alpha-based tight crop
        arr = np.array(result)
        alpha = arr[:, :, 3]

        if not (alpha > 10).any():
            logger.warning("[CLEAN] No subject detected in alpha — using original")
            return input_path

        rows = np.any(alpha > 10, axis=1)
        cols = np.any(alpha > 10, axis=0)
        r0, r1 = int(np.where(rows)[0][0]), int(np.where(rows)[0][-1])
        c0, c1 = int(np.where(cols)[0][0]), int(np.where(cols)[0][-1])

        h, w = result.size[1], result.size[0]
        r0 = max(0, r0 - PADDING)
        r1 = min(h - 1, r1 + PADDING)
        c0 = max(0, c0 - PADDING)
        c1 = min(w - 1, c1 + PADDING)

        cropped = white.crop((c0, r0, c1 + 1, r1 + 1))

        # Sanity: don't save tiny or degenerate crops
        cw, ch = cropped.size
        if cw < 50 or ch < 50:
            logger.warning(f"[CLEAN] Crop too small ({cw}x{ch}) — using original")
            return input_path

        cropped.save(output_path, "JPEG", quality=JPEG_QUALITY, optimize=True)
        logger.info(f"[CLEAN] Cleaned: {os.path.basename(input_path)} → {os.path.basename(output_path)}")
        return output_path

    except Exception as e:
        logger.warning(f"[CLEAN] Post-processing failed: {e}")
        return input_path


def is_available() -> bool:
    """Check whether rembg is importable without triggering full model load."""
    try:
        import rembg  # noqa: F401
        return True
    except ImportError:
        return False


# ─────────────────────────────────────────────
# CLI (batch clean DB images — backward compat)
# ─────────────────────────────────────────────

def _cli_main():
    import argparse
    import sqlite3
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Batch-clean DB images with rembg")
    parser.add_argument("--sku", help="Clean only this SKU")
    parser.add_argument("--force", action="store_true", help="Re-clean even if output exists")
    parser.add_argument("--imgdir", required=True, help="Path to image_db directory")
    parser.add_argument("--db", required=True, help="Path to images.db")
    parser.add_argument("--outdir", default="clean_images", help="Output directory (default: clean_images)")
    args = parser.parse_args()

    print(f"Database : {args.db}")
    print(f"Image dir: {args.imgdir}")

    out_dir = Path(args.outdir)
    out_dir.mkdir(exist_ok=True)
    img_dir = Path(args.imgdir)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    if args.sku:
        rows = conn.execute(
            "SELECT image_id, sku, path FROM images WHERE LOWER(sku) = LOWER(?)",
            (args.sku,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT image_id, sku, path FROM images").fetchall()
    conn.close()

    if not rows:
        conn2 = sqlite3.connect(args.db)
        sample = [r[0] for r in conn2.execute("SELECT DISTINCT sku FROM images LIMIT 10").fetchall()]
        conn2.close()
        print(f"No images found. Sample SKUs: {sample}")
        return

    def resolve(raw):
        p = Path(raw)
        if p.exists():
            return p
        c = img_dir / p.name
        if c.exists():
            return c
        return p

    total = len(rows)
    done = skipped = errors = 0

    for i, row in enumerate(rows, 1):
        image_id = str(row["image_id"])
        sku = row["sku"]
        src = resolve(row["path"])
        out_path = str(out_dir / f"{image_id}.jpg")

        if os.path.exists(out_path) and not args.force:
            skipped += 1
            continue

        print(f"[{i}/{total}] {sku} - {src.name} ...", end=" ", flush=True)

        if not src.exists():
            print(f"MISSING ({src})")
            errors += 1
            continue

        result_path = clean_query_image(str(src), output_path=out_path)
        if result_path == str(src):
            print("FAILED")
            errors += 1
        else:
            print("OK")
            done += 1

    print(f"\nDone: {done} cleaned, {skipped} skipped, {errors} errors")
    print(f"Output: {out_dir.resolve()}")


if __name__ == "__main__":
    _cli_main()