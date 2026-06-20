"""
setup_remotesdb.py — Creates the RemotesDB folder structure
=============================================================
Run on your machine:
    python setup_remotesdb.py

Creates:
    C:\\Users\\c_a_b\\My Drive\\RemotesDB\\
        ├── _README.txt
        └── <SKU folders for major remote brands>
"""

from pathlib import Path

REMOTESDB_ROOT = Path(r"C:\Users\c_a_b\My Drive\RemotesDB")

# Sample SKUs grouped by brand
# Format: (folder_name, description)
SAMPLE_SKUS = {
    "Came": [
        ("CAME-TOP-432NA",          "Came TOP-432NA, 2-button, 433.92MHz, fixed code"),
        ("CAME-TOP-434NA",          "Came TOP-434NA, 4-button, 433.92MHz, fixed code"),
        ("CAME-TOP-432EE",          "Came TOP-432EE, 2-button, 433.92MHz, fixed code"),
        ("CAME-TOP-862EV",          "Came TOP-862EV, 2-button, 868.35MHz, rolling code"),
        ("CAME-TOP-864EV",          "Came TOP-864EV, 4-button, 868.35MHz, rolling code"),
        ("CAME-TAM-432SA",          "Came TAM-432SA, 2-button, 433.92MHz, slim"),
        ("CAME-TWIN-2",             "Came TWIN 2, 2-button, 433.92MHz, rolling code"),
        ("CAME-TWIN-4",             "Came TWIN 4, 4-button, 433.92MHz, rolling code"),
        ("CAME-VER-V13",            "Came VER V13, 1-button, 433.92MHz, garage remote"),
    ],
    "BFT": [
        ("BFT-MITTO-B-RCB02",      "BFT Mitto B RCB02, 2-button, 433.92MHz, rolling code"),
        ("BFT-MITTO-B-RCB04",      "BFT Mitto B RCB04, 4-button, 433.92MHz, rolling code"),
        ("BFT-MITTO-2",            "BFT Mitto 2, 2-button, 433.92MHz, older model"),
        ("BFT-MITTO-4",            "BFT Mitto 4, 4-button, 433.92MHz, older model"),
        ("BFT-KLEIO-B-RCA02",      "BFT Kleio B RCA02, 2-button, 433.92MHz"),
        ("BFT-MITTO-2M",           "BFT Mitto 2M, 2-button, 433.92MHz, D111750"),
    ],
    "Nice": [
        ("NICE-FLO1",              "Nice FLO1, 1-button, 433.92MHz, fixed code"),
        ("NICE-FLO2",              "Nice FLO2, 2-button, 433.92MHz, fixed code"),
        ("NICE-FLO4",              "Nice FLO4, 4-button, 433.92MHz, fixed code"),
        ("NICE-FLO2R",             "Nice FLO2R, 2-button, 433.92MHz, rolling code"),
        ("NICE-FLO4R",             "Nice FLO4R, 4-button, 433.92MHz, rolling code"),
        ("NICE-ERA-INTI2",         "Nice Era INTI2, 2-button, 433.92MHz, rolling code"),
        ("NICE-ERA-ONE2",          "Nice Era ONE2, 2-button, 868.46MHz, rolling code"),
        ("NICE-SMILO-SM2",         "Nice SMILO SM2, 2-button, 433.92MHz"),
    ],
    "Faac": [
        ("FAAC-XT2-433-RC",        "Faac XT2 433 RC, 2-button, 433.92MHz, rolling code"),
        ("FAAC-XT4-433-RC",        "Faac XT4 433 RC, 4-button, 433.92MHz, rolling code"),
        ("FAAC-XT2-868-SLH",       "Faac XT2 868 SLH, 2-button, 868MHz, rolling code"),
        ("FAAC-DL2-868-SLH",       "Faac DL2 868 SLH, 2-button, 868MHz"),
        ("FAAC-TML2-433-SLR",      "Faac TML2-433-SLR, 2-button, 433.92MHz"),
    ],
    "Hormann": [
        ("HORMANN-HSE2-868-BS",     "Hörmann HSE2 868-BS, 2-button, 868.3MHz, rolling code"),
        ("HORMANN-HSE4-868-BS",     "Hörmann HSE4 868-BS, 4-button, 868.3MHz, rolling code"),
        ("HORMANN-HSM4-868",        "Hörmann HSM4 868, 4-button, 868.3MHz"),
        ("HORMANN-HS4-868-BS",      "Hörmann HS4 868-BS, 4-button, 868.3MHz, blue buttons"),
        ("HORMANN-HS5-868-BS",      "Hörmann HS5 868-BS, 5-button, 868.3MHz"),
        ("HORMANN-HSD2-868",        "Hörmann HSD2 868, 2-button, 868.3MHz, BiSecur"),
        ("HORMANN-FIT2-868-BS",     "Hörmann FIT 2 868 BS, 2-button, wall mount"),
    ],
    "Marantec": [
        ("MARANTEC-D382-868",       "Marantec Digital 382, 868.3MHz, 2-button"),
        ("MARANTEC-D384-868",       "Marantec Digital 384, 868.3MHz, 4-button"),
        ("MARANTEC-D302-433",       "Marantec Digital 302, 433.92MHz, 2-button"),
        ("MARANTEC-D304-433",       "Marantec Digital 304, 433.92MHz, 4-button"),
        ("MARANTEC-D313-868",       "Marantec Digital 313, 868.3MHz, micro"),
    ],
    "Chamberlain_LiftMaster": [
        ("LIFTMASTER-94335E",       "LiftMaster 94335E, 3-button, 433.92MHz"),
        ("LIFTMASTER-4335E",        "LiftMaster 4335E, 3-button, 433.92MHz"),
        ("CHAMBERLAIN-84335EML",    "Chamberlain 84335EML, 3-button, 433.92MHz"),
        ("LIFTMASTER-TX4UNIS",      "LiftMaster TX4UNIS universal, 4-button"),
    ],
    "Beninca": [
        ("BENINCA-TO-GO-2WV",       "Beninca TO.GO 2WV, 2-button, 433.92MHz, rolling"),
        ("BENINCA-TO-GO-4WV",       "Beninca TO.GO 4WV, 4-button, 433.92MHz, rolling"),
        ("BENINCA-IO-2WV",          "Beninca IO 2WV, 2-button, 433.92MHz"),
    ],
    "DEA": [
        ("DEA-GT2",                 "DEA GT2, 2-button, 433.92MHz, rolling code"),
        ("DEA-GT4",                 "DEA GT4, 4-button, 433.92MHz, rolling code"),
        ("DEA-MIO-TR2",             "DEA MIO TR2, 2-button, 433.92MHz"),
    ],
    "Sommer": [
        ("SOMMER-4020-TX03-868-4",  "Sommer 4020, 4-button, 868.8MHz"),
        ("SOMMER-4026-TX03-868-2",  "Sommer 4026, 2-button, 868.8MHz"),
        ("SOMMER-PEARL-TWIN-868",   "Sommer Pearl Twin, 2-button, 868.8MHz"),
    ],
    "Cardin": [
        ("CARDIN-S449-QZ2",         "Cardin S449 QZ/2, 2-button, 433.92MHz"),
        ("CARDIN-S449-QZ4",         "Cardin S449 QZ/4, 4-button, 433.92MHz"),
        ("CARDIN-TXQ449200",        "Cardin TXQ449200, 2-button, 433.92MHz"),
    ],
    "Ditec": [
        ("DITEC-GOL4",              "Ditec GOL4, 4-button, 433.92MHz, rolling code"),
        ("DITEC-BIXLP2",            "Ditec BIXLP2, 2-button, 433.92MHz"),
    ],
    "Novoferm_Garador": [
        ("NOVOFERM-NOVOTRON-502",   "Novoferm Novotron 502, 2-button, 433.92MHz"),
        ("NOVOFERM-NOVOTRON-504",   "Novoferm Novotron 504, 4-button, 433.92MHz"),
        ("GARADOR-HSE2-868-BS",     "Garador/Hörmann HSE2 868-BS, 2-button"),
    ],
    "Universal_Clones": [
        ("UNIVERSAL-433-FIXED-4",   "Universal clone, 4-button, 433.92MHz, fixed code"),
        ("UNIVERSAL-433-ROLLING-4", "Universal clone, 4-button, 433.92MHz, rolling code"),
        ("UNIVERSAL-868-ROLLING-4", "Universal clone, 4-button, 868MHz, rolling code"),
        ("UNIVERSAL-MULTI-FREQ-4",  "Universal multi-frequency, 4-button, 280-868MHz"),
    ],
}


