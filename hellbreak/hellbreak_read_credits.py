"""
hellbreak_read_credits.py
-------------------------
Step 7b. OCRs the credit strips produced by hellbreak_crop_credits.py and
extracts collector number, rarity, artist and rights-holder into a manifest.

Requires (pip only, no system binary):
    pip install rapidocr-onnxruntime

Reads:  <root>/_crops/strips/*.png
Writes: <root>/_crops/credits_extracted.csv

Confidence levels in the output:
    full     number + rarity both found
    partial  number found, rarity missing (rarity glyph is stylised and
             OCR misses it often - fill these by eye)
    none     no DOT number found - check the strip manually
    notext   no text at all - probably artwork, not a card

Run:
    python hellbreak_read_credits.py
    python hellbreak_read_credits.py --root "C:\\CardsDB\\hellbreak"
"""

import argparse
import csv
import os
import re
import sys

try:
    from rapidocr_onnxruntime import RapidOCR
except ImportError:
    try:
        from rapidocr import RapidOCR
    except ImportError:
        sys.exit("OCR engine missing. Run:  pip install rapidocr-onnxruntime")

DEFAULT_ROOT = r"C:\CardsDB\hellbreak"
RARITIES = "CURLIST"

# OCR routinely reads DOT as D0T / DQT, and glues GEN to it.
def normalise(t):
    t = t.replace("\u00a9", " ")
    t = re.sub(r"\bGEND[0O]T\b", "GEN DOT", t, flags=re.I)
    t = re.sub(r"\bD[0O]T\b", "DOT", t, flags=re.I)
    t = re.sub(r"\bGEN\b", "GEN", t, flags=re.I)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


NUM_RE = re.compile(r"\bDOT\s*([A-Z]?)\s*(\d{1,4})\b", re.I)
RAR_RE = re.compile(r"\bDOT\s*[A-Z]?\s*\d{1,4}\s+([%s])\b" % RARITIES)
ARTIST_RE = re.compile(r"([A-Z\u00c0-\u024f][A-Za-z\u00c0-\u024f'.\-]+(?:\s+[A-Z\u00c0-\u024f][A-Za-z\u00c0-\u024f'.\-]+){0,3})\s+(?:GEN\s+)?DOT\b")
RIGHTS_RE = re.compile(r"\b(20\d\d)\s+([A-Za-z.\s]{2,18}?)\s+20\d\d\s+SMI\b")


def parse(text):
    t = normalise(text)
    out = {"gen": "", "collector_number": "", "rarity": "",
           "artist": "", "rights_holder": "", "raw": t}

    if re.search(r"\bGEN\b", t):
        out["gen"] = "GEN"

    m = NUM_RE.search(t)
    if m:
        prefix, digits = m.group(1).upper(), m.group(2)
        # Main-set numbers are zero-padded to 3; B/T series are not.
        out["collector_number"] = prefix + digits if prefix else digits

    m = RAR_RE.search(t)
    if m:
        out["rarity"] = m.group(1).upper()

    m = ARTIST_RE.search(t)
    if m:
        a = m.group(1).strip()
        a = re.sub(r"\s+GEN$", "", a)
        if a.upper() not in ("SMI", "NBCU", "GEN"):
            out["artist"] = a

    m = RIGHTS_RE.search(t)
    if m:
        out["rights_holder"] = m.group(2).strip().rstrip(".")

    return out


def confidence(rec, had_text):
    if not had_text:
        return "notext"
    if rec["collector_number"] and rec["rarity"]:
        return "full"
    if rec["collector_number"]:
        return "partial"
    return "none"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_ROOT)
    args = ap.parse_args()

    stripdir = os.path.join(args.root, "_crops", "strips")
    if not os.path.isdir(stripdir):
        sys.exit("Strips folder not found: %s\nRun hellbreak_crop_credits.py first." % stripdir)

    files = sorted(f for f in os.listdir(stripdir) if f.lower().endswith(".png"))
    if not files:
        sys.exit("No strips found in %s" % stripdir)

    print("Loading OCR engine (first run downloads models, ~15 MB)...")
    ocr = RapidOCR()
    print("Reading %d strips...\n" % len(files))

    rows = []
    for i, fn in enumerate(files, 1):
        path = os.path.join(stripdir, fn)
        try:
            res, _ = ocr(path)
        except Exception as e:
            res = None
            print("  ! OCR failed on %s: %s" % (fn, e))
        text = " ".join(r[1] for r in res) if res else ""
        rec = parse(text)
        conf = confidence(rec, bool(text.strip()))
        rows.append({
            "strip": fn,
            "confidence": conf,
            "gen_marker": rec["gen"],
            "collector_number": rec["collector_number"],
            "rarity": rec["rarity"],
            "artist": rec["artist"],
            "rights_holder": rec["rights_holder"],
            "ocr_raw": rec["raw"][:200],
        })
        if i % 25 == 0:
            print("  %d/%d" % (i, len(files)))

    outpath = os.path.join(args.root, "_crops", "credits_extracted.csv")
    with open(outpath, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # ---- summary ----
    counts = {}
    for r in rows:
        counts[r["confidence"]] = counts.get(r["confidence"], 0) + 1

    print("")
    print("RESULTS")
    for k in ("full", "partial", "none", "notext"):
        if k in counts:
            print("  %-8s %4d" % (k, counts[k]))
    print("  %-8s %4d" % ("TOTAL", len(rows)))

    nums = [r["collector_number"] for r in rows if r["collector_number"]]
    main_nums = sorted({int(n) for n in nums if n.isdigit()})
    other = sorted({n for n in nums if not n.isdigit()})
    if main_nums:
        print("")
        print("MAIN-SET NUMBERS FOUND: %d unique, range %03d-%03d"
              % (len(main_nums), main_nums[0], main_nums[-1]))
        missing = [n for n in range(main_nums[0], main_nums[-1] + 1) if n not in main_nums]
        print("  gaps in that range: %d" % len(missing))
    if other:
        print("")
        print("NON-STANDARD NUMBERS: %s" % ", ".join(other))

    rars = {}
    for r in rows:
        if r["rarity"]:
            rars[r["rarity"]] = rars.get(r["rarity"], 0) + 1
    if rars:
        print("")
        print("RARITIES: " + "  ".join("%s=%d" % (k, rars[k]) for k in sorted(rars)))

    holders = {}
    for r in rows:
        h = r["rights_holder"]
        if h:
            holders[h] = holders.get(h, 0) + 1
    if holders:
        print("")
        print("RIGHTS HOLDERS")
        for h in sorted(holders, key=lambda x: -holders[x]):
            print("  %-20s %d" % (h, holders[h]))

    print("")
    print("Written: %s" % outpath)
    print("Sort by 'confidence' and fix 'partial'/'none' rows by eye.")


if __name__ == "__main__":
    main()