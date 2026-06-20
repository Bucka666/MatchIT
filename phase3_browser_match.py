# phase3_browser_match.py — Phase 3 (measure-only, no browser, no deploy).
# Match the REAL browser-canvas embeddings (embeddings_desktop.json) against the
# full gs-ondevice-v1 index using the VERBATIM gate, and compare to the Python
# bil_preprocess baseline (fullindex_bil_results.csv). Only changed variable vs
# baseline = preprocessing (browser canvas vs plain bilinear).
import os, csv, json
import numpy as np

IDX_DIR = "ondevice_index_v1"
EMB = "embeddings_desktop.json"
GT, BUCKETS = "groundtruth.csv", "buckets.csv"
BASELINE = "fullindex_bil_results.csv"
SCORE_FLOOR = 0.65
TAU, S = 0.02, 0.80   # operating point


def game_of(sku):
    return "yugioh" if sku.startswith("ygo-") else "mtg" if sku.startswith("mtg-") else "pokemon"

# ---- full index ----
M = np.load(os.path.join(IDX_DIR, "vectors_f16.npy")).astype(np.float32)  # L2-normed
skus = json.load(open(os.path.join(IDX_DIR, "skus.json"), encoding="utf-8"))
sku_set = set(skus)

# ---- ground truth + buckets (same filter as baseline) ----
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

# ---- browser embeddings ----
emb = {r["image"]: np.asarray(r["embedding"], dtype=np.float32)
       for r in json.load(open(EMB, encoding="utf-8"))["embeddings"] if not r.get("error")}

# ---- match + gate (browser queries) ----
def confident(r):
    # VERBATIM predicate from test_mobileclip.py:330 (+ YGO excluded)
    return r["game"] != "yugioh" and r["gap"] >= TAU and r["top1_sim"] >= S

rows = []
for fn, true in gt.items():
    q = emb.get(fn)
    if q is None:
        print(f"  [warn] no browser embedding for {fn}"); continue
    q = q / np.linalg.norm(q)            # ensure normalized (defensive; already normed)
    sims = M @ q
    order = np.argpartition(-sims, 5)[:5]
    order = order[np.argsort(-sims[order])]
    top = [(skus[i], float(sims[i])) for i in order]
    rows.append({"fn": fn, "true": true, "game": game_of(true),
                 "bucket": bucket_of.get(fn, "untagged"),
                 "pred": top[0][0], "top1_sim": top[0][1], "gap": top[0][1]-top[1][1],
                 "hit1": top[0][0] == true})

# ---- baseline join (Python bil_preprocess) ----
base = {}
for r in csv.DictReader(open(BASELINE, encoding="utf-8")):
    base[r["fn"]] = {"pred": r["pred"], "hit1": r["hit1"] == "True",
                     "confident": r["confident"] == "True",
                     "top1_sim": float(r["top1_sim"]), "gap": float(r["gap"])}

def stat(rs):
    total = len(rs); conf = [r for r in rs if confident(r)]
    cor = sum(1 for r in conf if r["hit1"]); wrong = len(conf)-cor
    cov = 100*len(conf)/total if total else 0
    prec = f"{100*cor/len(conf):.1f}%" if conf else "n/a"
    return total, len(conf), cov, prec, wrong

def line(label, rs):
    t, c, cov, prec, w = stat(rs)
    print(f"  {label:<10} total={t:>3}  on-dev={c:>3}  cov={cov:>5.1f}%  prec={prec:>6}  wrong={w}")

print("\n========== PHASE 3: BROWSER-CANVAS embeddings @ TAU=0.02 S=0.80 (YGO excluded) ==========")
print("  [baseline for reference: ALL cov=52.0% prec=100.0% wrong=0]")
line("ALL", rows)
print("\n  per-bucket (variant first — most sensitive):")
for b in ("variant", "mixed", "distinct"):
    line(b, [r for r in rows if r["bucket"] == b])
print("\n  per-game:")
for g in ("pokemon", "mtg", "yugioh"):
    line(g, [r for r in rows if r["game"] == g])

# ---- confident-but-WRONG (go-live blocker) ----
cw = [r for r in rows if confident(r) and not r["hit1"]]
print(f"\n  *** CONFIDENT-BUT-WRONG accepted by gate: {len(cw)} ***")
for r in cw:
    print(f"    [{r['game']}/{r['bucket']}] {r['fn']}  accepted={r['pred']}  true={r['true']}  "
          f"top1_sim={r['top1_sim']:.4f}  gap={r['gap']:.4f}  true_in_idx={r['true'] in sku_set}")

# ---- per-query delta vs baseline (join on filename) ----
flip_to_server, flip_to_wrong, new_correct, changed_pred = [], [], [], []
for r in rows:
    b = base.get(r["fn"])
    if not b: continue
    bc, brc = b["confident"], (b["confident"] and b["hit1"])
    nc = confident(r); ncr = (nc and r["hit1"])
    if brc and not nc:                          # baseline accepted-correct -> now declined
        flip_to_server.append(r)
    if (not bc or b["hit1"]) and nc and not r["hit1"]:   # now accepted but WRONG
        flip_to_wrong.append(r)
    if not bc and ncr:                          # baseline declined -> now accepted-correct
        new_correct.append(r)
    if nc and bc and r["pred"] != b["pred"]:    # both confident, top1 changed
        changed_pred.append(r)

print("\n========== PER-QUERY DELTA vs Python baseline (join on filename) ==========")
print(f"  baseline-accepted-correct now DECLINED (coverage loss, benign): {len(flip_to_server)}")
for r in flip_to_server:
    b = base[r["fn"]]
    print(f"    [{r['game']}/{r['bucket']}] {r['fn']} true={r['true']} "
          f"browser(sim={r['top1_sim']:.4f},gap={r['gap']:.4f}) vs base(sim={b['top1_sim']:.4f},gap={b['gap']:.4f})")
print(f"\n  baseline-OK now ACCEPTED-WRONG (PRECISION BREAK, blocks go-live): {len(flip_to_wrong)}")
for r in flip_to_wrong:
    print(f"    [{r['game']}/{r['bucket']}] {r['fn']} accepted={r['pred']} true={r['true']} "
          f"top1_sim={r['top1_sim']:.4f} gap={r['gap']:.4f}")
print(f"\n  newly accepted-correct (coverage gain): {len(new_correct)}")
print(f"  confident on both but top-1 sku changed: {len(changed_pred)}")
for r in changed_pred:
    b = base[r["fn"]]
    print(f"    [{r['game']}/{r['bucket']}] {r['fn']} base->{b['pred']} browser->{r['pred']} "
          f"(true={r['true']}, browser_hit1={r['hit1']})")
