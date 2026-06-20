"""
scrape_hardware_images_v2.py — Download test images for HardwareDB
===================================================================
Uses Bing Image Search to find product images for each hardware type.
Much more reliable than targeting specific retailer URLs.

Usage:
    pip install requests Pillow
    python scrape_hardware_images_v2.py

This is for PRIVATE TESTING ONLY.
"""

import os
import re
import time
import json
from pathlib import Path
from io import BytesIO
from urllib.parse import quote_plus

try:
    import requests
    from PIL import Image
except ImportError:
    print("\n  Missing dependencies. Run:")
    print("    pip install requests Pillow\n")
    raise SystemExit(1)


# ─── Config ───
HARDWAREDB_ROOT = Path(r"C:\Users\c_a_b\My Drive\HardwareDB")
DELAY = 1.5
TARGET_SIZE = 800

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

# ─── Products: (sku_name, search_query, description) ───
PRODUCTS = [
    # ══════ SCREWS ══════
    ("WOOD-CSK-POZI-5x50-YZP",
     "pozi countersunk wood screw 5x50mm yellow zinc plated product photo",
     "Wood screw, CSK pozi, 5x50mm, YZP"),
    ("WOOD-CSK-POZI-4x40-YZP",
     "pozi countersunk wood screw 4x40mm zinc plated product",
     "Wood screw, CSK pozi, 4x40mm, YZP"),
    ("WOOD-CSK-POZI-5x80-YZP",
     "countersunk wood screw 5x80mm pozi drive yellow zinc",
     "Wood screw, CSK pozi, 5x80mm, YZP"),
    ("WOOD-CSK-POZI-3.5x25-YZP",
     "wood screw 3.5x25mm pozi countersunk zinc product image",
     "Wood screw, CSK pozi, 3.5x25mm, YZP"),
    ("MACHINE-PAN-POZI-M4x20-BZP",
     "pan head pozi machine screw M4 20mm bright zinc plated",
     "Machine screw, pan head pozi, M4x20, BZP"),
    ("MACHINE-CSK-POZI-M5x40-BZP",
     "countersunk pozi machine screw M5x40mm zinc plated",
     "Machine screw, CSK pozi, M5x40, BZP"),
    ("SELFTAP-PAN-POZI-8x19-BZP",
     "self tapping screw pan head pozi number 8 zinc plated",
     "Self-tapping, pan head pozi, No.8, BZP"),
    ("COACH-HEX-M10x75-BZP",
     "coach screw hex head M10 75mm zinc plated product",
     "Coach screw, hex, M10x75, BZP"),

    # ══════ BOLTS ══════
    ("HEXBOLT-M8x50-BZP",
     "hex bolt M8x50mm bright zinc plated product photo",
     "Hex bolt, M8x50mm, BZP"),
    ("HEXBOLT-M10x80-BZP",
     "hex bolt M10 80mm zinc plated fastener product",
     "Hex bolt, M10x80mm, BZP"),
    ("HEXBOLT-M12x60-BZP",
     "hex bolt M12x60mm zinc plated steel",
     "Hex bolt, M12x60mm, BZP"),
    ("CARRIAGE-M8x100-BZP",
     "carriage bolt coach bolt M8x100mm zinc plated product",
     "Carriage bolt, M8x100mm, BZP"),
    ("STUDDING-M10x300-BZP",
     "threaded rod studding M10 zinc plated product",
     "Threaded rod, M10x300mm, BZP"),

    # ══════ NUTS ══════
    ("HEXNUT-M8-BZP",
     "hex nut M8 bright zinc plated steel product photo",
     "Hex nut, M8, BZP"),
    ("HEXNUT-M10-BZP",
     "hex nut M10 zinc plated product image",
     "Hex nut, M10, BZP"),
    ("NYLOC-M8-BZP",
     "nyloc lock nut M8 zinc plated product",
     "Nyloc nut, M8, BZP"),
    ("NYLOC-M10-A2",
     "nyloc nut M10 stainless steel A2 product photo",
     "Nyloc nut, M10, A2 stainless"),
    ("WINGNUT-M6-BZP",
     "wing nut M6 zinc plated butterfly nut product",
     "Wing nut, M6, BZP"),
    ("FLANGENUT-M8-BZP",
     "serrated flange nut M8 zinc plated product",
     "Flange nut, M8, BZP"),

    # ══════ NAILS ══════
    ("ROUND-WIRE-3.35x65-BW",
     "round wire nail 65mm bright steel product photo",
     "Round wire nail, 3.35x65mm, bright"),
    ("ROUND-WIRE-4.5x100-GALV",
     "round wire nail 100mm galvanised product",
     "Round wire nail, 4.5x100mm, galvanised"),
    ("OVAL-WIRE-2.65x40-BW",
     "oval wire nail 40mm bright product photo",
     "Oval wire nail, 2.65x40mm, bright"),
    ("PANEL-PIN-1.6x30",
     "panel pin 30mm bright steel product image",
     "Panel pin, 1.6x30mm"),
    ("MASONRY-NAIL-3.5x50",
     "masonry nail hardened 50mm zinc product photo",
     "Masonry nail, 3.5x50mm"),

    # ══════ WASHERS ══════
    ("FLAT-FORMA-M8-BZP",
     "flat washer form A M8 zinc plated product photo",
     "Flat washer Form A, M8, BZP"),
    ("FLAT-FORMA-M10-BZP",
     "flat washer M10 bright zinc plated form A",
     "Flat washer Form A, M10, BZP"),
    ("FLAT-FORMC-M8-BZP",
     "repair washer penny washer M8 zinc plated large",
     "Mudguard / Form C washer, M8, BZP"),
    ("SPRING-M8-BZP",
     "spring lock washer M8 zinc plated product",
     "Spring washer, M8, BZP"),
    ("PENNY-M10-BZP",
     "penny washer repair washer M10 zinc plated",
     "Penny washer, M10, BZP"),

    # ══════ WALL PLUGS & ANCHORS ══════
    ("WALLPLUG-RED-6mm",
     "red wall plug 6mm rawlplug product photo",
     "Standard wall plug, red, 6mm"),
    ("WALLPLUG-BROWN-7mm",
     "brown wall plug 7mm rawlplug product",
     "Standard wall plug, brown, 7mm"),
    ("WALLPLUG-YELLOW-5mm",
     "yellow wall plug 5mm product image",
     "Standard wall plug, yellow, 5mm"),
    ("PLASTERBOARD-SPRING-TOGGLE",
     "spring toggle plasterboard fixing metal product photo",
     "Spring toggle plasterboard fixing"),
    ("PLASTERBOARD-SELF-DRILL",
     "self drill plasterboard fixing metal product",
     "Metal self-drill plasterboard fixing"),
    ("FRAME-FIXING-10x100",
     "frame fixing concrete screw 10x100mm product",
     "Frame fixing, 10x100mm"),
    ("HEAVY-DUTY-ANCHOR-M10",
     "heavy duty through bolt anchor M10 product photo",
     "Heavy duty anchor, M10"),

    # ══════ RIVETS ══════
    ("BLIND-DOME-3.2x8-ALU",
     "blind pop rivet aluminium 3.2mm dome head product photo",
     "Blind rivet dome, 3.2x8mm, alu/steel"),
    ("BLIND-DOME-4.0x10-ALU",
     "pop rivet blind rivet 4mm aluminium dome product",
     "Blind rivet dome, 4.0x10mm, alu/steel"),
    ("BLIND-DOME-4.8x12-ALU",
     "blind rivet 4.8x12mm aluminium steel dome head",
     "Blind rivet dome, 4.8x12mm, alu/steel"),
    ("BLIND-CSK-4.0x10-ALU",
     "countersunk blind rivet 4mm aluminium product photo",
     "Blind rivet CSK, 4.0x10mm, alu/steel"),
    ("BLIND-LFLANGE-4.8-ALU",
     "large flange blind rivet 4.8mm aluminium product",
     "Blind rivet large flange, 4.8mm, alu/steel"),
    ("RIVETNUT-M5-BZP",
     "rivet nut nutsert M5 zinc plated product photo",
     "Rivet nut, M5, BZP"),
    ("RIVETNUT-M6-A2",
     "rivet nut nutsert M6 stainless steel product",
     "Rivet nut, M6, stainless"),

    # ══════ PLUMBING ══════
    ("COMP-STRAIGHT-15mm",
     "compression straight coupler 15mm brass plumbing fitting",
     "Compression straight coupler, 15mm"),
    ("COMP-ELBOW-15mm",
     "compression elbow 90 degree 15mm brass fitting product",
     "Compression elbow 90°, 15mm"),
    ("COMP-TEE-15mm",
     "compression tee 15mm brass plumbing fitting product",
     "Compression tee, 15mm"),
    ("COMP-STRAIGHT-22mm",
     "compression straight coupler 22mm brass fitting",
     "Compression straight coupler, 22mm"),
    ("COMP-REDUCER-22x15",
     "compression reducing coupler 22mm 15mm brass product",
     "Compression reducer, 22x15mm"),
    ("PUSHFIT-STRAIGHT-15mm",
     "push fit straight coupler 15mm speedfit product photo",
     "Push-fit straight coupler, 15mm"),
    ("PUSHFIT-ELBOW-15mm",
     "push fit elbow 15mm speedfit product photo",
     "Push-fit elbow 90°, 15mm"),
    ("PUSHFIT-TEE-22mm",
     "push fit tee 22mm speedfit plumbing product",
     "Push-fit tee, 22mm"),
    ("ENDFEED-STRAIGHT-15mm",
     "end feed solder ring straight coupler 15mm copper",
     "End feed coupler, 15mm"),
    ("ENDFEED-ELBOW-15mm",
     "end feed solder ring elbow 15mm copper fitting",
     "End feed elbow, 15mm"),
]


