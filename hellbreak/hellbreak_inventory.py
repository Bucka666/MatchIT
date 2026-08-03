"""
hellbreak_inventory.py
----------------------
Walks the Hellbreak image folders, records geometry, normalises names so the
official-gallery pull and the community pull can be matched up, and reports
what you actually have.

Run:
    python hellbreak_inventory.py
    python hellbreak_inventory.py --root "C:\\CardsDB\\hellbreak"
"""

import argparse
import csv
import os
import re
import sys

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow not installed. Run:  pip install pillow")

DEFAULT_ROOT = r"C:\CardsDB\hellbreak"
OUT_CSV = "hellbreak_inventory.csv"
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}

UUID_SUFFIX = re.compile(r"_[0-9a-f]{8}-[0-9a-f-]{20,}$", re.I)
MANUAL_PREFIX = re.compile(r"^(GEN_)?DOT_([A-Z]?\d+)_", re.I)
FACE = re.compile(r"(lurking|unleashed)", re.I)


def match_key(stem):
    """Normalise a filename stem so the same card from either source collides."""
    s = UUID_SUFFIX.sub("", stem)
    s = MANUAL_PREFIX.sub("", s)
    s = s.replace("_TT_", "_").replace("_KBL_", "_").replace("_Scourge_", "_")
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", s)
    s = re.sub(r"[^A-Za-z0-9]+", "", s)
    return s.upper()


def printed_number(stem):
    """Pull the collector number back out of a manually-renamed file, if present."""
    m = MANUAL_PREFIX.match(stem)
    if not m:
        return "", ""
    return ("GEN" if m.group(1) else ""), m.group(2).upper()


def orientation(w, h):
    r = w / h if h else 0
    if r < 0.9:
        return "portrait"
    if r > 1.1:
        return "landscape"
    return "square-ish"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--out", default=OUT_CSV)
    args = ap.parse_args()

    if not os.path.isdir(args.root):
        sys.exit("Root not found: %s" % args.root)

    rows = []
    for dirpath, _dirs, files in os.walk(args.root):
        source = os.path.relpath(dirpath, args.root)
        if source == ".":
            source = "(root)"
        for fn in sorted(files):
            stem, ext = os.path.splitext(fn)
            if ext.lower() not in IMG_EXT:
                continue
            path = os.path.join(dirpath, fn)
            try:
                with Image.open(path) as im:
                    w, h = im.size
                    mode = im.mode
            except Exception as e:
                print("  ! unreadable: %s (%s)" % (fn, e))
                continue
            gen, num = printed_number(stem)
            face = FACE.search(stem)
            rows.append({
                "match_key": match_key(stem),
                "filename": fn,
                "source": source,
                "width": w,
                "height": h,
                "aspect": round(w / h, 3) if h else 0,
                "orientation": orientation(w, h),
                "mode": mode,
                "kb": round(os.path.getsize(path) / 1024, 1),
                "gen_marker": gen,
                "collector_number": num,
                "face": face.group(1).lower() if face else "",
            })

    if not rows:
        sys.exit("No images found under %s" % args.root)

    rows.sort(key=lambda r: (r["match_key"], r["source"]))
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    by_source = {}
    for r in rows:
        by_source.setdefault(r["source"], []).append(r)

    keys_by_source = {s: {r["match_key"] for r in rs} for s, rs in by_source.items()}
    all_keys = set(r["match_key"] for r in rows)

    print("")
    print("FILES BY FOLDER")
    for s in sorted(by_source):
        print("  %-24s %4d files" % (s, len(by_source[s])))
    print("  %-24s %4d files" % ("TOTAL", len(rows)))

    print("")
    print("UNIQUE CARDS (by normalised name): %d" % len(all_keys))

    srcs = sorted(keys_by_source)
    if len(srcs) >= 2:
        print("")
        print("OVERLAP")
        for i in range(len(srcs)):
            for j in range(i + 1, len(srcs)):
                a, b = srcs[i], srcs[j]
                both = keys_by_source[a] & keys_by_source[b]
                print("  in both %s and %s: %d" % (a, b, len(both)))
                only_b = keys_by_source[b] - keys_by_source[a]
                print("  only in %s: %d" % (b, len(only_b)))

    print("")
    print("ORIENTATION")
    for o in ("portrait", "landscape", "square-ish"):
        sel = [r for r in rows if r["orientation"] == o]
        if sel:
            print("  %-12s %4d" % (o, len(sel)))
            if o != "portrait":
                for r in sel:
                    print("      %s  (%dx%d)" % (r["filename"], r["width"], r["height"]))

    numbered = [r for r in rows if r["collector_number"]]
    print("")
    print("COLLECTOR NUMBERS ALREADY IN FILENAMES: %d" % len(numbered))
    for r in sorted(numbered, key=lambda r: r["collector_number"]):
        prefix = r["gen_marker"] + " " if r["gen_marker"] else ""
        print("  %s%-5s %s" % (prefix, r["collector_number"], r["filename"]))

    faces = [r for r in rows if r["face"]]
    if faces:
        print("")
        print("DOUBLE-FACED CARDS: %d faces" % len(faces))

    print("")
    print("Written: %s" % os.path.abspath(args.out))


if __name__ == "__main__":
    main()