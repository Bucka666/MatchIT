# analyze_confident.py
# READ-ONLY. No server calls, no OCR. Recomputes per-query top-5/gap for
# MobileCLIP-S2/datacompdr using the CACHED DB embeddings (instant) + re-embeds
# the 150 queries, then compares confident-WRONG vs confident-CORRECT signal
# distributions at given TAUs. Answers: do wrong confident answers sit at lower
# absolute top1 similarity than right ones (=> --min-sim can help), or overlap?
import os, csv, json, statistics as stats
import numpy as np, torch, open_clip
from PIL import Image

MODEL, PRETRAINED = "MobileCLIP-S2", "datacompdr"
CARDSDB   = r"C:\CardsDB"
QUERIES   = "test_queries"
GT_CSV    = "groundtruth.csv"
BUCKETS   = "buckets.csv"
SCORE_FLOOR = 0.65
TAUS      = [0.08, 0.02]
GAMES     = ("pokemon", "mtg", "yugioh")
INDEX_CACHE = os.path.join(".cache_mobileclip_test", "cardsdb_index.json")
EMBED_DIR   = os.path.join(".cache_mobileclip_test", "embeds", f"{MODEL}__{PRETRAINED}")
CORRECT_SAMPLE = 18   # how many correct-confident rows to print verbatim

dev = "cuda" if torch.cuda.is_available() else "cpu"


def game_of(s):
    return "mtg" if s.startswith("mtg-") else "yugioh" if s.startswith("ygo-") else "pokemon"


def find_front_image(sku):
    for g in [game_of(sku)] + [x for x in GAMES if x != game_of(sku)]:
        p = os.path.join(CARDSDB, g, sku, "front.png")
        if os.path.isfile(p):
            return p
    return None


# ── ground truth + buckets ──
gt = {}
with open(GT_CSV, encoding="utf-8") as f:
    for row in csv.DictReader(f):
        if row["server_sku"]:
            try:
                if float(row["score"]) >= SCORE_FLOOR:
                    gt[row["filename"]] = row["server_sku"]
            except (TypeError, ValueError):
                pass
