"""_batch7_drift.py — Batch 7 of P1: formal FP16 drift + gate-survival measurement.
Scratch script; run from c:\\MatchIT. Outputs to fp16_drift_run/.
CPU EP only (Python validation). No browser, no deploy.
"""
import os, csv, json, sys
import numpy as np
import onnxruntime as ort
from PIL import Image

# ── Paths ──────────────────────────────────────────────────────────────────
FP32_ONNX = r"c:\MatchIT\web_spike\mobileclip2s2_image.onnx"
FP16_ONNX = r"c:\MatchIT\web_spike\mobileclip2s2_image_fp16.onnx"
IDX_DIR    = r"c:\MatchIT\ondevice_index_v1"
QUERIES    = r"c:\MatchIT\test_queries"
GT_FILE    = r"c:\MatchIT\groundtruth.csv"
BUCKETS    = r"c:\MatchIT\buckets.csv"
OUT_DIR    = r"c:\MatchIT\fp16_drift_run"
os.makedirs(OUT_DIR, exist_ok=True)

SCORE_FLOOR = 0.65
TAU, S      = 0.02, 0.80   # operating point from fullindex_bil_validation.py

# ── bil_preprocess — VERBATIM from fullindex_bil_validation.py:28-37 ───────
def bil_preprocess(img):
    """Plain bilinear (no antialias) resize shortest->256, center crop 256, [0,1]."""
    W, H = img.size
    s = 256 / min(W, H)
    nw, nh = round(W * s), round(H * s)
    im = img.resize((nw, nh), Image.BILINEAR)
    l = (nw - 256) // 2; t = (nh - 256) // 2
    im = im.crop((l, t, l + 256, t + 256))
    return np.asarray(im, dtype=np.float32) / 255.

# ── Helper ─────────────────────────────────────────────────────────────────
def game_of(sku):
    return "yugioh" if sku.startswith("ygo-") else "mtg" if sku.startswith("mtg-") else "pokemon"

# Gate predicate — VERBATIM from fullindex_bil_validation.py:100-101
def confident(r):
    return r["game"] != "yugioh" and r["gap"] >= TAU and r["top1_sim"] >= S

def cos(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))

# ── Load index ──────────────────────────────────────────────────────────────
print("[index] loading ...", flush=True)
M    = np.load(os.path.join(IDX_DIR, "vectors_f16.npy")).astype(np.float32)  # (102140,512) L2-normed
skus = json.load(open(os.path.join(IDX_DIR, "skus.json"), encoding="utf-8"))
print(f"[index] {M.shape[0]} cards x {M.shape[1]} dims", flush=True)

# ── Load ground truth ───────────────────────────────────────────────────────
gt = {}
for r in csv.DictReader(open(GT_FILE, encoding="utf-8")):
    if r["server_sku"]:
        try:
            if float(r["score"]) >= SCORE_FLOOR:
                gt[r["filename"]] = r["server_sku"]
        except (TypeError, ValueError):
            pass

bucket_of = {}
for r in csv.DictReader(open(BUCKETS, encoding="utf-8")):
    bucket_of[r["filename"]] = (r.get("bucket") or "untagged").strip() or "untagged"

print(f"[gt] {len(gt)} trusted query->sku pairs", flush=True)

# ── Load ONNX sessions (CPU EP only) ───────────────────────────────────────
print("[sessions] loading FP32 ONNX ...", flush=True)
sess32 = ort.InferenceSession(FP32_ONNX, providers=["CPUExecutionProvider"])
print("[sessions] loading FP16 ONNX ...", flush=True)
sess16 = ort.InferenceSession(FP16_ONNX, providers=["CPUExecutionProvider"])
print("[sessions] both sessions loaded OK", flush=True)

# ── A: Embed all queries with both models ──────────────────────────────────
print(f"\n[A] embedding {len(gt)} queries with FP32 and FP16 ONNX ...", flush=True)
fns      = []
fp32_embs = []
fp16_embs = []

