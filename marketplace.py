"""
marketplace.py — MatchIT Marketplace v4
========================================
USP Pipeline:
  1. CLIP zero-shot classifies the uploaded product image
  2. Auto-builds a precise shopping query from classification
  3. Google Shopping returns products with prices
  4. CLIP visual re-ranking compares thumbnails to query photo
  5. Results sorted by visual similarity — best match first

The user uploads a photo of an unknown product. MatchIT figures out
what it is, finds where to buy it, and ranks by visual match. No
category selection needed — the AI handles it.

Usage:
    from marketplace import marketplace_search, auto_classify_product,
        detect_barcode, barcode_to_search_query, build_search_query
"""

import os
import time
import json
import hashlib
import numpy as np
from pathlib import Path
from io import BytesIO
from typing import Optional, Tuple
from urllib.parse import quote_plus

import requests
from PIL import Image


# ─────────────────────────────────────────────
# Barcode / QR Code Detection
# ─────────────────────────────────────────────

_PYZBAR_AVAILABLE = False
try:
    from pyzbar.pyzbar import decode as pyzbar_decode
    _PYZBAR_AVAILABLE = True
except Exception:
    pass


def detect_barcode(image_path: str) -> dict:
    """Detect and decode barcodes/QR codes in an image."""
    if not _PYZBAR_AVAILABLE:
        print("[BARCODE] pyzbar not installed — skipping", flush=True)
        return {"found": False, "code": "", "type": "", "is_url": False}
    try:
        img = Image.open(image_path).convert("L")
        results = pyzbar_decode(img)
        if not results:
            from PIL import ImageEnhance
            results = pyzbar_decode(ImageEnhance.Contrast(img).enhance(2.0))
        if not results and max(img.size) < 1000:
            s = 1500 / max(img.size)
            results = pyzbar_decode(img.resize((int(img.width * s), int(img.height * s)), Image.LANCZOS))
        if results:
            code = results[0].data.decode("utf-8", errors="replace").strip()
            print(f"[BARCODE] Detected {results[0].type}: {code}", flush=True)
            return {"found": True, "code": code, "type": results[0].type,
                    "is_url": code.startswith("http")}
        return {"found": False, "code": "", "type": "", "is_url": False}
    except Exception as e:
        print(f"[BARCODE] Error: {e}", flush=True)
        return {"found": False, "code": "", "type": "", "is_url": False}


def barcode_to_search_query(barcode_info: dict) -> str:
    return barcode_info.get("code", "")


# ─────────────────────────────────────────────
# CLIP Zero-Shot Product Classifier
# ─────────────────────────────────────────────

_CLIP_MODEL = None
_CLIP_PREPROCESS = None
_CLIP_TOKENIZER = None


def _load_clip():
    """Load CLIP model for zero-shot classification. Reuses if already loaded."""
    global _CLIP_MODEL, _CLIP_PREPROCESS, _CLIP_TOKENIZER
    if _CLIP_MODEL is not None:
        return _CLIP_MODEL, _CLIP_PREPROCESS, _CLIP_TOKENIZER

    import torch
    try:
        import open_clip
        model, _, preprocess = open_clip.create_model_and_transforms(
            "ViT-L-14", pretrained="laion2b_s32b_b82k"
        )
        tokenizer = open_clip.get_tokenizer("ViT-L-14")
        model.eval()
        _CLIP_MODEL = model
        _CLIP_PREPROCESS = preprocess
        _CLIP_TOKENIZER = tokenizer
        print("[CLASSIFY] CLIP model loaded for zero-shot", flush=True)
        return model, preprocess, tokenizer
    except Exception as e:
        print(f"[CLASSIFY] Failed to load CLIP: {e}", flush=True)
        return None, None, None


