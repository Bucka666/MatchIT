# phase4_pixel_match.py — authoritative Pixel-7 match+gate, three-way comparison.
# Python baseline (fullindex_bil_results.csv) | desktop browser | Pixel 7.
# Same full index, same VERBATIM gate (test_mobileclip.py:330). Measure-only.
import os, csv, json
import numpy as np

IDX_DIR = "ondevice_index_v1"
GT, BUCKETS = "groundtruth.csv", "buckets.csv"
BASELINE = "fullindex_bil_results.csv"
DESKTOP, PIXEL = "embeddings_desktop.json", "embeddings_pixel7.json"
SCORE_FLOOR = 0.65
TAU, S = 0.02, 0.80


def game_of(sku):
    return "yugioh" if sku.startswith("ygo-") else "mtg" if sku.startswith("mtg-") else "pokemon"

M = np.load(os.path.join(IDX_DIR, "vectors_f16.npy")).astype(np.float32)
skus = json.load(open(os.path.join(IDX_DIR, "skus.json"), encoding="utf-8"))
sku_set = set(skus)

gt, bucket_of = {}, {}
for r in csv.DictReader(open(GT, encoding="utf-8")):
    if r["server_sku"]:
        try:
            if float(r["score"]) >= SCORE_FLOOR:
                gt[r["filename"]] = r["server_sku"]
        except (TypeError, ValueError):
            pass
for r in csv.DictReader(open(BUCKETS, encoding="utf-8")):
    bucket_of[r["filename"]] = (r.get("bucket") or "untagged").strip() or "untagged"


def load_emb(p):
    return {r["image"]: np.asarray(r["embedding"], np.float32)
            for r in json.load(open(p, encoding="utf-8"))["embeddings"] if not r.get("error")}


def match(emb):
    rows = {}
    for fn, true in gt.items():
        q = emb.get(fn)
        if q is None:
            continue
        q = q / np.linalg.norm(q)
        sims = M @ q
        o = np.argpartition(-sims, 5)[:5]; o = o[np.argsort(-sims[o])]
        top = [(skus[i], float(sims[i])) for i in o]
        rows[fn] = {"fn": fn, "true": true, "game": game_of(true),
                    "bucket": bucket_of.get(fn, "untagged"),
                    "pred": top[0][0], "top1_sim": top[0][1], "gap": top[0][1]-top[1][1],
                    "hit1": top[0][0] == true}
    return rows


def confident(r):  # VERBATIM predicate (test_mobileclip.py:330) + YGO excluded
    return r["game"] != "yugioh" and r["gap"] >= TAU and r["top1_sim"] >= S


def stat(rs):
    conf = [r for r in rs if confident(r)]
    cor = sum(1 for r in conf if r["hit1"])
    cov = 100*len(conf)/len(rs) if rs else 0
    prec = 100*cor/len(conf) if conf else float("nan")
    return len(conf), cov, prec, len(conf)-cor


# ---- baseline rows from CSV ----
base = {}
for r in csv.DictReader(open(BASELINE, encoding="utf-8")):
    base[r["fn"]] = {"fn": r["fn"], "true": r["true"], "game": game_of(r["true"]),
                     "bucket": r["bucket"], "pred": r["pred"],
                     "top1_sim": float(r["top1_sim"]), "gap": float(r["gap"]),
                     "hit1": r["hit1"] == "True"}
dk = match(load_emb(DESKTOP))
px = match(load_emb(PIXEL))

runs = [("Python", base), ("Desktop", dk), ("Pixel7", px)]

def threeway(label, keyfilter):
    cells = []
    for _, rows in runs:
        rs = [r for r in rows.values() if keyfilter(r)]
        c, cov, prec, w = stat(rs)
        ps = f"{prec:.1f}%" if c else "n/a"
        cells.append(f"{cov:5.1f}% {ps:>6} w{w}")
    print(f"  {label:<10} | " + " | ".join(f"{c:>16}" for c in cells))