for i, (fn, true_sku) in enumerate(gt.items()):
    qp = os.path.join(QUERIES, fn)
    if not os.path.isfile(qp):
        print(f"  [WARN] missing file: {fn}", flush=True)
        continue

    arr = bil_preprocess(Image.open(qp).convert("RGB"))          # HWC float32 [0,1]
    x   = arr.transpose(2, 0, 1)[np.newaxis].astype(np.float32)  # 1×3×256×256

    e32 = sess32.run(None, {"image": x})[0].squeeze(0).astype(np.float32)
    e32 = e32 / (np.linalg.norm(e32) + 1e-12)

    e16 = sess16.run(None, {"image": x})[0].squeeze(0).astype(np.float32)
    e16 = e16 / (np.linalg.norm(e16) + 1e-12)

    fns.append(fn)
    fp32_embs.append(e32)
    fp16_embs.append(e16)

    if (i + 1) % 25 == 0:
        print(f"  {i+1}/{len(gt)} done ...", flush=True)

fp32_arr = np.array(fp32_embs, dtype=np.float32)
fp16_arr = np.array(fp16_embs, dtype=np.float32)
print(f"[A] done: {len(fns)} queries embedded", flush=True)

np.save(os.path.join(OUT_DIR, "fp32_query_embeddings.npy"), fp32_arr)
np.save(os.path.join(OUT_DIR, "fp16_query_embeddings.npy"), fp16_arr)
with open(os.path.join(OUT_DIR, "query_filenames.json"), "w", encoding="utf-8") as f:
    json.dump(fns, f)
print(f"[A] saved to {OUT_DIR}", flush=True)

# ── C: Per-query cosine(FP16, FP32) drift ─────────────────────────────────
print("\n=== C: FP16 vs FP32 embedding drift ===", flush=True)
cosines = [cos(fp32_arr[i], fp16_arr[i]) for i in range(len(fns))]
worst_cos = min(cosines)
mean_cos  = float(np.mean(cosines))
print(f"  min  cosine(FP16, FP32): {worst_cos:.6f}", flush=True)
print(f"  mean cosine(FP16, FP32): {mean_cos:.6f}", flush=True)
print(f"  max  cosine(FP16, FP32): {max(cosines):.6f}", flush=True)
# breakdown
under999  = sum(1 for c in cosines if c < 0.999)
under9999 = sum(1 for c in cosines if c < 0.9999)
print(f"  queries with cosine < 0.9999: {under9999}", flush=True)
print(f"  queries with cosine < 0.999:  {under999}", flush=True)

if worst_cos < 0.99:
    print(f"\n[C] PRE-CONDITION FAIL: worst cosine {worst_cos:.6f} < 0.99 — STOP.", flush=True)
    sys.exit(1)
print(f"\n[C] PRE-CONDITION PASS: all cosines >= 0.99 (worst={worst_cos:.6f})", flush=True)

# ── D: Gate-survival (FP32 sanity + FP16 measurement) ─────────────────────
def search_rows(embs, fns_list):
    rows = []
    for i, fn in enumerate(fns_list):
        true_sku = gt[fn]
        q = embs[i]
        sims  = M @ q
        order = np.argpartition(-sims, 5)[:5]
        order = order[np.argsort(-sims[order])]
        top   = [(skus[j], float(sims[j])) for j in order]
        gap   = top[0][1] - top[1][1]
        rows.append({
            "fn": fn, "game": game_of(true_sku),
            "bucket": bucket_of.get(fn, "untagged"),
            "true": true_sku, "pred": top[0][0],
            "top1_sim": top[0][1], "gap": gap,
            "hit1": top[0][0] == true_sku,
        })
    return rows