# Hardware product descriptions for zero-shot classification
# Each maps to a natural-language prompt that CLIP understands well
PRODUCT_LABELS = {
    # Screws
    "countersunk wood screw pozi drive": "WOOD_SCREW",
    "pan head machine screw": "MACHINE_SCREW",
    "self tapping screw": "SELF_TAPPING",
    "coach screw hex head lag bolt": "COACH_SCREW",
    # Bolts
    "hex bolt hexagon head bolt": "HEX_BOLT",
    "carriage bolt coach bolt round head": "CARRIAGE_BOLT",
    "threaded rod studding all thread bar": "STUD_BOLT",
    # Nuts
    "hex nut hexagon nut": "HEX_NUT",
    "nyloc nut nylon lock nut": "LOCK_NUT",
    "wing nut butterfly nut": "WING_NUT",
    "coupling nut standoff hex connector": "HEX_NUT",
    "flanged nut serrated flange": "HEX_NUT",
    "dome nut acorn nut cap nut": "HEX_NUT",
    "coupling nut hex standoff connector nut": "HEX_NUT",
    "dome nut acorn nut cap nut": "HEX_NUT",
    "flanged nut serrated flange": "HEX_NUT",
    "t nut tee nut furniture nut": "HEX_NUT",
    "cage nut server rack clip nut": "HEX_NUT",
    # Nails
    "round wire nail bright steel": "ROUND_NAIL",
    "oval wire nail": "OVAL_NAIL",
    "panel pin small nail": "PANEL_PIN",
    "masonry nail hardened concrete": "MASONRY_NAIL",
    # Washers
    "flat washer steel": "FLAT_WASHER",
    "spring washer lock washer split": "SPRING_WASHER",
    "penny washer repair washer large": "PENNY_WASHER",
    # Wall plugs
    "wall plug rawl plug anchor": "WALL_PLUG",
    "plasterboard fixing cavity anchor": "WALL_PLUG",
    # Rivets
    "blind pop rivet aluminium": "BLIND_RIVET",
    "rivet nut nutsert rivnut": "RIVET_NUT",
    # Plumbing
    "compression fitting brass plumbing": "COMPRESSION",
    "push fit plumbing fitting speedfit": "PUSH_FIT",
    "solder end feed copper fitting": "SOLDERED",
    "threaded pipe fitting bsp": "THREADED_FITTING",
}

# Broader category labels for the search query
CATEGORY_SEARCH_NAMES = {
    "WOOD_SCREW": "wood screw",
    "MACHINE_SCREW": "machine screw",
    "SELF_TAPPING": "self tapping screw",
    "COACH_SCREW": "coach screw",
    "HEX_BOLT": "hex bolt",
    "CARRIAGE_BOLT": "carriage bolt",
    "STUD_BOLT": "threaded rod",
    "HEX_NUT": "hex nut",
    "HEX_NUT": "hex nut coupling nut",
    "LOCK_NUT": "nyloc nut",
    "WING_NUT": "wing nut",
    "ROUND_NAIL": "round wire nail",
    "OVAL_NAIL": "oval nail",
    "PANEL_PIN": "panel pin",
    "MASONRY_NAIL": "masonry nail",
    "FLAT_WASHER": "flat washer",
    "SPRING_WASHER": "spring washer",
    "PENNY_WASHER": "penny washer",
    "WALL_PLUG": "wall plug",
    "BLIND_RIVET": "blind pop rivet",
    "RIVET_NUT": "rivet nut",
    "COMPRESSION": "compression fitting",
    "PUSH_FIT": "push fit fitting",
    "SOLDERED": "solder end feed fitting",
    "THREADED_FITTING": "threaded pipe fitting",
}


def auto_classify_product(image_path: str) -> Tuple[str, str, float, list]:
    """
    Zero-shot classify a product image using CLIP.

    Returns:
        (category_id, search_name, confidence, top5_predictions)

    top5_predictions is list of (label_text, category_id, score)
    """
    import torch

    model, preprocess, tokenizer = _load_clip()
    if model is None:
        return "", "hardware product", 0.0, []

    try:
        img = Image.open(image_path).convert("RGB")
        img_tensor = preprocess(img).unsqueeze(0)

        # Build text prompts
        labels = list(PRODUCT_LABELS.keys())
        prompts = [f"a photo of a {label}" for label in labels]
        text_tokens = tokenizer(prompts)

        with torch.no_grad():
            image_features = model.encode_image(img_tensor)
            text_features = model.encode_text(text_tokens)

            image_features = image_features / image_features.norm(dim=-1, keepdim=True)
            text_features = text_features / text_features.norm(dim=-1, keepdim=True)

            similarities = (image_features @ text_features.T).squeeze(0)
            probs = similarities.softmax(dim=-1)

        # Get top 5
        top5_idx = probs.argsort(descending=True)[:5]
        top5 = []
        for idx in top5_idx:
            i = idx.item()
            label_text = labels[i]
            cat_id = PRODUCT_LABELS[label_text]
            score = probs[i].item()
            top5.append((label_text, cat_id, score))

        best_label, best_cat, best_score = top5[0]
        search_name = CATEGORY_SEARCH_NAMES.get(best_cat, best_label)

        print(f"[CLASSIFY] Top prediction: {search_name} ({best_cat}) — "
              f"confidence: {best_score:.1%}", flush=True)
        for label, cat, score in top5:
            print(f"  {score:.1%}  {label} → {cat}", flush=True)

        return best_cat, search_name, best_score, top5

    except Exception as e:
        print(f"[CLASSIFY] Error: {e}", flush=True)
        return "", "hardware product", 0.0, []


