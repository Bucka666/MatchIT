"""
scrape_hardware_images.py — Download test images for HardwareDB
================================================================
Fetches product images from Screwfix and saves them into the
HardwareDB folder structure ready for MatchIT import.

Usage:
    pip install requests beautifulsoup4 Pillow
    python scrape_hardware_images.py

This is for PRIVATE TESTING ONLY. Product images remain the
property of their respective owners.
"""

import os
import re
import time
import json
import hashlib
from pathlib import Path
from io import BytesIO

try:
    import requests
    from bs4 import BeautifulSoup
    from PIL import Image
except ImportError:
    print("\n  Missing dependencies. Run:")
    print("    pip install requests beautifulsoup4 Pillow\n")
    raise SystemExit(1)


# ─── Config ───
HARDWAREDB_ROOT = Path(r"C:\Users\c_a_b\My Drive\HardwareDB")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}
DELAY = 2.0  # seconds between requests (be polite)
TARGET_SIZE = 800  # resize longest edge to this


# ─── Product list ───
# Format: (sku_name, screwfix_url, description)
PRODUCTS = [
    # ══════════════ SCREWS ══════════════
    ("WOOD-CSK-POZI-5x50-YZP",
     "https://www.screwfix.com/p/goldscrew-pz-double-countersunk-self-tapping-multipurpose-screws-5mm-x-50mm-200-pack/16364",
     "Wood screw, CSK pozi, 5x50mm, yellow zinc"),

    ("WOOD-CSK-POZI-4x40-YZP",
     "https://www.screwfix.com/p/goldscrew-pz-double-countersunk-self-tapping-multipurpose-screws-4mm-x-40mm-200-pack/52764",
     "Wood screw, CSK pozi, 4x40mm, yellow zinc"),

    ("WOOD-CSK-POZI-5x80-YZP",
     "https://www.screwfix.com/p/optimaxx-pz-countersunk-wood-screws-5mm-x-80mm-200-pack/997ty",
     "Wood screw, CSK pozi, 5x80mm, yellow zinc"),

    ("MACHINE-PAN-POZI-M4x20-BZP",
     "https://www.screwfix.com/p/easyfix-pozi-pan-head-machine-screws-bzp-m4-x-20mm-100-pack/8187k",
     "Machine screw, pan head pozi, M4x20mm, BZP"),

    ("MACHINE-CSK-POZI-M5x40-BZP",
     "https://www.screwfix.com/p/easyfix-pozi-countersunk-machine-screws-bzp-m5-x-40mm-50-pack/1689k",
     "Machine screw, CSK pozi, M5x40mm, BZP"),

    ("SELFTAP-PAN-POZI-8x0.75-BZP",
     "https://www.screwfix.com/p/easyfix-pz-pan-head-self-tapping-screws-no-8-x-3-4-100-pack/8540k",
     "Self-tapping, pan head pozi, No.8 x 3/4in, BZP"),

    ("COACH-HEX-M10x75-BZP",
     "https://www.screwfix.com/p/easyfix-hex-bolt-bzp-steel-m10-x-75mm-50-pack/71306",
     "Coach/hex bolt, M10x75mm, BZP"),

    # ══════════════ BOLTS ══════════════
    ("HEXBOLT-M8x50-BZP",
     "https://www.screwfix.com/p/easyfix-hex-bolts-bzp-steel-m8-x-50mm-50-pack/48718",
     "Hex bolt, M8x50mm, BZP"),

    ("HEXBOLT-M10x80-BZP",
     "https://www.screwfix.com/p/easyfix-hex-bolts-bzp-steel-m10-x-80mm-50-pack/76230",
     "Hex bolt, M10x80mm, BZP"),

    ("HEXBOLT-M12x60-BZP",
     "https://www.screwfix.com/p/easyfix-hex-bolts-bzp-steel-m12-x-60mm-25-pack/52348",
     "Hex bolt, M12x60mm, BZP"),

    ("CARRIAGE-M8x100-BZP",
     "https://www.screwfix.com/p/easyfix-carriage-bolts-bzp-m8-x-100mm-10-pack/76254",
     "Carriage bolt, M8x100mm, BZP"),

    # ══════════════ NUTS ══════════════
    ("HEXNUT-M8-BZP",
     "https://www.screwfix.com/p/easyfix-hex-nuts-bzp-steel-m8-100-pack/12345",
     "Hex nut, M8, BZP"),

    ("HEXNUT-M10-BZP",
     "https://www.screwfix.com/p/easyfix-hex-nuts-bzp-steel-m10-100-pack/75224",
     "Hex nut, M10, BZP"),

    ("NYLOC-M8-BZP",
     "https://www.screwfix.com/p/easyfix-nylon-lock-nuts-bzp-m8-100-pack/76458",
     "Nyloc nut, M8, BZP"),

    ("WINGNUT-M6-BZP",
     "https://www.screwfix.com/p/wing-nuts-bzp-m6-10-pack/61942",
     "Wing nut, M6, BZP"),

    # ══════════════ NAILS ══════════════
    ("ROUND-WIRE-3.35x65-BW",
     "https://www.screwfix.com/p/round-wire-nails-bright-3-35mm-x-65mm-1kg-pack/35478",
     "Round wire nail, 3.35x65mm, bright"),

    ("OVAL-WIRE-2.65x40-BW",
     "https://www.screwfix.com/p/oval-wire-nails-bright-40mm-x-2-65mm-1kg-pack/96540",
     "Oval wire nail, 2.65x40mm, bright"),

    ("PANEL-PIN-1.6x30",
     "https://www.screwfix.com/p/panel-pins-bright-30mm-x-1-6mm-0-5kg-pack/59817",
     "Panel pin, 1.6x30mm"),

    ("MASONRY-NAIL-3.5x50",
     "https://www.screwfix.com/p/hardened-masonry-nails-3-5mm-x-50mm-100-pack/73816",
     "Masonry nail, 3.5x50mm"),

    # ══════════════ WASHERS ══════════════
    ("FLAT-FORMA-M8-BZP",
     "https://www.screwfix.com/p/easyfix-flat-washers-bzp-m8-100-pack/50728",
     "Flat washer Form A, M8, BZP"),

    ("FLAT-FORMA-M10-BZP",
     "https://www.screwfix.com/p/easyfix-flat-washers-bzp-m10-100-pack/61893",
     "Flat washer Form A, M10, BZP"),

    ("SPRING-M8-BZP",
     "https://www.screwfix.com/p/easyfix-spring-washers-bzp-m8-100-pack/83594",
     "Spring washer, M8, BZP"),

    ("PENNY-M8-BZP",
     "https://www.screwfix.com/p/easyfix-penny-washers-bzp-m8-100-pack/94127",
     "Penny washer, M8, BZP"),

    # ══════════════ WALL PLUGS & ANCHORS ══════════════
    ("WALLPLUG-RED-6mm",
     "https://www.screwfix.com/p/rawlplug-uno-wall-plugs-6-x-28mm-96-pack/62494",
     "Standard wall plug, red, 6mm"),

    ("WALLPLUG-BROWN-7mm",
     "https://www.screwfix.com/p/rawlplug-uno-wall-plugs-7-x-30mm-96-pack/22870",
     "Standard wall plug, brown, 7mm"),

    ("PLASTERBOARD-METAL-TOGGLE",
     "https://www.screwfix.com/p/easyfix-self-drill-plasterboard-fixings-metal-25-pack/6698v",
     "Metal self-drill plasterboard fixing"),

    ("FRAME-FIXING-10x100",
     "https://www.screwfix.com/p/easyfix-countersunk-concrete-frame-screws-7-5-x-102mm-100-pack/8891v",
     "Frame fixing, 10x100mm"),

    # ══════════════ RIVETS ══════════════
    ("BLIND-DOME-3.2x8-ALU",
     "https://www.screwfix.com/p/blind-rivets-aluminium-steel-3-2-x-8mm-100-pack/98102",
     "Blind rivet dome, 3.2x8mm, aluminium/steel"),

    ("BLIND-DOME-4.0x10-ALU",
     "https://www.screwfix.com/p/blind-rivets-aluminium-steel-4-x-10mm-100-pack/80617",
     "Blind rivet dome, 4.0x10mm, aluminium/steel"),

    ("BLIND-DOME-4.8x12-ALU",
     "https://www.screwfix.com/p/blind-rivets-aluminium-steel-4-8-x-12mm-100-pack/74293",
     "Blind rivet dome, 4.8x12mm, aluminium/steel"),

    ("RIVETNUT-M5-BZP",
     "https://www.screwfix.com/p/rivet-nuts-steel-m5-50-pack/38720",
     "Rivet nut, M5, BZP"),

    # ══════════════ PLUMBING ══════════════
    ("COMP-STRAIGHT-15mm",
     "https://www.screwfix.com/p/flomasta-compression-straight-coupler-15mm-x-15mm-2-pack/4792g",
     "Compression straight coupler, 15mm"),

    ("COMP-ELBOW-15mm",
     "https://www.screwfix.com/p/flomasta-compression-elbow-15mm-x-15mm-2-pack/8965g",
     "Compression elbow 90°, 15mm"),

    ("COMP-TEE-15mm",
     "https://www.screwfix.com/p/flomasta-compression-tee-15mm-x-15mm-x-15mm/6743g",
     "Compression tee, 15mm"),

    ("COMP-STRAIGHT-22mm",
     "https://www.screwfix.com/p/flomasta-compression-straight-coupler-22mm-x-22mm-2-pack/7894g",
     "Compression straight coupler, 22mm"),

    ("PUSHFIT-STRAIGHT-15mm",
     "https://www.screwfix.com/p/flomasta-push-fit-equal-coupler-pipe-fitting-15mm/5621g",
     "Push-fit straight coupler, 15mm"),

    ("PUSHFIT-ELBOW-15mm",
     "https://www.screwfix.com/p/flomasta-push-fit-equal-elbow-pipe-fitting-15mm/8234g",
     "Push-fit elbow 90°, 15mm"),
]


