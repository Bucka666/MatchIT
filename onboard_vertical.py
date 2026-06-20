"""
onboard_vertical.py — Client onboarding tool for MatchIT API
=============================================================
Takes a folder of product images and creates a fully configured
vertical ready for API matching.

What it does:
    1. Scans a folder of product images (supports nested SKU folders or flat)
    2. Creates the vertical.json config
    3. Copies/organises images into the MatchIT DB structure
    4. Optionally imports profile data from a CSV
    5. Inserts images into SQLite
    6. Generates CLIP embeddings
    7. Outputs an API key for the client

Usage:
    # Basic — folder of images, one image per product
    python onboard_vertical.py --name "acme_hardware" --source "C:/client/photos"

    # Nested — SKU subfolders each containing front/back images
    python onboard_vertical.py --name "acme_hardware" --source "C:/client/photos" --nested

    # With profile CSV
    python onboard_vertical.py --name "acme_hardware" --source "C:/client/photos" --profiles "C:/client/products.csv"

    # Dry run — show what would happen without changing anything
    python onboard_vertical.py --name "acme_hardware" --source "C:/client/photos" --dry-run

Requires: Flask app importable (run from MatchIT root directory)
"""

import argparse
import csv
import json
import os
import secrets
import shutil
import sqlite3
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
MATCHIT_ROOT = os.path.dirname(os.path.abspath(__file__))


def is_image(p: Path) -> bool:
    return p.suffix.lower() in IMAGE_EXTS


# ─────────────────────────────────────────────
# Step 1: Scan source folder
# ─────────────────────────────────────────────

def scan_flat(source: Path) -> List[Tuple[str, Path]]:
    """
    Flat folder: each image IS a product.
    SKU = filename stem (e.g. "widget_A.jpg" → SKU "widget_A")
    """
    results = []
    for p in sorted(source.iterdir()):
        if p.is_file() and is_image(p):
            sku = p.stem.strip()
            results.append((sku, p))
    return results


def scan_nested(source: Path) -> List[Tuple[str, Path]]:
    """
    Nested folders: each subfolder is a SKU, images inside are views.
    SKU = folder name, picks first image as primary.
    """
    results = []
    for d in sorted(source.iterdir()):
        if not d.is_dir() or d.name.startswith(("_", ".")):
            continue
        sku = d.name.strip()
        images = sorted([p for p in d.iterdir() if p.is_file() and is_image(p)])
        if images:
            # Use all images, mark first as FRONT
            for img in images:
                results.append((sku, img))
    return results


# ─────────────────────────────────────────────
# Step 2: Create vertical config
# ─────────────────────────────────────────────

def create_vertical_config(
    vertical_name: str,
    display_name: str,
    db_root: str,
    profile_fields: Optional[List[dict]] = None,
    categories: Optional[dict] = None,
) -> dict:
    """Generate a vertical.json config."""
    config = {
        "id": vertical_name,
        "name": f"MatchIT - {display_name.upper()}",
        "page_title": f"MatchIT – {display_name.upper()}",
        "subtitle": f"AI Guided {display_name} Identification",
        "icon": "🔍",
        "query_title": f"Identify Your {display_name}",
        "image_labels": {"front": "Image 1", "back": "Image 2"},

        "ui_text": {
            "how_to_title": f"How to identify your {display_name.lower()}",
            "step1_text": f"Enter a <strong>product code</strong> or <strong>name</strong> if known",
            "step1_placeholder": "e.g. product code, name…",
            "step2_text": f"Or <strong>upload / take a photo</strong> of the {display_name.lower()}",
            "progress_subtitle": "Analysing against the database…",
            "guide_tips": [
                "Fill most of the frame with the <strong>product</strong>.",
                "Use good lighting — avoid heavy shadows or glare.",
                "Keep the item flat and straight.",
            ],
            "feedback_correct_prompt": "Was the correct product in these results?",
            "feedback_notfound_prompt": "Which product was it? (enter code or leave blank)",
            "feedback_sku_placeholder": "e.g. product code",
        },

        "require_two_images": False,
        "style_detection_enabled": False,
        "ras_images_enabled": False,
        "multi_crop": False,
        "suppress_bg": True,
        "db_root": db_root,

        "categories": categories or {
            "DEFAULT": {
                "label": "All Products",
                "show": [],
            }
        },

        "profile_fields": profile_fields or [],
        "category_families": {},
        "silhouette_map": {},

        "disclaimer": "Product images and brand names remain the property of their respective owners. Used for identification purposes only.",
        "guidance_text": "The top 5 closest matches are listed below.",
        "guidance_feedback": "Please use the feedback bar to tell us how accurate the results were.",
    }

    return config


