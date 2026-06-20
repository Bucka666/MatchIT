# audit_groundtruth_totals.py
# READ-ONLY. Audits all 150 groundtruth.csv labels by OCR'ing each Pokemon
# card's printed set total (the /NNN denominator, tight single crop, NO pooling)
# and flagging labels whose total contradicts their set.
#
# Authoritative printed totals are NOT available locally (profile.json has no
# total field; set_metadata.json printed_total is null for new sets). So each
# set's reference total is derived by OCR-CONSENSUS: the modal /NNN denominator
# across all cards labelled with that set. A card whose total is the minority
# outlier in its own set-group is the suspected mislabel. No server calls.
import os
os.environ.setdefault("FLAGS_use_mkldnn", "0")
import warnings; warnings.filterwarnings("ignore")
import logging; logging.disable(logging.WARNING)
import csv, re
from collections import Counter, defaultdict
import numpy as np
from PIL import Image, ImageEnhance
from ocr_confirm import _CARD_NUMBER_RE

GT_CSV   = "groundtruth.csv"
QUERIES  = "test_queries"
CARDSDB  = r"C:\CardsDB"
OCR_CACHE = "ocr_totals_cache.csv"     # fn,ocr_num,ocr_total  (so reruns are instant)
OUT_CSV  = "groundtruth_contradictions.csv"
REGIONS  = [(0.00, 0.88, 0.45, 1.00), (0.00, 0.82, 0.55, 0.97)]


def is_pokemon(sku):
    return not (sku.startswith("ygo-") or sku.startswith("mtg-"))


def set_of(sku):     # pokemon sku = {set}-{num}
    return sku.rsplit("-", 1)[0]


def num_of(sku):
    return sku.rsplit("-", 1)[1]


# ── PaddleOCR (lazy) ──
_ocr = None
def reader():
    global _ocr
    if _ocr is None:
        from paddleocr import PaddleOCR
        _ocr = PaddleOCR(lang="en", use_textline_orientation=False, enable_mkldnn=False)
    return _ocr


def read_total(fn):
    """Return (num, total) from a tight single crop, or (None, None)."""
    img = Image.open(os.path.join(QUERIES, fn)).convert("RGB")
    w, h = img.size
    for region in REGIONS:
        crop = img.crop((int(region[0]*w), int(region[1]*h),
                         int(region[2]*w), int(region[3]*h)))
        cw, ch = crop.size
        if cw < 700:
            sc = 700 / cw
            crop = crop.resize((int(cw*sc), int(ch*sc)), Image.LANCZOS)
        crop = ImageEnhance.Contrast(crop).enhance(2.0)
        crop = ImageEnhance.Sharpness(crop).enhance(2.5)
        texts = []
        for r in reader().predict(np.array(crop)):
            texts.extend(r.get("rec_texts", []) or [])
        for t in texts:
            m = _CARD_NUMBER_RE.search(t)
            if m:
                n, tot = int(m.group(1)), int(m.group(2))
                if 1 <= n <= 400 and 1 <= tot <= 400:
                    return n, tot
    return None, None


# ── load groundtruth ──
gt_rows = []
with open(GT_CSV, encoding="utf-8") as f:
    for row in csv.DictReader(f):
        if row["server_sku"]:
            gt_rows.append(row)

