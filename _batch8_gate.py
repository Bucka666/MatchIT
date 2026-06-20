"""_batch8_gate.py — Batch 8 D+G: browser/Pixel WebGPU gate-survival analysis.
Usage:
  python _batch8_gate.py          # auto-detects desktop + pixel JSON if present
  python _batch8_gate.py desktop  # Stage 1 (D) only
  python _batch8_gate.py pixel    # Stage 2 (G) only
Run from c:\\MatchIT after capturing embeddings JSON from harness.
"""
import os, csv, json, sys
import numpy as np

# ── Paths ──────────────────────────────────────────────────────────────────
IDX_DIR     = r"c:\MatchIT\ondevice_index_v1"
GT_FILE     = r"c:\MatchIT\groundtruth.csv"
BUCKETS     = r"c:\MatchIT\buckets.csv"
OUT_DIR     = r"c:\MatchIT\fp16_drift_run"
CPU_FP16    = os.path.join(OUT_DIR, "fp16_query_embeddings.npy")   # Batch 7 reference
FNAMES_JSON = os.path.join(OUT_DIR, "query_filenames.json")
DESKTOP_JSON = os.path.join(OUT_DIR, "embeddings_desktop_fp16.json")
PIXEL_JSON   = os.path.join(OUT_DIR, "embeddings_pixel7_fp16.json")

SCORE_FLOOR = 0.65
TAU, S      = 0.02, 0.80

# ── Gate predicate — VERBATIM from fullindex_bil_validation.py:100-101 ─────
def game_of(sku):
    return "yugioh" if sku.startswith("ygo-") else "mtg" if sku.startswith("mtg-") else "pokemon"

def confident(r):
    return r["game"] != "yugioh" and r["gap"] >= TAU and r["top1_sim"] >= S

def cos(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))

# ── Load shared state ───────────────────────────────────────────────────────
print("[index] loading ...", flush=True)
M    = np.load(os.path.join(IDX_DIR, "vectors_f16.npy")).astype(np.float32)
skus = json.load(open(os.path.join(IDX_DIR, "skus.json"), encoding="utf-8"))
print(f"[index] {M.shape[0]} x {M.shape[1]}", flush=True)

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

# Batch 7 CPU FP16 reference embeddings (ordered by query_filenames.json)
cpu_fns  = json.load(open(FNAMES_JSON, encoding="utf-8"))
cpu_fp16 = np.load(CPU_FP16).astype(np.float32)
fn_to_idx = {fn: i for i, fn in enumerate(cpu_fns)}

def load_browser_json(path):
    """Load harness output JSON → {filename: np.array(512)} already L2-normed."""
    data = json.load(open(path, encoding="utf-8"))
    ep   = data.get("execution_provider", "?")
    model = data.get("model", "?")
    embs = {}
    errors = []
    for entry in data["embeddings"]:
        name = entry["image"]
        if "error" in entry:
            errors.append(name)
        else:
            embs[name] = np.array(entry["embedding"], dtype=np.float32)
    return embs, ep, model, errors

def search_rows(embs_dict, fns_ordered):
    rows = []
    for fn in fns_ordered:
        if fn not in embs_dict:
            continue
        true_sku = gt[fn]
        q = embs_dict[fn]
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