def fetch_image_url(page_url: str, session: requests.Session) -> str | None:
    """Fetch a Screwfix product page and extract the main product image URL."""
    try:
        resp = session.get(page_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"    ⚠ Failed to fetch page: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Strategy 1: Open Graph image meta tag (most reliable)
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        return og["content"]

    # Strategy 2: Main product image in the gallery
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if "product" in src.lower() and ("media" in src or "images" in src):
            if not src.startswith("http"):
                src = "https://www.screwfix.com" + src
            return src

    # Strategy 3: JSON-LD structured data
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            if isinstance(data, dict) and "image" in data:
                imgs = data["image"]
                if isinstance(imgs, list) and imgs:
                    return imgs[0]
                elif isinstance(imgs, str):
                    return imgs
        except (json.JSONDecodeError, TypeError):
            pass

    # Strategy 4: Any large product image
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src and (".jpg" in src or ".png" in src or ".webp" in src):
            if "logo" not in src.lower() and "icon" not in src.lower():
                if not src.startswith("http"):
                    src = "https://www.screwfix.com" + src
                return src

    return None


def download_and_save(image_url: str, sku_dir: Path, sku_name: str,
                      session: requests.Session) -> bool:
    """Download an image, resize it, and save as SKU_FRONT.jpg."""
    try:
        resp = session.get(image_url, headers=HEADERS, timeout=15, stream=True)
        resp.raise_for_status()

        img = Image.open(BytesIO(resp.content))

        # Convert to RGB if needed (handles RGBA/P mode)
        if img.mode not in ("RGB",):
            img = img.convert("RGB")

        # Resize longest edge
        w, h = img.size
        if max(w, h) > TARGET_SIZE:
            scale = TARGET_SIZE / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

        # Save
        out_path = sku_dir / f"{sku_name}_FRONT.jpg"
        img.save(str(out_path), "JPEG", quality=92)
        print(f"    ✅ Saved: {out_path.name} ({img.size[0]}x{img.size[1]})")
        return True

    except Exception as e:
        print(f"    ⚠ Failed to download image: {e}")
        return False


def main():
    print(f"\n{'='*60}")
    print(f"  HardwareDB Image Scraper")
    print(f"{'='*60}\n")
    print(f"  Target: {HARDWAREDB_ROOT}")
    print(f"  Products: {len(PRODUCTS)}")
    print(f"  Delay: {DELAY}s between requests\n")

    if not HARDWAREDB_ROOT.exists():
        print(f"  ⚠ HardwareDB root not found. Run setup_hardwaredb.py first.")
        return

    session = requests.Session()
    success = 0
    skipped = 0
    failed = 0

    for i, (sku_name, url, description) in enumerate(PRODUCTS, 1):
        print(f"  [{i}/{len(PRODUCTS)}] {sku_name}")
        print(f"    {description}")

        sku_dir = HARDWAREDB_ROOT / sku_name
        front_path = sku_dir / f"{sku_name}_FRONT.jpg"

        # Skip if already downloaded
        if front_path.exists():
            print(f"    ⏭ Already exists, skipping")
            skipped += 1
            continue

        sku_dir.mkdir(parents=True, exist_ok=True)

        # Clean up any placeholder .txt files
        for txt in sku_dir.glob("*.txt"):
            txt.unlink()

        print(f"    Fetching: {url[:70]}...")
        img_url = fetch_image_url(url, session)

        if img_url:
            print(f"    Image: {img_url[:80]}...")
            if download_and_save(img_url, sku_dir, sku_name, session):
                success += 1
            else:
                failed += 1
        else:
            print(f"    ⚠ Could not find product image on page")
            failed += 1

        # Be polite
        if i < len(PRODUCTS):
            time.sleep(DELAY)

    # ── Summary ──
    print(f"\n{'='*60}")
    print(f"  DONE!")
    print(f"  Downloaded: {success}")
    print(f"  Skipped (existing): {skipped}")
    print(f"  Failed: {failed}")
    print(f"  Total SKU folders: {sum(1 for d in HARDWAREDB_ROOT.iterdir() if d.is_dir() and not d.name.startswith('_'))}")
    print(f"{'='*60}\n")

    if success > 0:
        print("  NEXT STEPS:")
        print("  1. In MatchIT (hardware vertical), go to Admin")
        print("  2. Click 'Sync from Product DB'")
        print("  3. Then click 'Re-embed Missing Only (fast)'")
        print("  4. Try matching with a photo of a screw, bolt, etc.!")
        print()

    if failed > 0:
        print(f"  NOTE: {failed} products failed to download.")
        print("  Screwfix may have changed URLs or blocked the request.")
        print("  You can manually add images to those folders or")
        print("  re-run the script to retry failed ones.\n")


if __name__ == "__main__":
    main()