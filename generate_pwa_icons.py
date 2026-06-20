"""
Generate PWA icons for GrailSweep Microsoft Store submission.

Produces 3 files in ./static/assets/:
  - icon-192.png            (192x192, any)
  - icon-512.png            (512x512, any)
  - icon-maskable-512.png   (512x512, maskable — logo at 80% with bg fill)

Source: ./static/assets/grailsweep_app_icon.png (1024x1024)
"""

from PIL import Image
from pathlib import Path

# --- Config -----------------------------------------------------------------
SOURCE_PATH = Path("static/assets/grailsweep_app_icon.png")
OUTPUT_DIR  = Path("static/assets")

# Must match manifest.json background_color exactly
BACKGROUND_COLOR = (0x0f, 0x0f, 0x1a, 0xff)  # #0f0f1a

# Maskable safe zone: logo occupies inner 80%, 10% padding on each side
MASKABLE_SAFE_ZONE = 0.80

# --- Generation -------------------------------------------------------------
def main() -> None:
    if not SOURCE_PATH.exists():
        raise FileNotFoundError(f"Missing source icon: {SOURCE_PATH}")

    src = Image.open(SOURCE_PATH).convert("RGBA")
    print(f"Loaded source: {SOURCE_PATH} ({src.size[0]}x{src.size[1]})")

    # 1. icon-192.png — straight resize
    out_192 = src.resize((192, 192), Image.LANCZOS)
    out_192.save(OUTPUT_DIR / "icon-192.png", "PNG", optimize=True)
    print("  ✅ icon-192.png")

    # 2. icon-512.png — straight resize
    out_512 = src.resize((512, 512), Image.LANCZOS)
    out_512.save(OUTPUT_DIR / "icon-512.png", "PNG", optimize=True)
    print("  ✅ icon-512.png")

    # 3. icon-maskable-512.png — logo at 80%, bg fills the rest
    canvas = Image.new("RGBA", (512, 512), BACKGROUND_COLOR)
    inner_size = int(512 * MASKABLE_SAFE_ZONE)              # 409 px
    logo = src.resize((inner_size, inner_size), Image.LANCZOS)
    offset = (512 - inner_size) // 2                         # 51 px
    canvas.paste(logo, (offset, offset), logo)               # use alpha as mask
    canvas.save(OUTPUT_DIR / "icon-maskable-512.png", "PNG", optimize=True)
    print("  ✅ icon-maskable-512.png")

    print("\nDone. Verify all three files in static/assets/ before deploying.")

if __name__ == "__main__":
    main()