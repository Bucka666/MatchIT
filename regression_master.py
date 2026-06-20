import os
import csv
import time
import sqlite3
from pathlib import Path
from typing import Dict, List, Tuple, Any

import numpy as np

# ============================================================
# CONFIG — SET ONCE ONLY
# ============================================================

DB_PATH = r"C:\Users\c_a_b\AppData\Local\MatchITv2_ProductMatch_Data\images.db"

QUERY_DIR_BLANK = Path(r"C:\Users\c_a_b\Desktop\15xblank_cut images for regression testing\blank")
QUERY_DIR_CUT   = Path(r"C:\Users\c_a_b\Desktop\15xblank_cut images for regression testing\cut")

EXPECTED_CSV_BLANK = QUERY_DIR_BLANK / "expected_blank.csv"
EXPECTED_CSV_CUT   = QUERY_DIR_CUT / "expected_cut.csv"

# Match app.py scoring knobs
TOP_K = 5
TOP_M_PER_SKU = 3

# ============================================================
# AUTO MODES TO TEST
# NOTE: max_side is applied by monkey-patching feature_extractor._resize_max_side
# ============================================================

MODES: List[Dict[str, Any]] = [
    {"name": "FAST_noBG_768",   "multi_crop": False, "suppress_bg": False, "max_side": 768},
    {"name": "TURBO_CPU_768",   "multi_crop": False, "suppress_bg": True,  "max_side": 768},
    {"name": "FULL_like_DB",    "multi_crop": True,  "suppress_bg": True,  "max_side": 1024},
    {"name": "HQ_1280",         "multi_crop": True,  "suppress_bg": True,  "max_side": 1280},
]

# ============================================================

def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)

def l2norm(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    return v / (np.linalg.norm(v) + 1e-12)

def pct(x: float) -> str:
    return f"{x*100:.1f}%"

# ============================================================
# Expected CSV
# ============================================================

def load_expected_map(path: Path) -> Dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(f"Expected CSV not found: {path}")

    out: Dict[str, str] = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        need = {"filename", "expected_sku"}
        got = set([h.strip() for h in (r.fieldnames or [])])
        if not need.issubset(got):
            raise ValueError(f"CSV must have columns: filename,expected_sku (got: {r.fieldnames})")

        for row in r:
            fn = (row.get("filename") or "").strip()
            sku = (row.get("expected_sku") or "").strip()
            if fn and sku:
                out[fn] = sku
    return out

# ============================================================
# DB embeddings
# ============================================================

def load_db_embeddings(db_path: str) -> Dict[str, List[np.ndarray]]:
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"DB not found: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT sku, embedding FROM images WHERE embedding IS NOT NULL").fetchall()
    finally:
        conn.close()

    by_sku: Dict[str, List[np.ndarray]] = {}
    for sku, blob in rows:
        sku = (sku or "").strip()
        if not sku or blob is None:
            continue
        v = from_blob(blob)
        if v.size == 0:
            continue
        by_sku.setdefault(sku, []).append(l2norm(v))

    return by_sku

# ============================================================
# Match scoring (same as app.py intent)
# ============================================================

def score_skus(q_vec: np.ndarray, by_sku: Dict[str, List[np.ndarray]]) -> List[Tuple[float, str, float]]:
    """
    Returns list of (score, sku, best_sim) sorted desc, length TOP_K
    Score = mean top-3 sims (or weighted top-2 / top-1 fallbacks)
    """
    out: List[Tuple[float, str, float]] = []
    q_vec = l2norm(q_vec)

    for sku, vecs in by_sku.items():
        if not vecs:
            continue

        sims = [float(np.dot(q_vec, v)) for v in vecs]
        sims.sort(reverse=True)

        top = sims[:TOP_M_PER_SKU]
        if len(top) >= 3:
            score = float(np.mean(top[:3]))
        elif len(top) == 2:
            score = float(0.70 * top[0] + 0.30 * top[1])
        else:
            score = float(top[0])

        best_sim = float(top[0])
        out.append((score, sku, best_sim))

    out.sort(reverse=True, key=lambda x: (x[0], x[2]))
    return out[:TOP_K]

# ============================================================
# Monkey-patch feature_extractor resize cap (NO file edits)
# ============================================================

_PATCH_STATE = {"installed": False, "orig_resize": None}

