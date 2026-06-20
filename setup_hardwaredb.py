"""
setup_hardwaredb.py — Creates the HardwareDB folder structure
=============================================================
Run this on your machine (Windows):
    python setup_hardwaredb.py

Creates:
    C:\\Users\\c_a_b\\My Drive\\HardwareDB\\
        ├── _README.txt
        ├── _TEMPLATE_SKU/
        │     ├── SKU_FRONT/
        │     └── SKU_BACK/
        └── <sample SKU folders for each category>
"""

import os
from pathlib import Path


# ─── Config ───
HARDWAREDB_ROOT = Path(r"C:\Users\c_a_b\My Drive\HardwareDB")

# Sample SKUs for each hardware category
# Format: (folder_name, category_description)
SAMPLE_SKUS = {
    # ── Screws ──
    "Screws": [
        ("WOOD-CSK-POZI-M4x40-BZP",        "Wood screw, countersunk, pozi, M4 x 40mm, bright zinc plated"),
        ("WOOD-CSK-POZI-M5x50-BZP",        "Wood screw, countersunk, pozi, M5 x 50mm, bright zinc plated"),
        ("MACHINE-PAN-POZI-M4x20-BZP",     "Machine screw, pan head, pozi, M4 x 20mm, BZP"),
        ("MACHINE-CSK-POZI-M5x30-A2",      "Machine screw, countersunk, pozi, M5 x 30mm, stainless A2"),
        ("SELFTAP-PAN-POZI-G8x0.75-BZP",   "Self-tapping, pan head, pozi, No.8 x 3/4in, BZP"),
        ("SELFTAP-CSK-POZI-G10x1.5-BZP",   "Self-tapping, countersunk, pozi, No.10 x 1.5in, BZP"),
        ("COACH-HEX-M10x100-BZP",          "Coach screw, hex head, M10 x 100mm, BZP"),
    ],
    # ── Bolts ──
    "Bolts": [
        ("HEXBOLT-M8x40-BZP",              "Hex bolt, M8 x 40mm, BZP"),
        ("HEXBOLT-M10x60-A2",              "Hex bolt, M10 x 60mm, stainless A2"),
        ("HEXBOLT-M12x80-88",              "Hex bolt, M12 x 80mm, Grade 8.8"),
        ("CARRIAGE-M8x50-BZP",             "Carriage bolt, M8 x 50mm, BZP"),
        ("STUDDING-M10x1000-BZP",          "Threaded rod, M10 x 1000mm, BZP"),
    ],
    # ── Nuts ──
    "Nuts": [
        ("HEXNUT-M6-BZP",                  "Hex nut, M6, BZP"),
        ("HEXNUT-M8-A2",                   "Hex nut, M8, stainless A2"),
        ("NYLOC-M8-BZP",                   "Nyloc lock nut, M8, BZP"),
        ("NYLOC-M10-A2",                   "Nyloc lock nut, M10, stainless A2"),
        ("WINGNUT-M6-BZP",                 "Wing nut, M6, BZP"),
        ("HEXNUT-FLANGED-M8-BZP",          "Flanged hex nut, M8, BZP"),
        ("DOMENUT-M8-A2",                  "Dome / acorn nut, M8, stainless A2"),
    ],
    # ── Nails ──
    "Nails": [
        ("ROUND-WIRE-3.35x65-BW",          "Round wire nail, 3.35 x 65mm, bright"),
        ("ROUND-WIRE-4.5x100-GALV",        "Round wire nail, 4.5 x 100mm, galvanised"),
        ("OVAL-WIRE-2.65x40-BW",           "Oval wire nail, 2.65 x 40mm, bright"),
        ("PANEL-PIN-1.6x30-BW",            "Panel pin, 1.6 x 30mm, bright"),
        ("MASONRY-3.5x50-BZP",             "Masonry nail, 3.5 x 50mm, BZP"),
    ],
    # ── Washers ──
    "Washers": [
        ("FLAT-FORMA-M6-BZP",              "Flat washer Form A, M6, BZP"),
        ("FLAT-FORMA-M8-A2",               "Flat washer Form A, M8, stainless A2"),
        ("FLAT-FORMC-M8-BZP",              "Flat washer Form C (mudguard), M8, BZP"),
        ("SPRING-M8-BZP",                  "Spring washer, M8, BZP"),
        ("PENNY-M8-BZP",                   "Penny / repair washer, M8, BZP"),
    ],
    # ── Rivets ──
    "Rivets": [
        ("BLIND-DOME-3.2x8-ALU-STL",       "Blind rivet, dome, 3.2 x 8mm, aluminium body / steel mandrel"),
        ("BLIND-DOME-4.0x10-ALU-STL",      "Blind rivet, dome, 4.0 x 10mm, aluminium body / steel mandrel"),
        ("BLIND-DOME-4.8x12-ALU-STL",      "Blind rivet, dome, 4.8 x 12mm, aluminium body / steel mandrel"),
        ("BLIND-CSK-4.0x10-ALU-STL",       "Blind rivet, countersunk, 4.0 x 10mm, aluminium / steel"),
        ("BLIND-LFLANGE-4.8x14-ALU-STL",   "Blind rivet, large flange, 4.8 x 14mm, aluminium / steel"),
        ("BLIND-DOME-4.8x12-A2-A2",        "Blind rivet, dome, 4.8 x 12mm, stainless / stainless"),
        ("STRUCTURAL-6.4x16-STL-STL",      "Structural rivet, 6.4 x 16mm, steel / steel"),
        ("RIVETNUT-CSK-M5-BZP",            "Rivet nut, countersunk, M5, BZP"),
        ("RIVETNUT-DOME-M6-A2",            "Rivet nut, dome, M6, stainless A2"),
    ],
    # ── Wall Plugs & Anchors ──
    "Wall_Plugs_Anchors": [
        ("WALLPLUG-RED-6mm",                "Standard wall plug, red, 6mm"),
        ("WALLPLUG-BROWN-7mm",              "Standard wall plug, brown, 7mm"),
        ("WALLPLUG-YELLOW-5mm",             "Standard wall plug, yellow, 5mm"),
        ("PLASTERBOARD-METAL-TOGGLE",       "Plasterboard metal toggle anchor"),
        ("PLASTERBOARD-NYLON-TWIST",        "Plasterboard nylon twist anchor"),
        ("FRAME-FIXING-8x100",              "Frame fixing, 8 x 100mm"),
        ("HEAVY-DUTY-ANCHOR-M10",           "Heavy duty through-bolt anchor, M10"),
    ],
    # ── Plumbing Fittings ──
    "Plumbing_Fittings": [
        ("COMP-STRAIGHT-15mm",              "Compression straight coupler, 15mm"),
        ("COMP-ELBOW-15mm",                "Compression elbow 90°, 15mm"),
        ("COMP-TEE-15mm",                  "Compression tee, 15mm"),
        ("COMP-STRAIGHT-22mm",              "Compression straight coupler, 22mm"),
        ("COMP-REDUCER-22x15mm",            "Compression reducer, 22 x 15mm"),
        ("PUSHFIT-STRAIGHT-15mm",           "Push-fit straight coupler, 15mm"),
        ("PUSHFIT-ELBOW-15mm",             "Push-fit elbow 90°, 15mm"),
        ("PUSHFIT-TEE-22mm",               "Push-fit tee, 22mm"),
        ("ENDFEED-STRAIGHT-15mm",           "End feed / solder straight coupler, 15mm"),
        ("ENDFEED-ELBOW-15mm",             "End feed / solder elbow 90°, 15mm"),
        ("THREADED-ELBOW-0.5BSP-BZP",      "Threaded elbow, ½″ BSP, BZP"),
    ],
}


