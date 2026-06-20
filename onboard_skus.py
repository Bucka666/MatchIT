"""
onboard_skus.py — One-stop SKU onboarding for MatchIT
=====================================================
Run this ONE script after adding new key images to KeysDB.
It does everything in the correct order:

  1. Sync from KeysDB       (imports new images into DB)
  2. Re-embed missing       (generates CLIP embeddings for new images only)
  3. Clean images            (rembg background removal for DB display)
  4. Refresh cache           (reloads embeddings into memory)
  5. Profile new SKUs       (opens profile tool filtered to un-profiled SKUs)
  6. Import cross-refs       (reads Excel cross-ref file into sku_crossrefs.json)
  7. Cross-ref check        (shows SKUs still missing from Excel)
  8. RAS image check        (shows SKUs missing illustration/raster images)

Usage:
    python onboard_skus.py              Full onboarding (all steps)
    python onboard_skus.py --sync       Just sync + embed + cache
    python onboard_skus.py --profile    Just open profile tool for new SKUs
    python onboard_skus.py --status     Show what needs doing (no changes)
    python onboard_skus.py --ras        Just show missing RAS images
    python onboard_skus.py --rename OLD NEW   Rename a SKU everywhere

Requires: Flask app importable (run from project root)
"""

import os
import sys
import json
import sqlite3
import shutil
import time
from pathlib import Path
from datetime import datetime

# ─────────────────────────────────────────────
# Setup — import from the Flask app
# ─────────────────────────────────────────────

os.environ.setdefault("FLASK_APP", "app")

try:
    import app as matchit_app
    from app import (
        get_images_db_path, get_image_db_dir, init_db,
        normalize_uploaded_image, get_embedder, to_blob,
        load_embedding_cache, CFG, _iter_keysdb_images,
    )
    from vertical_loader import load_vertical, get_vertical
    import numpy as np
except ImportError as e:
    print(f"ERROR: Cannot import app module. Run from the project root directory.")
    print(f"  {e}")
    sys.exit(1)


# ─────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────

DB_PATH = get_images_db_path()
IMG_DIR = get_image_db_dir()
PROFILES_PATH = os.path.join(matchit_app.app.root_path, "sku_profiles.json")
CROSSREFS_PATH = os.path.join(matchit_app.app.root_path, "sku_crossrefs.json")

# Load vertical config
_VERT = load_vertical(str(CFG.get("vertical", "keys")), matchit_app.app.root_path)
RAS_ENABLED = bool(get_vertical().get("ras_images_enabled", False))

# RAS image directories (only used if vertical enables them)
RAS_DIRS = [
    r"C:\Users\c_a_b\OneDrive\Pictures\RASTER",
    os.path.join(matchit_app.app.root_path, "ras_images"),
]
RAS_EXTS = [".jpg", ".png", ".jpeg", ".webp"]


def load_json(path):
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ─────────────────────────────────────────────
# RAS image check helper
# ─────────────────────────────────────────────

def _sku_has_ras(sku: str) -> bool:
    """Check if a SKU has an _RAS image in any of the RAS directories."""
    for ras_dir in RAS_DIRS:
        if not os.path.isdir(ras_dir):
            continue
        for ext in RAS_EXTS:
            filename = f"{sku}_RAS{ext}"
            filepath = os.path.join(ras_dir, filename)
            if os.path.exists(filepath):
                return True
    return False


def _get_missing_ras(all_skus) -> list:
    """Return sorted list of SKUs that don't have an _RAS image."""
    missing = []
    for sku in all_skus:
        if not _sku_has_ras(sku):
            missing.append(sku)
    return sorted(missing)


# ─────────────────────────────────────────────
# Status check
# ─────────────────────────────────────────────

