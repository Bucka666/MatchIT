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

Also holds cross_check_back_language(), which compares the classifier's
back_label against the confirmed SKU's own set_id (jpn- prefix = Japanese
release). This is deliberately a separate, gentler check from
_build_auth_result_for_result()'s existing back_type/expectedBack logic in
app.py: that function's mismatch path terminates in "counterfeit", which
is wrong here — a front/back language mismatch is just as likely to mean
"user photographed two different cards" as anything about authenticity.
cross_check_back_language() never returns counterfeit; its worst outcome
is "needs_review".
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


def cross_check_back_language(set_id: str, back_label: str) -> dict:
    """Compare the classifier's back_label against the confirmed SKU's own
    set_id. Standalone -- not called from /match yet.

    front_language is derived from set_id.startswith("jpn-") alone (see
    Phase 2 recon: this is a construction-time label assigned by which
    scraper ingested the set, not a content-inspection heuristic — the
    128 jpn- / 174 non-jpn- split across set_metadata.json's POKEMON
    entries is exact and non-overlapping by definition of the prefix
    check; no evidence found of a set mislabelled either direction).

    Rules:
        back_label == "unknown"                          -> no_back_signal
        back_label == "japanese"      and jpn- front      -> back_consistent
        back_label == "english-style" and non-jpn- front  -> back_consistent
        anything else                                     -> needs_review

    NEVER returns a counterfeit-flavoured status — worst case is
    "needs_review" with a front_back_language_mismatch flag. A mismatch
    here is just as likely to mean "two different cards got photographed"
    as anything about authenticity, so this stays deliberately softer than
    _build_auth_result_for_result()'s existing (still-inert) back_type
    mismatch path in app.py, which terminates in "counterfeit".
    """
    set_id = (set_id or "").strip()
    is_jpn_front = set_id.startswith("jpn-")
    front_language = "japanese" if is_jpn_front else "english-style"

    if back_label == "unknown":
        status = "no_back_signal"
        flags: list = []
        reason = "No back-image language signal available."
    elif (back_label == "japanese" and is_jpn_front) or (back_label == "english-style" and not is_jpn_front):
        status = "back_consistent"
        flags = []
        back_desc = "Japanese" if back_label == "japanese" else "English"
        reason = f"The card back appears {back_desc}, consistent with this set's release."
    else:
        status = "needs_review"
        flags = ["front_back_language_mismatch"]
        front_desc = "a Japanese release" if is_jpn_front else "an English release"
        back_desc = "Japanese" if back_label == "japanese" else "English"
        reason = f"The set code indicates {front_desc}, but the card back appears to be {back_desc}."

    print(f"[AUTH-XCHECK] set_id={set_id} front_language={front_language} "
          f"back_label={back_label} status={status} "
          f"flag={flags[0] if flags else None}", flush=True)

    return {
        "status": status,
        "flags": flags,
        "reason": reason,
        "set_id": set_id,
        "front_language": front_language,
        "back_label": back_label,
    }