def bing_image_search(query: str, session: requests.Session) -> list[str]:
    """Search Bing Images and return a list of image URLs."""
    url = f"https://www.bing.com/images/search?q={quote_plus(query)}&qft=+filterui:photo-photo&FORM=IRFLTR&first=1"

    try:
        resp = session.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"    ⚠ Search failed: {e}")
        return []

    # Extract image URLs from Bing's response
    # Bing embeds image URLs in 'murl' parameters and data attributes
    image_urls = []

    # Pattern 1: murl in the page content
    murl_pattern = re.findall(r'"murl":"(https?://[^"]+)"', resp.text)
    for u in murl_pattern:
        u = u.replace("\\u0026", "&")
        if _is_good_image_url(u):
            image_urls.append(u)

    # Pattern 2: data-src attributes
    src_pattern = re.findall(r'src2="(https?://[^"]+)"', resp.text)
    for u in src_pattern:
        if _is_good_image_url(u):
            image_urls.append(u)

    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for u in image_urls:
        if u not in seen:
            seen.add(u)
            deduped.append(u)

    return deduped[:10]  # Return top 10


def _is_good_image_url(url: str) -> bool:
    """Filter out tiny thumbnails, icons, logos, etc."""
    low = url.lower()
    if any(x in low for x in ["logo", "icon", "sprite", "pixel", "badge", "banner",
                                "avatar", "placeholder", "1x1", "blank"]):
        return False
    if any(low.endswith(x) for x in [".svg", ".gif", ".bmp"]):
        return False
    return True