print("\n================ THREE-WAY: Python baseline | Desktop | Pixel 7 ================")
print(f"  (gate: gap>={TAU} and top1_sim>={S}, YGO excluded; cells = coverage / precision / wrong)")
print(f"  {'':<10} | {'Python':>16} | {'Desktop':>16} | {'Pixel7':>16}")
print("  " + "-"*70)
threeway("ALL", lambda r: True)
print("  per-bucket (variant first):")
for b in ("variant", "mixed", "distinct"):
    threeway(b, lambda r, b=b: r["bucket"] == b)
print("  per-game:")
for g in ("pokemon", "mtg", "yugioh"):
    threeway(g, lambda r, g=g: r["game"] == g)

# ---- wrong-sku acceptances (Pixel) ----
cw = [r for r in px.values() if confident(r) and not r["hit1"]]
print(f"\n  *** PIXEL CONFIDENT-BUT-WRONG: {len(cw)} ***")
for r in cw:
    print(f"    [{r['game']}/{r['bucket']}] {r['fn']} accepted={r['pred']} true={r['true']} "
          f"top1_sim={r['top1_sim']:.4f} gap={r['gap']:.4f}")

# ---- per-query delta: Pixel vs desktop AND vs Python ----
def cls(r):  # decision class for a row
    return "ACCEPT" if confident(r) else "server"

flip_dt_to_server, flip_to_wrong, gate_diff_vs_py = [], [], 0
for fn, r in px.items():
    d = dk.get(fn); b = base.get(fn)
    pc = confident(r)
    # vs desktop
    if d:
        dc = confident(d)
        if dc and d["hit1"] and not (pc and r["hit1"]):
            flip_dt_to_server.append((fn, r, d))      # desktop accept-correct -> pixel not
        if pc and not r["hit1"] and not (dc and not d["hit1"]):
            flip_to_wrong.append((fn, r))             # pixel accepts wrong (desktop didn't)
    # vs python
    if b and confident(b) != pc:
        gate_diff_vs_py += 1

print("\n  PER-QUERY DELTA:")
print(f"    desktop accept-correct now DECLINED by Pixel (benign coverage loss): {len(flip_dt_to_server)}")
for fn, r, d in flip_dt_to_server:
    print(f"      [{r['game']}/{r['bucket']}] {fn} true={r['true']} "
          f"pixel(sim={r['top1_sim']:.4f},gap={r['gap']:.4f}) desktop(sim={d['top1_sim']:.4f},gap={d['gap']:.4f})")
print(f"    Pixel accepts WRONG that desktop did not (PRECISION BREAK): {len(flip_to_wrong)}")
for fn, r in flip_to_wrong:
    print(f"      [{r['game']}/{r['bucket']}] {fn} accepted={r['pred']} true={r['true']} "
          f"top1_sim={r['top1_sim']:.4f} gap={r['gap']:.4f}")
print(f"    gate decision differs from Python baseline (either direction): {gate_diff_vs_py}")

# ---- drift diagnostic: Pixel vs desktop ----
de, pe = load_emb(DESKTOP), load_emb(PIXEL)
common = sorted(set(de) & set(pe))
dsim, dgap, predchg, flipped = [], [], 0, 0
for fn in common:
    rd, rp = dk[fn], px[fn]
    dsim.append(abs(rp["top1_sim"]-rd["top1_sim"]))
    dgap.append(abs(rp["gap"]-rd["gap"]))
    if rp["pred"] != rd["pred"]:
        predchg += 1
        if confident(rd) != confident(rp):
            flipped += 1
dsim, dgap = np.array(dsim), np.array(dgap)
print("\n  DRIFT (Pixel vs Desktop):")
print(f"    |delta top1_sim| mean={dsim.mean():.5f} max={dsim.max():.5f}")
print(f"    |delta gap|      mean={dgap.mean():.5f} max={dgap.max():.5f}")
print(f"    top-1 sku changed: {predchg}/{len(common)}  (of which flipped a GATED decision: {flipped})")