def get_status():
    """Return counts for each onboarding stage."""
    init_db()
    conn = sqlite3.connect(DB_PATH)

    total_images = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    total_skus = conn.execute("SELECT COUNT(DISTINCT sku) FROM images").fetchone()[0]
    missing_embed = conn.execute("SELECT COUNT(*) FROM images WHERE embedding IS NULL").fetchone()[0]

    # SKUs with/without FRONT images
    all_skus_rows = conn.execute(
        "SELECT DISTINCT sku FROM images WHERE sku IS NOT NULL AND sku != ''"
    ).fetchall()
    all_skus = {r[0] for r in all_skus_rows}

    front_skus_rows = conn.execute(
        "SELECT DISTINCT sku FROM images WHERE UPPER(original_filename) LIKE '%_FRONT%'"
    ).fetchall()
    front_skus = {r[0] for r in front_skus_rows}
    missing_front = all_skus - front_skus

    conn.close()

    profiles = load_json(PROFILES_PATH)
    profiled_skus = set(profiles.keys())
    unprofiled = all_skus - profiled_skus

    crossrefs = load_json(CROSSREFS_PATH)
    xref_skus = set(crossrefs.keys())
    missing_xrefs = all_skus - xref_skus

    # Clean images
    clean_dir = os.path.join(matchit_app.app.root_path, "clean_images")
    all_image_ids = set()
    conn2 = sqlite3.connect(DB_PATH)
    for row in conn2.execute("SELECT image_id FROM images").fetchall():
        all_image_ids.add(row[0])
    conn2.close()
    cleaned_count = 0
    if os.path.isdir(clean_dir):
        for f in os.listdir(clean_dir):
            if f.endswith(".jpg"):
                img_id = f[:-4]
                if img_id in all_image_ids:
                    cleaned_count += 1
    missing_clean = len(all_image_ids) - cleaned_count

    # RAS images (only if vertical enables them)
    missing_ras = _get_missing_ras(all_skus) if RAS_ENABLED else []

    return {
        "total_images": total_images,
        "total_skus": total_skus,
        "missing_embed": missing_embed,
        "missing_clean": missing_clean,
        "cleaned_count": cleaned_count,
        "missing_front": sorted(missing_front),
        "unprofiled": sorted(unprofiled),
        "missing_xrefs": sorted(missing_xrefs),
        "missing_ras": missing_ras,
        "all_skus": sorted(all_skus),
    }


def print_status():
    s = get_status()
    print(f"\n{'='*55}")
    print(f"  MatchIT Onboarding Status")
    print(f"{'='*55}\n")
    print(f"  Database images:     {s['total_images']}")
    print(f"  Unique SKUs:         {s['total_skus']}")
    print(f"  Missing embeddings:  {s['missing_embed']}", end="")
    print(f"  {'⚠ NEED RE-EMBED' if s['missing_embed'] > 0 else '✓'}")
    print(f"  Missing clean imgs:  {s['missing_clean']}", end="")
    print(f"  {'⚠ NEED CLEANING' if s['missing_clean'] > 0 else '✓'}")
    print(f"  Missing FRONT image: {len(s['missing_front'])}", end="")
    print(f"  {'⚠ ' + ', '.join(s['missing_front'][:5]) if s['missing_front'] else '✓'}")
    print(f"  Un-profiled SKUs:    {len(s['unprofiled'])}", end="")
    print(f"  {'⚠ NEED PROFILING' if s['unprofiled'] else '✓'}")
    print(f"  Missing cross-refs:  {len(s['missing_xrefs'])}", end="")
    print(f"  {'(optional)' if s['missing_xrefs'] else '✓'}")
    if RAS_ENABLED:
        print(f"  Missing RAS images:  {len(s['missing_ras'])}", end="")
        print(f"  {'⚠ NEED ILLUSTRATIONS' if s['missing_ras'] else '✓'}")

    if s['unprofiled']:
        print(f"\n  Un-profiled SKUs:")
        for sku in s['unprofiled']:
            print(f"    • {sku}")

    if s['missing_front']:
        print(f"\n  SKUs without FRONT image (won't show in profile tool):")
        for sku in s['missing_front']:
            print(f"    • {sku}")

    if RAS_ENABLED and s['missing_ras']:
        print(f"\n  SKUs without RAS illustration ({len(s['missing_ras'])}):")
        for sku in s['missing_ras']:
            print(f"    • {sku}")
        print(f"\n  RAS images should be named {{SKU}}_RAS.jpg and placed in:")
        for d in RAS_DIRS:
            exists = "✓" if os.path.isdir(d) else "✗ NOT FOUND"
            print(f"    {d}  [{exists}]")

    print()
    return s


# ─────────────────────────────────────────────
# Step 1: Sync from KeysDB
# ─────────────────────────────────────────────