# ─────────────────────────────────────────────
# Step 3: Import images into DB structure
# ─────────────────────────────────────────────

def import_images(
    items: List[Tuple[str, Path]],
    images_dir: str,
    db_path: str,
) -> Tuple[int, int]:
    """
    Copy images into MatchIT image store and insert into SQLite.
    Returns (inserted, skipped).
    """
    os.makedirs(images_dir, exist_ok=True)

    # Load existing to avoid duplicates
    existing = set()
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS images (
            image_id TEXT PRIMARY KEY,
            sku TEXT,
            description TEXT,
            original_filename TEXT,
            path TEXT,
            added_at TEXT,
            embedding BLOB
        )
    """)

    rows = conn.execute(
        "SELECT original_filename FROM images WHERE original_filename IS NOT NULL"
    ).fetchall()
    for (orig,) in rows:
        if orig:
            existing.add(orig.strip().lower())

    inserted = 0
    skipped = 0

    for sku, src_path in items:
        # Build a relative ID like "SKU/SKU_FRONT" or "SKU/filename"
        rel_id = f"{sku}/{src_path.stem}"
        if rel_id.strip().lower() in existing:
            skipped += 1
            continue

        image_id = str(uuid.uuid4())
        dst_path = os.path.join(images_dir, f"{image_id}.jpg")

        try:
            shutil.copy2(str(src_path), dst_path)

            conn.execute(
                """INSERT OR REPLACE INTO images
                   (image_id, sku, description, original_filename, path, added_at, embedding)
                   VALUES (?, ?, ?, ?, ?, ?, NULL)""",
                (
                    image_id,
                    sku,
                    "",
                    rel_id,
                    dst_path,
                    datetime.utcnow().isoformat(),
                ),
            )
            existing.add(rel_id.strip().lower())
            inserted += 1

        except Exception as e:
            print(f"  [ERROR] Failed to import {src_path}: {e}")
            skipped += 1

    conn.commit()
    conn.close()
    return inserted, skipped


# ─────────────────────────────────────────────
# Step 4: Import profile data from CSV
# ─────────────────────────────────────────────

def import_profiles_csv(csv_path: str, output_path: str) -> Tuple[int, List[str]]:
    """
    Import product profiles from a CSV file.
    First column must be 'sku'. Other columns become profile fields.

    CSV format:
        sku,manufacturer,material,size,color
        WIDGET_A,Acme,Steel,Large,Red
        WIDGET_B,Acme,Brass,Small,Blue

    Returns (count, field_names).
    """
    profiles = {}
    field_names = []

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        field_names = [fn for fn in (reader.fieldnames or []) if fn.lower() != "sku"]

        for row in reader:
            sku = row.get("sku", "").strip()
            if not sku:
                continue
            prof = {}
            for field in field_names:
                val = row.get(field, "").strip()
                if val:
                    prof[field] = val
            profiles[sku] = prof

    # Save as sku_profiles.json format
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2, ensure_ascii=False)

    return len(profiles), field_names


def generate_profile_fields_from_csv(field_names: List[str]) -> List[dict]:
    """Auto-generate vertical profile_fields from CSV column names."""
    fields = []
    for name in field_names:
        field = {
            "id": name.lower().replace(" ", "_"),
            "label": name.replace("_", " ").title(),
            "type": "select",
            "options": [["", "Unknown"]],
            "default": "",
            "penalty": 0.960,
            "match_rule": "exact",
        }
        fields.append(field)
    return fields


# ─────────────────────────────────────────────
# Step 5: Generate embeddings
# ─────────────────────────────────────────────

def generate_embeddings(db_path: str, multi_crop: bool = False, suppress_bg: bool = True):
    """Generate CLIP embeddings for all images without embeddings."""
    import numpy as np

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT image_id, path FROM images WHERE embedding IS NULL"
    ).fetchall()

    if not rows:
        print("  No images need embedding.")
        conn.close()
        return 0

    print(f"  Embedding {len(rows)} images...")

    from feature_extractor import ImageEmbedder
    embedder = ImageEmbedder()

    updated = 0
    for i, (image_id, path) in enumerate(rows, 1):
        if not path or not os.path.exists(path):
            continue

        try:
            emb = embedder.embed_path(path, multi_crop=multi_crop, suppress_bg=suppress_bg)
        except TypeError:
            emb = embedder.embed_path(path)

        if emb is None:
            continue

        emb = np.asarray(emb, dtype=np.float32).reshape(-1)
        conn.execute(
            "UPDATE images SET embedding = ? WHERE image_id = ?",
            (emb.tobytes(), image_id),
        )
        updated += 1

        if i % 25 == 0 or i == len(rows):
            conn.commit()
            elapsed_per = (time.time() - _embed_start) / i if i > 0 else 0
            remaining = elapsed_per * (len(rows) - i)
            print(f"    [{i}/{len(rows)}] ~{remaining:.0f}s remaining", flush=True)

    conn.commit()
    conn.close()
    return updated


# ─────────────────────────────────────────────
# Step 6: Generate API key
# ─────────────────────────────────────────────

def generate_api_key(vertical_name: str, config_path: str) -> str:
    """Generate and save an API key for the new client."""
    key = f"matchit-{vertical_name}-{secrets.token_hex(12)}"

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    api_keys = config.get("api_keys", {})
    api_keys[vertical_name] = key
    config["api_keys"] = api_keys

    if "vertical" not in config:
        config["vertical"] = "cards"

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    return key


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Onboard a new client vertical for MatchIT API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Simple flat folder of product images
  python onboard_vertical.py --name "salvage_yard" --display "Architectural Hardware" --source "C:/client/photos"

  # Nested SKU folders with profile CSV
  python onboard_vertical.py --name "parts_co" --display "Auto Parts" --source "C:/client/inventory" --nested --profiles "C:/client/parts.csv"

  # Dry run
  python onboard_vertical.py --name "test" --source "C:/photos" --dry-run
        """,
    )
    parser.add_argument("--name", required=True,
                        help="Vertical ID (lowercase, no spaces, e.g. 'salvage_yard')")
    parser.add_argument("--display", default="",
                        help="Display name (e.g. 'Architectural Hardware')")
    parser.add_argument("--source", required=True,
                        help="Path to folder of product images")
    parser.add_argument("--nested", action="store_true",
                        help="Source has SKU subfolders (not flat)")
    parser.add_argument("--profiles", default="",
                        help="CSV file with product profiles (first column must be 'sku')")
    parser.add_argument("--db-root", default="",
                        help="Database root path (default: auto-generated)")
    parser.add_argument("--embed", action="store_true",
                        help="Generate embeddings immediately (can be slow)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without making changes")
    args = parser.parse_args()

    display_name = args.display or args.name.replace("_", " ").title()
    source = Path(args.source)

    if not source.exists():
        sys.exit(f"Source folder not found: {source}")

    print(f"\n{'='*60}")
    print(f"  MatchIT Client Onboarding")
    print(f"  Vertical: {args.name} ({display_name})")
    print(f"  Source:   {source}")
    print(f"{'='*60}\n")

    # ── Scan images ──
    print("[1/6] Scanning source folder...")
    if args.nested:
        items = scan_nested(source)
    else:
        items = scan_flat(source)

    unique_skus = len(set(sku for sku, _ in items))
    print(f"  Found {len(items)} images across {unique_skus} SKUs")

    if not items:
        sys.exit("No images found. Check the source folder.")

    if args.dry_run:
        print(f"\n[DRY RUN] Would process {len(items)} images into vertical '{args.name}'")
        for sku, p in items[:10]:
            print(f"  {sku}: {p.name}")
        if len(items) > 10:
            print(f"  ... and {len(items) - 10} more")
        return

    # ── Set up paths ──
    vertical_dir = os.path.join(MATCHIT_ROOT, "verticals", args.name)
    db_root = args.db_root or os.path.join(MATCHIT_ROOT, "data", args.name)

    # Data dir (where images.db lives) — uses AppData pattern
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    data_dir = os.path.join(base, "MatchITv2_ProductMatch_Data", args.name)
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "images.db")
    images_dir = os.path.join(data_dir, "images")

    # ── Create vertical config ──
    print("\n[2/6] Creating vertical config...")
    os.makedirs(vertical_dir, exist_ok=True)

    profile_fields = []
    if args.profiles:
        print(f"  Importing profiles from: {args.profiles}")
        profiles_output = os.path.join(MATCHIT_ROOT, f"sku_profiles_{args.name}.json")
        count, field_names = import_profiles_csv(args.profiles, profiles_output)
        print(f"  Loaded {count} profiles with fields: {', '.join(field_names)}")
        profile_fields = generate_profile_fields_from_csv(field_names)

    config = create_vertical_config(
        vertical_name=args.name,
        display_name=display_name,
        db_root=db_root,
        profile_fields=profile_fields,
    )

    config_path = os.path.join(vertical_dir, "vertical.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    print(f"  Saved: {config_path}")

    # ── Import images ──
    print(f"\n[3/6] Importing {len(items)} images...")
    inserted, skipped = import_images(items, images_dir, db_path)
    print(f"  Inserted: {inserted}, Skipped: {skipped}")

    # ── Generate embeddings ──
    if args.embed:
        print(f"\n[4/6] Generating CLIP embeddings...")
        global _embed_start
        _embed_start = time.time()
        updated = generate_embeddings(db_path, multi_crop=False, suppress_bg=True)
        elapsed = time.time() - _embed_start
        print(f"  Embedded {updated} images in {elapsed:.1f}s")
    else:
        print(f"\n[4/6] Skipping embeddings (use --embed flag, or run re-embed from admin)")

    # ── Generate API key ──
    print(f"\n[5/6] Generating API key...")
    main_config = os.path.join(MATCHIT_ROOT, "config.json")
    if os.path.exists(main_config):
        key = generate_api_key(args.name, main_config)
        print(f"  API key: {key}")
    else:
        print(f"  [WARN] config.json not found — add API key manually")
        key = f"matchit-{args.name}-{secrets.token_hex(12)}"
        print(f"  Generated key: {key}")

    # ── Summary ──
    print(f"\n{'='*60}")
    print(f"  ONBOARDING COMPLETE")
    print(f"{'='*60}")
    print(f"  Vertical:     {args.name}")
    print(f"  Config:       {config_path}")
    print(f"  Database:     {db_path}")
    print(f"  Images:       {inserted} imported")
    print(f"  API key:      {key}")
    print(f"\n  Next steps:")
    print(f"  1. Set \"vertical\": \"{args.name}\" in config.json")
    print(f"  2. Restart Flask")
    if not args.embed:
        print(f"  3. Run re-embed from admin panel (or rerun with --embed)")
    print(f"  4. Test: http://localhost:5000/api/v1/health")
    print(f"  5. Demo: http://localhost:5000/static/demo_widget.html")
    print(f"{'='*60}\n")


_embed_start = time.time()  # module-level for timing

if __name__ == "__main__":
    main()