def download_and_save(image_urls: list[str], sku_dir: Path, sku_name: str,
                      session: requests.Session) -> bool:
    """Try downloading from each URL until one succeeds."""
    for i, url in enumerate(image_urls[:5]):  # Try up to 5
        try:
            resp = session.get(url, headers={
                "User-Agent": HEADERS["User-Agent"],
                "Accept": "image/*,*/*;q=0.8",
                "Referer": "https://www.bing.com/",
            }, timeout=10, stream=True)

            if resp.status_code != 200:
                continue

            content_type = resp.headers.get("content-type", "")
            if "image" not in content_type and "octet" not in content_type:
                continue

            data = resp.content
            if len(data) < 5000:  # Too small, probably an icon
                continue

            img = Image.open(BytesIO(data))

            # Skip tiny images
            w, h = img.size
            if w < 150 or h < 150:
                continue

            # Convert to RGB
            if img.mode not in ("RGB",):
                img = img.convert("RGB")

            # Resize longest edge
            if max(w, h) > TARGET_SIZE:
                scale = TARGET_SIZE / max(w, h)
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

            out_path = sku_dir / f"{sku_name}_FRONT.jpg"
            img.save(str(out_path), "JPEG", quality=92)
            print(f"    ✅ Saved: {out_path.name} ({img.size[0]}x{img.size[1]}) from result #{i+1}")
            return True

        except Exception:
            continue

    return False