def print_table(rows, label):
    print(f"\n  --- {label} (TAU={TAU}, S={S}, YGO excluded) ---", flush=True)
    print(f"  {'Bucket':<14} {'N':>4} {'OnDev':>6} {'Cov%':>6} {'Prec%':>7} {'Wrong':>6}", flush=True)
    print("  " + "-" * 48, flush=True)
    buckets_all = sorted({r["bucket"] for r in rows})
    for b in buckets_all + ["ALL"]:
        rs = rows if b == "ALL" else [r for r in rows if r["bucket"] == b]
        total = len(rs)
        conf  = [r for r in rs if confident(r)]
        cor   = sum(1 for r in conf if r["hit1"])
        wrong = len(conf) - cor
        cov   = 100 * len(conf) / total if total else 0.0
        prec  = 100 * cor / len(conf) if conf else float("nan")
        ps    = f"{prec:.1f}" if conf else "n/a"
        print(f"  {b:<14} {total:>4} {len(conf):>6} {cov:>6.1f} {ps:>7} {wrong:>6}", flush=True)
    # per-game
    print(flush=True)
    for g in ("pokemon", "mtg", "yugioh"):
        rs = [r for r in rows if r["game"] == g]
        total = len(rs)
        conf  = [r for r in rs if confident(r)]
        cor   = sum(1 for r in conf if r["hit1"])
        wrong = len(conf) - cor
        cov   = 100 * len(conf) / total if total else 0.0
        prec  = 100 * cor / len(conf) if conf else float("nan")
        ps    = f"{prec:.1f}" if conf else "n/a"
        print(f"  {g:<14} {total:>4} {len(conf):>6} {cov:>6.1f} {ps:>7} {wrong:>6}", flush=True)

def gate_diff(rows_ref, rows_test, ref_label, test_label):
    ref_map  = {r["fn"]: r for r in rows_ref}
    test_map = {r["fn"]: r for r in rows_test}
    common   = [fn for fn in ref_map if fn in test_map]
    flips    = []
    for fn in common:
        r_ref  = ref_map[fn]
        r_test = test_map[fn]
        if confident(r_ref) != confident(r_test) or r_ref["pred"] != r_test["pred"]:
            flips.append((fn, r_ref, r_test))
    print(f"\n  Gate-decision flips ({ref_label} -> {test_label}): {len(flips)}", flush=True)
    for fn, rr, rt in flips:
        print(f"    {fn}  game={rr['game']}  bucket={rr['bucket']}", flush=True)
        print(f"      {ref_label}:  pred={rr['pred']}  gate={confident(rr)}  hit={rr['hit1']}", flush=True)
        print(f"      {test_label}: pred={rt['pred']}  gate={confident(rt)}  hit={rt['hit1']}", flush=True)
    return len(flips)

# ── CPU FP16 rows for reference ────────────────────────────────────────────
cpu_embs_dict = {fn: cpu_fp16[fn_to_idx[fn]] for fn in cpu_fns if fn in gt}
cpu_rows = search_rows(cpu_embs_dict, cpu_fns)

mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
do_desktop = mode in ("auto", "desktop") and os.path.isfile(DESKTOP_JSON)
do_pixel   = mode in ("auto", "pixel")   and os.path.isfile(PIXEL_JSON)

if not do_desktop and not do_pixel:
    if mode == "auto":
        print("No JSON files found in fp16_drift_run/. Save harness output first.", flush=True)
    elif mode == "desktop":
        print(f"Not found: {DESKTOP_JSON}", flush=True)
    else:
        print(f"Not found: {PIXEL_JSON}", flush=True)
    sys.exit(1)

desktop_rows = None

