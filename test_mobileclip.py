# test_mobileclip.py
# LOCAL, READ-ONLY against C:\CardsDB. Simulates on-device matching with a
# MobileCLIP model and scores it against the trusted server's ground truth.
#
# Touches nothing in prod / Modal. Reads CardsDB profile.json + front.png only.
# Writes only local artefacts: index cache, per-model embed cache, misses CSV.
#
# Usage:
#   python test_mobileclip.py --model MobileCLIP-S2  --pretrained datacompdr
#   python test_mobileclip.py --model MobileCLIP2-S2 --pretrained dfndr2b
#
# Fairness: the candidate DB is NOT just the ground-truth SKUs. For every GT
# SKU we add its same-set siblings AND same-name variants across other sets, so
# look-alikes genuinely compete. Without that, the test is falsely easy.
#
import argparse
import os
import csv
import json
import re
import time
import numpy as np
import torch
import open_clip
from PIL import Image

ap = argparse.ArgumentParser()
ap.add_argument("--model", required=True)
ap.add_argument("--pretrained", required=True)
ap.add_argument("--cardsdb", default=r"C:\CardsDB")
ap.add_argument("--queries", default="test_queries")
ap.add_argument("--groundtruth", default="groundtruth.csv")
ap.add_argument("--buckets", default="buckets.csv",
                help="optional filename,bucket CSV (distinct/variant/mixed) for per-bucket stats")
ap.add_argument("--tau", type=float, default=0.05,
                help="top1-top2 similarity gap below which we'd fall back to the server")
ap.add_argument("--score-floor", type=float, default=0.65,
                help="only trust GT rows at/above this server score")
ap.add_argument("--rebuild-index", action="store_true",
                help="force rebuild of the CardsDB sibling index cache")
ap.add_argument("--hybrid-split", action="store_true",
                help="report the confidence-gated hybrid (on-device confident "
                     "answers + server fallback) sweeping TAU; no OCR/server calls")
ap.add_argument("--reprint-guard", action="store_true",
                help="extra on-device gate: punt to server when >=2 of the top-5 "
                     "share top-1's card-number/passcode identity (reprint "
                     "ambiguity). Detected from SKU structure, no OCR.")
args = ap.parse_args()

# TAU sweep for --hybrid-split (top1-top2 gap thresholds).
HYBRID_TAUS = [0.02, 0.05, 0.08, 0.12]
# Absolute top1-similarity floors swept crossed with TAU (a scan answers
# on-device only if top1_sim >= S AND gap >= TAU).
HYBRID_MIN_SIMS = [0.80, 0.85, 0.88, 0.90]

GAMES = ("pokemon", "mtg", "yugioh")
INDEX_CACHE = os.path.join(".cache_mobileclip_test", "cardsdb_index.json")
EMBED_DIR   = os.path.join(".cache_mobileclip_test", "embeds", f"{args.model}__{args.pretrained}")

dev = "cuda" if torch.cuda.is_available() else "cpu"


# ───────────────────────── helpers ─────────────────────────
def game_of(sku):
    """Game folder a SKU lives in. Pokémon SKUs carry no prefix."""
    if sku.startswith("mtg-"):
        return "mtg"
    if sku.startswith("ygo-"):
        return "yugioh"
    return "pokemon"


def find_front_image(sku, cardsdb):
    """Locate front.png for a SKU. SKU == folder name in CardsDB/<game>/<sku>/."""
    g = game_of(sku)
    p = os.path.join(cardsdb, g, sku, "front.png")
    if os.path.isfile(p):
        return p
    # fallback: prefix heuristic can miss; probe all game dirs
    for g2 in GAMES:
        if g2 == g:
            continue
        p2 = os.path.join(cardsdb, g2, sku, "front.png")
        if os.path.isfile(p2):
            return p2
    return None