def set_embedder_max_side(max_side: int) -> None:
    """
    Your feature_extractor.ImageEmbedder calls _resize_max_side(img) without args.
    In your file, _resize_max_side has default max_side captured at definition time,
    so changing feature_extractor.MAX_SIDE won't reliably work.

    We patch feature_extractor._resize_max_side to force our chosen max_side.
    """
    import feature_extractor  # local import so running file doesn't require it at top

    if not _PATCH_STATE["installed"]:
        if not hasattr(feature_extractor, "_resize_max_side"):
            return
        _PATCH_STATE["orig_resize"] = feature_extractor._resize_max_side
        _PATCH_STATE["installed"] = True

    orig = _PATCH_STATE["orig_resize"]

    def patched_resize(pil_img, _unused_max_side=None):
        return orig(pil_img, max_side=int(max_side))

    feature_extractor._resize_max_side = patched_resize

# ============================================================
# Bucket runner
# ============================================================

def run_bucket_mode(bucket_name: str,
                    query_dir: Path,
                    expected_map: Dict[str, str],
                    emb,
                    by_sku: Dict[str, List[np.ndarray]],
                    mode: Dict[str, Any]) -> Dict[str, Any]:

    # Only use files that exist in expected_map
    files = [p for p in sorted(query_dir.glob("*")) if p.is_file() and p.name in expected_map]
    if not files:
        raise RuntimeError(
            f"[{bucket_name}] No query files matched expected CSV filenames.\n"
            f"Query dir: {query_dir}\nExpected rows: {len(expected_map)}"
        )

    # Apply max_side patch for this run
    max_side = int(mode["max_side"])
    set_embedder_max_side(max_side)

    multi_crop = bool(mode["multi_crop"])
    suppress_bg = bool(mode["suppress_bg"])

    total = 0
    top1 = top3 = top5 = 0
    times_ms: List[float] = []

    for p in files:
        exp = expected_map[p.name]

        t0 = time.perf_counter()

        q = emb.embed_path(str(p), multi_crop=multi_crop, suppress_bg=suppress_bg)
        q = l2norm(np.asarray(q, dtype=np.float32))

        ranked = score_skus(q, by_sku)
        dt = (time.perf_counter() - t0) * 1000.0

        times_ms.append(dt)
        total += 1

        skus = [r[1] for r in ranked]
        if skus and skus[0] == exp:
            top1 += 1
        if exp in skus[:3]:
            top3 += 1
        if exp in skus[:5]:
            top5 += 1

    avg_ms = float(np.mean(times_ms)) if times_ms else 0.0
    p95_ms = float(np.percentile(times_ms, 95)) if len(times_ms) >= 2 else avg_ms

    return {
        "bucket": bucket_name,
        "mode": mode["name"],
        "n": total,
        "top1": top1 / max(total, 1),
        "top3": top3 / max(total, 1),
        "top5": top5 / max(total, 1),
        "avg_ms": avg_ms,
        "p95_ms": p95_ms,
        "multi_crop": multi_crop,
        "suppress_bg": suppress_bg,
        "max_side": max_side,
    }