# ─────────────────────────────────────────────
# Image cache for downloaded thumbnails
# ─────────────────────────────────────────────

_CACHE_DIR = None

def _get_cache_dir() -> Path:
    global _CACHE_DIR
    if _CACHE_DIR is None:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        _CACHE_DIR = Path(base) / "MatchITv2_ProductMatch_Data" / "marketplace_cache"
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR


def download_product_image(url: str, target_size: int = 512) -> Optional[str]:
    """Download a product thumbnail, resize, cache locally."""
    if not url:
        return None

    cache_path = _get_cache_dir() / f"{hashlib.md5(url.encode()).hexdigest()[:16]}.jpg"
    if cache_path.exists():
        return str(cache_path)

    try:
        resp = requests.get(url, timeout=8, headers={
            "User-Agent": "Mozilla/5.0 Chrome/120.0.0.0",
            "Accept": "image/*,*/*;q=0.8",
        })
        if resp.status_code != 200 or len(resp.content) < 2000:
            return None

        img = Image.open(BytesIO(resp.content))
        if min(img.size) < 50:
            return None
        if img.mode != "RGB":
            img = img.convert("RGB")

        w, h = img.size
        if max(w, h) > target_size:
            scale = target_size / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

        img.save(str(cache_path), "JPEG", quality=88)
        return str(cache_path)
    except Exception:
        return None


# ─────────────────────────────────────────────
# Visual re-ranking with CLIP
# ─────────────────────────────────────────────

def compute_visual_similarity(query_embedding, image_path: str, embedder) -> float:
    """Compute CLIP cosine similarity between query and product image."""
    try:
        product_embedding = embedder.embed_path(image_path, multi_crop=False, suppress_bg=False)
        if product_embedding is None or query_embedding is None:
            return 0.0

        q = np.array(query_embedding).flatten()
        p = np.array(product_embedding).flatten()
        dot = np.dot(q, p)
        nq, np_ = np.linalg.norm(q), np.linalg.norm(p)
        if nq < 1e-8 or np_ < 1e-8:
            return 0.0
        return float(dot / (nq * np_))
    except Exception:
        return 0.0


# ─────────────────────────────────────────────
# Google Shopping text search
# ─────────────────────────────────────────────