def create_structure():
    print(f"\n{'='*60}")
    print(f"  RemotesDB Setup Script")
    print(f"{'='*60}\n")
    print(f"  Target: {REMOTESDB_ROOT}\n")

    REMOTESDB_ROOT.mkdir(parents=True, exist_ok=True)

    readme_path = REMOTESDB_ROOT / "_README.txt"
    if not readme_path.exists():
        readme_path.write_text(
            "RemotesDB — Image database for MatchIT Remotes vertical\n"
            "=========================================================\n\n"
            "FOLDER STRUCTURE:\n"
            "  Each remote model gets its own folder directly under RemotesDB/.\n"
            "  The folder name IS the SKU / model name.\n\n"
            "IMAGE NAMING:\n"
            "  RemotesDB/CAME-TOP-432NA/\n"
            "    CAME-TOP-432NA_FRONT.jpg     ← required (front with buttons visible)\n"
            "    CAME-TOP-432NA_BACK.jpg      ← recommended (back with label/sticker)\n\n"
            "TIPS:\n"
            "  - Photograph front clearly showing button layout, colour, shape\n"
            "  - Photograph back showing any model number, frequency label\n"
            "  - Use .jpg (preferred) or .png\n"
            "  - Plain background, good lighting\n"
            "  - Fill the frame with the remote\n",
            encoding="utf-8"
        )
        print("  ✅ Created _README.txt")

    total_created = 0
    total_skipped = 0

    for brand, skus in SAMPLE_SKUS.items():
        print(f"\n  ── {brand} ──")
        for sku_name, description in skus:
            sku_dir = REMOTESDB_ROOT / sku_name
            if sku_dir.exists():
                total_skipped += 1
                continue

            sku_dir.mkdir(exist_ok=True)

            placeholder = sku_dir / f"{sku_name}_FRONT.txt"
            placeholder.write_text(
                f"PLACEHOLDER — Replace with {sku_name}_FRONT.jpg\n\n"
                f"Product: {description}\n"
                f"SKU: {sku_name}\n\n"
                f"Take a clear photo of the FRONT (buttons visible).\n"
                f"Save as: {sku_name}_FRONT.jpg\n"
                f"Also take BACK photo: {sku_name}_BACK.jpg\n\n"
                f"Then delete this .txt file.\n",
                encoding="utf-8"
            )

            print(f"    📁 {sku_name:36s}  ({description})")
            total_created += 1

    total_skus = sum(len(v) for v in SAMPLE_SKUS.values())
    print(f"\n{'='*60}")
    print(f"  DONE!")
    print(f"  Created: {total_created} SKU folders")
    if total_skipped:
        print(f"  Skipped: {total_skipped} (already existed)")
    print(f"  Total models: {total_skus} across {len(SAMPLE_SKUS)} brands")
    print(f"  Location: {REMOTESDB_ROOT}")
    print(f"{'='*60}\n")
    print("  NEXT STEPS:")
    print("  1. Add front + back photos to each SKU folder")
    print("  2. Delete the .txt placeholder files")
    print("  3. Set config.json: \"vertical\": \"remotes\"")
    print("  4. In MatchIT Admin > Sync from RemotesDB")
    print("  5. Admin > Re-embed Missing Only")
    print()


if __name__ == "__main__":
    create_structure()