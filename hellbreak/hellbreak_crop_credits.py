"""
hellbreak_crop_credits.py
-------------------------
Step 7a. Crops the credit strip (artist / set code / collector number / rarity)
from the bottom of each Hellbreak card image, upscales it, and assembles
contact sheets so you can eyeball whether the crop region is correct BEFORE
we invest in OCR.

Produces:
    _crops/strips/<name>.png      individual upscaled strips (OCR input later)
    _crops/contact_NN.png         contact sheets, ~18 strips each
    _crops/crop_report.csv        what was cropped, what was skipped, and why

Classification by aspect ratio:
    0.65-0.78   portrait card       -> crop bottom-right
    1.30-1.55   landscape card      -> crop bottom-right (wider)
    anything else                   -> SKIPPED (screenshot / diagram / photo)

Skipped files are not failures. Phone screenshots need the card located first,
which is a separate problem. The report tells you which ones need attention.

Run:
    python hellbreak_crop_credits.py
    python hellbreak_crop_credits.py --root "C:\\CardsDB\\hellbreak"
    python hellbreak_crop_credits.py --scale 4
"""

import argparse
import csv
import os
import sys

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Pillow not installed. Run:  pip install pillow")

DEFAULT_ROOT = r"C:\CardsDB\hellbreak"
OUT_DIRNAME = "_crops"
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}

# Crop box as fractions of (width, height): (left, top, right, bottom)
# Generous on purpose - includes the copyright line above the credit line.
PORTRAIT_BOX = (0.48, 0.895, 1.00, 1.00)
LANDSCAPE_BOX = (0.45, 0.880, 1.00, 1.00)

PORTRAIT_RANGE = (0.65, 0.78)
LANDSCAPE_RANGE = (1.30, 1.55)

SHEET_COLS = 1
SHEET_ROWS = 18
LABEL_H = 22


def classify(w, h):
    if not h:
        return None, 0.0
    r = w / h
    if PORTRAIT_RANGE[0] <= r <= PORTRAIT_RANGE[1]:
        return "portrait", r
    if LANDSCAPE_RANGE[0] <= r <= LANDSCAPE_RANGE[1]:
        return "landscape", r
    return None, r


def crop_strip(im, kind, scale):
    w, h = im.size
    box_f = PORTRAIT_BOX if kind == "portrait" else LANDSCAPE_BOX
    box = (int(w * box_f[0]), int(h * box_f[1]),
           int(w * box_f[2]), int(h * box_f[3]))
    strip = im.crop(box)
    if strip.mode != "RGB":
        strip = strip.convert("RGB")
    nw, nh = strip.size[0] * scale, strip.size[1] * scale
    return strip.resize((nw, nh), Image.LANCZOS)


def get_font(size):
    for name in ("arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def build_sheets(strips, outdir):
    """strips: list of (label, PIL.Image)"""
    if not strips:
        return []
    font = get_font(14)
    max_w = max(s.size[0] for _, s in strips)
    sheets = []
    per = SHEET_ROWS * SHEET_COLS
    for idx in range(0, len(strips), per):
        chunk = strips[idx:idx + per]
        heights = [s.size[1] + LABEL_H for _, s in chunk]
        sheet_h = sum(heights) + 20
        sheet = Image.new("RGB", (max_w + 20, sheet_h), (24, 24, 28))
        d = ImageDraw.Draw(sheet)
        y = 10
        for label, s in chunk:
            d.text((10, y), label[:90], fill=(150, 210, 255), font=font)
            y += LABEL_H
            sheet.paste(s, (10, y))
            y += s.size[1]
        path = os.path.join(outdir, "contact_%02d.png" % (len(sheets) + 1))
        sheet.save(path)
        sheets.append(path)
    return sheets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--scale", type=int, default=3, help="upscale factor")
    args = ap.parse_args()

    if not os.path.isdir(args.root):
        sys.exit("Root not found: %s" % args.root)

    outdir = os.path.join(args.root, OUT_DIRNAME)
    stripdir = os.path.join(outdir, "strips")
    os.makedirs(stripdir, exist_ok=True)

    report = []
    strips = []

    for dirpath, dirs, files in os.walk(args.root):
        dirs[:] = [d for d in dirs if d != OUT_DIRNAME]
        source = os.path.relpath(dirpath, args.root)
        if source == ".":
            continue
        for fn in sorted(files):
            stem, ext = os.path.splitext(fn)
            if ext.lower() not in IMG_EXT:
                continue
            path = os.path.join(dirpath, fn)
            try:
                with Image.open(path) as im:
                    im.load()
                    w, h = im.size
                    kind, ratio = classify(w, h)
                    if kind is None:
                        report.append({
                            "filename": fn, "source": source,
                            "width": w, "height": h, "aspect": round(ratio, 3),
                            "status": "skipped", "reason": "aspect outside card range",
                            "strip": "",
                        })
                        continue
                    strip = crop_strip(im, kind, args.scale)
            except Exception as e:
                report.append({
                    "filename": fn, "source": source, "width": 0, "height": 0,
                    "aspect": 0, "status": "error", "reason": str(e)[:80],
                    "strip": "",
                })
                continue

            safe = "%s__%s.png" % (source.replace(os.sep, "-"), stem)
            safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in safe)
            spath = os.path.join(stripdir, safe)
            strip.save(spath)
            strips.append(("%s / %s" % (source, fn), strip))
            report.append({
                "filename": fn, "source": source, "width": w, "height": h,
                "aspect": round(ratio, 3), "status": "cropped", "reason": kind,
                "strip": safe,
            })

    if not report:
        sys.exit("No images found under %s" % args.root)

    rpath = os.path.join(outdir, "crop_report.csv")
    with open(rpath, "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=list(report[0].keys()))
        wr.writeheader()
        wr.writerows(report)

    sheets = build_sheets(strips, outdir)

    cropped = [r for r in report if r["status"] == "cropped"]
    skipped = [r for r in report if r["status"] == "skipped"]
    errored = [r for r in report if r["status"] == "error"]

    print("")
    print("CROPPED  %d" % len(cropped))
    print("SKIPPED  %d  (aspect outside card range - screenshots, diagrams, photos)" % len(skipped))
    print("ERRORS   %d" % len(errored))
    print("")
    if skipped:
        print("SKIPPED FILES")
        for r in skipped[:40]:
            print("  %-52s %sx%s  aspect %s" % (r["filename"][:52], r["width"], r["height"], r["aspect"]))
        if len(skipped) > 40:
            print("  ... and %d more (see crop_report.csv)" % (len(skipped) - 40))
        print("")
    if errored:
        print("ERRORS")
        for r in errored:
            print("  %-52s %s" % (r["filename"][:52], r["reason"]))
        print("")
    print("Contact sheets: %d" % len(sheets))
    for s in sheets:
        print("  %s" % s)
    print("")
    print("Report: %s" % rpath)
    print("Strips: %s" % stripdir)


if __name__ == "__main__":
    main()