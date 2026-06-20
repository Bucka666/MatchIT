# build_ondevice_index.py — First on-device match index (Commander included).
#
# ============================================================================
#  PREPROCESSING IS VERSIONED IN LOCKSTEP WITH THE BROWSER.
# ----------------------------------------------------------------------------
#  This index is built with PLAIN BILINEAR preprocessing:
#     1. resize SHORTEST side -> 256  (PIL Image.BILINEAR, NO antialias)
#     2. center-crop 256x256
#     3. scale to [0,1], NCHW, mean=0 / std=1 (no normalization)
#  This is byte-for-byte the pipeline validated in audit_browser_preproc.py
#  (bil_preprocess) which achieved 100% precision @ 53% coverage on 150 cards.
#
#  ⚠️  The browser-side canvas preprocessing MUST match this EXACTLY. If you
#  change the resize/crop/antialias here, you MUST change the browser in the
#  same commit and bump INDEX_VERSION. A silent mismatch (e.g. antialias on
#  one side only) degrades matching with NO error thrown. The current spike
#  HTML (web_spike/test_onnx_inference.html) uses antialiased smoothing and
#  must be switched to plain bilinear to stay in lockstep with this index.
# ============================================================================
import os, sys, json, time, hashlib, shutil
os.environ.setdefault("FLAGS_use_mkldnn", "0")
import warnings; warnings.filterwarnings("ignore")
import numpy as np
import torch, open_clip
from PIL import Image

INDEX_VERSION    = "gs-ondevice-v1"          # bump together with browser preprocessing
PREPROC_ID       = "plain-bilinear-shortest256-centercrop256"
MODEL, PRE       = "MobileCLIP2-S2", "dfndr2b"
CARDSDB          = r"C:\CardsDB"
BATCH            = 256
CHECKPOINT_EVERY = int(os.environ.get("GS_BUILD_CHECKPOINT_EVERY", "2048"))
BUILD_LIMIT      = int(os.environ.get("GS_BUILD_LIMIT", "0"))   # 0 = all
OUT_DIR          = os.environ.get("GS_BUILD_OUT_DIR", "ondevice_index_v1")
CKPT_DIR         = os.path.join(OUT_DIR, "_checkpoint")
DRY_RUN          = os.environ.get("DRY_RUN") == "1"

# Keep these MTG set_types; drop only 'funny'. Pokemon: all (no filter).
KEEP_MTG = {"expansion", "masters", "core", "draft_innovation", "commander"}

os.makedirs(OUT_DIR, exist_ok=True)


def bil_preprocess(img):
    """Plain bilinear (no antialias) resize shortest->256, center crop 256, [0,1].
    Identical to audit_browser_preproc.py::bil_preprocess — see lockstep banner."""
    W, H = img.size
    s = 256 / min(W, H)
    nw, nh = round(W * s), round(H * s)
    im = img.resize((nw, nh), Image.BILINEAR)
    l = (nw - 256) // 2; t = (nh - 256) // 2
    im = im.crop((l, t, l + 256, t + 256))
    return np.asarray(im, dtype=np.float32) / 255.   # HWC, [0,1]


def select_skus():
    dates = json.load(open("set_dates.json", encoding="utf-8"))
    skus = []   # (game, sku)
    # Pokemon: all
    pk = os.path.join(CARDSDB, "pokemon")
    for x in os.scandir(pk):
        if x.is_dir():
            skus.append(("pokemon", x.name))
    # MTG: keep by set_type
    mt = os.path.join(CARDSDB, "mtg")
    kept_mtg = 0
    for x in os.scandir(mt):
        if not x.is_dir():
            continue
        sid = x.name.split("-")[1]
        meta = dates.get(sid)
        if not meta or meta.get("source") != "scryfall":
            continue
        if (meta.get("set_type") or "") in KEEP_MTG:
            skus.append(("mtg", x.name)); kept_mtg += 1
    return skus, kept_mtg


def front_path(game, sku):
    p = os.path.join(CARDSDB, game, sku, "front.png")
    return p if os.path.isfile(p) else None


