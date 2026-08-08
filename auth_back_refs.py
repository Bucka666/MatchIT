"""
auth_back_refs.py — Card-back style classifier (EN vs JP), standalone
=======================================================================
Phase 2 recon/build. NOT wired into app.py or /match — this module is
self-contained and nothing in the live app imports it yet.

Classifies a photographed card back as "english-style" / "japanese" /
"unknown" by nearest-neighbour cosine similarity against exactly two
reference embeddings (one EN back, one JP back — see auth_refs/).

Deliberately does NOT touch images.db, FRONT_MATRIX or BACK_MATRIX — see
the Phase 2 recon for why: a SKU with only a back image can never become
a match candidate (Stage 1 of _run_match_paired_two_stage requires a front
image), but BACK_MATRIX is pooled unfiltered into the orientation-swap
resolver, so anything added there has a live side effect on real scans.
This reference store is a separate, tiny file with zero coupling to the
matching engine.
"""

import json
import os
from typing import Optional, Tuple

import numpy as np

# ── Tunable without a code change. Evidence (see Phase 2 build report):
# a single gap-based floor (|sim_en - sim_jp|) does not work -- tested on
# 6 real photos of the two reference cards (varied lighting/angle/glare)
# and 20 real card fronts, the minimum positive gap (0.0316) fell BELOW
# the maximum negative gap (0.1035): the distributions overlap, no split
# point exists. Splitting the decision into two stages instead removed
# the overlap entirely:
#   weakest positive max(sim_en, sim_jp)  = 0.8988 (JP_test_3)
#   strongest negative max(sim_en, sim_jp) = 0.6208 (base1-4)
#   midpoint = (0.8988 + 0.6208) / 2 = 0.7598, rounded to 0.76
# At 0.76: 26/26 correct (6/6 positives labelled correctly, 20/20
# negatives rejected as unknown) -- see classify_back_style().
BACK_PRESENCE_FLOOR = 0.76

_REFS_VOLUME_DIR = "/modal_data/auth_refs"
_REFS_FILENAME = "back_style_refs.npz"
_REFS_META_FILENAME = "back_style_refs.json"

_EMBED_PARAMS = dict(multi_crop=False, suppress_bg=True, max_side=1024)

_cached_refs: Optional[Tuple[np.ndarray, np.ndarray]] = None
_cached_embedder = None


def _refs_candidate_paths():
    candidates = []
    if os.path.exists("/modal_data"):
        candidates.append(os.path.join(_REFS_VOLUME_DIR, _REFS_FILENAME))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth_refs", _REFS_FILENAME))
    return candidates


def load_back_style_refs(force: bool = False) -> Tuple[np.ndarray, np.ndarray]:
    """Load the two reference embeddings (english-style, japanese).
    Volume-first, local-file fallback (same pattern as ocr_confirm.py's
    set_metadata loader) — mirrors app.py's own conventions rather than
    inventing a new one.
    """
    global _cached_refs
    if _cached_refs is not None and not force:
        return _cached_refs

    for path in _refs_candidate_paths():
        if os.path.exists(path):
            data = np.load(path)
            refs = (data["english_style"].astype(np.float32), data["japanese"].astype(np.float32))
            _cached_refs = refs
            return refs

    raise FileNotFoundError(
        f"back_style_refs.npz not found in any of: {_refs_candidate_paths()}. "
        f"Run build_auth_back_refs.py first."
    )


def _get_embedder():
    global _cached_embedder
    if _cached_embedder is None:
        from feature_extractor import ImageEmbedder
        _cached_embedder = ImageEmbedder()
    return _cached_embedder


def embed_back_image(image_path: str, embedder=None) -> np.ndarray:
    """Embed a card-back photo with the same preprocessing settings the
    live /match pipeline already uses for back images (params_back in
    match()) — single-crop, background-suppressed, max_side=1024 — so a
    reference built here is comparable to what a real scan would produce.
    """
    emb = embedder or _get_embedder()
    v = emb.embed_path(str(image_path), **_EMBED_PARAMS)
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v) + 1e-12)
    return v / n


def classify_back_style(
    image_path: str,
    refs: Optional[Tuple[np.ndarray, np.ndarray]] = None,
    embedder=None,
) -> Tuple[str, float, float, float]:
    """Classify a card-back photo as "english-style" / "japanese" / "unknown".

    Two stages — a single |sim_en - sim_jp| gap floor was tried first and
    failed (see BACK_PRESENCE_FLOOR's comment): it conflates "is this a
    card back at all" with "which language", and those two questions
    turned out to need different signals.

      Stage 1 (is this a card back?): is_back = max(sim_en, sim_jp) >=
      BACK_PRESENCE_FLOOR. Below the floor -> return "unknown" immediately.
      This is where ALL of the separation lives (see below).

      Stage 2 (which language?): only reached if stage 1 passed.
      label = "english-style" if sim_en >= sim_jp else "japanese".

    *** Stage 2 has no discriminative power on non-backs. *** All 20 real
    card fronts tested (Pokemon across WOTC/BW/XY/SM/SV/SWSH eras, MTG,
    YGO, one JP-card front) scored sim_en > sim_jp -- every one of them
    would be confidently labelled "english-style" if stage 2 ran on it.
    Stage 1 is the only thing standing between this function and "everything
    that isn't a card back gets called english-style". Anyone weakening or
    bypassing BACK_PRESENCE_FLOOR turns this into that classifier.

    Returns (label, similarity_en, similarity_jp, gap). gap is
    |sim_en - sim_jp| -- kept in the return value for logging/debugging
    only, it is NOT a decision input anywhere in this function anymore.
    """
    en_ref, jp_ref = refs if refs is not None else load_back_style_refs()
    v = embed_back_image(image_path, embedder=embedder)

    sim_en = float(np.dot(v, en_ref))
    sim_jp = float(np.dot(v, jp_ref))
    gap = abs(sim_en - sim_jp)

    passed_stage1 = max(sim_en, sim_jp) >= BACK_PRESENCE_FLOOR
    if passed_stage1:
        label = "english-style" if sim_en >= sim_jp else "japanese"
    else:
        label = "unknown"

    print(f"[BACK-STYLE] label={label} sim_en={sim_en:.4f} sim_jp={sim_jp:.4f} "
          f"floor={BACK_PRESENCE_FLOOR} passed_stage1={passed_stage1}", flush=True)

    return label, sim_en, sim_jp, gap
