# test_ocr_confirm.py
# READ-ONLY ceiling test: does on-device set-code OCR fix the confident-but-wrong
# answers from the MobileCLIP2-S2 hybrid? No /match calls. Local PaddleOCR only.
#
# Reuses the server's hardened text parsers + set-code maps from ocr_confirm.py
# (version-independent), with a PaddleOCR-3.x reader (MKL-DNN disabled to dodge
# the paddlepaddle 3.x oneDNN/PIR bug). Candidate set reproduced from the cached
# MobileCLIP2-S2 embeddings (instant) at the operating point TAU=0.02, S=0.80.
import os
os.environ.setdefault("FLAGS_use_mkldnn", "0")   # dodge paddlepaddle 3.x oneDNN bug
import warnings; warnings.filterwarnings("ignore")
import logging; logging.disable(logging.WARNING)
import csv, json
import numpy as np, torch, open_clip
from PIL import Image, ImageEnhance

from ocr_confirm import parse_pokemon_text, parse_mtg_text, parse_ygo_text

MODEL, PRETRAINED = "MobileCLIP2-S2", "dfndr2b"
CARDSDB   = r"C:\CardsDB"
QUERIES   = "test_queries"
GT_CSV    = "groundtruth.csv"
SCORE_FLOOR = 0.65
TAU, S    = 0.02, 0.80          # operating point under test
GAMES     = ("pokemon", "mtg", "yugioh")
INDEX_CACHE = os.path.join(".cache_mobileclip_test", "cardsdb_index.json")
EMBED_DIR   = os.path.join(".cache_mobileclip_test", "embeds", f"{MODEL}__{PRETRAINED}")
dev = "cuda" if torch.cuda.is_available() else "cpu"


def game_of(s):
    return "mtg" if s.startswith("mtg-") else "yugioh" if s.startswith("ygo-") else "pokemon"


def tcg_of(sku):
    return {"mtg": "MTG", "yugioh": "YUGIOH", "pokemon": "POKEMON"}[game_of(sku)]


# ── PaddleOCR 3.x reader (lazy) ──
_ocr = None
def _reader():
    global _ocr
    if _ocr is None:
        from paddleocr import PaddleOCR
        _ocr = PaddleOCR(lang="en", use_textline_orientation=False, enable_mkldnn=False)
    return _ocr


def crop_read(img, region, min_w=600):
    w, h = img.size
    crop = img.crop((int(region[0]*w), int(region[1]*h), int(region[2]*w), int(region[3]*h)))
    cw, ch = crop.size
    if cw < min_w:
        sc = min_w / cw
        crop = crop.resize((int(cw*sc), int(ch*sc)), Image.LANCZOS)
    crop = ImageEnhance.Contrast(crop).enhance(2.0)
    crop = ImageEnhance.Sharpness(crop).enhance(2.5)
    out = []
    for r in _reader().predict(np.array(crop)):
        out.extend(r.get("rec_texts", []) or [])
    return out


# Crop regions copied from ocr_confirm.py per TCG.
_PKM_REGIONS = [(0.00, 0.80, 0.55, 1.00), (0.00, 0.75, 0.60, 0.95), (0.00, 0.85, 0.50, 0.98)]
_YGO_REGIONS = [(0.66, 0.68, 0.95, 0.79), (0.00, 0.80, 0.70, 0.92), (0.00, 0.88, 0.50, 1.00),
                (0.02, 0.93, 0.22, 1.00)]
_MTG_REGION  = (0.0, 0.80, 0.55, 1.00)


def ocr_identifier(img, tcg):
    """Return a resolved identifier string the parser is confident about, or None."""
    texts = []
    if tcg == "POKEMON":
        for reg in _PKM_REGIONS:
            texts += crop_read(img, reg)
        return parse_pokemon_text(texts), texts
    if tcg == "MTG":
        texts = crop_read(img, _MTG_REGION)
        return parse_mtg_text(texts), texts
    if tcg == "YUGIOH":
        for reg in _YGO_REGIONS:
            texts += crop_read(img, reg)
        setc, passc = parse_ygo_text(texts)
        return (setc or (f"PASSCODE:{passc}" if passc else None)), texts
    return None, texts


# ── resolve OCR identifier -> concrete SKU using the full index ──
with open(INDEX_CACHE, encoding="utf-8") as f:
    idx = json.load(f)
ALL_SKUS = idx["sku"]            # sku -> meta (all ~148k)


def resolve_sku(ident, tcg):
    """Map a parser identifier to a real SKU (direct-lookup). None if ambiguous."""
    if not ident:
        return None
    if tcg == "POKEMON":
        # hyphenated 'sv8pt5-112' is a direct sku; bare number is ambiguous -> skip
        if "-" in ident and ident in ALL_SKUS:
            return ident
        return None
    if tcg == "MTG":
        if "-" in ident:
            sku = f"mtg-{ident}"
            return sku if sku in ALL_SKUS else None
        return None
    if tcg == "YUGIOH":
        if ident.startswith("PASSCODE:"):
            pc = ident[9:]
            for sku in ALL_SKUS:
                if sku.startswith("ygo-") and sku.endswith(f"-{pc}"):
                    return sku
            return None
        # set code e.g. SDBT-EN015 -> ygo-SDBT-EN015-*
        pref = f"ygo-{ident.upper()}-"
        for sku in ALL_SKUS:
            if sku.upper().startswith(pref):
                return sku
        return None
    return None


