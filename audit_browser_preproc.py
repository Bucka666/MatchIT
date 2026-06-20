# audit_browser_preproc.py — READ-ONLY, no phone, no server.
# Production-consistency check: re-embed BOTH the index and the 150 queries with
# plain-bilinear ("browser-like") preprocessing (matched, as they'd be on-device)
# and re-run the MobileCLIP2-S2 hybrid. Compare to the torchvision pipeline
# per-card: did any top-1 change, or any card flip across the gap/sim gate?
import os
os.environ.setdefault("FLAGS_use_mkldnn", "0")
import warnings; warnings.filterwarnings("ignore")
import csv, json
import numpy as np, torch, open_clip
from PIL import Image

MODEL, PRE = "MobileCLIP2-S2", "dfndr2b"
CARDSDB = r"C:\CardsDB"; QUERIES = "test_queries"; GT = "groundtruth.csv"
SCORE_FLOOR = 0.65; TAU, S = 0.02, 0.80
GAMES = ("pokemon", "mtg", "yugioh")
IDX = os.path.join(".cache_mobileclip_test", "cardsdb_index.json")
TV_DIR  = os.path.join(".cache_mobileclip_test", "embeds", f"{MODEL}__{PRE}")        # torchvision (existing)
BIL_DIR = os.path.join(".cache_mobileclip_test", "embeds_bil", f"{MODEL}__{PRE}")    # plain bilinear (new)
os.makedirs(BIL_DIR, exist_ok=True)
dev = "cuda" if torch.cuda.is_available() else "cpu"

def game_of(s): return "yugioh" if s.startswith("ygo-") else "mtg" if s.startswith("mtg-") else "pokemon"
def front(sku):
    for g in [game_of(sku)] + [x for x in GAMES if x != game_of(sku)]:
        p = os.path.join(CARDSDB, g, sku, "front.png")
        if os.path.isfile(p): return p
    return None

print("[load] model ...", flush=True)
model, _, tv_preprocess = open_clip.create_model_and_transforms(MODEL, pretrained=PRE)
model = model.to(dev).eval()

def bil_preprocess(img):
    """plain bilinear (no antialias) resize shortest->256, center crop 256, [0,1]."""
    W, H = img.size; s = 256 / min(W, H); nw, nh = round(W*s), round(H*s)
    im = img.resize((nw, nh), Image.BILINEAR)
    l = (nw-256)//2; t = (nh-256)//2; im = im.crop((l, t, l+256, t+256))
    return torch.from_numpy(np.asarray(im, dtype=np.float32)/255.).permute(2,0,1)

@torch.no_grad()
def embed(t):
    f = model.encode_image(t.unsqueeze(0).to(dev)); f = f/f.norm(dim=-1, keepdim=True)
    return f.squeeze(0).cpu().numpy().astype(np.float32)

def embed_path(path, prep):
    return embed(prep(Image.open(path).convert("RGB")))

# ---- ground truth (corrected) ----
gt = {}
with open(GT, encoding="utf-8") as f:
    for r in csv.DictReader(f):
        if r["server_sku"]:
            try:
                if float(r["score"]) >= SCORE_FLOOR: gt[r["filename"]] = r["server_sku"]
            except (TypeError, ValueError): pass

idx = json.load(open(IDX, encoding="utf-8"))
def expand(gskus):
    out = set()
    for sku in gskus:
        out.add(sku); m = idx["sku"].get(sku)
        if not m: continue
        g = m["game"]; out |= set(idx["sets"].get(f"{g}::{m['set_id']}", []))
        if m["name"]: out |= set(idx["names"].get(f"{g}::{m['name']}", []))
    return out
needed = sorted(expand(set(gt.values())))

# ---- build aligned index matrices (only skus that have BOTH a tv-cache and a front img) ----
print(f"[db] embedding {len(needed)} candidates (plain-bilinear; tv from cache) ...", flush=True)
db_skus, M_tv, M_bil = [], [], []
done = 0
for sku in needed:
    tvp = os.path.join(TV_DIR, f"{sku}.npy")
    if not os.path.isfile(tvp):  # keep index identical to the torchvision run
        continue
    fp = front(sku)
    if not fp: continue
    bilp = os.path.join(BIL_DIR, f"{sku}.npy")
    if os.path.isfile(bilp):
        bv = np.load(bilp)
    else:
        bv = embed_path(fp, bil_preprocess); np.save(bilp, bv)
    db_skus.append(sku); M_tv.append(np.load(tvp)); M_bil.append(bv)
    done += 1
    if done % 1000 == 0: print(f"   ...{done}", flush=True)
