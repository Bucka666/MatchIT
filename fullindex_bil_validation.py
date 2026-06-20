# fullindex_bil_validation.py — UN-CONFOUNDING STEP (no browser, read-only).
# Re-runs the original Python bil_preprocess validation but matches against the
# FULL gs-ondevice-v1 index (102,140 cards) instead of the small sibling set.
# Goal: isolate the candidate-set-size variable from the later browser test.
#
# Gate predicate is reused VERBATIM from test_mobileclip.py:330-336:
#     confident = gap >= TAU and top1_sim >= min_sim   (+ YGO excluded)
# with the operating point TAU=0.02, min_sim=0.80.
import os, csv, json
os.environ.setdefault("FLAGS_use_mkldnn", "0")
import warnings; warnings.filterwarnings("ignore")
import numpy as np, torch, open_clip
from PIL import Image

MODEL, PRE = "MobileCLIP2-S2", "dfndr2b"
QUERIES, GT = "test_queries", "groundtruth.csv"
BUCKETS = "buckets.csv"
IDX_DIR = "ondevice_index_v1"
SCORE_FLOOR = 0.65
TAU, S = 0.02, 0.80            # operating point (test_mobileclip.py)
dev = "cuda" if torch.cuda.is_available() else "cpu"


def game_of(sku):
    return "yugioh" if sku.startswith("ygo-") else "mtg" if sku.startswith("mtg-") else "pokemon"


def bil_preprocess(img):
    """Plain bilinear (no antialias) resize shortest->256, center crop 256, [0,1].
    Identical to build_ondevice_index.py / audit_browser_preproc.py."""
    W, H = img.size
    s = 256 / min(W, H)
    nw, nh = round(W * s), round(H * s)
    im = img.resize((nw, nh), Image.BILINEAR)
    l = (nw - 256) // 2; t = (nh - 256) // 2
    im = im.crop((l, t, l + 256, t + 256))
    return np.asarray(im, dtype=np.float32) / 255.


# ---- load FULL index ----
M = np.load(os.path.join(IDX_DIR, "vectors_f16.npy")).astype(np.float32)   # (102140,512), L2-normed
skus = json.load(open(os.path.join(IDX_DIR, "skus.json"), encoding="utf-8"))
sku_set = set(skus)
print(f"[index] {M.shape[0]} cards x {M.shape[1]} dims (full gs-ondevice-v1)", flush=True)

# ---- ground truth (same filter as original run) ----
gt = {}
for r in csv.DictReader(open(GT, encoding="utf-8")):
    if r["server_sku"]:
        try:
            if float(r["score"]) >= SCORE_FLOOR:
                gt[r["filename"]] = r["server_sku"]
        except (TypeError, ValueError):
            pass
bucket_of = {}
if os.path.isfile(BUCKETS):
    for r in csv.DictReader(open(BUCKETS, encoding="utf-8")):
        bucket_of[r["filename"]] = (r.get("bucket") or "untagged").strip() or "untagged"
print(f"[gt] {len(gt)} trusted query->sku pairs", flush=True)

# index-content sanity: are the GT true-skus even present in the full index?
gt_present = {fn: (sku in sku_set) for fn, sku in gt.items()}
miss_pk_mtg = [(fn, sku) for fn, sku in gt.items()
               if game_of(sku) != "yugioh" and not gt_present[fn]]
print(f"[index-content] GT true-sku present in index: "
      f"{sum(gt_present.values())}/{len(gt)}  "
      f"(pokemon+mtg GT missing from index: {len(miss_pk_mtg)})", flush=True)
for fn, sku in miss_pk_mtg:
    print(f"    MISSING from index: {fn} -> {sku} [{game_of(sku)}]", flush=True)

# ---- embed queries with bil_preprocess ----
print(f"[model] {MODEL}/{PRE} on {dev} ...", flush=True)
model, _, _ = open_clip.create_model_and_transforms(MODEL, pretrained=PRE)
model = model.to(dev).eval()

@torch.no_grad()
def embed(path):
    arr = bil_preprocess(Image.open(path).convert("RGB"))
    t = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0).to(dev)
    f = model.encode_image(t); f = f / f.norm(dim=-1, keepdim=True)
    return f.squeeze(0).cpu().numpy().astype(np.float32)

rows = []
for fn, true in gt.items():
    qp = os.path.join(QUERIES, fn)
    if not os.path.isfile(qp):
        continue
    q = embed(qp)
    sims = M @ q
    order = np.argpartition(-sims, 5)[:5]
    order = order[np.argsort(-sims[order])]
    top = [(skus[i], float(sims[i])) for i in order]
    gap = top[0][1] - top[1][1]
    rows.append({"fn": fn, "true": true, "game": game_of(true),
                 "bucket": bucket_of.get(fn, "untagged"),
                 "pred": top[0][0], "top1_sim": top[0][1], "gap": gap,
                 "hit1": top[0][0] == true, "top5": [s for s, _ in top]})

# ---- VERBATIM gate (test_mobileclip.py:330-336) + YGO exclusion ----
def confident(r):
    return r["game"] != "yugioh" and r["gap"] >= TAU and r["top1_sim"] >= S

def report(rs, label):
    total = len(rs)
    conf = [r for r in rs if confident(r)]
    cor = sum(1 for r in conf if r["hit1"])
    wrong = len(conf) - cor
    cov = 100 * len(conf) / total if total else 0
    prec = 100 * cor / len(conf) if conf else float("nan")
    ps = f"{prec:.1f}%" if conf else "n/a"
    print(f"  {label:<10} total={total:>3}  on-dev={len(conf):>3}  "
          f"cov={cov:>5.1f}%  prec={ps:>6}  wrong={wrong}")
    return conf, wrong

n = len(rows)
print(f"\n========== FULL-INDEX bil_preprocess @ TAU={TAU}, S={S} (YGO excluded) ==========", flush=True)
conf_all, wrong_all = report(rows, "ALL")
print("\n  per-game:")
for g in ("pokemon", "mtg", "yugioh"):
    report([r for r in rows if r["game"] == g], g)
print("\n  per-bucket:")
for b in sorted({r["bucket"] for r in rows}):
    report([r for r in rows if r["bucket"] == b], b)

# ---- confident-but-WRONG (the go-live blocker) ----
cw = [r for r in rows if confident(r) and not r["hit1"]]
print(f"\n  CONFIDENT-BUT-WRONG accepted by gate: {len(cw)}", flush=True)
for r in cw:
    print(f"    [{r['game']}/{r['bucket']}] {r['fn']}  true={r['true']}  pred={r['pred']}  "
          f"top1_sim={r['top1_sim']:.4f}  gap={r['gap']:.4f}  in_idx={r['true'] in sku_set}", flush=True)

# ---- save for side-by-side with the later browser run ----
with open("fullindex_bil_results.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["fn", "game", "bucket", "true", "pred", "top1_sim", "gap", "hit1", "confident"])
    for r in rows:
        w.writerow([r["fn"], r["game"], r["bucket"], r["true"], r["pred"],
                    round(r["top1_sim"], 4), round(r["gap"], 4), r["hit1"], confident(r)])
print("\n[saved] fullindex_bil_results.csv", flush=True)