def main():
    t0 = time.time()
    skus, kept_mtg = select_skus()
    pk_n = sum(1 for g, _ in skus if g == "pokemon")
    print(f"[select] pokemon(all)={pk_n}  mtg(kept)={kept_mtg}  total={len(skus)}", flush=True)

    # resolve front.png, count missing
    resolved = []
    missing = 0
    for game, sku in skus:
        fp = front_path(game, sku)
        if fp:
            resolved.append((sku, fp))
        else:
            missing += 1
    print(f"[front] resolved={len(resolved)}  missing_front={missing}", flush=True)

    # B2: deterministic order + optional limit
    resolved.sort(key=lambda r: r[0])
    if BUILD_LIMIT > 0:
        resolved = resolved[:BUILD_LIMIT]
    print(f"[order] sorted by sku, N={len(resolved)} "
          f"(test_limit={BUILD_LIMIT if BUILD_LIMIT else 'none'})", flush=True)
    planned_sku_list = [r[0] for r in resolved]
    planned_hash = hashlib.sha256(
        json.dumps(planned_sku_list).encode()
    ).hexdigest()[:16]
    print(f"[order] planned_hash={planned_hash}", flush=True)

    if DRY_RUN:
        print("[dry-run] stopping before model load / embedding.", flush=True)
        return

    print(f"[load] {MODEL}/{PRE} ...", flush=True)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model, _, _ = open_clip.create_model_and_transforms(MODEL, pretrained=PRE)
    model = model.to(dev).eval()

    # B3: checkpoint-resume initialisation
    N = len(resolved)
    start_idx = 0
    vecs = np.zeros((N, 512), dtype=np.float16)
    out_skus = []

    manifest_path = os.path.join(CKPT_DIR, "_resume_manifest.json")
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            m = json.load(f)
        if m.get("planned_hash") != planned_hash:
            print("[checkpoint] ABORT: planned SKU list differs from checkpoint", flush=True)
            print(f"  checkpoint hash: {m.get('planned_hash')}", flush=True)
            print(f"  current hash:    {planned_hash}", flush=True)
            print(f"  Delete {CKPT_DIR} to start fresh, or restore the prior input state.",
                  flush=True)
            sys.exit(2)
        part_vecs = np.load(os.path.join(CKPT_DIR, "vectors_f16.npy.partial"))
        with open(os.path.join(CKPT_DIR, "skus.json.partial"), encoding="utf-8") as f:
            part_skus = json.load(f)
        assert part_vecs.shape[0] == len(part_skus), \
            f"checkpoint corruption: vecs={part_vecs.shape[0]} skus={len(part_skus)}"
        vecs[:len(part_skus)] = part_vecs
        out_skus.extend(part_skus)
        start_idx = len(part_skus)
        print(f"[checkpoint] resuming from {start_idx}/{N} "
              f"({start_idx * 100 / N:.1f}%)", flush=True)
    else:
        os.makedirs(CKPT_DIR, exist_ok=True)
        print(f"[checkpoint] fresh build, cadence={CHECKPOINT_EVERY}", flush=True)

    # B5: atomic checkpoint writer
    # np.save appends ".npy" when the destination path does not already end
    # with ".npy". Our tmp path ends with ".tmp", so numpy creates
    # "vectors_f16.npy.partial.tmp.npy"; os.replace then renames it to the
    # final ".partial" name with no ".npy" suffix (np.load reads by magic
    # bytes, not extension).
    def write_checkpoint(vecs, out_skus, planned_hash, total):
        os.makedirs(CKPT_DIR, exist_ok=True)
        v_partial = vecs[:len(out_skus)]
        tmp_vecs = os.path.join(CKPT_DIR, "vectors_f16.npy.partial.tmp")
        tmp_skus = os.path.join(CKPT_DIR, "skus.json.partial.tmp")
        tmp_mani = os.path.join(CKPT_DIR, "_resume_manifest.json.tmp")
        np.save(tmp_vecs, v_partial)   # writes tmp_vecs + ".npy"
        with open(tmp_skus, "w", encoding="utf-8") as f:
            json.dump(out_skus, f)
        with open(tmp_mani, "w", encoding="utf-8") as f:
            json.dump({
                "planned_hash": planned_hash,
                "completed_count": len(out_skus),
                "total_count": total,
            }, f, indent=2)
        final_vecs = os.path.join(CKPT_DIR, "vectors_f16.npy.partial")
        os.replace(tmp_vecs + ".npy", final_vecs)
        os.replace(tmp_skus, os.path.join(CKPT_DIR, "skus.json.partial"))
        os.replace(tmp_mani, os.path.join(CKPT_DIR, "_resume_manifest.json"))

    last_ckpt_count = start_idx
    last_ckpt_time  = time.time()

    @torch.no_grad()
    def flush_batch(buf_imgs, buf_skus, start):
        t = torch.from_numpy(np.stack(buf_imgs)).permute(0, 3, 1, 2).to(dev)
        f = model.encode_image(t)
        f = f / f.norm(dim=-1, keepdim=True)
        vecs[start:start + len(buf_skus)] = f.cpu().numpy().astype(np.float16)
        out_skus.extend(buf_skus)

    buf_imgs, buf_skus, start = [], [], start_idx
    for sku, fp in resolved[start_idx:]:          # B4: skip already-done entries
        try:
            arr = bil_preprocess(Image.open(fp).convert("RGB"))
        except Exception as e:
            print(f"[skip] {sku}: {e}", flush=True)
            continue
        buf_imgs.append(arr); buf_skus.append(sku)
        if len(buf_imgs) == BATCH:
            flush_batch(buf_imgs, buf_skus, start)
            start += len(buf_skus)
            buf_imgs, buf_skus = [], []
            # B6: checkpoint + rate print when enough cards since last save
            if len(out_skus) - last_ckpt_count >= CHECKPOINT_EVERY:
                elapsed = time.time() - last_ckpt_time
                rate    = (len(out_skus) - last_ckpt_count) / elapsed if elapsed > 0 else 0
                pct     = 100 * len(out_skus) / N
                write_checkpoint(vecs, out_skus, planned_hash, N)
                print(f"[checkpoint] saved {len(out_skus)}/{N} ({pct:.1f}%) "
                      f"-- {rate:.0f} cards/sec", flush=True)
                last_ckpt_count = len(out_skus)
                last_ckpt_time  = time.time()
    if buf_imgs:
        flush_batch(buf_imgs, buf_skus, start)

    vecs = vecs[:len(out_skus)]   # trim any skipped rows

    vec_path  = os.path.join(OUT_DIR, "vectors_f16.npy")
    sku_path  = os.path.join(OUT_DIR, "skus.json")
    meta_path = os.path.join(OUT_DIR, "meta.json")
    np.save(vec_path, vecs)
    json.dump(out_skus, open(sku_path, "w", encoding="utf-8"))
    meta = {
        "index_version": INDEX_VERSION,
        "preproc_id": PREPROC_ID,
        "preproc": "resize shortest-side->256 (PIL BILINEAR, no antialias) + center-crop 256, /255, NCHW, mean0/std1",
        "model": MODEL, "pretrained": PRE,
        "dim": 512, "dtype": "float16", "l2_normalized": True,
        "count": int(vecs.shape[0]),
        "pokemon": pk_n, "mtg_kept": kept_mtg,
        "lockstep_note": "Browser canvas preprocessing MUST match preproc_id; bump index_version on any change.",
    }
    json.dump(meta, open(meta_path, "w", encoding="utf-8"), indent=2)

    # B7: remove checkpoint dir on successful completion
    if os.path.exists(CKPT_DIR):
        shutil.rmtree(CKPT_DIR)
        print(f"[checkpoint] build complete, removed {CKPT_DIR}", flush=True)

    sz = os.path.getsize(vec_path) / 1e6
    print(f"\n[done] vectors={vecs.shape[0]}x{vecs.shape[1]} f16  "
          f"file={vec_path} ({sz:.1f} MB)  in {time.time()-t0:.0f}s", flush=True)
    print(f"[done] version={INDEX_VERSION}  preproc={PREPROC_ID}", flush=True)


if __name__ == "__main__":
    main()