def gate_stats(rows, label):
    total = len(rows)
    conf  = [r for r in rows if confident(r)]
    cor   = sum(1 for r in conf if r["hit1"])
    wrong = len(conf) - cor
    cov   = 100 * len(conf) / total if total else 0.0
    prec  = 100 * cor / len(conf) if conf else float("nan")
    ps    = f"{prec:.1f}%" if conf else "n/a"
    print(f"  {label:<14} total={total:>3}  on-dev={len(conf):>3}  "
          f"cov={cov:>5.1f}%  prec={ps:>6}  wrong={wrong}", flush=True)
    return conf, cor, wrong, cov, prec

print("\n[D] searching index ...", flush=True)
rows32 = search_rows(fp32_arr, fns)
rows16 = search_rows(fp16_arr, fns)

print("\n--- D: FP32 ONNX sanity check (should reproduce ~52%/100%/0) ---", flush=True)
conf32_all, cor32, wrong32, cov32, prec32 = gate_stats(rows32, "ALL (FP32)")
for g in ("pokemon", "mtg", "yugioh"):
    gate_stats([r for r in rows32 if r["game"] == g], g)

if wrong32 != 0 or cov32 < 49.0:
    print(f"\n[D] SANITY FAIL: FP32 ONNX wrong={wrong32}, coverage={cov32:.1f}%. "
          f"Expected 0 wrong, coverage >=49%. STOP.", flush=True)
    sys.exit(1)
print(f"[D] FP32 SANITY PASS", flush=True)

# ── E: Full results table ───────────────────────────────────────────────────
print("\n=== E: RESULTS TABLE (TAU={:.2f}, S={:.2f}, YGO excluded) ===".format(TAU, S), flush=True)
print(f"\n  {'Bucket':<14} {'Model':<5} {'N':>4} {'OnDev':>6} {'Cov%':>6} {'Prec%':>7} {'Wrong':>6}", flush=True)
print("  " + "-" * 55, flush=True)

buckets_all = sorted({r["bucket"] for r in rows32})
for b in buckets_all + ["ALL"]:
    rs32 = rows32 if b == "ALL" else [r for r in rows32 if r["bucket"] == b]
    rs16 = rows16 if b == "ALL" else [r for r in rows16 if r["bucket"] == b]
    for label, rs in [("FP32", rs32), ("FP16", rs16)]:
        total = len(rs)
        conf  = [r for r in rs if confident(r)]
        cor   = sum(1 for r in conf if r["hit1"])
        wrong = len(conf) - cor
        cov   = 100 * len(conf) / total if total else 0.0
        prec  = 100 * cor / len(conf) if conf else float("nan")
        ps    = f"{prec:.1f}" if conf else "n/a"
        bname = b if label == "FP32" else ""
        print(f"  {bname:<14} {label:<5} {total:>4} {len(conf):>6} {cov:>6.1f} {ps:>7} {wrong:>6}", flush=True)
    print(flush=True)

# ── F: Per-query diff classification ───────────────────────────────────────
print("=== F: per-query diff classification ===", flush=True)
identical = shuffled_in = shuffled_out = shuffled_sku = 0
diff_rows = []

for r32, r16 in zip(rows32, rows16):
    assert r32["fn"] == r16["fn"]
    c32, c16 = confident(r32), confident(r16)
    p32, p16 = r32["pred"],    r16["pred"]

    if c32 == c16 and p32 == p16:
        cat = "identical";    identical   += 1
    elif not c32 and c16:
        cat = "shuffled_in";  shuffled_in += 1
    elif c32 and not c16:
        cat = "shuffled_out"; shuffled_out += 1
    elif c32 and c16 and p32 != p16:
        cat = "shuffled_sku"; shuffled_sku += 1
    else:
        cat = "other";        shuffled_sku += 0   # shouldn't happen

    if cat != "identical":
        diff_rows.append((r32["fn"], cat, r32["game"], r32["bucket"],
                          r32["true"], p32, p16, c32, c16, r32["hit1"], r16["hit1"]))