def _norm_name(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _card_identity(sku):
    """Card-number / passcode identity from SKU structure (no OCR).

    Two printings of the same base card share this trailing token:
      YGO  ygo-{set}-{code}-{passcode} -> passcode  (true card id)
      MTG  mtg-{set}-{number}          -> collector number
      PKM  {set}-{number}              -> card number
    All are the segment after the last '-'.
    """
    return sku.rsplit("-", 1)[-1] if "-" in sku else sku


def _reprint_ambiguous(top5):
    """True if >=2 of the top-5 share top-1's card-number/passcode identity,
    i.e. the model's top candidates are different printings of the same card."""
    if not top5:
        return False
    ident0 = _card_identity(top5[0][0])
    shared = sum(1 for sku, _ in top5 if _card_identity(sku) == ident0)
    return shared >= 2


def build_index(cardsdb):
    """Walk every profile.json once and index per-card (game, set_id, name).

    Returns dict with:
      sku   -> {"game","set_id","name"}
      sets  -> {"game::set_id": [sku,...]}
      names -> {"game::norm_name": [sku,...]}
    Used for sibling/variant expansion. Cached to disk; reused across models.
    """
    t0 = time.time()
    sku_meta, sets, names = {}, {}, {}
    total = 0
    for g in GAMES:
        gdir = os.path.join(cardsdb, g)
        if not os.path.isdir(gdir):
            continue
        with os.scandir(gdir) as it:
            for ent in it:
                if not ent.is_dir():
                    continue
                sku = ent.name
                prof = os.path.join(ent.path, "profile.json")
                if not os.path.isfile(prof):
                    continue
                try:
                    with open(prof, "r", encoding="utf-8") as f:
                        d = json.load(f)
                except Exception:
                    continue
                set_id = str(d.get("set_id", ""))
                name   = _norm_name(d.get("name", ""))
                sku_meta[sku] = {"game": g, "set_id": set_id, "name": name}
                sets.setdefault(f"{g}::{set_id}", []).append(sku)
                if name:
                    names.setdefault(f"{g}::{name}", []).append(sku)
                total += 1
    idx = {"sku": sku_meta, "sets": sets, "names": names}
    os.makedirs(os.path.dirname(INDEX_CACHE), exist_ok=True)
    with open(INDEX_CACHE, "w", encoding="utf-8") as f:
        json.dump(idx, f)
    print(f"[index] built {total} cards across {len(sets)} sets in {time.time()-t0:.1f}s "
          f"-> {INDEX_CACHE}")
    return idx


def load_index(cardsdb):
    if args.rebuild_index or not os.path.isfile(INDEX_CACHE):
        return build_index(cardsdb)
    with open(INDEX_CACHE, "r", encoding="utf-8") as f:
        idx = json.load(f)
    print(f"[index] loaded cache ({len(idx['sku'])} cards) from {INDEX_CACHE}")
    return idx


def expand_siblings(gt_skus, idx):
    """Each GT sku -> itself + same-set siblings + same-name variants (cross-set)."""
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


# ───────────────────────── model + embeds ─────────────────────────
print(f"[model] loading {args.model}/{args.pretrained} on {dev} ...")
model, _, preprocess = open_clip.create_model_and_transforms(args.model, pretrained=args.pretrained)
model = model.to(dev).eval()


@torch.no_grad()
def embed_image(path):
    im = Image.open(path).convert("RGB")
    x = preprocess(im).unsqueeze(0).to(dev)
    f = model.encode_image(x)
    f = f / f.norm(dim=-1, keepdim=True)
    return f.squeeze(0).cpu().numpy().astype(np.float32)


def _embed_cache_path(sku):
    return os.path.join(EMBED_DIR, f"{sku}.npy")


def embed_sku(sku, cardsdb):
    """Embed a DB card's front.png, caching the vector per (model,pretrained)."""
    cp = _embed_cache_path(sku)
    if os.path.isfile(cp):
        return np.load(cp)
    img = find_front_image(sku, cardsdb)
    if not img:
        return None
    v = embed_image(img)
    os.makedirs(EMBED_DIR, exist_ok=True)
    np.save(cp, v)
    return v


# ───────────────────────── ground truth ─────────────────────────
gt = {}
with open(args.groundtruth, encoding="utf-8") as f:
    for row in csv.DictReader(f):
        if not row["server_sku"]:
            continue
        try:
            if float(row["score"]) >= args.score_floor:
                gt[row["filename"]] = row["server_sku"]
        except (TypeError, ValueError):
            pass
if not gt:
    raise SystemExit("No usable ground-truth rows (check groundtruth.csv / --score-floor).")
print(f"[gt] {len(gt)} trusted query->sku pairs")

# optional per-bucket tags
bucket_of = {}
if os.path.isfile(args.buckets):
    with open(args.buckets, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            bucket_of[row["filename"]] = row.get("bucket", "").strip() or "untagged"

# ───────────────────────── build candidate DB ─────────────────────────
idx = load_index(args.cardsdb)
needed = expand_siblings(set(gt.values()), idx)
print(f"[db] {len(gt.values())} GT skus expanded to {len(needed)} candidate cards "
      f"(siblings + variants)")

db_skus, db_vecs, missing = [], [], []
for sku in sorted(needed):
    v = embed_sku(sku, args.cardsdb)
    if v is None:
        missing.append(sku)
        continue
    db_skus.append(sku)
    db_vecs.append(v)
if missing:
    print(f"[db] WARNING: no front.png for {len(missing)} skus (skipped), e.g. {missing[:5]}")
# guard: every GT sku must be in the index, else top-1 is impossible by construction
gt_missing = [s for s in set(gt.values()) if s not in set(db_skus)]
if gt_missing:
    print(f"[db] WARNING: {len(gt_missing)} GT skus missing from candidate DB "
          f"(can never be matched): {gt_missing[:5]}")
M = np.stack(db_vecs)          # (N, 512)
sku_pos = {s: i for i, s in enumerate(db_skus)}
print(f"[db] index matrix: {M.shape}")

# ───────────────────────── run queries ─────────────────────────
results = []
for fn, true_sku in gt.items():
    qpath = os.path.join(args.queries, fn)
    if not os.path.isfile(qpath):
        print(f"  skip (query image missing): {fn}")
        continue
    q = embed_image(qpath)
    sims = M @ q
    order = np.argsort(-sims)[:5]
    top = [(db_skus[i], float(sims[i])) for i in order]
    gap = top[0][1] - top[1][1] if len(top) > 1 else 1.0
    _game = "yugioh" if true_sku.startswith("ygo-") else "mtg" if true_sku.startswith("mtg-") else "pokemon"
    results.append({
        "fn": fn, "true": true_sku, "pred": top[0][0],
        "hit1": top[0][0] == true_sku,
        "hit5": any(s == true_sku for s, _ in top),
        "gap": round(gap, 4), "top1_sim": round(top[0][1], 4), "top5": top,
        "bucket": bucket_of.get(fn, "untagged"), "game": _game,
    })

n = len(results)
if not n:
    raise SystemExit("No queries evaluated (missing query images?).")

h1 = sum(r["hit1"] for r in results)
h5 = sum(r["hit5"] for r in results)
uncertain = [r for r in results if r["gap"] < args.tau]

print(f"\n=== {args.model}/{args.pretrained} ===")
print(f"queries evaluated: {n}")
print(f"top-1: {h1}/{n} = {100*h1/n:.1f}%")
print(f"top-5: {h5}/{n} = {100*h5/n:.1f}%")
print(f"uncertain (gap<{args.tau}): {len(uncertain)}/{n} = {100*len(uncertain)/n:.1f}%  "
      f"(would fall back to server)")

# of the uncertain ones, how many would the server have rescued (i.e. we were wrong)?
unc_wrong = sum(1 for r in uncertain if not r["hit1"])
print(f"   of those uncertain, {unc_wrong} were actually top-1 wrong "
      f"(server fallback correctly triggered)")

# per-bucket breakdown
if bucket_of:
    print("\nper-bucket top-1:")
    for b in sorted({r["bucket"] for r in results}):
        rs = [r for r in results if r["bucket"] == b]
        bh = sum(r["hit1"] for r in rs)
        print(f"  {b:10s}: {bh}/{len(rs)} = {100*bh/len(rs):.1f}%")

# ───────────────────────── hybrid confidence-gated split ─────────────────────────
# Model the deployed system: on-device answers when confident (gap >= TAU),
# else falls back to the trusted server. Server is treated as always-correct
# (it scored 150/150 in groundtruth — it IS the reference). So every uncertain
# scan contributes a correct blended answer; the only real errors the user ever
# sees are CONFIDENT-but-wrong on-device answers (server never sees them).
if args.hybrid_split:
    print(f"\n========== HYBRID CONFIDENCE-GATED SPLIT -- {args.model}/{args.pretrained} ==========")
    print(f"queries: {n}   (server = trusted reference, assumed correct on fallback)")
    if args.reprint_guard:
        print("reprint-guard: ON (punt when >=2 of top-5 share top-1 card-number/passcode)")
    buckets_present = sorted({r["bucket"] for r in results}) if bucket_of else []

    # Precompute reprint-ambiguity per query (tau/S-independent).
    for r in results:
        r["reprint_ambiguous"] = _reprint_ambiguous(r["top5"])

    def _hybrid_stats(rows, tau, min_sim=0.0, reprint_guard=False):
        # On-device only if gap >= TAU AND top1_sim >= min_sim AND (guard off or
        # not reprint-ambiguous).
        total = len(rows)
        confident = [r for r in rows
                     if r["gap"] >= tau and r["top1_sim"] >= min_sim
                     and not (reprint_guard and r["reprint_ambiguous"])]
        uncertain_n = total - len(confident)
        on_dev_correct = sum(1 for r in confident if r["hit1"])
        on_dev_wrong   = len(confident) - on_dev_correct
        coverage  = 100 * len(confident) / total if total else 0.0
        precision = 100 * on_dev_correct / len(confident) if confident else float("nan")
        # blended: on-device-correct + uncertain-treated-as-server-correct
        blended_correct = on_dev_correct + uncertain_n
        blended_acc = 100 * blended_correct / total if total else 0.0
        server_load = 100 * uncertain_n / total if total else 0.0
        return {
            "total": total, "confident": len(confident), "uncertain": uncertain_n,
            "on_dev_correct": on_dev_correct, "on_dev_wrong": on_dev_wrong,
            "coverage": coverage, "precision": precision,
            "blended_acc": blended_acc, "server_load": server_load,
        }

    for tau in HYBRID_TAUS:
        s = _hybrid_stats(results, tau, reprint_guard=args.reprint_guard)
        prec_str = f"{s['precision']:.1f}%" if s["confident"] else "n/a"
        print(f"\n--- TAU = {tau} ---")
        print(f"  on-device COVERAGE : {s['coverage']:.1f}%  ({s['confident']}/{s['total']} answered on-device)")
        print(f"  on-device PRECISION: {prec_str}  (of confident, correct)   <-- sacred number")
        print(f"  on-device WRONG    : {s['on_dev_wrong']}  (confident-but-incorrect; server never sees these)")
        print(f"  blended ACCURACY   : {s['blended_acc']:.1f}%  ({s['on_dev_correct']}+{s['uncertain']} server / {s['total']})")
        print(f"  server LOAD        : {s['server_load']:.1f}%  (fallback share = your cost)")

        if buckets_present:
            print(f"  per-bucket (coverage | precision | wrong):")
            for b in buckets_present:
                bs = _hybrid_stats([r for r in results if r["bucket"] == b], tau,
                                   reprint_guard=args.reprint_guard)
                bp = f"{bs['precision']:.0f}%" if bs["confident"] else "n/a"
                print(f"     {b:9s}: cov {bs['coverage']:5.1f}% | prec {bp:>4} | wrong {bs['on_dev_wrong']}"
                      f"  ({bs['confident']}/{bs['total']} on-device)")

        # dump confident-but-wrong (the real on-device errors) for this TAU
        err_path = f"ondevice_errors_{tau}.csv"
        with open(err_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["fn", "bucket", "true", "pred", "gap", "top5"])
            for r in results:
                if r["gap"] >= tau and not r["hit1"]:
                    w.writerow([r["fn"], r["bucket"], r["true"], r["pred"],
                                r["gap"], json.dumps(r["top5"])])
        print(f"  -> on-device errors dumped to {err_path}")

    # ── TAU × min-sim grid (gap AND absolute-similarity gating) ──
    print(f"\n========== TAU x MIN-SIM GRID -- {args.model}/{args.pretrained} ==========")
    print("each cell: coverage% / precision% / wrong   (on-device only if "
          "gap>=TAU AND top1_sim>=S)")
    header = "  TAU \\ S  " + "".join(f"| {s:>17.2f} " for s in HYBRID_MIN_SIMS)
    print(header)
    print("  " + "-" * (len(header) - 2))
    grid = {}
    for tau in HYBRID_TAUS:
        cells = []
        for s_floor in HYBRID_MIN_SIMS:
            st = _hybrid_stats(results, tau, min_sim=s_floor,
                               reprint_guard=args.reprint_guard)
            grid[(tau, s_floor)] = st
            prec = f"{st['precision']:.0f}" if st["confident"] else "--"
            flag = "*" if (st["confident"] and st["precision"] >= 99.0) else " "
            cells.append(f"|{flag}{st['coverage']:4.0f}/{prec:>3}%/{st['on_dev_wrong']:<2}{flag} "
                         f"  ".ljust(19))
        print(f"  {tau:<8} " + "".join(cells))
    print("  (* marks cells with precision >= 99%)")

    # best qualifying cell: precision >= 99%, then maximise coverage
    qualifying = [(tau, s_floor, st) for (tau, s_floor), st in grid.items()
                  if st["confident"] and st["precision"] >= 99.0]
    print("\n========== OPERATING POINT ==========")
    if qualifying:
        op_tau, op_s, st = max(qualifying, key=lambda x: x[2]["coverage"])
        print(f"  BEST >=99% cell: TAU={op_tau}, min-sim={op_s}  ->  "
              f"PRECISION {st['precision']:.1f}%  COVERAGE {st['coverage']:.1f}%  "
              f"wrong={st['on_dev_wrong']}  server-load {st['server_load']:.1f}%  "
              f"blended {st['blended_acc']:.1f}%")
        print(f"  ({st['confident']}/{st['total']} answered on-device, "
              f"{st['on_dev_correct']} correct)")
    else:
        (op_tau, op_s), st = max(grid.items(),
                   key=lambda kv: (kv[1]["precision"] if kv[1]["confident"] else -1,
                                   kv[1]["coverage"]))
        print(f"  No cell reached 99% on-device precision. Best precision cell: "
              f"TAU={op_tau}, min-sim={op_s}  ->  precision "
              f"{st['precision']:.1f}% at coverage {st['coverage']:.1f}% "
              f"(wrong={st['on_dev_wrong']}).")

    # ── per-GAME breakdown at the operating point ──
    print(f"\n  PER-GAME at operating point (TAU={op_tau}, S={op_s}):")
    print(f"  {'game':<9} {'total':>5} {'on-dev':>7} {'cov%':>6} {'prec%':>7} {'wrong':>6}")
    games_present = sorted({r["game"] for r in results})
    for g in games_present:
        gs = _hybrid_stats([r for r in results if r["game"] == g], op_tau, min_sim=op_s,
                           reprint_guard=args.reprint_guard)
        gp = f"{gs['precision']:.1f}" if gs["confident"] else "n/a"
        print(f"  {g:<9} {gs['total']:>5} {gs['confident']:>7} {gs['coverage']:>6.1f} "
              f"{gp:>7} {gs['on_dev_wrong']:>6}")

    # ── dump confident-but-wrong at the operating point (esp. YGO) ──
    op_conf_wrong = [r for r in results
                     if r["gap"] >= op_tau and r["top1_sim"] >= op_s
                     and not (args.reprint_guard and r["reprint_ambiguous"])
                     and not r["hit1"]]
    print(f"\n  CONFIDENT-BUT-WRONG at operating point: {len(op_conf_wrong)}")
    for r in op_conf_wrong:
        flag = "  <-- YGO (unverified by audit)" if r["game"] == "yugioh" else ""
        print(f"    [{r['game']}] {r['fn']}  true={r['true']}  pred={r['pred']}  "
              f"top1={r['top1_sim']:.4f} gap={r['gap']:.4f}{flag}")
    op_err_path = f"operating_point_errors_{args.model}.csv"
    with open(op_err_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["game", "fn", "true", "pred", "top1_sim", "gap", "top5"])
        for r in op_conf_wrong:
            w.writerow([r["game"], r["fn"], r["true"], r["pred"], r["top1_sim"],
                        r["gap"], json.dumps(r["top5"])])
    print(f"  -> dumped to {op_err_path}")

    # ── reprint-guard diagnostic: did the guard catch the confident errors? ──
    if args.reprint_guard:
        print("\n========== REPRINT-GUARD EFFECT (vs no guard) ==========")
        # Reference cell to inspect the actual confident-but-wrong cases.
        ref_tau, ref_s = 0.02, 0.80
        confident_noguard = [r for r in results
                             if r["gap"] >= ref_tau and r["top1_sim"] >= ref_s]
        wrong_noguard = [r for r in confident_noguard if not r["hit1"]]
        print(f"  at TAU={ref_tau}, min-sim={ref_s}: "
              f"{len(wrong_noguard)} confident-but-wrong WITHOUT guard")
        for r in wrong_noguard:
            ident = _card_identity(r["pred"])
            shared = [s for s, _ in r["top5"] if _card_identity(s) == ident]
            caught = r["reprint_ambiguous"]
            print(f"    {r['true']:<24.24} -> {r['pred']:<24.24} "
                  f"top1={r['top1_sim']:.4f} gap={r['gap']:.4f}  "
                  f"id='{ident}' x{len(shared)}  "
                  f"{'GUARD PUNTS (removed)' if caught else 'survives guard'}")
        removed = sum(1 for r in wrong_noguard if r["reprint_ambiguous"])
        print(f"  => guard removes {removed}/{len(wrong_noguard)} of these errors")
        # also: how many CORRECT confident answers does the guard cost?
        correct_noguard = [r for r in confident_noguard if r["hit1"]]
        lost = sum(1 for r in correct_noguard if r["reprint_ambiguous"])
        print(f"  => guard also punts {lost}/{len(correct_noguard)} correct-confident "
              f"answers (coverage cost)")

# ───────────────────────── misses dump ─────────────────────────
miss_path = f"misses_{args.model}.csv"
with open(miss_path, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["fn", "bucket", "true", "pred", "gap", "top5"])
    for r in results:
        if not r["hit1"]:
            w.writerow([r["fn"], r["bucket"], r["true"], r["pred"], r["gap"],
                        json.dumps(r["top5"])])
print(f"\nmisses written to {miss_path}")