def create_structure():
    """Create the full HardwareDB folder structure."""
    print(f"\n{'='*60}")
    print(f"  HardwareDB Setup Script")
    print(f"{'='*60}\n")
    print(f"  Target: {HARDWAREDB_ROOT}\n")

    if HARDWAREDB_ROOT.exists():
        print(f"  ⚠  Folder already exists — will add missing folders only.\n")
    else:
        print(f"  Creating root folder...\n")

    # Create root
    HARDWAREDB_ROOT.mkdir(parents=True, exist_ok=True)

    # ── README ──
    readme_path = HARDWAREDB_ROOT / "_README.txt"
    if not readme_path.exists():
        readme_path.write_text(
            "HardwareDB — Image database for MatchIT Hardware vertical\n"
            "==========================================================\n\n"
            "FOLDER STRUCTURE:\n"
            "  Each SKU gets its own folder directly under HardwareDB/.\n"
            "  The folder name IS the SKU name.\n\n"
            "IMAGE NAMING (pick one layout):\n\n"
            "  Layout A — Flat (simpler):\n"
            "    HardwareDB/MY-SKU-NAME/\n"
            "      MY-SKU-NAME_FRONT.jpg     ← required (main product photo)\n"
            "      MY-SKU-NAME_BACK.jpg      ← optional (reverse / alternate angle)\n"
            "      MY-SKU-NAME_SIDE_C.jpg    ← optional (third view)\n\n"
            "  Layout B — Sub-directories:\n"
            "    HardwareDB/MY-SKU-NAME/\n"
            "      SKU_FRONT/\n"
            "        photo.jpg               ← any filename, takes first image found\n"
            "      SKU_BACK/\n"
            "        photo.jpg\n"
            "      SKU_SIDE_C/\n"
            "        photo.jpg\n\n"
            "TIPS:\n"
            "  - Use .jpg or .jpeg (preferred) or .png\n"
            "  - Minimum 1 image per SKU (the FRONT)\n"
            "  - Keep backgrounds clean and plain if possible\n"
            "  - Fill the frame with the product\n"
            "  - Good lighting, no heavy shadows\n"
            "  - Use the Admin > Sync button in MatchIT to import\n\n"
            "SKU NAMING CONVENTION (suggested):\n"
            "  TYPE-DETAIL-SIZE-MATERIAL-FINISH\n"
            "  Examples:\n"
            "    HEXBOLT-M8x40-BZP\n"
            "    BLIND-DOME-4.8x12-ALU-STL\n"
            "    COMP-ELBOW-15mm\n"
            "    NYLOC-M10-A2\n",
            encoding="utf-8"
        )
        print("  ✅ Created _README.txt")

    # ── Template SKU folder ──
    template_dir = HARDWAREDB_ROOT / "_TEMPLATE_SKU"
    if not template_dir.exists():
        template_dir.mkdir(exist_ok=True)
        (template_dir / "SKU_FRONT").mkdir(exist_ok=True)
        (template_dir / "SKU_BACK").mkdir(exist_ok=True)
        print("  ✅ Created _TEMPLATE_SKU/ (with SKU_FRONT/ and SKU_BACK/)")

    # ── Sample SKU folders ──
    total_created = 0
    total_skipped = 0

    for group_name, skus in SAMPLE_SKUS.items():
        print(f"\n  ── {group_name} ──")
        for sku_name, description in skus:
            sku_dir = HARDWAREDB_ROOT / sku_name

            if sku_dir.exists():
                total_skipped += 1
                continue

            sku_dir.mkdir(exist_ok=True)

            # Create placeholder files showing where to put images
            front_placeholder = sku_dir / f"{sku_name}_FRONT.txt"
            front_placeholder.write_text(
                f"PLACEHOLDER — Replace this file with {sku_name}_FRONT.jpg\n\n"
                f"Product: {description}\n"
                f"SKU: {sku_name}\n\n"
                f"Take a clear photo of the product front/top and save it as:\n"
                f"  {sku_name}_FRONT.jpg\n\n"
                f"Then delete this .txt file.\n",
                encoding="utf-8"
            )

            print(f"    📁 {sku_name:42s}  ({description})")
            total_created += 1

    # ── Summary ──
    print(f"\n{'='*60}")
    print(f"  DONE!")
    print(f"  Created: {total_created} SKU folders")
    if total_skipped:
        print(f"  Skipped: {total_skipped} (already existed)")
    print(f"  Location: {HARDWAREDB_ROOT}")
    print(f"{'='*60}")
    print()
    print("  NEXT STEPS:")
    print("  1. Add product photos to each SKU folder")
    print("     - Name them: SKUNAME_FRONT.jpg (required)")
    print("     -            SKUNAME_BACK.jpg  (optional)")
    print("  2. Delete the .txt placeholder files")
    print("  3. In MatchIT (hardware vertical), go to Admin > Sync")
    print("  4. Images will be imported and embeddings generated")
    print()


if __name__ == "__main__":
    create_structure()