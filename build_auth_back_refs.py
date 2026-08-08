"""
build_auth_back_refs.py — Embed the two card-back reference images
=====================================================================
Standalone Modal script. Embeds auth_refs/EN_BACK.jpeg and
auth_refs/JP_BACK.jpeg with the live embedder (same backend/preprocessing
auth_back_refs.py's classify_back_style() expects) and writes the
resulting pair to the matchit-data-v2 volume as a small, separate file:

    /modal_data/auth_refs/back_style_refs.npz   (the 2 embeddings)
    /modal_data/auth_refs/back_style_refs.json  (source/metadata)

Does NOT touch images.db, FRONT_MATRIX or BACK_MATRIX -- see
auth_back_refs.py's module docstring and the Phase 2 recon for why.

Run:
    modal run build_auth_back_refs.py
"""
import sys

sys.path.insert(0, "/app")
from modal_config import vol, image

import modal

app = modal.App("matchit-auth-back-refs")


@app.function(
    image=image,
    volumes={"/modal_data": vol},
    timeout=300,
)
def build_refs():
    import json
    import os
    import time

    import numpy as np

    sys.path.insert(0, "/app")
    from auth_back_refs import embed_back_image, _EMBED_PARAMS

    LOG = "[AUTH-REFS]"
    src_dir = "/app/auth_refs"
    sources = {
        "english_style": os.path.join(src_dir, "EN_BACK.jpeg"),
        "japanese": os.path.join(src_dir, "JP_BACK.jpeg"),
    }

    for label, path in sources.items():
        if not os.path.exists(path):
            print(f"{LOG} ABORT: missing source image {path}", flush=True)
            return

    from feature_extractor import ImageEmbedder
    embedder = ImageEmbedder()

    vectors = {}
    for label, path in sources.items():
        v = embed_back_image(path, embedder=embedder)
        vectors[label] = v
        print(f"{LOG} embedded {label} from {path} -> dim={v.shape[0]}", flush=True)

    out_dir = "/modal_data/auth_refs"
    os.makedirs(out_dir, exist_ok=True)

    npz_path = os.path.join(out_dir, "back_style_refs.npz")
    tmp_npz = npz_path + ".tmp"
    # np.savez() auto-appends ".npz" to any filename that doesn't already end
    # in it -- passing a plain ".tmp" path silently writes to
    # "back_style_refs.npz.tmp.npz" instead, breaking the rename below.
    # Passing an open file handle avoids that auto-extension behavior.
    with open(tmp_npz, "wb") as f:
        np.savez(f, english_style=vectors["english_style"], japanese=vectors["japanese"])
    os.replace(tmp_npz, npz_path)

    meta = {
        "sources": {k: os.path.basename(v) for k, v in sources.items()},
        "embedder_backend": embedder.backend_name,
        "embedding_dim": embedder.embedding_dim,
        "embed_params": _EMBED_PARAMS,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    meta_path = os.path.join(out_dir, "back_style_refs.json")
    tmp_meta = meta_path + ".tmp"
    with open(tmp_meta, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    os.replace(tmp_meta, meta_path)

    vol.commit()
    print(f"{LOG} wrote {npz_path} and {meta_path}, volume committed.", flush=True)


@app.local_entrypoint()
def main():
    build_refs.remote()
