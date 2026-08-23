"""
Build cardID -> collector-number maps for downloaded Japanese SWSH Pokemon sets.

For each jpn-{setcode} folder in CardsDB, crops the bottom-left corner of every
card image (where the set prints "s12a 001/172" style collector numbers),
OCRs it, and writes a {cardID: "NNN"} map to jp_cardid_maps/{setcode}_map.json.

Two-tier OCR:
  1. Tesseract (fast) on an upscaled/contrast-boosted crop, matched against
     the literal "NNN/TTT" pattern. Reliable on non-holo cards.
  2. EasyOCR fallback for images Tesseract can't read (mainly holo/foil
     rarity variants, whose textured backgrounds defeat Tesseract). EasyOCR
     reads the digits correctly but often mangles the "/" into a stray
     digit, so instead of matching "/" literally we anchor on the set's
     known total (TTT) and take the 3 digits immediately preceding it.

The anchor total is derived per-set from the mode of TTT values seen among
successful Tesseract reads in that same set (set_metadata.json's
printed_total field is sparse/missing for most of these sets, so it is used
only as a cross-check when present, never as the sole source).
"""
import argparse
import json
import os
import re
import sys
from collections import Counter

CARDSDB_ROOT = r"C:\CardsDB"
OUTPUT_DIR = r"C:\MatchIT\jp_cardid_maps"
SET_METADATA_PATH = r"C:\MatchIT\set_metadata.json"
TESSERACT_CMD = r"C:\Users\c_a_b\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"

NUM_RE = re.compile(r"\b(\d{1,3})\s*/\s*(\d{1,3})\b")

CROP_WIDTH_FRAC = 0.40
CROP_HEIGHT_FRAC = 0.15

# Max multiple of the anchor total a single-engine (uncorroborated) read is
# trusted at. See the plausibility check in process_set() for the evidence
# behind this figure.
PLAUSIBILITY_RATIO = 3