def pick_winner(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Winner logic:
      1) highest top1
      2) then highest top3
      3) then highest top5
      4) then lowest avg_ms
      5) then lowest p95_ms
    """
    if not rows:
        raise ValueError("pick_winner got empty rows")

    rows_sorted = sorted(
        rows,
        key=lambda r: (-r["top1"], -r["top3"], -r["top5"], r["avg_ms"], r["p95_ms"])
    )
    return rows_sorted[0]

# ============================================================
# MAIN
# ============================================================

def main():
    print("\n=== MATCHIT REGRESSION AUTO MODE SELECTOR ===")

    # Sanity checks
    for p in [QUERY_DIR_BLANK, QUERY_DIR_CUT]:
        if not p.exists():
            raise FileNotFoundError(f"Query folder not found: {p}")

    expected_blank = load_expected_map(EXPECTED_CSV_BLANK)
    expected_cut   = load_expected_map(EXPECTED_CSV_CUT)

    print(f"[SETUP] DB: {DB_PATH}")
    print(f"[SETUP] BLANK queries: {QUERY_DIR_BLANK}  expected_rows={len(expected_blank)}")
    print(f"[SETUP] CUT   queries: {QUERY_DIR_CUT}    expected_rows={len(expected_cut)}")

    print("[SETUP] Loading DB embeddings…")
    by_sku = load_db_embeddings(DB_PATH)
    sku_count = len(by_sku)
    emb_count = sum(len(v) for v in by_sku.values())
    print(f"[SETUP] Loaded SKUs={sku_count} embeddings={emb_count}")

    from feature_extractor import ImageEmbedder
    emb = ImageEmbedder()

    all_rows: List[Dict[str, Any]] = []

    # Run all modes for each bucket
    buckets = [
        ("BLANK", QUERY_DIR_BLANK, expected_blank),
        ("CUT",   QUERY_DIR_CUT,   expected_cut),
    ]

    for bucket_name, qdir, exp_map in buckets:
        print(f"\n--- BUCKET: {bucket_name} ---")
        bucket_rows = []

        for mode in MODES:
            print(f"[RUN] {bucket_name} :: {mode['name']}  (multi_crop={mode['multi_crop']} bg={mode['suppress_bg']} max_side={mode['max_side']})")
            r = run_bucket_mode(bucket_name, qdir, exp_map, emb, by_sku, mode)
            bucket_rows.append(r)
            all_rows.append(r)
            print(f"      n={r['n']}  top1={pct(r['top1'])} top3={pct(r['top3'])} top5={pct(r['top5'])}  avg={r['avg_ms']:.0f}ms p95={r['p95_ms']:.0f}ms")

        win = pick_winner(bucket_rows)
        print(f"[WINNER] {bucket_name} -> {win['mode']}  top1={pct(win['top1'])} top3={pct(win['top3'])} top5={pct(win['top5'])} avg={win['avg_ms']:.0f}ms")

    # Combined winner: weight by n (so 15+15 behaves correctly)
    print("\n--- COMBINED (weighted) ---")
    combined_rows: Dict[str, Dict[str, Any]] = {}
    for r in all_rows:
        key = r["mode"]
        combined_rows.setdefault(key, {
            "bucket": "COMBINED",
            "mode": key,
            "n": 0,
            "top1_hits": 0.0,
            "top3_hits": 0.0,
            "top5_hits": 0.0,
            "time_sum": 0.0,
            "p95_avg": 0.0,
            "multi_crop": r["multi_crop"],
            "suppress_bg": r["suppress_bg"],
            "max_side": r["max_side"],
        })
        cr = combined_rows[key]
        n = r["n"]
        cr["n"] += n
        cr["top1_hits"] += r["top1"] * n
        cr["top3_hits"] += r["top3"] * n
        cr["top5_hits"] += r["top5"] * n
        cr["time_sum"] += r["avg_ms"] * n
        cr["p95_avg"] += r["p95_ms"]  # simple mean of p95 across buckets

    combined_list: List[Dict[str, Any]] = []
    for mode_name, cr in combined_rows.items():
        n = max(int(cr["n"]), 1)
        combined_list.append({
            "bucket": "COMBINED",
            "mode": mode_name,
            "n": n,
            "top1": float(cr["top1_hits"]) / n,
            "top3": float(cr["top3_hits"]) / n,
            "top5": float(cr["top5_hits"]) / n,
            "avg_ms": float(cr["time_sum"]) / n,
            "p95_ms": float(cr["p95_avg"]) / 2.0,  # since we have 2 buckets
            "multi_crop": cr["multi_crop"],
            "suppress_bg": cr["suppress_bg"],
            "max_side": cr["max_side"],
        })

    for r in combined_list:
        print(f"[COMBO] {r['mode']}  top1={pct(r['top1'])} top3={pct(r['top3'])} top5={pct(r['top5'])} avg={r['avg_ms']:.0f}ms")

    combined_winner = pick_winner(combined_list)
    print(f"[WINNER] COMBINED -> {combined_winner['mode']}  top1={pct(combined_winner['top1'])} top3={pct(combined_winner['top3'])} top5={pct(combined_winner['top5'])} avg={combined_winner['avg_ms']:.0f}ms")

    # Write full report CSV
    out_csv = Path("regression_auto_report.csv")
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["bucket","mode","n","top1","top3","top5","avg_ms","p95_ms","multi_crop","suppress_bg","max_side"])
        for r in all_rows + combined_list:
            w.writerow([
                r["bucket"], r["mode"], r["n"],
                r["top1"], r["top3"], r["top5"],
                r["avg_ms"], r["p95_ms"],
                r["multi_crop"], r["suppress_bg"], r["max_side"]
            ])

    # Winners CSV
    winners = []
    for bucket in ["BLANK", "CUT"]:
        bucket_rows = [r for r in all_rows if r["bucket"] == bucket]
        winners.append(pick_winner(bucket_rows))
    winners.append(combined_winner)

    out_win = Path("regression_auto_winners.csv")
    with open(out_win, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["bucket","winner_mode","top1","top3","top5","avg_ms","p95_ms","multi_crop","suppress_bg","max_side"])
        for r in winners:
            w.writerow([
                r["bucket"], r["mode"],
                r["top1"], r["top3"], r["top5"],
                r["avg_ms"], r["p95_ms"],
                r["multi_crop"], r["suppress_bg"], r["max_side"]
            ])

    print(f"\n[OUT] Wrote: {out_csv.resolve()}")
    print(f"[OUT] Wrote: {out_win.resolve()}")
    print("\nDone.")

if __name__ == "__main__":
    main()