# ── Stage 1 D: Desktop WebGPU FP16 ────────────────────────────────────────
if do_desktop:
    print("\n" + "=" * 60, flush=True)
    print("=== STAGE 1 D: Desktop WebGPU FP16 analysis ===", flush=True)
    print("=" * 60, flush=True)

    embs_d, ep_d, model_d, errs_d = load_browser_json(DESKTOP_JSON)
    print(f"  model:              {model_d}", flush=True)
    print(f"  execution_provider: {ep_d}", flush=True)
    print(f"  embeddings loaded:  {len(embs_d)}", flush=True)
    if errs_d:
        print(f"  ERRORS during capture: {errs_d}", flush=True)

    if ep_d.lower() != "webgpu":
        print(f"\n  [WARN] EP is '{ep_d}' — expected 'webgpu'. Stage 1 result is NOT comparable.", flush=True)

    # (1) Cosine vs Batch 7 CPU FP16
    common_fns = [fn for fn in cpu_fns if fn in embs_d]
    cosines_d  = [cos(cpu_fp16[fn_to_idx[fn]], embs_d[fn]) for fn in common_fns]
    print(f"\n  (1) Cosine (desktop-WebGPU-FP16 vs CPU-FP16, {len(common_fns)} queries):", flush=True)
    print(f"      min  = {min(cosines_d):.6f}", flush=True)
    print(f"      mean = {float(np.mean(cosines_d)):.6f}", flush=True)
    print(f"      max  = {max(cosines_d):.6f}", flush=True)
    under9999 = sum(1 for c in cosines_d if c < 0.9999)
    under999  = sum(1 for c in cosines_d if c < 0.999)
    print(f"      cosine < 0.9999: {under9999}", flush=True)
    print(f"      cosine < 0.999:  {under999}", flush=True)

    # (2) Gate results table
    desktop_rows = search_rows(embs_d, cpu_fns)
    print_table(desktop_rows, "Desktop WebGPU FP16")

    # Gate-decision diffs vs CPU FP16
    n_flips_d = gate_diff(cpu_rows, desktop_rows, "CPU-FP16", "Desktop-FP16")

    # ALL-coverage and wrong count
    conf_d = [r for r in desktop_rows if confident(r)]
    wrong_d = sum(1 for r in conf_d if not r["hit1"])
    cov_d   = 100 * len(conf_d) / len(desktop_rows) if desktop_rows else 0.0

    # Stage 1 verdict
    print(f"\n  Stage 1 summary:", flush=True)
    print(f"    wrong:     {wrong_d}", flush=True)
    print(f"    coverage:  {cov_d:.1f}%  (Batch 7 baseline 52.0%)", flush=True)
    print(f"    gate flips vs CPU FP16: {n_flips_d}", flush=True)

    if wrong_d == 0 and abs(cov_d - 52.0) <= 1.0 and n_flips_d <= 2:
        print(f"\n  *** STAGE 1 VERDICT: PASS ***", flush=True)
    else:
        fails = []
        if wrong_d > 0:              fails.append(f"wrong={wrong_d}")
        if abs(cov_d - 52.0) > 1.0: fails.append(f"cov={cov_d:.1f}% vs 52.0% (>1pp delta)")
        if n_flips_d > 2:            fails.append(f"gate flips={n_flips_d} > 2")
        print(f"\n  *** STAGE 1 VERDICT: INVESTIGATE ({'; '.join(fails)}) ***", flush=True)