M_tv = np.stack(M_tv); M_bil = np.stack(M_bil)
print(f"[db] aligned index: {M_tv.shape[0]} cards", flush=True)

# ---- per-query both pipelines ----
def topk(M, q):
    sims = M @ q; order = np.argsort(-sims)[:5]
    top = [(db_skus[i], float(sims[i])) for i in order]
    return top, top[0][0], float(top[0][1]), float(top[0][1]-top[1][1])

rows = []
for fn, true in gt.items():
    qp = os.path.join(QUERIES, fn)
    if not os.path.isfile(qp): continue
    q_tv = embed_path(qp, tv_preprocess); q_bil = embed_path(qp, bil_preprocess)
    _, p_tv, s_tv, g_tv = topk(M_tv, q_tv)
    _, p_bil, s_bil, g_bil = topk(M_bil, q_bil)
    rows.append({"fn": fn, "true": true, "game": game_of(true),
                 "pred_tv": p_tv, "s_tv": s_tv, "g_tv": g_tv,
                 "pred_bil": p_bil, "s_bil": s_bil, "g_bil": g_bil})

def confident(s, g): return g >= TAU and s >= S
n = len(rows)

# ---- operating point on the BILINEAR-matched run ----
def hybrid(rs):
    conf = [r for r in rs if confident(r["s_bil"], r["g_bil"])]
    cor = sum(1 for r in conf if r["pred_bil"] == r["true"])
    return len(rs), len(conf), cor, len(conf)-cor

print("\n========== BILINEAR-MATCHED OPERATING POINT (TAU=0.02, S=0.80) ==========")
tot, conf, cor, wrong = hybrid(rows)
print(f"  on-device: {conf}/{tot} = {100*conf/tot:.1f}% coverage | precision "
      f"{100*cor/conf:.1f}% ({cor}/{conf}) | confident-wrong {wrong}")
print(f"\n  per-game:")
print(f"  {'game':<9}{'total':>6}{'on-dev':>7}{'cov%':>7}{'prec%':>7}{'wrong':>6}")
for gm in GAMES:
    rs = [r for r in rows if r["game"] == gm]
    t, c, cc, w = hybrid(rs)
    pp = f"{100*cc/c:.1f}" if c else "n/a"
    print(f"  {gm:<9}{t:>6}{c:>7}{(100*c/t if t else 0):>7.1f}{pp:>7}{w:>6}")

# ---- diffs vs torchvision pipeline ----
top1_changed = [r for r in rows if r["pred_tv"] != r["pred_bil"]]
gate_flipped = [r for r in rows
                if confident(r["s_tv"], r["g_tv"]) != confident(r["s_bil"], r["g_bil"])]
print(f"\n========== DIFFS vs torchvision pipeline ==========")
print(f"  top-1 changed: {len(top1_changed)} / {n}")
for r in top1_changed:
    print(f"    [{r['game']}] {r['fn']} true={r['true']}  tv->{r['pred_tv']}  bil->{r['pred_bil']}")
print(f"  gate flips (confident<->fallback): {len(gate_flipped)} / {n}")
for r in gate_flipped:
    ctv = confident(r["s_tv"], r["g_tv"]); cb = confident(r["s_bil"], r["g_bil"])
    print(f"    [{r['game']}] {r['fn']} true={r['true']} pred_bil={r['pred_bil']}  "
          f"tv(s={r['s_tv']:.3f},g={r['g_tv']:.3f},{'C' if ctv else 'F'}) -> "
          f"bil(s={r['s_bil']:.3f},g={r['g_bil']:.3f},{'C' if cb else 'F'})")

# whether any flip introduced a NEW confident error
new_err = [r for r in gate_flipped
           if confident(r["s_bil"], r["g_bil"]) and r["pred_bil"] != r["true"]]
print(f"\n  NEW confident errors introduced by bilinear: {len(new_err)}")
for r in new_err:
    print(f"    [{r['game']}] {r['fn']} true={r['true']} pred_bil={r['pred_bil']}")

with open("browser_preproc_diffs.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f); w.writerow(["fn","game","true","pred_tv","s_tv","g_tv","pred_bil","s_bil","g_bil"])
    for r in rows:
        w.writerow([r["fn"],r["game"],r["true"],r["pred_tv"],round(r["s_tv"],4),round(r["g_tv"],4),
                    r["pred_bil"],round(r["s_bil"],4),round(r["g_bil"],4)])
print("\n[saved] browser_preproc_diffs.csv")