bucket_of = {}
if os.path.isfile(BUCKETS):
    with open(BUCKETS, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            bucket_of[row["filename"]] = (row.get("bucket") or "untagged").strip() or "untagged"

# ── candidate DB from cached vectors (must match the prior run) ──
with open(INDEX_CACHE, encoding="utf-8") as f:
    idx = json.load(f)


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


needed = expand_siblings(set(gt.values()))
db_skus, db_vecs = [], []
for sku in sorted(needed):
    cp = os.path.join(EMBED_DIR, f"{sku}.npy")
    if os.path.isfile(cp):                       # cache only — no re-embed of DB
        db_skus.append(sku)
        db_vecs.append(np.load(cp))
M = np.stack(db_vecs)
print(f"[db] {M.shape[0]} cached candidate vectors loaded")

# ── model only needed to embed the 150 query photos ──
print(f"[model] loading {MODEL}/{PRETRAINED} on {dev} (queries only) ...")
model, _, preprocess = open_clip.create_model_and_transforms(MODEL, pretrained=PRETRAINED)
model = model.to(dev).eval()


@torch.no_grad()
def embed_image(path):
    im = Image.open(path).convert("RGB")
    x = preprocess(im).unsqueeze(0).to(dev)
    f = model.encode_image(x)
    f = f / f.norm(dim=-1, keepdim=True)
    return f.squeeze(0).cpu().numpy().astype(np.float32)


# ── recompute per-query top-5 / gap ──
results = []
for fn, true_sku in gt.items():
    qp = os.path.join(QUERIES, fn)
    if not os.path.isfile(qp):
        continue
    sims = M @ embed_image(qp)
    order = np.argsort(-sims)[:5]
    top = [(db_skus[i], float(sims[i])) for i in order]
    gap = top[0][1] - top[1][1] if len(top) > 1 else 1.0
    results.append({
        "fn": fn, "true": true_sku, "pred": top[0][0],
        "top1_sim": top[0][1], "gap": gap,
        "hit1": top[0][0] == true_sku,
        "true_in_top5": any(s == true_sku for s, _ in top),
        "bucket": bucket_of.get(fn, "untagged"),
        "top5_skus": [s for s, _ in top],
    })
print(f"[q] {len(results)} queries recomputed\n")


def _dist(label, vals):
    if not vals:
        return f"  {label:18s}: (none)"
    return (f"  {label:18s}: n={len(vals):2d}  min={min(vals):.4f}  "
            f"med={stats.median(vals):.4f}  mean={sum(vals)/len(vals):.4f}  max={max(vals):.4f}")


for tau in TAUS:
    confident = [r for r in results if r["gap"] >= tau]
    wrong   = [r for r in confident if not r["hit1"]]
    correct = [r for r in confident if r["hit1"]]
    print("=" * 78)
    print(f"TAU = {tau}   |  confident={len(confident)}  correct={len(correct)}  WRONG={len(wrong)}")
    print("=" * 78)

    print(f"\n  CONFIDENT-BUT-WRONG ({len(wrong)}) -- the real on-device errors:")
    print(f"  {'true':<26} {'pred':<26} {'top1':>7} {'gap':>7} {'t5?':>4} bucket")
    for r in sorted(wrong, key=lambda x: x["top1_sim"]):
        print(f"  {r['true']:<26.26} {r['pred']:<26.26} {r['top1_sim']:>7.4f} "
              f"{r['gap']:>7.4f} {('Y' if r['true_in_top5'] else 'n'):>4} {r['bucket']}")

    print(f"\n  CORRECT-CONFIDENT sample (up to {CORRECT_SAMPLE} of {len(correct)}), "
          f"lowest top1 first:")
    print(f"  {'true=pred':<26} {'top1':>7} {'gap':>7} bucket")
    for r in sorted(correct, key=lambda x: x["top1_sim"])[:CORRECT_SAMPLE]:
        print(f"  {r['true']:<26.26} {r['top1_sim']:>7.4f} {r['gap']:>7.4f} {r['bucket']}")

    print(f"\n  SIGNAL DISTRIBUTIONS at TAU={tau}:")
    print(_dist("WRONG top1_sim",   [r["top1_sim"] for r in wrong]))
    print(_dist("CORRECT top1_sim", [r["top1_sim"] for r in correct]))
    print(_dist("WRONG gap",        [r["gap"] for r in wrong]))
    print(_dist("CORRECT gap",      [r["gap"] for r in correct]))

    # separability verdict for --min-sim
    if wrong and correct:
        w_max = max(r["top1_sim"] for r in wrong)
        c_min = min(r["top1_sim"] for r in correct)
        w_med = stats.median(r["top1_sim"] for r in wrong)
        c_med = stats.median(r["top1_sim"] for r in correct)
        overlap = w_max >= c_min
        # how many correct would a min-sim set just above the wrong-max keep?
        thresh = w_max
        kept_correct = sum(1 for r in correct if r["top1_sim"] > thresh)
        print(f"\n  --min-sim separability: WRONG top1 max={w_max:.4f}  "
              f"CORRECT top1 min={c_min:.4f}  -> "
              f"{'OVERLAP (min-sim alone cannot cleanly separate)' if overlap else 'CLEAN GAP (min-sim can separate!)'}")
        print(f"     medians: WRONG={w_med:.4f} vs CORRECT={c_med:.4f} "
              f"(delta={c_med - w_med:+.4f})")
        print(f"     a min-sim just above worst error ({thresh:.4f}) would keep "
              f"{kept_correct}/{len(correct)} correct-confident answers "
              f"({100*kept_correct/len(correct):.0f}% of coverage) and drop all {len(wrong)} errors")
    print()