# ── OCR cache ──
cache = {}
if os.path.isfile(OCR_CACHE):
    with open(OCR_CACHE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            cache[r["fn"]] = (
                int(r["ocr_num"]) if r["ocr_num"] not in ("", "None") else None,
                int(r["ocr_total"]) if r["ocr_total"] not in ("", "None") else None,
            )

# ── OCR all pokemon rows (skip cached) ──
pokemon_rows = [r for r in gt_rows if is_pokemon(r["server_sku"])]
todo = [r for r in pokemon_rows if r["filename"] not in cache]
print(f"[ocr] {len(pokemon_rows)} pokemon rows; {len(cache)} cached; {len(todo)} to read")
for i, r in enumerate(todo, 1):
    fn = r["filename"]
    if not os.path.isfile(os.path.join(QUERIES, fn)):
        cache[fn] = (None, None); continue
    cache[fn] = read_total(fn)
    if i % 20 == 0:
        print(f"  ...{i}/{len(todo)}")

with open(OCR_CACHE, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f); w.writerow(["fn", "ocr_num", "ocr_total"])
    for fn, (n, t) in cache.items():
        w.writerow([fn, n, t])

# ── per-set OCR consensus total (modal denominator among clean reads) ──
set_totals = defaultdict(list)
for r in pokemon_rows:
    n, tot = cache.get(r["filename"], (None, None))
    if tot is not None:
        set_totals[set_of(r["server_sku"])].append(tot)

consensus = {}            # set -> (modal_total, n_samples)
for s, totals in set_totals.items():
    c = Counter(totals)
    modal, cnt = c.most_common(1)[0]
    consensus[s] = (modal, len(totals))

# total -> sets that consense to it (for likely_correct_set)
total_to_sets = defaultdict(list)
for s, (modal, n) in consensus.items():
    if n >= 2:
        total_to_sets[modal].append(s)


def likely_set(ocr_total, ocr_num):
    """A set whose consensus total == ocr_total and that actually has ocr_num."""
    for s in total_to_sets.get(ocr_total, []):
        if ocr_num is not None and os.path.isdir(os.path.join(CARDSDB, "pokemon", f"{s}-{ocr_num}")):
            return f"{s}-{ocr_num}"
    # fall back: name the set even if number unconfirmed
    cands = total_to_sets.get(ocr_total, [])
    return cands[0] if cands else ""


# ── classify ──
clean = contradicted = unverified = 0
contradiction_rows = []
unverified_reasons = Counter()

for r in gt_rows:
    sku = r["server_sku"]; fn = r["filename"]
    if not is_pokemon(sku):
        unverified += 1; unverified_reasons["non_pokemon"] += 1; continue
    n, tot = cache.get(fn, (None, None))
    if tot is None:
        unverified += 1; unverified_reasons["ocr_no_total"] += 1; continue
    s = set_of(sku)
    modal, nsamp = consensus.get(s, (None, 0))
    if nsamp < 2:
        unverified += 1; unverified_reasons["set_lt2_samples"] += 1; continue
    if tot == modal:
        clean += 1
    else:
        contradicted += 1
        contradiction_rows.append({
            "filename": fn,
            "server_sku": sku,
            "server_set_total": modal,        # consensus total for labelled set
            "ocr_total": tot,
            "likely_correct_set": likely_set(tot, n),
        })

# ── output ──
print(f"\n========== GROUNDTRUTH SET-TOTAL AUDIT (150 rows) ==========")
print(f"  CLEAN        : {clean}")
print(f"  CONTRADICTED : {contradicted}")
print(f"  UNVERIFIED   : {unverified}   {dict(unverified_reasons)}")
print(f"  (pokemon rows audited: {len(pokemon_rows)}; consensus sets with >=2 samples: "
      f"{sum(1 for _,(_,n) in consensus.items() if n>=2)})")

if contradiction_rows:
    print(f"\n  CONTRADICTED LABELS:")
    print(f"  {'filename':<32} {'server_sku':<16} {'set_total':>9} {'ocr':>5}  likely_correct")
    for cr in contradiction_rows:
        print(f"  {cr['filename']:<32} {cr['server_sku']:<16} "
              f"{cr['server_set_total']:>9} {cr['ocr_total']:>5}  {cr['likely_correct_set']}")

with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["filename", "server_sku", "server_set_total",
                                      "ocr_total", "likely_correct_set"])
    w.writeheader(); w.writerows(contradiction_rows)
print(f"\ncontradictions -> {OUT_CSV}")