print(f"  identical:    {identical}", flush=True)
print(f"  shuffled_in:  {shuffled_in}  (FP16 newly accepted — check if correct)", flush=True)
print(f"  shuffled_out: {shuffled_out} (FP16 newly rejected — safe, falls to server)", flush=True)
print(f"  shuffled_sku: {shuffled_sku} (accepted in both, different top-1)", flush=True)

if diff_rows:
    print("\n  Non-identical rows:", flush=True)
    for fn, cat, game, bkt, true, p32, p16, c32, c16, h32, h16 in diff_rows:
        print(f"    [{cat}] {fn}  game={game}  bucket={bkt}", flush=True)
        print(f"      true={true}", flush=True)
        print(f"      FP32: pred={p32}  gate={c32}  hit={h32}", flush=True)
        print(f"      FP16: pred={p16}  gate={c16}  hit={h16}", flush=True)

# ── G: Verdict ──────────────────────────────────────────────────────────────
conf16_all = [r for r in rows16 if confident(r)]
wrong16    = sum(1 for r in conf16_all if not r["hit1"])
cov16      = 100 * len(conf16_all) / len(rows16) if rows16 else 0.0
prec16     = 100 * sum(1 for r in conf16_all if r["hit1"]) / len(conf16_all) if conf16_all else float("nan")

print("\n=== G: VERDICT ===", flush=True)
print(f"  FP16  wrong:    {wrong16}", flush=True)
print(f"  FP16  coverage: {cov16:.1f}%", flush=True)
print(f"  FP16  precision: {prec16:.1f}%", flush=True)
print(f"  FP32  coverage: {cov32:.1f}%  (baseline)", flush=True)
print(f"  delta coverage: {cov16 - cov32:+.1f}pp vs FP32 baseline", flush=True)

if wrong16 == 0 and cov16 >= 49.0:
    verdict = "PASS"
    note    = "0 wrong AND coverage >= 49%"
elif wrong16 == 0 and 42.0 <= cov16 < 49.0:
    verdict = "BORDERLINE"
    note    = "0 wrong but coverage in 42-49%"
else:
    verdict = "FAIL"
    note    = f"wrong={wrong16} OR coverage={cov16:.1f}% < 42%"

print(f"\n  *** BATCH 7 VERDICT: {verdict} ({note}) ***", flush=True)

# ── Save gate_results.csv ───────────────────────────────────────────────────
gate_csv = os.path.join(OUT_DIR, "gate_results.csv")
# Build a diff_cat lookup
diff_cat = {}
for r32, r16 in zip(rows32, rows16):
    c32, c16 = confident(r32), confident(r16)
    p32, p16 = r32["pred"], r16["pred"]
    if c32 == c16 and p32 == p16:
        cat = "identical"
    elif not c32 and c16:
        cat = "shuffled_in"
    elif c32 and not c16:
        cat = "shuffled_out"
    elif c32 and c16 and p32 != p16:
        cat = "shuffled_sku"
    else:
        cat = "other"
    diff_cat[r32["fn"]] = cat

with open(gate_csv, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow([
        "fn", "game", "bucket", "true",
        "fp32_pred", "fp32_top1_sim", "fp32_gap", "fp32_hit1", "fp32_gate",
        "fp16_pred", "fp16_top1_sim", "fp16_gap", "fp16_hit1", "fp16_gate",
        "cosine_fp16_fp32", "diff_cat",
    ])
    for i, (r32, r16) in enumerate(zip(rows32, rows16)):
        w.writerow([
            r32["fn"], r32["game"], r32["bucket"], r32["true"],
            r32["pred"], round(r32["top1_sim"], 6), round(r32["gap"], 6),
            r32["hit1"], confident(r32),
            r16["pred"], round(r16["top1_sim"], 6), round(r16["gap"], 6),
            r16["hit1"], confident(r16),
            round(cosines[i], 8), diff_cat[r32["fn"]],
        ])

print(f"\n[saved] {gate_csv}", flush=True)
print("Batch 7 complete.", flush=True)