# ── ground truth + reproduce confident set from cached embeddings ──
gt = {}
with open(GT_CSV, encoding="utf-8") as f:
    for row in csv.DictReader(f):
        if row["server_sku"]:
            try:
                if float(row["score"]) >= SCORE_FLOOR:
                    gt[row["filename"]] = row["server_sku"]
            except (TypeError, ValueError):
                pass


def expand_siblings(gt_skus):
    out = set()
    for sku in gt_skus:
        out.add(sku)
        meta = idx["sku"].get(sku)
        if not meta:
            continue
        g = meta["game"]
        out.update(idx["sets"].get(f"{g}::{meta['set_id']}", []))
        if meta["name"]:
            out.update(idx["names"].get(f"{g}::{meta['name']}", []))
    return out


db_skus, db_vecs = [], []
for sku in sorted(expand_siblings(set(gt.values()))):
    cp = os.path.join(EMBED_DIR, f"{sku}.npy")
    if os.path.isfile(cp):
        db_skus.append(sku); db_vecs.append(np.load(cp))
M = np.stack(db_vecs)
print(f"[db] {M.shape[0]} cached candidate vectors")

print(f"[model] loading {MODEL}/{PRETRAINED} (queries only) ...")
model, _, preprocess = open_clip.create_model_and_transforms(MODEL, pretrained=PRETRAINED)
model = model.to(dev).eval()


@torch.no_grad()
def embed(path):
    x = preprocess(Image.open(path).convert("RGB")).unsqueeze(0).to(dev)
    f = model.encode_image(x); f = f / f.norm(dim=-1, keepdim=True)
    return f.squeeze(0).cpu().numpy().astype(np.float32)


# confident set: gap>=TAU and top1_sim>=S
confident = []
for fn, true_sku in gt.items():
    qp = os.path.join(QUERIES, fn)
    if not os.path.isfile(qp):
        continue
    sims = M @ embed(qp)
    order = np.argsort(-sims)[:5]
    top = [(db_skus[i], float(sims[i])) for i in order]
    gap = top[0][1] - top[1][1]
    if gap >= TAU and top[0][1] >= S:
        confident.append({"fn": fn, "true": true_sku, "pred": top[0][0],
                          "top1": top[0][1], "gap": gap})
print(f"[confident] {len(confident)} on-device answers at TAU={TAU}, S={S}\n")

# ── run OCR confirm on the confident set ──
print(f"[ocr] running PaddleOCR on {len(confident)} confident queries ...")
rows = []
for c in confident:
    img = Image.open(os.path.join(QUERIES, c["fn"])).convert("RGB")
    tcg = tcg_of(c["pred"])
    ident, _texts = ocr_identifier(img, tcg)
    ocr_sku = resolve_sku(ident, tcg)
    final = ocr_sku if ocr_sku else c["pred"]
    rows.append({**c, "tcg": tcg, "ocr_ident": ident, "ocr_sku": ocr_sku,
                 "final": final,
                 "pred_correct": c["pred"] == c["true"],
                 "final_correct": final == c["true"],
                 "ocr_agrees_pred": ocr_sku == c["pred"] if ocr_sku else None})

n = len(rows)
pred_corr  = sum(r["pred_correct"] for r in rows)
final_corr = sum(r["final_correct"] for r in rows)
ocr_fired  = sum(1 for r in rows if r["ocr_sku"])
fixed   = [r for r in rows if not r["pred_correct"] and r["final_correct"]]
broke   = [r for r in rows if r["pred_correct"] and not r["final_correct"]]
# OCR disagrees with the server ground-truth label but agrees with the model:
gt_suspect = [r for r in rows
              if r["ocr_sku"] and r["ocr_sku"] == r["pred"] and r["pred"] != r["true"]]

print(f"\n========== OCR-CONFIRM CEILING — {MODEL}, TAU={TAU}/S={S} ==========")
print(f"confident answers      : {n}")
print(f"OCR produced a SKU      : {ocr_fired}/{n} ({100*ocr_fired/n:.0f}%)")
print(f"precision BEFORE OCR    : {pred_corr}/{n} = {100*pred_corr/n:.1f}%")
print(f"precision AFTER  OCR    : {final_corr}/{n} = {100*final_corr/n:.1f}%")
print(f"errors FIXED by OCR     : {len(fixed)}")
print(f"correct BROKEN by OCR   : {len(broke)}")
print(f"GT-suspect (OCR==model!=server label): {len(gt_suspect)}")

def _show(tag, rs):
    if not rs:
        return
    print(f"\n  {tag}:")
    for r in rs:
        print(f"    {r['fn']}  true(server)={r['true']}  model={r['pred']}  "
              f"ocr_ident={r['ocr_ident']!r}  ocr_sku={r['ocr_sku']}  final={r['final']}")

_show("ERRORS FIXED by OCR", fixed)
_show("CORRECT BROKEN by OCR", broke)
_show("GROUND-TRUTH SUSPECTS (OCR backs the model, not the server label)", gt_suspect)

with open("ocr_confirm_effect.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["fn", "tcg", "true", "pred", "top1", "gap",
                                      "ocr_ident", "ocr_sku", "final",
                                      "pred_correct", "final_correct"])
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k) for k in w.fieldnames})
print("\nper-query detail -> ocr_confirm_effect.csv")
