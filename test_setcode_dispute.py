# test_setcode_dispute.py
# READ-ONLY. Targeted single-crop set-code read to resolve whether the server
# ground-truth labels for the 2 disputed cards are correct, by comparing against
# trusted reference cards from the same sets. No pooling, no server calls.
import os
os.environ.setdefault("FLAGS_use_mkldnn", "0")
import warnings; warnings.filterwarnings("ignore")
import logging; logging.disable(logging.WARNING)
import re
import numpy as np
from PIL import Image, ImageEnhance
from ocr_confirm import _PKM_SETCODE_MAP, _CARD_NUMBER_RE

# printed-code -> set, restricted to the sets in play (reverse of map for display)
_SET_TO_CODE = {v: k for k, v in _PKM_SETCODE_MAP.items()}

CARDS = [
    ("DISPUTED", "PXL_20260614_151551049.jpg", "sv8pt5-112"),
    ("DISPUTED", "PXL_20260614_150120399.jpg", "sv8pt5-117"),
    ("ref me1",    "PXL_20260614_142644963.jpg", "me1-84"),
    ("ref me1",    "PXL_20260614_150145533.jpg", "me1-18"),
    ("ref me2pt5", "PXL_20260614_142509362.jpg", "me2pt5-28"),
    ("ref me2pt5", "PXL_20260614_150103783.jpg", "me2pt5-169"),
    ("ref sv8pt5", "PXL_20260614_150023502.jpg", "sv8pt5-16"),
    ("ref sv8pt5", "PXL_20260614_150058376.jpg", "sv8pt5-77"),
]

# Tight single regions over the bottom-left set-code / number zone (no pooling).
REGIONS = [(0.00, 0.88, 0.45, 1.00), (0.00, 0.82, 0.55, 0.97)]

_ocr = None
def reader():
    global _ocr
    if _ocr is None:
        from paddleocr import PaddleOCR
        _ocr = PaddleOCR(lang="en", use_textline_orientation=False, enable_mkldnn=False)
    return _ocr


def read_region(img, region, min_w=700):
    w, h = img.size
    crop = img.crop((int(region[0]*w), int(region[1]*h), int(region[2]*w), int(region[3]*h)))
    cw, ch = crop.size
    if cw < min_w:
        sc = min_w / cw
        crop = crop.resize((int(cw*sc), int(ch*sc)), Image.LANCZOS)
    crop = ImageEnhance.Contrast(crop).enhance(2.0)
    crop = ImageEnhance.Sharpness(crop).enhance(2.5)
    out = []
    for r in reader().predict(np.array(crop)):
        out.extend(r.get("rec_texts", []) or [])
    return out


def detect(texts):
    """Return (set_code_token, mapped_set, number) found in these texts."""
    joined = " ".join(texts).upper()
    found_set, found_code = None, None
    for code, db_id in _PKM_SETCODE_MAP.items():
        if re.search(r'\b[A-Z]?' + re.escape(code) + r'[A-Z]?\b', joined):
            found_code, found_set = code, db_id
            break
    num = None
    for t in texts:
        m = _CARD_NUMBER_RE.search(t)
        if m and int(m.group(1)) <= 400 and int(m.group(2)) <= 400:
            num = f"{int(m.group(1))}/{int(m.group(2))}"
            break
    return found_code, found_set, num


print("Reading set codes (single tight crop, no pooling)\n")
print(f"{'card':<12} {'server label':<14} {'expect':<5} | read_code mapped_set  number   raw")
print("-" * 100)
for tag, fn, label in CARDS:
    setp = label.rsplit("-", 1)[0]
    expect = _SET_TO_CODE.get(setp, "?")
    img = Image.open(os.path.join("test_queries", fn)).convert("RGB")
    best = (None, None, None, [])
    for reg in REGIONS:
        texts = read_region(img, reg)
        code, mset, num = detect(texts)
        if code:                      # prefer a region that found a set code
            best = (code, mset, num, texts); break
        if num and best[2] is None:
            best = (code, mset, num, texts)
    code, mset, num, texts = best
    verdict = ""
    if code:
        verdict = "MATCHES label" if mset == setp else f"CONTRADICTS -> reads {mset}"
    raw = " | ".join(t for t in texts if t.strip())[:60]
    print(f"{tag:<12} {label:<14} {expect:<5} | {str(code):<9} {str(mset):<11} "
          f"{str(num):<8} {verdict}")
    print(f"{'':<34}raw: {raw}")