# ── Stage 2 G: Pixel 7A WebGPU FP16 ───────────────────────────────────────
if do_pixel:
    print("\n" + "=" * 60, flush=True)
    print("=== STAGE 2 G: Pixel 7A WebGPU FP16 analysis ===", flush=True)
    print("=" * 60, flush=True)

    embs_p, ep_p, model_p, errs_p = load_browser_json(PIXEL_JSON)
    print(f"  model:              {model_p}", flush=True)
    print(f"  execution_provider: {ep_p}", flush=True)
    print(f"  embeddings loaded:  {len(embs_p)}", flush=True)
    if errs_p:
        print(f"  ERRORS during capture: {errs_p}", flush=True)

    if ep_p.lower() != "webgpu":
        print(f"\n  [WARN] EP is '{ep_p}' — expected 'webgpu'. Stage 2 result is NOT the authoritative Pixel-WebGPU run.", flush=True)

    pixel_rows = search_rows(embs_p, cpu_fns)
    common_fns = [fn for fn in cpu_fns if fn in embs_p]

    # (1) Pixel-FP16 vs Desktop-FP16
    if desktop_rows is not None and do_desktop:
        embs_d_load, _, _, _ = load_browser_json(DESKTOP_JSON)
        pd_common = [fn for fn in common_fns if fn in embs_d_load]
        cosines_pd = [cos(embs_d_load[fn], embs_p[fn]) for fn in pd_common]
        print(f"\n  (1) Cosine (Pixel-FP16 vs Desktop-FP16, {len(pd_common)} queries):", flush=True)
        print(f"      min  = {min(cosines_pd):.6f}", flush=True)
        print(f"      mean = {float(np.mean(cosines_pd)):.6f}", flush=True)
        under9999 = sum(1 for c in cosines_pd if c < 0.9999)
        print(f"      cosine < 0.9999: {under9999}", flush=True)
    elif os.path.isfile(DESKTOP_JSON):
        embs_d_load, _, _, _ = load_browser_json(DESKTOP_JSON)
        pd_common = [fn for fn in common_fns if fn in embs_d_load]
        cosines_pd = [cos(embs_d_load[fn], embs_p[fn]) for fn in pd_common]
        print(f"\n  (1) Cosine (Pixel-FP16 vs Desktop-FP16, {len(pd_common)} queries):", flush=True)
        print(f"      min  = {min(cosines_pd):.6f}", flush=True)
        print(f"      mean = {float(np.mean(cosines_pd)):.6f}", flush=True)
        under9999 = sum(1 for c in cosines_pd if c < 0.9999)
        print(f"      cosine < 0.9999: {under9999}", flush=True)
    else:
        print("\n  (1) Desktop JSON not found — skipping Pixel-vs-Desktop comparison.", flush=True)

    # (2) Pixel-FP16 vs CPU-FP16
    cosines_pc = [cos(cpu_fp16[fn_to_idx[fn]], embs_p[fn]) for fn in common_fns]
    print(f"\n  (2) Cosine (Pixel-FP16 vs CPU-FP16, {len(common_fns)} queries):", flush=True)
    print(f"      min  = {min(cosines_pc):.6f}", flush=True)
    print(f"      mean = {float(np.mean(cosines_pc)):.6f}", flush=True)
    print(f"      max  = {max(cosines_pc):.6f}", flush=True)
    under9999 = sum(1 for c in cosines_pc if c < 0.9999)
    under999  = sum(1 for c in cosines_pc if c < 0.999)
    print(f"      cosine < 0.9999: {under9999}", flush=True)
    print(f"      cosine < 0.999:  {under999}", flush=True)
    # Phase 1 envelope reference: mean 0.999914, worst 1-cos 0.00043
    print(f"      (Phase 1 FP32 Pixel-vs-desktop reference: mean ~0.999914, worst 1-cos ~0.00043)", flush=True)

    # (3) Results table
    print_table(pixel_rows, "Pixel 7A WebGPU FP16")

    # Gate diffs vs CPU FP16
    n_flips_p = gate_diff(cpu_rows, pixel_rows, "CPU-FP16", "Pixel-FP16")

    # Final verdict
    conf_p  = [r for r in pixel_rows if confident(r)]
    wrong_p = sum(1 for r in conf_p if not r["hit1"])
    cov_p   = 100 * len(conf_p) / len(pixel_rows) if pixel_rows else 0.0

    print(f"\n  Stage 2 summary:", flush=True)
    print(f"    wrong:     {wrong_p}", flush=True)
    print(f"    coverage:  {cov_p:.1f}%  (Batch 7 baseline 52.0%)", flush=True)
    print(f"    gate flips vs CPU FP16: {n_flips_p}", flush=True)

    if wrong_p == 0 and abs(cov_p - 52.0) <= 2.0 and n_flips_p <= 3:
        print(f"\n  *** STAGE 2 VERDICT: PASS ***", flush=True)
        print(f"\n  *** P1 FINAL VERDICT: PASS — FP16 ONNX cleared for production ***", flush=True)
    else:
        fails = []
        if wrong_p > 0:              fails.append(f"wrong={wrong_p}")
        if abs(cov_p - 52.0) > 2.0: fails.append(f"cov={cov_p:.1f}% vs 52.0% (>2pp delta)")
        if n_flips_p > 3:            fails.append(f"gate flips={n_flips_p} > 3")
        print(f"\n  *** STAGE 2 VERDICT: INVESTIGATE ({'; '.join(fails)}) ***", flush=True)

print("\nBatch 8 analysis complete.", flush=True)