def main():
    print(f"\n{'='*60}")
    print(f"  HardwareDB Image Scraper v2 (Bing Image Search)")
    print(f"{'='*60}\n")
    print(f"  Target: {HARDWAREDB_ROOT}")
    print(f"  Products: {len(PRODUCTS)}")
    print(f"  Delay: {DELAY}s between searches\n")

    if not HARDWAREDB_ROOT.exists():
        print(f"  ⚠ HardwareDB root not found. Run setup_hardwaredb.py first.")
        return

    session = requests.Session()
    success = 0
    skipped = 0
    failed = 0

    for i, (sku_name, query, description) in enumerate(PRODUCTS, 1):
        print(f"  [{i}/{len(PRODUCTS)}] {sku_name}")
        print(f"    {description}")

        sku_dir = HARDWAREDB_ROOT / sku_name
        front_path = sku_dir / f"{sku_name}_FRONT.jpg"

        if front_path.exists():
            print(f"    ⏭ Already exists, skipping")
            skipped += 1
            continue

        sku_dir.mkdir(parents=True, exist_ok=True)

        # Clean up placeholders
        for txt in sku_dir.glob("*.txt"):
            txt.unlink()

        print(f"    🔍 Searching: {query[:60]}...")
        image_urls = bing_image_search(query, session)

        if image_urls:
            print(f"    Found {len(image_urls)} candidate images")
            if download_and_save(image_urls, sku_dir, sku_name, session):
                success += 1
            else:
                print(f"    ⚠ All download attempts failed")
                failed += 1
        else:
            print(f"    ⚠ No images found in search")
            failed += 1

        if i < len(PRODUCTS):
            time.sleep(DELAY)

    # ── Summary ──
    print(f"\n{'='*60}")
    print(f"  DONE!")
    print(f"  Downloaded: {success}")
    print(f"  Skipped (existing): {skipped}")
    print(f"  Failed: {failed}")
    total_with_images = sum(
        1 for d in HARDWAREDB_ROOT.iterdir()
        if d.is_dir() and not d.name.startswith("_")
        and any(f.suffix.lower() in {".jpg", ".jpeg", ".png"} for f in d.iterdir() if f.is_file())
    )
    print(f"  SKU folders with images: {total_with_images}")
    print(f"{'='*60}\n")

    if success > 0:
        print("  NEXT STEPS:")
        print("  1. Start app.py with hardware vertical")
        print("  2. Go to Admin > Sync from Product DB")
        print("  3. Then Admin > Re-embed Missing Only (fast)")
        print("  4. Try matching with a photo of a screw, bolt, etc.!")
        print()

    if failed > 0:
        print(f"  TIP: {failed} products didn't get images.")
        print("  Re-run the script — it skips already-downloaded ones.")
        print("  Or manually save a photo into the SKU folder as SKUNAME_FRONT.jpg")
        print()


if __name__ == "__main__":
    main()