def search_google_shopping(query: str, api_key: str, num_results: int = 15,
                           location: str = "United Kingdom") -> list[dict]:
    """Search Google Shopping via SerpAPI."""
    params = {
        "engine": "google_shopping",
        "q": query,
        "api_key": api_key,
        "num": num_results,
        "gl": "uk",
        "hl": "en",
        "location": location,
    }
    try:
        resp = requests.get("https://serpapi.com/search", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[SHOPPING] SerpAPI error: {e}", flush=True)
        return []

    results = []
    for item in data.get("shopping_results", [])[:num_results]:
        title = item.get("title", "Unknown")
        source = item.get("source", "")
        
        # Build a direct search link to find this product at the retailer
        if source:
            direct_search = f"https://www.google.co.uk/search?q={quote_plus(title + ' ' + source)}"
        else:
            direct_search = f"https://www.google.co.uk/search?q={quote_plus(title)}"
        
        results.append({
            "title": title,
            "price": item.get("extracted_price") or item.get("price", ""),
            "price_raw": item.get("extracted_price", 0) or 0,
            "currency": "£",
            "source": source,
            "link": direct_search,
            "product_link": item.get("product_link", ""),
            "thumbnail_url": item.get("thumbnail", ""),
        })

    print(f"[SHOPPING] {len(results)} results for: {query}", flush=True)
    return results


# ─────────────────────────────────────────────
# Build search query from profile fields
# ─────────────────────────────────────────────

def build_search_query(category: str, profile: dict, categories: dict) -> str:
    """Build a search query from user-selected category and profile fields."""
    parts = []
    if category and category in categories:
        parts.append(categories[category].get("label", category))

    for field_id in ["head_type", "drive_type", "material", "finish", "diameter",
                     "length_mm", "thread_type", "washer_form", "anchor_type",
                     "pipe_diameter", "fitting_shape", "mandrel_material", "grip_range"]:
        val = profile.get(field_id, "")
        if not val or str(val).strip() in ("", "-1", "-1.0", "-1.00"):
            continue
        try:
            if float(str(val).strip()) < 0:
                continue
        except (ValueError, TypeError):
            pass
        parts.append(str(val).replace("_", " ").title())

    return " ".join(parts) if parts else "hardware fastener"


# ─────────────────────────────────────────────
# Main search orchestrator
# ─────────────────────────────────────────────

def marketplace_search(
    query_image_path: Optional[str],
    search_terms: str,
    api_key: str,
    embedder=None,
    max_results: int = 10,
    num_fetch: int = 15,
) -> dict:
    """
    MatchIT Marketplace search pipeline:

    1. CLIP zero-shot classifies the product from the image
    2. Builds a precise search query automatically
    3. Queries Google Shopping for products with prices
    4. Downloads thumbnails and CLIP re-ranks by visual similarity
    5. Returns results sorted by best visual match

    Returns dict:
    {
        "results": [...],
        "classification": {"category": "HEX_NUT", "name": "hex nut",
                          "confidence": 0.85, "top5": [...]},
        "search_query": "hex nut M10 zinc plated",
        "search_mode": "ai_visual" | "text" | "barcode",
        "timings": {"classify": 1.2, "search": 1.5, "rerank": 8.0, "total": 10.7}
    }
    """
    _t0 = time.time()
    timings = {}
    classification = None
    search_mode = "text"
    final_query = search_terms

    # ─── STEP 1: AI Classification (if image provided) ───
    if query_image_path and os.path.exists(query_image_path):
        _tc = time.time()
        cat_id, search_name, confidence, top5 = auto_classify_product(query_image_path)
        timings["classify"] = round(time.time() - _tc, 1)

        classification = {
            "category": cat_id,
            "name": search_name,
            "confidence": confidence,
            "top5": [(label, cat, round(score, 3)) for label, cat, score in top5],
        }

        # If AI is confident, use its classification for the search query
        # If user also provided manual text, prefer that (they know better)
        if search_terms.strip():
            # User typed something — use their text, AI still re-ranks visually
            final_query = search_terms
            search_mode = "ai_visual"
        elif search_name and search_name != "hardware product":
            # Always use AI's best guess — even low confidence is better than generic
            # Use the specific label that won, not just the category name
            best_label = classification["top5"][0][0] if classification.get("top5") else             	search_name
            final_query = best_label
            search_mode = "ai_visual"
        else:
            final_query = "hardware product"
            search_mode = "text"

        print(f"[MARKETPLACE] AI says: {search_name} ({confidence:.0%}) → "
              f"query: '{final_query}'", flush=True)
    else:
        search_mode = "text"

    # ─── STEP 2: Google Shopping search ───
    _ts = time.time()
    shopping_results = search_google_shopping(final_query, api_key, num_results=num_fetch)
    timings["search"] = round(time.time() - _ts, 1)

    if not shopping_results:
        return {
            "results": [],
            "classification": classification,
            "search_query": final_query,
            "search_mode": search_mode,
            "timings": timings,
        }

    # ─── STEP 3: Visual re-ranking (if image + embedder available) ───
    scored = []
    has_visual_ranking = False

    if query_image_path and embedder and os.path.exists(query_image_path):
        _tr = time.time()

        print(f"[MARKETPLACE] Embedding query image (single crop)...", flush=True)
        query_embedding = embedder.embed_path(query_image_path, multi_crop=False, suppress_bg=False)

        if query_embedding is not None:
            has_visual_ranking = True
            print(f"[MARKETPLACE] Re-ranking {len(shopping_results)} results by visual similarity...", flush=True)

            for item in shopping_results:
                thumb_url = item.get("thumbnail_url", "")
                local_path = download_product_image(thumb_url)

                sim = 0.0
                if local_path:
                    sim = compute_visual_similarity(query_embedding, local_path, embedder)

                scored.append({**item, "similarity": sim, "local_thumb": local_path})

            # Sort by visual similarity
            scored.sort(key=lambda x: x["similarity"], reverse=True)
            search_mode = "ai_visual"
        else:
            scored = [{**item, "similarity": 0.0, "local_thumb": None} for item in shopping_results]

        timings["rerank"] = round(time.time() - _tr, 1)
    else:
        scored = [{**item, "similarity": 0.0, "local_thumb": None} for item in shopping_results]

    # ─── STEP 4: Assign ranks ───
    for i, item in enumerate(scored[:max_results], 1):
        item["rank"] = i
        item["search_mode"] = search_mode

    timings["total"] = round(time.time() - _t0, 1)

    if scored:
        top = scored[0]
        sim_str = f" (sim={top['similarity']:.3f})" if has_visual_ranking else ""
        print(f"[MARKETPLACE] Done in {timings['total']}s — "
              f"top: {top['title'][:50]}{sim_str}", flush=True)

    return {
        "results": scored[:max_results],
        "classification": classification,
        "search_query": final_query,
        "search_mode": search_mode,
        "timings": timings,
    }