def load_set_metadata():
    try:
        with open(SET_METADATA_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def discover_sets():
    if not os.path.isdir(CARDSDB_ROOT):
        return []
    names = []
    for entry in sorted(os.listdir(CARDSDB_ROOT)):
        full = os.path.join(CARDSDB_ROOT, entry)
        if entry.startswith("jpn-s") and os.path.isdir(full):
            names.append(entry[len("jpn-"):])
    return names


def init_tesseract():
    import pytesseract

    if not os.path.exists(TESSERACT_CMD):
        print(
            f"ERROR: Tesseract binary not found at {TESSERACT_CMD}\n"
            "Install it (e.g. `winget install --id UB-Mannheim.TesseractOCR -e`) "
            "or update TESSERACT_CMD in this script to point at your install.",
            file=sys.stderr,
        )
        sys.exit(1)
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    return pytesseract


_easyocr_reader = None


def get_easyocr_reader():
    global _easyocr_reader
    if _easyocr_reader is None:
        import easyocr

        print("  (loading EasyOCR model for fallback pass...)")
        _easyocr_reader = easyocr.Reader(["en"], gpu=True)
    return _easyocr_reader


def crop_bottom_left(img):
    w, h = img.size
    crop_w = int(w * CROP_WIDTH_FRAC)
    crop_h = int(h * CROP_HEIGHT_FRAC)
    return img.crop((0, h - crop_h, crop_w, h))


def preprocess_for_tesseract(crop, Image, ImageOps):
    c = crop.resize((crop.width * 3, crop.height * 3), Image.LANCZOS)
    g = ImageOps.grayscale(c)
    g = ImageOps.autocontrast(g, cutoff=2)
    return g


def tesseract_pass(crop, pytesseract, Image, ImageOps):
    # PSM 3 (fully automatic page segmentation) only finds a text block at
    # all on clean, non-holo crops, and its reads have been 100% reliable
    # in testing. PSM 6 (force a single uniform block) will still produce
    # *a* match on noisy holo/foil backgrounds, but it hallucinates digits
    # there (observed: "1" misread as "7") -- confidently wrong, not just
    # unreliable. So PSM 6 is deliberately NOT used as a trusted source;
    # anything PSM 3 can't read falls through to the EasyOCR anchor pass.
    p = preprocess_for_tesseract(crop, Image, ImageOps)
    text = pytesseract.image_to_string(p, config="--psm 3")
    m = NUM_RE.search(text)
    if m:
        return m.group(1).zfill(3), m.group(2).zfill(3)
    return None


def parse_with_anchor(digits_only, total_str):
    # NNN always sits at the START of the text immediately preceding the
    # anchor total; any junk digits from a mis-OCR'd "/" (1 or more chars)
    # trail AFTER it, right before the anchor. So always take the first
    # (up to) 3 characters of the preceding text, never the last.
    idx = digits_only.rfind(total_str)
    if idx == -1:
        return None
    remaining = digits_only[:idx]
    if not remaining:
        return None
    return remaining[:3].zfill(3)


def easyocr_pass(crop, total_str, Image, np):
    reader = get_easyocr_reader()
    c_big = crop.resize((crop.width * 2, crop.height * 2), Image.LANCZOS)
    arr = np.array(c_big)
    results = reader.readtext(arr, detail=1)
    for _, text, _ in results:
        digits_only = "".join(ch for ch in text if ch.isdigit())
        if total_str not in digits_only:
            continue
        nnn = parse_with_anchor(digits_only, total_str)
        if nnn:
            return nnn
    return None


def process_set(setcode, set_meta):
    from PIL import Image, ImageOps
    import numpy as np
    import pytesseract

    set_dir = os.path.join(CARDSDB_ROOT, f"jpn-{setcode}")
    if not os.path.isdir(set_dir):
        print(f"  SKIP: {set_dir} does not exist")
        return

    files = sorted(f for f in os.listdir(set_dir) if f.lower().endswith(".jpg"))
    if not files:
        print(f"  SKIP: no .jpg files in {set_dir}")
        return

    # --- Pass 1: Tesseract fast path on every image ---
    tesseract_results = {}  # cardID -> (nnn, ttt), NOT yet validated against the anchor
    crops = {}  # cardID -> materialized crop, kept for every image in case
                # pass 1's tesseract read has to be demoted to the EasyOCR
                # fallback below (unreadable OR its ttt disagrees with the
                # set-wide anchor)
    open_failed = []
    for fname in files:
        cardid = os.path.splitext(fname)[0]
        path = os.path.join(set_dir, fname)
        try:
            img = Image.open(path)
        except Exception as e:
            print(f"  WARN: could not open {fname}: {e}")
            open_failed.append(cardid)
            continue
        crop = crop_bottom_left(img)
        # Force full pixel materialization now: `crop` is a lazy view into
        # `img`'s decoder state, and `img` goes out of scope once this loop
        # iteration ends. Without .load(), a later pass (which runs much
        # later, after all images have been opened) can read back corrupted
        # pixel data for entries queued early in this pass.
        crop.load()
        crops[cardid] = crop
        result = tesseract_pass(crop, pytesseract, Image, ImageOps)
        if result:
            tesseract_results[cardid] = result
        img.close()

    # --- Derive the anchor total (TTT) for this set ---
    # Robust to individual misreads: even on cards where Tesseract garbles
    # the collector number, TTT is far more often read correctly than not,
    # so the mode across all candidate reads settles on the true value.
    ttt_votes = Counter(ttt for _, ttt in tesseract_results.values())
    anchor_total = None
    if ttt_votes:
        anchor_total = ttt_votes.most_common(1)[0][0]

    # A regex match alone isn't proof of a correct Tesseract read: on
    # textured/holo backgrounds Tesseract can misread a digit ("1"->"7",
    # "0"->"9" observed) in the numerator ALONE while the denominator still
    # reads correctly (e.g. true "105/100" read as "705/100") -- a
    # confidently wrong match that a TTT-only check can't catch, since TTT
    # matches the anchor either way. So: discard any Tesseract match whose
    # TTT disagrees with the anchor outright (definitely wrong), then cross
    # -check every remaining candidate against an independent EasyOCR
    # anchor-based read. EasyOCR has its own occasional misreads too (also
    # observed a "1"->"7" digit substitution), so neither engine is trusted
    # alone -- a value is only accepted when both agree, or only one of the
    # two produced any reading at all. When they actively disagree, there's
    # no way to know which is right, so the card is left as None rather
    # than guessing.
    if anchor_total:
        for cardid, (nnn, ttt) in list(tesseract_results.items()):
            if ttt != anchor_total:
                del tesseract_results[cardid]

    meta_printed_total = None
    meta = set_meta.get(f"jpn-{setcode}")
    if meta and meta.get("printed_total"):
        meta_printed_total = str(meta["printed_total"])
        if anchor_total and meta_printed_total != anchor_total:
            print(
                f"  NOTE: derived anchor total {anchor_total} differs from "
                f"set_metadata.json printed_total {meta_printed_total}; "
                f"using the derived value (majority of {len(tesseract_results)} "
                f"successful reads)."
            )
        if anchor_total is None:
            anchor_total = meta_printed_total

    # --- Pass 2: EasyOCR anchor cross-check on every image ---
    mapping = {}
    method = {}
    failed = []
    disagreements = []

    all_cardids = list(crops.keys()) + open_failed
    for cardid in all_cardids:
        crop = crops.get(cardid)
        tess = tesseract_results.get(cardid)
        tess_nnn = tess[0] if tess else None
        easy_nnn = easyocr_pass(crop, anchor_total, Image, np) if (crop is not None and anchor_total) else None

        if tess_nnn and easy_nnn:
            if tess_nnn == easy_nnn:
                mapping[cardid] = tess_nnn
                method[cardid] = "agreed"
            else:
                mapping[cardid] = None
                method[cardid] = "disagreement"
                failed.append(cardid)
                disagreements.append((cardid, tess_nnn, easy_nnn))
        elif tess_nnn or easy_nnn:
            # Single-engine read, no independent corroboration. Across every
            # set validated so far, legitimate secret-rare numbers never
            # exceeded ~1.75x the base printed total (e.g. 326/190), while
            # every observed single-digit-garbled misread landed at 4x-11x+
            # (e.g. true "078" read as "787"). A single uncorroborated
            # engine can still hallucinate a digit, so cap how far past the
            # anchor an unconfirmed read is trusted; anything wilder is
            # unreliable and left as None rather than guessed.
            nnn, origin = (tess_nnn, "tesseract-only") if tess_nnn else (easy_nnn, "easyocr-only")
            if anchor_total and int(nnn) > int(anchor_total) * PLAUSIBILITY_RATIO:
                mapping[cardid] = None
                method[cardid] = "implausible"
                failed.append(cardid)
            else:
                mapping[cardid] = nnn
                method[cardid] = origin
        else:
            mapping[cardid] = None
            method[cardid] = "failed"
            failed.append(cardid)

    # --- Duplicate / contiguity analysis ---
    number_counts = Counter(v for v in mapping.values() if v is not None)
    duplicates = {num: cnt for num, cnt in number_counts.items() if cnt > 1}

    contiguous = None
    gaps = []
    if anchor_total:
        total_n = int(anchor_total)
        present = {int(v) for v in mapping.values() if v is not None}
        gaps = [n for n in range(1, total_n + 1) if n not in present]
        contiguous = len(gaps) == 0
    extended = sorted(
        {int(v) for v in mapping.values() if v is not None and anchor_total and int(v) > int(anchor_total)}
    )

    # --- Write output ---
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, f"{setcode}_map.json")
    out_data = dict(mapping)
    out_data["failed"] = failed
    if disagreements:
        out_data["disagreements"] = {
            cardid: {"tesseract": t, "easyocr": e} for cardid, t, e in disagreements
        }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out_data, f, ensure_ascii=False, indent=2)

    # --- Summary ---
    print(f"  Set: {setcode}")
    print(f"    Total images processed: {len(files)}")
    print(f"    Successfully mapped:    {len(files) - len(failed)}")
    print(f"    Failed (OCR None):      {len(failed)}"
          + (f"  (of which {len(disagreements)} were Tesseract/EasyOCR disagreements)" if disagreements else ""))
    if anchor_total:
        print(f"    Anchor total (TTT):     {anchor_total}"
              + (f"  (metadata says {meta_printed_total})" if meta_printed_total and meta_printed_total != anchor_total else ""))
    else:
        print("    Anchor total (TTT):     UNKNOWN (no Tesseract successes and no metadata; all failures left as None)")
    print(f"    Duplicate numbers:      {len(duplicates)} distinct value(s) appear more than once"
          f" (expected — parallel/foil variants share a printed number)")
    if contiguous is not None:
        print(f"    Base range 1-{anchor_total} contiguous: {contiguous}"
              + ("" if contiguous else f"  (missing: {gaps})"))
    if extended:
        print(f"    Numbers beyond {anchor_total} (secret/rare variants): {len(extended)}"
              f" (range {extended[0]}-{extended[-1]})")
    print(f"    Wrote: {out_path}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Map JP cardIDs to collector numbers via OCR.")
    parser.add_argument(
        "--sets",
        type=str,
        default=None,
        help="Comma-separated set codes (e.g. s12a,s12). Default = all jpn-* sets found in CardsDB.",
    )
    args = parser.parse_args()

    init_tesseract()

    set_meta = load_set_metadata()

    if args.sets:
        sets = [s.strip() for s in args.sets.split(",") if s.strip()]
    else:
        sets = discover_sets()
        print(f"No --sets given; auto-discovered {len(sets)} jpn-* sets in {CARDSDB_ROOT}:")
        print(f"  {', '.join(sets)}")
        print()

    for setcode in sets:
        process_set(setcode, set_meta)


if __name__ == "__main__":
    main()