def step_sync():
    print(f"\n── Step 1: Sync from KeysDB ──")
    keysdb_root = Path(CFG.get("keysdb_root", r"C:\Users\c_a_b\My Drive\KeysDB"))
    if not keysdb_root.exists():
        print(f"  ⚠ KeysDB not found: {keysdb_root}")
        return 0

    init_db()

    # Get existing
    existing = set()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT original_filename FROM images WHERE original_filename IS NOT NULL"
    ).fetchall()
    for (orig,) in rows:
        if orig:
            existing.add(str(orig).strip().lower())

    inserted = 0
    skipped = 0

    for sku, src_path, rel_id in _iter_keysdb_images(keysdb_root):
        if not rel_id:
            continue

        parent_dir = src_path.parent.name.upper()
        if parent_dir not in {"SKU_FRONT", "SKU_BACK", "SKU_SIDE_C", sku.upper()}:
            continue

        rel_key = rel_id.strip().lower()
        if rel_key in existing:
            skipped += 1
            continue

        try:
            import uuid
            image_id = str(uuid.uuid4())
            dst_path = os.path.join(IMG_DIR, f"{image_id}.jpg")
            shutil.copy2(str(src_path), dst_path)
            normalize_uploaded_image(dst_path)

            conn.execute("""
                INSERT OR REPLACE INTO images(image_id, sku, description, original_filename, path, added_at, embedding)
                VALUES(?,?,?,?,?,?,NULL)
            """, (image_id, sku, "", rel_id, dst_path, datetime.utcnow().isoformat()))

            existing.add(rel_key)
            inserted += 1
            print(f"  + {sku}: {rel_id}")
        except Exception as e:
            print(f"  ✗ {sku}: {e}")

    conn.commit()
    conn.close()

    print(f"  Imported: {inserted} new images ({skipped} already existed)")
    return inserted


# ─────────────────────────────────────────────
# Step 2: Re-embed missing
# ─────────────────────────────────────────────

def step_reembed():
    print(f"\n── Step 2: Re-embed missing ──")
    init_db()

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT image_id, path FROM images WHERE embedding IS NULL").fetchall()

    if not rows:
        print(f"  All images have embeddings ✓")
        conn.close()
        return 0

    print(f"  {len(rows)} images need embedding...")
    embedder = get_embedder()

    updated = 0
    for i, (image_id, p) in enumerate(rows, 1):
        if not p:
            continue
        file_path = p if os.path.isabs(p) else os.path.normpath(os.path.join(os.path.dirname(DB_PATH), p))
        if not os.path.exists(file_path):
            print(f"  ✗ Missing file: {file_path}")
            continue

        try:
            emb = embedder.embed_path(file_path, multi_crop=True, suppress_bg=True)
        except TypeError:
            emb = embedder.embed_path(file_path)

        if emb is None:
            continue

        emb = np.asarray(emb, dtype=np.float32).reshape(-1)
        conn.execute("UPDATE images SET embedding = ? WHERE image_id = ?", (to_blob(emb), str(image_id)))
        updated += 1

        if i % 10 == 0 or i == len(rows):
            print(f"  Embedded {i}/{len(rows)}...", flush=True)

    conn.commit()
    conn.close()
    print(f"  Done: {updated} embeddings generated")
    return updated


# ─────────────────────────────────────────────
# Step 3: Clean images (rembg)
# ─────────────────────────────────────────────

