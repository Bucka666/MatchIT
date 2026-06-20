"""
vertical_loader.py — Loads vertical configuration for MatchIT
=============================================================
Reads verticals/<name>/vertical.json to configure the app
for a specific domain (keys, hardware, watches, etc).

The active vertical is set in config.json: "vertical": "keys"

Usage:
    from vertical_loader import get_vertical, get_field_defs, compute_field_penalty
"""

import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Any


_VERTICAL: Optional[dict] = None
_VERTICAL_NAME: Optional[str] = None


def _find_vertical_path(vertical_name: str, app_root: str) -> Optional[str]:
    """Find the vertical.json file."""
    candidates = [
        os.path.join(app_root, "verticals", vertical_name, "vertical.json"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "verticals", vertical_name, "vertical.json"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def load_vertical(vertical_name: str, app_root: str) -> dict:
    """Load a vertical config by name. Caches after first load."""
    global _VERTICAL, _VERTICAL_NAME

    if _VERTICAL is not None and _VERTICAL_NAME == vertical_name:
        return _VERTICAL

    path = _find_vertical_path(vertical_name, app_root)
    if path is None:
        print(f"[VERTICAL] WARNING: vertical '{vertical_name}' not found, using empty config", flush=True)
        _VERTICAL = {}
        _VERTICAL_NAME = vertical_name
        return _VERTICAL

    try:
        _VERTICAL = json.loads(Path(path).read_text(encoding="utf-8"))
        _VERTICAL_NAME = vertical_name
        print(f"[VERTICAL] Loaded: {_VERTICAL.get('name', vertical_name)} "
              f"({len(_VERTICAL.get('profile_fields', []))} profile fields, "
              f"{len(_VERTICAL.get('categories', {}))} categories)", flush=True)
    except Exception as e:
        print(f"[VERTICAL] Failed to load {path}: {e}", flush=True)
        _VERTICAL = {}
        _VERTICAL_NAME = vertical_name

    return _VERTICAL


def get_vertical() -> dict:
    """Get the currently loaded vertical config."""
    return _VERTICAL or {}

def get_db_root() -> str:
    """Get the database root path for the current vertical."""
    return get_vertical().get("db_root", "")

# ─────────────────────────────────────────────
# Branding helpers
# ─────────────────────────────────────────────

def get_branding() -> dict:
    """Get branding info for templates."""
    v = get_vertical()
    return {
        "name": v.get("name", "MatchIT"),
        "page_title": v.get("page_title", "MatchIT"),
        "subtitle": v.get("subtitle", "AI Guided Product Matching"),
        "icon": v.get("icon", ""),
        "query_title": v.get("query_title", "Identify Your Product"),
        "image_labels": v.get("image_labels", {"front": "Image 1", "back": "Image 2"}),
        "require_two_images": v.get("require_two_images", True),
        "ras_images_enabled": v.get("ras_images_enabled", False),
        "disclaimer": v.get("disclaimer", ""),
        "guidance_text": v.get("guidance_text", ""),
        "guidance_feedback": v.get("guidance_feedback", ""),
        "ui_text": v.get("ui_text", {}),
    }


# ─────────────────────────────────────────────
# Category helpers
# ─────────────────────────────────────────────

def get_categories() -> dict:
    """Get category definitions {id: {label, show}}."""
    return get_vertical().get("categories", {})


def get_category_list() -> List[dict]:
    """Get categories as a list of {id, label} for dropdowns."""
    cats = get_categories()
    return [{"id": k, "label": v.get("label", k)} for k, v in cats.items()]


def get_visible_fields(category_id: str) -> List[str]:
    """Get list of field IDs visible for a given category."""
    cats = get_categories()
    cat = cats.get(category_id, {})
    return cat.get("show", [])


def get_category_family(category_id: str) -> str:
    """
    Get the family root for a category (for penalty compatibility).
    E.g. MORTICE_PIN → MORTICE family.
    Returns the family name, or the category itself if not in a family.
    """
    families = get_vertical().get("category_families", {})
    for family, members in families.items():
        if category_id in members:
            return family
    return category_id


def get_silhouette_type(category_id: str) -> str:
    """Map a category to its silhouette detection type."""
    smap = get_vertical().get("silhouette_map", {})
    return smap.get(category_id, "UNKNOWN")


# ─────────────────────────────────────────────
# Profile field helpers
# ─────────────────────────────────────────────

def get_field_defs() -> List[dict]:
    """Get all profile field definitions."""
    return get_vertical().get("profile_fields", [])


def get_field_def(field_id: str) -> Optional[dict]:
    """Get a single field definition by ID."""
    for f in get_field_defs():
        if f.get("id") == field_id:
            return f
    return None


def get_field_ids() -> List[str]:
    """Get all profile field IDs."""
    return [f["id"] for f in get_field_defs()]


def parse_field_value(field_def: dict, raw_value: str) -> Any:
    """Parse a form value according to the field type."""
    ftype = field_def.get("type", "select")
    default = field_def.get("default", "")

    if raw_value is None or str(raw_value).strip() == "":
        return default

    raw = str(raw_value).strip()

    if ftype in ("int_range",):
        try:
            v = int(raw)
            fmin = field_def.get("min", 0)
            fmax = field_def.get("max", 999)
            if v < fmin or v > fmax:
                return int(default) if default != "" else -1
            return v
        except (ValueError, TypeError):
            return int(default) if default != "" else -1

    elif ftype == "float_select":
        try:
            v = float(raw)
            if v < 0:
                return float(default) if default != "" else -1.0
            return v
        except (ValueError, TypeError):
            return float(default) if default != "" else -1.0

    elif ftype == "select":
        return raw.upper()

    return raw


def parse_all_fields(form_data: dict) -> dict:
    """Parse all profile fields from form data."""
    result = {}
    for fdef in get_field_defs():
        fid = fdef["id"]
        raw = form_data.get(fid, "")
        result[fid] = parse_field_value(fdef, raw)
    return result


# ─────────────────────────────────────────────
# Generic penalty computation
# ─────────────────────────────────────────────

def compute_field_penalty(
    sku: str,
    profiles: dict,
    query_values: dict,
    query_category: str = "",
) -> float:
    """
    Compute soft penalty multiplier for profile-mismatching SKUs.
    Reads field definitions from vertical config.

    Returns:
        1.0   = match or unknown (no penalty)
        < 1.0 = confirmed mismatch (penalty stacks multiplicatively)

    Rules:
        - Query value is default (unknown) → skip (1.0)
        - SKU has no profile → skip (1.0)
        - SKU field is default → skip (1.0)
        - SKU matches query → 1.0
        - SKU disagrees → multiply by field's penalty
    """
    prof = profiles.get(sku)
    if prof is None:
        return 1.0

    mult = 1.0

    for fdef in get_field_defs():
        fid = fdef["id"]
        ftype = fdef.get("type", "select")
        default = fdef.get("default", "")
        penalty = float(fdef.get("penalty", 0.970))
        match_rule = fdef.get("match_rule", "exact")

        q_val = query_values.get(fid, default)
        s_val = prof.get(fid, default)

        # Skip if query is unknown/default
        if ftype in ("int_range",) and (q_val is None or int(q_val) < 0):
            continue
        elif ftype == "float_select" and (q_val is None or float(q_val) < 0):
            continue
        elif ftype == "select" and (not q_val or q_val == default):
            continue

        # Skip if SKU profile is unknown/default
        if ftype in ("int_range",):
            try:
                if s_val is None or int(s_val) < 0:
                    continue
            except (ValueError, TypeError):
                continue
        elif ftype == "float_select":
            try:
                if s_val is None or float(s_val) < 0:
                    continue
            except (ValueError, TypeError):
                continue
        elif ftype == "select":
            if not s_val or s_val == default:
                continue

        # Compare
        is_match = False
        if match_rule == "exact_float":
            try:
                is_match = abs(float(q_val) - float(s_val)) < 0.01
            except (ValueError, TypeError):
                continue
        elif match_rule == "exact":
            is_match = (str(q_val) == str(s_val))
        else:
            is_match = (str(q_val) == str(s_val))

        if not is_match:
            mult *= penalty

    # Category penalty (with family compatibility)
    if query_category:
        sku_cat = prof.get("key_type", "") or prof.get("category", "")
        if sku_cat and sku_cat != query_category:
            q_family = get_category_family(query_category)
            s_family = get_category_family(sku_cat)

            if q_family == s_family:
                # Same family — only penalize specific vs specific
                families = get_vertical().get("category_families", {})
                family_members = families.get(q_family, [])
                q_is_root = query_category == q_family or query_category not in family_members
                s_is_root = sku_cat == s_family or sku_cat not in family_members
                if not q_is_root and not s_is_root:
                    mult *= 0.960  # Both specific, different subtypes
            else:
                mult *= 0.960  # Different families entirely

    return mult


# ─────────────────────────────────────────────
# Silhouette penalty (optional per vertical)
# ─────────────────────────────────────────────

def is_style_detection_enabled() -> bool:
    """Check if the vertical uses silhouette-based style detection."""
    return bool(get_vertical().get("style_detection_enabled", False))