def step_clean():
    print(f"\n── Step 3: Clean images (rembg background removal) ──")

    try:
        from image_cleaner import clean_query_image, is_available
    except ImportError:
        print(f"  ⚠ image_cleaner.py not found — skipping")
        return 0

    if not is_available():
        print(f"  ⚠ rembg not installed — skipping (pip install rembg onnxruntime)")
        return 0

    clean_dir = os.path.join(matchit_app.app.root_path, "clean_images")
    os.makedirs(clean_dir, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT image_id, sku, path FROM images").fetchall()
    conn.close()

    cleaned = 0
    skipped = 0

    for i, row in enumerate(rows, 1):
        image_id = str(row["image_id"])
        out_path = os.path.join(clean_dir, f"{image_id}.jpg")

        # Skip if already cleaned
        if os.path.exists(out_path):
            skipped += 1
            continue

        src = row["path"]
        if not src or not os.path.exists(src):
            continue

        result = clean_query_image(src, output_path=out_path)
        if result != src:
            cleaned += 1
        
        if i % 20 == 0 or i == len(rows):
            print(f"  Cleaned {i}/{len(rows)}... ({cleaned} new, {skipped} existed)", flush=True)

    print(f"  Done: {cleaned} newly cleaned, {skipped} already existed")
    return cleaned


# ─────────────────────────────────────────────
# Step 4: Refresh cache
# ─────────────────────────────────────────────

def step_refresh_cache():
    print(f"\n── Step 4: Refresh cache ──")
    with matchit_app.app.app_context():
        load_embedding_cache(force=True)
    print(f"  Cache refreshed ✓")


# ─────────────────────────────────────────────
# Step 5: Profile new SKUs
# ─────────────────────────────────────────────

def step_profile():
    print(f"\n── Step 5: Profile un-profiled SKUs ──")
    s = get_status()
    if not s['unprofiled']:
        print(f"  All SKUs are profiled ✓")
        return

    print(f"  {len(s['unprofiled'])} SKUs need profiling:")
    for sku in s['unprofiled']:
        print(f"    • {sku}")

    print(f"\n  Opening profile tool...")
    print(f"  (Navigate to un-profiled SKUs using Skip →)")

    try:
        import tkinter as tk
        from profile_tool import ProfileTool
        root = tk.Tk()
        tool = ProfileTool(root)

        # Jump to first un-profiled SKU
        for i, (sku, _, _) in enumerate(tool.skus):
            if sku in s['unprofiled']:
                tool.idx = i
                tool._load_sku()
                break

        root.mainloop()
    except Exception as e:
        print(f"  Could not open profile tool: {e}")
        print(f"  Run manually: python profile_tool.py")


# ─────────────────────────────────────────────
# Step 6: Import cross-references from Excel
# ─────────────────────────────────────────────

XLSX_CANDIDATES = [
    "MatchIT Key Ref File.xlsx",
    "MatchIT Key Ref File..xlsx",
    "MatchIT_Key_Ref_File_.xlsx",
    "MatchIT_Key_Ref_File.xlsx",
]

def step_import_crossrefs():
    print(f"\n── Step 6: Import cross-references from Excel ──")

    # Find the Excel file
    xlsx_path = None
    project_root = matchit_app.app.root_path
    for name in XLSX_CANDIDATES:
        p = os.path.join(project_root, name)
        if os.path.exists(p):
            xlsx_path = p
            break

    if not xlsx_path:
        # Try any .xlsx in project root that looks like a key ref file
        for f in os.listdir(project_root):
            fl = f.lower()
            if fl.endswith(".xlsx") and ("key" in fl or "ref" in fl or "matchit" in fl):
                xlsx_path = os.path.join(project_root, f)
                break

    if not xlsx_path:
        print(f"  ⚠ No cross-ref Excel file found in {project_root}")
        print(f"    Expected: {XLSX_CANDIDATES[0]}")
        print(f"    Skipping — run 'python import_crossrefs.py' manually")
        return

    print(f"  Found: {os.path.basename(xlsx_path)}")

    try:
        from import_crossrefs import load_excel, build_crossrefs_for_row, get_db_skus
    except ImportError:
        print(f"  ⚠ import_crossrefs.py not found — skipping")
        print(f"    Run manually: python import_crossrefs.py")
        return

    try:
        headers, brand_headers, lookup, ws = load_excel(str(xlsx_path))
        db_skus = get_db_skus(DB_PATH)

        result = {}
        matched = 0
        unmatched_skus = []

        for sku in db_skus:
            row = lookup.get(sku.upper())
            if row:
                mfr = ws.cell(row, 1).value or ""
                refs = build_crossrefs_for_row(ws, row, brand_headers)
                result[sku] = {
                    "manufacturer": str(mfr).strip(),
                    "crossrefs": refs
                }
                matched += 1
            else:
                unmatched_skus.append(sku)

        # Also store remaining Excel rows for future SKUs
        future_count = 0
        for row in range(2, ws.max_row + 1):
            name_val = ws.cell(row, 2).value
            if not name_val:
                continue
            name_clean = str(name_val).strip()
            if name_clean.upper() in {"NULL", "NONE", "", "-", "N/A"}:
                continue
            if name_clean in result:
                continue
            mfr = ws.cell(row, 1).value or ""
            refs = build_crossrefs_for_row(ws, row, brand_headers)
            if refs:
                result[name_clean] = {
                    "manufacturer": str(mfr).strip(),
                    "crossrefs": refs
                }
                future_count += 1

        # Write JSON
        save_json(CROSSREFS_PATH, result)
        total_refs = sum(len(v["crossrefs"]) for v in result.values())
        print(f"  Matched {matched} DB SKUs, stored {future_count} future entries")
        print(f"  Total: {len(result)} entries, {total_refs} cross-references")

        if unmatched_skus:
            print(f"  ⚠ {len(unmatched_skus)} DB SKUs not found in Excel:")
            for sku in sorted(unmatched_skus):
                print(f"    • {sku}")
            print(f"    Add these to {os.path.basename(xlsx_path)} and re-run")

    except Exception as e:
        print(f"  ✗ Failed: {e}")
        print(f"    Run manually: python import_crossrefs.py")


# ─────────────────────────────────────────────
# Step 7: Cross-ref check (verify)
# ─────────────────────────────────────────────

def step_xref_check():
    print(f"\n── Step 7: Cross-reference verification ──")
    s = get_status()
    if not s['missing_xrefs']:
        print(f"  All SKUs have cross-references ✓")
        return

    print(f"  {len(s['missing_xrefs'])} SKUs still without cross-references:")
    for sku in s['missing_xrefs']:
        print(f"    • {sku}")
    print(f"\n  These SKUs are not in the Excel cross-ref file.")
    print(f"  Add them to your MatchIT_Key_Ref_File_.xlsx and re-run onboarding.")


# ─────────────────────────────────────────────
# Step 8: RAS image check
# ─────────────────────────────────────────────

def step_ras_check():
    print(f"\n── Step 8: RAS illustration image check ──")

    # Show which RAS directories exist
    for d in RAS_DIRS:
        exists = "✓ found" if os.path.isdir(d) else "✗ not found"
        print(f"  {d}  [{exists}]")

    s = get_status()
    missing = s['missing_ras']

    if not missing:
        print(f"\n  All {s['total_skus']} SKUs have RAS images ✓")
        return

    have_count = s['total_skus'] - len(missing)
    print(f"\n  {have_count}/{s['total_skus']} SKUs have RAS images")
    print(f"  {len(missing)} SKUs are MISSING RAS illustrations:\n")

    for sku in missing:
        print(f"    • {sku}    (need {sku}_RAS.jpg)")

    print(f"\n  To fix: create illustration images named {{SKU}}_RAS.jpg")
    print(f"  and place them in one of the directories above.")


# ─────────────────────────────────────────────
# Rename helper
# ─────────────────────────────────────────────

def rename_sku(old_name, new_name):
    """Rename a SKU across DB, profiles, and crossrefs."""
    old = old_name.strip()
    new = new_name.strip()
    print(f"\n  Renaming '{old}' → '{new}'...\n")

    init_db()
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM images WHERE sku = ?", (old,)).fetchone()[0]
    if count > 0:
        conn.execute("UPDATE images SET sku = ? WHERE sku = ?", (new, old))
        conn.commit()
        print(f"  images.db: {count} rows updated")
    else:
        print(f"  images.db: '{old}' not found")
    conn.close()

    profiles = load_json(PROFILES_PATH)
    if old in profiles:
        profiles[new] = profiles.pop(old)
        save_json(PROFILES_PATH, profiles)
        print(f"  sku_profiles.json: renamed")
    else:
        print(f"  sku_profiles.json: '{old}' not found")

    xrefs = load_json(CROSSREFS_PATH)
    if old in xrefs:
        xrefs[new] = xrefs.pop(old)
        save_json(CROSSREFS_PATH, xrefs)
        print(f"  sku_crossrefs.json: renamed")
    else:
        print(f"  sku_crossrefs.json: '{old}' not found")

    print(f"\n  Done. Restart app.py for changes to take effect.")


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    print(f"\n  MatchIT SKU Onboarding Tool")
    print(f"  {'─'*40}")

    if not args:
        # Full onboarding
        print_status()

        input("  Press Enter to start onboarding (or Ctrl+C to cancel)...")

        new = step_sync()
        if new > 0:
            step_reembed()
        else:
            # Still check for any missing embeddings
            conn = sqlite3.connect(DB_PATH)
            missing = conn.execute("SELECT COUNT(*) FROM images WHERE embedding IS NULL").fetchone()[0]
            conn.close()
            if missing > 0:
                step_reembed()

        step_clean()
        step_refresh_cache()
        step_profile()
        step_import_crossrefs()
        step_xref_check()
        if RAS_ENABLED:
            step_ras_check()

        print(f"\n{'='*55}")
        print(f"  Onboarding complete!")
        print(f"  Restart app.py and run your tests.")
        print(f"{'='*55}\n")

    elif args[0] == "--status":
        print_status()

    elif args[0] == "--sync":
        step_sync()
        step_reembed()
        step_clean()
        step_refresh_cache()
        print(f"\n  Done. Restart app.py for changes to take effect.")

    elif args[0] == "--profile":
        step_profile()

    elif args[0] == "--ras":
        if RAS_ENABLED:
            step_ras_check()
        else:
            print("  RAS images not enabled for this vertical.")

    elif args[0] == "--rename" and len(args) == 3:
        rename_sku(args[1], args[2])

    else:
        print(__doc__)


if __name__ == "__main__":
    main()