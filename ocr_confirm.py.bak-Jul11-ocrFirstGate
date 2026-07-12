"""
ocr_confirm.py — Google Vision-based set code / card number confirmation for MatchIT
=====================================================================================
Runs after CLIP + DINOv2 matching to promote the correct print to rank 1
by reading the set code or card number printed on the card face.

Supports:
    YUGIOH  — set code from bottom-left  e.g. LDS1-EN087
    POKEMON — card number from bottom-right  e.g. 025/198
    MTG     — collector number from bottom-left  e.g. 287/287

Integration in api_routes.py (after _dinov2_tiebreak call):

    from ocr_confirm import ocr_confirm_ranking
    if results and query_category:
        results, ocr_info = ocr_confirm_ranking(
            results, str(query_path1), query_category
        )

Drop into C:\\MatchIT\\ alongside api_routes.py.

PaddleOCR fallback removed: a 150-card empirical test
(_paddle_necessity_harness.py) showed it fires 0/150 times with Vision
working, and when forced on (Vision disabled), throws on 150/150 calls —
the installed paddleocr version moved reader.ocr(cls=True) to
reader.predict(), so the old call signature here errored every time. It was
providing zero function; removing it loses nothing.
"""

import logging
import os
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _fuzzy_set_code_match(candidate, set_code_map, max_dist=2):
    """Match a potentially misread set code to the closest known code.
    Requires first character to match. Returns (code, dist) or (None, 999)."""
    candidate_upper = candidate.upper()
    if not candidate_upper or len(candidate_upper) < 3:
        return None, 999
    best_code = None
    best_dist = max_dist + 1
    ambiguous = False
    for code in set_code_map:
        code_upper = code.upper()
        if not code_upper or candidate_upper[0] != code_upper[0]:
            continue
        diff = sum(a != b for a, b in zip(candidate_upper, code_upper))
        diff += abs(len(candidate_upper) - len(code_upper))
        if diff < best_dist:
            best_dist = diff
            best_code = code
            ambiguous = False
        elif diff == best_dist and best_code != code:
            ambiguous = True
    if ambiguous:
        return None, 999
    if best_code is not None and best_dist <= max_dist:
        return best_code, best_dist
    return None, 999


_last_ocr_confidence = 0.0
_last_pkm_denominator: Optional[int] = None
# When True, the SWSH printed-total fingerprint path in _extract_pokemon_number
# is skipped. Set per-scan by ocr_confirm_ranking when the scan is Japanese
# (explicit jpn_mode from caller, OR the top CLIP candidate is a jpn- SKU) so an
# EN SWSH set-total (e.g. 80 -> swsh35) can't override a correct JPN CLIP match.
_suppress_swsh_for_jpn = False

# Set per-call by _extract_pokemon_number when it sees an OCR token shaped
# like a JP set code (letter(s)+digit(s), e.g. M5, S12A) that isn't in
# _JP_SETCODE_MAP — lets ocr_direct_lookup flag a likely-unlisted set
# instead of silently falling through to CLIP. Reset at the top of every
# _extract_pokemon_number call to avoid stale carryover between scans.
_last_unmapped_jp_setcode: Optional[str] = None

# Shape of a JP set code as printed on card faces: letter(s) + digit(s),
# optionally with trailing letter(s) (M5, S12A, SV3S). Requires a digit so
# plain-letter card text (EX, GX, TAG) never false-positives. Excludes the
# 2-char "X<digit>" shape specifically — OCR commonly misreads the "×"
# weakness/resistance multiplier symbol (e.g. "Fire ×2") as the letter X,
# which would otherwise look identical to a genuine short code like M2/M3.
_JP_CODE_SHAPE_RE = re.compile(r'^(?!X\d$)(?=[A-Z0-9]{2,4}$)(?=.*[A-Z])(?=.*\d)[A-Z][A-Z0-9]*$')

# Pokémon SWSH era: printed total → DB set ID (for disambiguation)
# When OCR reads "106/217" the total 217 = swsh11 (Chilling Reign)
_SWSH_TOTAL_MAP = {
    216: ['swsh1', 'swsh10'],   # Sword & Shield base, Astral Radiance
    209: ['swsh2'],             # Rebel Clash
    201: ['swsh3'],             # Darkness Ablaze
    203: ['swsh4'],             # Vivid Voltage
    183: ['swsh5'],             # Battle Styles
    233: ['swsh6'],             # Chilling Reign (oops — 233 not 217)
    237: ['swsh7'],             # Evolving Skies
    284: ['swsh8'],             # Fusion Strike
    186: ['swsh9'],             # Brilliant Stars
    217: ['swsh11', 'me2pt5'],  # Lost Origin / Ascended Heroes (same printed total)
    215: ['swsh12'],            # Silver Tempest
    160: ['swsh12pt5'],         # Crown Zenith
    80:  ['swsh35'],            # Shining Fates
    73:  ['swsh45'],            # Celebrations
}

# Pokémon SV era: printed set code → pokemontcg.io DB set ID
_PKM_SETCODE_MAP = {
    # Scarlet & Violet era — printed 3-letter codes on card face
    # ptcgoCode (printed on card) → pokemontcg.io DB set ID
    "SVE": "sve",       # Scarlet & Violet Energies
    "SVI": "sv1",       # Scarlet & Violet base
    "PAL": "sv2",       # Paldea Evolved
    "OBF": "sv3",       # Obsidian Flames
    "MEW": "sv3pt5",    # 151
    "PAR": "sv4",       # Paradox Rift
    "PAF": "sv4pt5",    # Paldean Fates
    "TEF": "sv5",       # Temporal Forces
    "TWM": "sv6",       # Twilight Masquerade
    "SFA": "sv6pt5",    # Shrouded Fable
    "SCR": "sv7",       # Stellar Crown
    "SSP": "sv8",       # Surging Sparks
    "PRE": "sv8pt5",    # Prismatic Evolutions
    "JTG": "sv9",       # Journey Together
    "DRI": "sv10",      # Destined Rivals
    "BLK": "zsv10pt5",  # Black Bolt (fixed — was sv11b)
    "WHT": "rsv10pt5",  # White Flare
    "ASC": "me2pt5",    # Ascended Heroes
    "MEG": "me1",       # Mega Evolution 1 (printed on card face)
}


_JP_SETCODE_MAP = {
    # sv1a (Triplet Beat)
    "SV1A": "sv1a",
    # sv1s (Scarlet ex)
    "SV1S": "sv1s",
    # sv1v (Violet ex) — confirmed 1→I misread in logs
    "SV1V": "sv1v",
    "SVIV": "sv1v",
    # sv2a (Snow Hazard/Clay Burst)
    "SV2A": "sv2a",
    # sv2d (Snow Hazard)
    "SV2D": "sv2d",
    # sv2p (Clay Burst)
    "SV2P": "sv2p",
    # sv3 (Ruler of the Black Flame) — confirmed V→Y and V→5 misreads
    "SV3":  "sv3",
    "SY3":  "sv3",
    "G53":  "sv3",
    # sv3a (Raging Surf)
    "SV3A": "sv3a",
    "SY3A": "sv3a",
    # sv4a (Ancient Roar/Future Flash) — confirmed A→0 misread
    "SV4A": "sv4a",
    "SV40": "sv4a",
    # sv4k (Future Flash)
    "SV4K": "sv4k",
    # sv4m (Ancient Roar)
    "SV4M": "sv4m",
    # sv5a (Crimson Haze)
    "SV5A": "sv5a",
    # sv5k (Wild Force)
    "SV5K": "sv5k",
    # sv6 (Transformation Mask)
    "SV6":  "sv6",
    # sv6a (Night Wanderer)
    "SV6A": "sv6a",
    # sv7 (Stellar Miracle)
    "SV7":  "sv7",
    # sv7a (Paradise Dragona)
    "SV7A": "sv7a",
    # sv8 (Super Electric Breaker)
    "SV8":  "sv8",
    # sv8a (Terastal Festival)
    "SV8A": "sv8a",
    # sv9 (Journey Together JP)
    "SV9":  "sv9",
    # sv9a
    "SV9A": "sv9a",
    # sv10 (Destined Rivals JP)
    "SV10": "sv10",

    # Mega Evolution JP series
    "M2":   "m2",     # Inferno X (80 cards)
    "M3":   "m3",     # Munix Zero (80 cards)
    "M1L":  "m1l",    # Mega Brave
    "M1S":  "m1s",    # Mega Symphonia

    # Sword/Shield era s-series
    "S4":   "s4",
    "S4A":  "s4a",
    "S5A":  "s5a",
    "S5I":  "s5i",
    "S5R":  "s5r",
    "S6A":  "s6a",
    "S6H":  "s6h",
    "S6K":  "s6k",
    "S7D":  "s7d",
    "S7R":  "s7r",
    "S8":   "s8",
    "S8A":  "s8a",
    "S8B":  "s8b",
    "S9":   "s9",
    "S9A":  "s9a",
    "S10A": "s10a",
    "S10B": "s10b",
    "S10D": "s10d",
    "S10P": "s10p",
    "S11":  "s11",
    "S11A": "s11a",
    "S12":  "s12",
    "S12A": "s12a",

    # SV sibling s-suffix series
    "SV3S": "sv3s",
    "SV4S": "sv4s",
    "SV5S": "sv5s",
    "SV5M": "sv5m",
    "SV6S": "sv6s",
    "SV7S": "sv7s",
    "SV8S": "sv8s",
    "SV9S": "sv9s",

    # SV11 (Black Bolt / White Flare)
    "SV11B": "sv11b",
    "SV11W": "sv11w",

    # Classic era (e-series, neo, pcg, pmcg, vs, web)
    "E1": "e1", "E2": "e2", "E3": "e3", "E4": "e4", "E5": "e5",
    "NEO1": "neo1", "NEO2": "neo2", "NEO3": "neo3", "NEO4": "neo4",
    "PCG1": "pcg1", "PCG2": "pcg2", "PCG3": "pcg3", "PCG4": "pcg4",
    "PCG5": "pcg5", "PCG6": "pcg6", "PCG7": "pcg7", "PCG8": "pcg8",
    "PCG9": "pcg9",
    "PMCG1": "pmcg1", "PMCG2": "pmcg2", "PMCG3": "pmcg3",
    "PMCG4": "pmcg4", "PMCG5": "pmcg5", "PMCG6": "pmcg6",
    "VS1": "vs1", "WEB1": "web1",

    # SV promos and special sets
    "SV-P": "sv-p",
    "SVK":  "svk",
}


# ─────────────────────────────────────────────────────────────
# Image crop helper
# ─────────────────────────────────────────────────────────────

def _crop_and_read(
    image_path: str,
    region: tuple,
    min_width: int = 400,
) -> list:
    """
    Crop image to fractional region (left, top, right, bottom),
    send to Google Cloud Vision API, return list of text strings.
    """
    try:
        import numpy as np
        from PIL import Image, ImageEnhance

        img = Image.open(image_path).convert("RGB")
        w, h = img.size
        left   = int(region[0] * w)
        top    = int(region[1] * h)
        right  = int(region[2] * w)
        bottom = int(region[3] * h)
        crop = img.crop((left, top, right, bottom))
        logger.warning(f"[OCR-CROP] image={w}x{h} region={region} → crop=({left},{top})-({right},{bottom}) size={right-left}x{bottom-top}")

        # Scale up small crops
        cw, ch = crop.size
        if cw < min_width:
            scale = min_width / cw
            crop = crop.resize(
                (int(cw * scale), int(ch * scale)),
                Image.LANCZOS,
            )

        # Enhance contrast
        crop = ImageEnhance.Contrast(crop).enhance(2.0)
        crop = ImageEnhance.Sharpness(crop).enhance(2.5)

        # Try Google Cloud Vision first
        api_key = _get_vision_api_key()
        if api_key:
            logger.info("[OCR] using Google Vision")
            global _last_ocr_confidence
            _last_ocr_confidence = 0.95
            return _vision_api_read(crop, api_key)

        logger.warning("[OCR] Google Vision key not found — no OCR result")
        return []

    except Exception as e:
        logger.warning(f"[OCR] crop_and_read error: {e}")
        return []


def _get_vision_api_key() -> str:
    import os
    # Check environment variable first (Modal deployment)
    key = os.environ.get("GOOGLE_VISION_API_KEY", "")
    if key:
        return key

    # Fall back to config.json
    try:
        import json
        config_paths = [
            os.path.join(os.path.dirname(__file__), "config.json"),
            "/app/config.json",
        ]
        for path in config_paths:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                key = cfg.get("google_vision_api_key", "")
                if key:
                    return key
    except Exception as e:
        logger.warning(f"[OCR] Could not load Vision API key: {e}")

    return ""


def _vision_api_read(crop_image, api_key: str) -> list:
    """
    Send cropped PIL image to Google Cloud Vision API.
    Returns list of text strings found.
    """
    import base64
    import json
    import urllib.request
    import io

    try:
        # Convert PIL image to base64 JPEG
        buffer = io.BytesIO()
        crop_image.save(buffer, format="JPEG", quality=95)
        image_b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

        # Build Vision API request
        payload = {
            "requests": [
                {
                    "image": {"content": image_b64},
                    "features": [
                        {"type": "TEXT_DETECTION", "maxResults": 50}
                    ],
                    "imageContext": {
                        "languageHints": ["en"]
                    }
                }
            ]
        }

        url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())

        # Extract text annotations
        responses = result.get("responses", [])
        if not responses:
            return []

        text_annotations = responses[0].get("textAnnotations", [])
        if not text_annotations:
            return []

        # First annotation is the full text block — split into lines
        full_text = text_annotations[0].get("description", "")
        lines = [line.strip().upper() for line in full_text.splitlines() if line.strip()]
        logger.info(f"[OCR-VISION] extracted {len(lines)} lines")
        return lines

    except Exception as e:
        logger.warning(f"[OCR-VISION] API error: {e}")
        return []


# ─────────────────────────────────────────────────────────────
# TCG-specific text extraction
# ─────────────────────────────────────────────────────────────

# YGO set code pattern: 2-6 uppercase alphanumeric, optional hyphen/space, 2 letters, 3 digits
# e.g. LDS1-EN087, DUNE-EN032, OP05-EN009, DT03-EN033, MGEDEN103 (no hyphen)
_YGO_SETCODE_RE = re.compile(
    r"([A-Z0-9]{2,6}[-\s]?[A-Z]{2}[A-Z0-9O0IL]{3,4})",
    re.IGNORECASE,
)
_YGO_PASSCODE_RE = re.compile(r'\b(\d{8})\b')

# Card number pattern: digits / digits  e.g. 025/198, 4/102
_CARD_NUMBER_RE = re.compile(r"(\d{1,4})\s*/\s*(\d{1,4})")


def _extract_ygo_setcode_fuzzy(text: str) -> Optional[str]:
    """
    Fuzzy extraction when OCR mangles the hyphen and letter/digit boundaries.
    Looks for the EN marker and reconstructs the set code around it.
    e.g. SDBERENOOS → find EN position → SDBT-EN005
    """
    text = text.upper().replace(" ", "")

    # Find EN (or similar) marker position
    en_pos = -1
    for marker in ['EN', 'DE', 'FR', 'IT', 'PT', 'SP']:
        pos = text.find(marker)
        if pos >= 2:  # must have at least 2 chars before it
            en_pos = pos
            lang = marker
            break

    if en_pos < 0:
        return None
    # Reject if the text before EN looks like a common English word
    prefix_check = text[max(0, en_pos-6):en_pos].upper()
    if any(w in prefix_check for w in ['WHIRL', 'WHEN', 'YOUR', 'THIS', 'THAT', 'FROM', 'POOL', 'HAND', 'DECK', 'CARD']):
        return None

    # Try different prefix lengths - OCR may add/drop characters
    after = text[en_pos+2:en_pos+5]
    after = after.replace('O', '0').replace('I', '1').replace('L', '1').replace('S', '5')

    if len(after) < 3:
        return None

    # Try prefix lengths from longest to shortest
    for prefix_len in [4, 3, 2, 5, 6]:
        prefix = text[max(0, en_pos-prefix_len):en_pos]
        prefix = re.sub(r'[^A-Z0-9]', '', prefix)  # strip non-alphanumeric
        if len(prefix) < 2:
            continue
        code = f"{prefix}-{lang}{after}"
        logger.info(f"[OCR-YGO] fuzzy trying: {code}")
        # Try DB lookup immediately for each candidate
        db_match = _lookup_sku_by_setcode(code, 'YUGIOH')
        if db_match:
            logger.info(f"[OCR-YGO] fuzzy matched: {code} → {db_match}")
            return code

    return None


def _extract_ygo_setcode(image_path: str) -> Optional[str]:
    """
    Read the set code from a YGO card.
    On most modern cards it sits on the RIGHT side just below the artwork
    e.g. MGED-EN103 at roughly (55-100% x, 70-80% y).
    Older cards may have it bottom-left of the text box.
    We try both regions.
    Returns e.g. 'MGED-EN103' or None.
    """
    regions = [
        (0.66, 0.68, 0.95, 0.79),   # right side set code - precise from measurements
        (0.00, 0.80, 0.70, 0.92),   # fallback older cards
        (0.00, 0.88, 0.50, 1.00),   # bottom-left fallback
    ]
    for i, region in enumerate(regions):
        texts = _crop_and_read(image_path, region)
        if i == 0:
            logger.warning(f"[OCR-YGO] region={region} raw texts: {texts}")
        else:
            logger.debug(f"[OCR-YGO] region={region} raw texts: {texts}")
        for text in texts:
            clean = text.replace(" ", "").replace("—", "-").replace("–", "-")
            # Fix common OCR misread: letter O mistaken for zero in card number
            clean = re.sub(r'([A-Z]{2})O(\d{2})', r'\g<1>0\2', clean)
            clean = re.sub(r'([A-Z]{2}\d)O(\d)', r'\g<1>0\2', clean)
            # Fix common OCR misread: letter I mistaken for 1 in card number
            clean = re.sub(r'([A-Z]{2})I(\d{2})', r'\g<1>1\2', clean)
            clean = re.sub(r'([A-Z]{2}\d)I(\d)', r'\g<1>1\2', clean)
            clean = re.sub(r'([A-Z]{2}\d{2})I', r'\g<1>1', clean)
            # Fix common OCR misread: letter L mistaken for 1 in card number
            clean = re.sub(r'([A-Z]{2})L(\d{2})', r'\g<1>1\2', clean)
            clean = re.sub(r'([A-Z]{2}\d)L(\d)', r'\g<1>1\2', clean)
            clean = re.sub(r'([A-Z]{2}\d{2})L', r'\g<1>1', clean)
            # Handle multiple consecutive misreads e.g. OOS→005, OOL→001
            clean = re.sub(r'([A-Z]{2})[OIL]([OIL])(\d)', r'\g<1>0\g<2>\3', clean)
            clean = re.sub(r'([A-Z]{2}\d)[OIL]([OIL])', r'\g<1>0\g<2>', clean)
            clean = clean.replace('OOS', '005').replace('OOL', '001').replace('OOI', '001')
            clean = clean.replace('IOO', '100').replace('LOO', '100')
            # Fix S misread as 5 in card number suffix e.g. EN11S → EN115
            clean = re.sub(r'([A-Z]{2}\d{2})S\b', r'\g<1>5', clean)
            clean = re.sub(r'([A-Z]{2}\d)S(\d)', r'\g<1>5\2', clean)
            # Fix common digit misreads anywhere in the code (not just card number)
            # X is commonly misread as 4 or K
            clean = re.sub(r'\bRA0X\b', 'RA04', clean)
            clean = re.sub(r'\bRA0K\b', 'RA04', clean)
            m = _YGO_SETCODE_RE.search(clean)
            if m:
                code = m.group(1).replace(" ", "").upper()
                # Ensure hyphen is present between set prefix and EN/DE etc
                code = re.sub(r'([A-Z0-9]{2,6})([A-Z]{2}[\d]{3})', r'\1-\2', code)
                code = code.replace(" ", "-")
                logger.debug(f"[OCR-YGO] extracted: {code} from region {region}")
                return code
    # Fuzzy fallback — try each text string
    for region in regions:
        texts = _crop_and_read(image_path, region)
        for text in texts:
            result = _extract_ygo_setcode_fuzzy(text)
            if result:
                return result

    # Passcode fallback — read 8-digit number from bottom-left
    passcode_region = (0.02, 0.93, 0.22, 1.00)
    passcode_texts = _crop_and_read(image_path, passcode_region)
    for text in passcode_texts:
        clean = text.replace(" ", "")
        m = _YGO_PASSCODE_RE.search(clean)
        if m:
            passcode = m.group(1)
            logger.info(f"[OCR-YGO] found passcode: {passcode}")
            return f"PASSCODE:{passcode}"
    return None


def _extract_pokemon_number(image_path: str) -> Optional[str]:
    """
    Read the card number and set code from the bottom of a Pokémon card.
    For SV era cards returns e.g. 'sv6-18' for direct DB lookup.
    For older cards returns plain number e.g. '18'.
    """
    global _last_pkm_denominator, _last_unmapped_jp_setcode
    _last_pkm_denominator = None
    _last_unmapped_jp_setcode = None
    # Try multiple regions — the card number position varies between card types.
    # Short-circuit: if a crop yields BOTH a set code and a valid card number
    # (the strongest signal — a direct DB-lookup key), stop early. Saves up to
    # 2 Google Vision calls per scan on cards where crop 1 already nailed it.
    # Ambiguous signals (plain number, SWSH total) do NOT trigger exit — those
    # genuinely benefit from pooling all three crops.
    all_texts = []
    for region in [
        (0.00, 0.80, 0.55, 1.00),  # bottom-left quadrant
        (0.00, 0.75, 0.60, 0.95),  # slightly higher, wider
        (0.00, 0.85, 0.50, 0.98),  # narrow bottom strip
    ]:
        _t = _crop_and_read(image_path, region)
        if _t:
            all_texts.extend(_t)
        # Early-exit check on text pooled so far
        _so_far = " ".join(all_texts).upper()
        _sc = None
        for _code, _db_id in _PKM_SETCODE_MAP.items():
            if _code in _so_far:
                _sc = _db_id
                break
        if _sc:
            for _txt in all_texts:
                _m = _CARD_NUMBER_RE.search(_txt)
                if _m:
                    _n = int(_m.group(1))
                    _tot = int(_m.group(2))
                    if _n <= 400 and _tot <= 400:
                        logger.info(f"[OCR-PKM] early-exit after crop: set={_sc} num={_n}")
                        break
            else:
                continue
            break
    texts = list(set(all_texts))  # deduplicate
    logger.info(f"[OCR-PKM] raw texts: {texts}")
    num = None
    total = None
    set_code = None
    all_text = " ".join(texts).upper()
    # Look for SV era set code in all extracted text
    for code, db_id in _PKM_SETCODE_MAP.items():
        if code in all_text:
            set_code = db_id
            break
    # JP set code fallback — check individual OCR tokens against JP map
    if set_code is None:
        for raw in texts:  # individual OCR text lines, not all_text
            for token in raw.upper().split():
                # Check token as-is
                if token in _JP_SETCODE_MAP:
                    set_code = f"jpn-{_JP_SETCODE_MAP[token]}"
                    break
                # Also check with leading G stripped
                # (handles GSV3, GSV4A etc. — OCR attaches the grade letter)
                if token.startswith('G') and len(token) > 3:
                    stripped = token[1:]
                    if stripped in _JP_SETCODE_MAP:
                        set_code = f"jpn-{_JP_SETCODE_MAP[stripped]}"
                        break
            if set_code:
                break
    # Unrecognized JP-set-code-shaped token — only meaningful in JP mode
    # (_suppress_swsh_for_jpn doubles as the jp_mode signal here, same as
    # its existing use below). Lets ocr_direct_lookup tell the caller this
    # looks like a genuinely unlisted set, instead of silently falling
    # through to CLIP with no signal at all.
    if set_code is None and _suppress_swsh_for_jpn:
        for raw in texts:
            for token in raw.upper().split():
                if (_JP_CODE_SHAPE_RE.match(token)
                        and token not in _JP_SETCODE_MAP
                        and token not in _PKM_SETCODE_MAP):
                    _last_unmapped_jp_setcode = token
                    logger.info(f"[OCR-PKM] unmapped JP-shaped set code seen: {token}")
                    break
            if _last_unmapped_jp_setcode:
                break
    # Plain number pattern for energy cards e.g. "015" without total
    _PLAIN_NUM_RE = re.compile(r'(?<![×xX*+\-])\b(\d{1,4})\b')
    for text in texts:
        m = _CARD_NUMBER_RE.search(text)
        if m and num is None:
            _n = int(m.group(1))
            _t = int(m.group(2))
            # Reject impossible values — no Pokémon card has number > 400
            if _n > 400 or _t > 400:
                continue
            num = str(_n)
            total = _t
    # Fallback — plain number if no slash format found
    _SKIP_STAT_WORDS = {'WEAKNESS', 'RESISTANCE', 'RETREAT'}
    if num is None:
        for text in texts:
            if any(w in text.upper() for w in _SKIP_STAT_WORDS):
                continue
            m = _PLAIN_NUM_RE.search(text)
            if m:
                candidate = str(int(m.group(1)))
                # Sanity check — must be a plausible card number
                if 1 <= int(candidate) <= 500:
                    num = candidate
                    break
    if num and set_code:
        combined = f"{set_code}-{num}"
        logger.info(f"[OCR-PKM] extracted set+number: {combined}")
        _last_pkm_denominator = total
        return combined
    # SWSH fingerprint — use total to identify set
    # Reject impossible totals (no Pokémon set has more than ~300 cards)
    if total and total > 300:
        total = None
    # Skip the EN SWSH set-total fingerprint entirely for Japanese scans — an EN
    # printed total (e.g. 80 -> swsh35) must never override a correct JPN CLIP
    # match. Falls through to the bare-number path below, preserving visual rank.
    if num and total and not set_code and not _suppress_swsh_for_jpn:
        candidates = _SWSH_TOTAL_MAP.get(total, [])
        if len(candidates) == 1:
            combined = f"{candidates[0]}-{num}"
            if total:
                _last_pkm_denominator = total
            logger.info(f"[OCR-PKM] SWSH fingerprint: total={total} → {combined}")
            return combined
        elif len(candidates) > 1:
            logger.info(f"[OCR-PKM] SWSH ambiguous total={total}, candidates={candidates}")
            # The set is ambiguous, but the /TTT denominator itself was read
            # cleanly — record it so _denominator_blocks_promotion can still
            # veto a candidate whose set total doesn't match, even though we
            # can't pick a set ourselves (15a fix).
            _last_pkm_denominator = total
            return num
    if num:
        if total:
            _last_pkm_denominator = total
        logger.info(f"[OCR-PKM] extracted card number only: {num} (denom={total})")
        return num
    return None


# MTG set code pattern: 2-5 uppercase letters e.g. SNC, 2X2, MOM, LTR
_MTG_SETCODE_RE = re.compile(r'\b([A-Z]{2,5})\b')

def _extract_mtg_collector(image_path: str) -> Optional[str]:
    """
    Read the collector number and set code from the bottom of an MTG card.
    Returns combined e.g. 'snc-149' for direct DB lookup.
    Falls back to plain number e.g. '149' if no set code found.
    Note: pre-8th Edition sets have no collector number — returns None.
    """
    texts = _crop_and_read(image_path, (0.0, 0.80, 0.55, 1.00))
    logger.info(f"[OCR-MTG] raw texts: {texts}")
    num = None
    set_code = None
    for text in texts:
        m = _CARD_NUMBER_RE.search(text)
        if m and num is None:
            num = str(int(m.group(1)))
        # Look for set code in same line or nearby lines
        # Set code is typically 2-5 uppercase letters/numbers e.g. SNC, 2X2
        for candidate in _MTG_SETCODE_RE.findall(text.upper()):
            # Filter out common false positives
            if candidate in ('EN', 'DE', 'FR', 'IT', 'ES', 'PT', 'JP', 'KR',
                             'C', 'U', 'R', 'M', 'S', 'A', 'T',
                             'THE', 'AND', 'OF', 'IN', 'BY', 'AT',
                             'ART', 'ILL', 'ILLUS', 'ARTIST', 'MAGIC'):
                continue                          # skip rarity/language/common words
            if not candidate.isalpha():
                continue                          # reject anything with digits or symbols
            set_code = candidate.lower()
            break
    if num and set_code:
        combined = f"{set_code}-{num}"
        logger.info(f"[OCR-MTG] extracted set+number: {combined}")
        return combined
    elif num:
        logger.info(f"[OCR-MTG] extracted collector number only: {num}")
        return num
    return None


# ─────────────────────────────────────────────────────────────
# SKU matching per TCG
# ─────────────────────────────────────────────────────────────

def _ygo_matches(sku: str, extracted: str) -> bool:
    """
    YGO SKU format: ygo-LDS1-EN087-12652643
    extracted:      LDS1-EN087  or  PASSCODE:12652643
    Strategy: normalise both and check containment.
    """
    if extracted.startswith("PASSCODE:"):
        passcode = extracted[9:]
        return sku.endswith(f"-{passcode}")
    # Normalise extracted: remove spaces, uppercase
    code = extracted.upper().replace(" ", "")
    sku_upper = sku.upper().replace(" ", "")
    return code in sku_upper


def _pokemon_matches(sku: str, extracted: str) -> bool:
    """
    Pokémon SKU format: base1-4, swsh1-25, sv3pt5-123
    extracted: either 'sv6-18' (set+number) or '18' (number only)
    Strategy: if set+number, check containment; if number only, compare last segment.
    """
    if '-' in extracted:
        return extracted in sku
    parts = sku.split("-")
    if not parts:
        return False
    try:
        sku_num = str(int(parts[-1]))
    except ValueError:
        sku_num = parts[-1].lstrip("0") or "0"
    return sku_num == extracted


def _mtg_matches(sku: str, extracted: str) -> bool:
    """
    MTG SKU format: mtg-snc-149
    extracted: either 'snc-149' (set+number) or '149' (number only)
    Strategy: if set+number, check containment; if number only, check SKU ends with it.
    """
    if '-' in extracted:
        # Combined format e.g. snc-149 — check containment in SKU
        return extracted in sku
    return sku.endswith(f"-{extracted}")


# ─────────────────────────────────────────────────────────────
def _get_images_db_path() -> 'Path':
    from pathlib import Path
    localappdata = os.environ.get("LOCALAPPDATA", "/modal_data")
    return Path(localappdata) / "MatchITv2_ProductMatch_Data" / "cards" / "images.db"


def _is_pokemon_sku(sku: str) -> bool:
    return not (sku.startswith('ygo-') or sku.startswith('mtg-'))


def _lookup_sku_by_setcode(extracted: str, tcg: str) -> Optional[str]:
    """
    Direct DB lookup when OCR finds a set code not in the top visual results.
    Searches images.db for a SKU containing the extracted set code.
    Returns the first matching SKU or None.
    """
    import sqlite3
    db_path = _get_images_db_path()
    logger.info(f"[OCR-DB] Called with extracted={extracted}, tcg={tcg}, db_path={db_path}, exists={db_path.exists()}")
    if not db_path.exists():
        logger.warning(f"[OCR-DB] DB NOT FOUND at {db_path}")
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        if tcg == "YUGIOH":
            # extracted = SDBT-EN011, SKU = ygo-SDBT-EN011-46502744
            # or extracted = PASSCODE:12652643, SKU ends with -12652643
            if extracted.startswith("PASSCODE:"):
                passcode = extracted[9:]
                pattern = f"ygo-%-{passcode}"
            else:
                pattern = f"ygo-{extracted}-%"
        elif tcg == "POKEMON":
            # extracted = sv6-14, SKU = sv6-14
            if '-' in extracted:
                # JP SKUs are always 3-digit zero-padded (e.g. jpn-sv3-073),
                # unlike EN SKUs which are never padded (e.g. sv1-5, not
                # sv1-005) — only pad the jpn- prefixed form, or this would
                # break every EN exact-match lookup.
                if extracted.startswith("jpn-"):
                    prefix, num_part = extracted.rsplit('-', 1)
                    if num_part.isdigit():
                        extracted = f"{prefix}-{num_part.zfill(3)}"
                row = conn.execute(
                    "SELECT sku FROM images WHERE sku = ?",
                    (extracted,)
                ).fetchone()
                logger.info(f"[OCR-DB] Pokemon lookup: extracted={extracted}, db_path={db_path}, found={row}")
                conn.close()
                return row[0] if row else None
            return None
        elif tcg == "MTG":
            # extracted = snc-149, SKU = mtg-snc-149
            if '-' in extracted:
                pattern = f"mtg-{extracted}"
                row = conn.execute(
                    "SELECT sku FROM images WHERE sku = ?",
                    (pattern,)
                ).fetchone()
                conn.close()
                return row[0] if row else None
            return None
        else:
            return None
        row = conn.execute(
            "SELECT sku FROM images WHERE sku LIKE ? LIMIT 1",
            (pattern,)
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        logger.warning(f"[OCR] DB lookup error: {e}")
        return None


def _extracted_set_id(extracted: Optional[str]) -> Optional[str]:
    """
    Derive a set-id from an OCR-extracted card identity, in the same
    id-space _get_set_id_from_sku() (app.py) uses for real SKUs —
    e.g. 'sv6-18' -> 'sv6', 'snc-149' -> 'snc', 'SDBT-EN006' -> 'SDBT'.

    Returns None for a bare number or passcode-only read: neither
    identifies a set, so there is nothing for a missing-set check to
    act on in that case.
    """
    if not extracted or extracted.startswith("PASSCODE:") or "-" not in extracted:
        return None
    return extracted.rsplit("-", 1)[0]


def ocr_direct_lookup(
    image_path: str, tcg: str, jp_mode: bool = False
) -> Tuple[Optional[str], Optional[str]]:
    """
    OCR-first lookup for YGO, MTG, and Pokémon — runs before CLIP.
    Extracts set code / collector number directly from the card image
    and looks it up in the DB.

    Returns (matched_sku, extracted_set_id):
      - matched_sku: the DB SKU if the extracted identity resolved to a
        real row, else None (existing behaviour, unchanged).
      - extracted_set_id: the set the card claims to be from, populated
        whenever OCR successfully read a set code + number — even when
        matched_sku is None because that set has no rows in the DB at
        all. A missing-set check must anchor to THIS, never to
        matched_sku alone: a miss there is indistinguishable from a
        misread, and never to CLIP's own guess, which can only ever
        land on a set we already have images for.
    """
    tcg_upper = tcg.upper()
    if tcg_upper == "YUGIOH":
        extracted = _extract_ygo_setcode(image_path)
    elif tcg_upper == "MTG":
        extracted = _extract_mtg_collector(image_path)
    elif tcg_upper == "POKEMON":
        # _suppress_swsh_for_jpn is normally set from a CLIP result
        # (ocr_confirm_ranking, post-CLIP) — no CLIP result exists yet
        # at this point, so set it directly from the request's own
        # jp_mode instead, same purpose: never let an EN SWSH printed-
        # total fingerprint override a genuine JP card.
        global _suppress_swsh_for_jpn, _last_unmapped_jp_setcode
        _suppress_swsh_for_jpn = bool(jp_mode)
        extracted = _extract_pokemon_number(image_path)
    else:
        return None, None
    if not extracted:
        # Extraction failed entirely, but an unmapped JP-shaped set code
        # was still seen — surface it so the caller can tell the user
        # this looks like a genuinely unlisted set, rather than a plain
        # miss indistinguishable from a misread.
        if jp_mode and _last_unmapped_jp_setcode:
            synthetic_id = f"jpn-{_last_unmapped_jp_setcode.lower()}"
            logger.info(f"[OCR-FIRST] unmapped JP set code {_last_unmapped_jp_setcode} (no number read) -> {synthetic_id}")
            return None, synthetic_id
        return None, None
    logger.info(f"[OCR-FIRST] extracted: {extracted} for {tcg_upper}")
    extracted_set_id = _extracted_set_id(extracted)
    db_match = _lookup_sku_by_setcode(extracted, tcg_upper)
    if jp_mode and db_match and not db_match.startswith("jpn-"):
        logger.warning(f"[OCR-FIRST] rejecting EN match {db_match} in jp_mode")
        db_match = None
    # extracted resolved to a bare number with no recognized set code (e.g.
    # _extracted_set_id found no '-' to split on) — if we separately saw an
    # unmapped JP-shaped code, use it as a synthetic set id so _is_set_imaged
    # correctly reports this as not-in-database instead of silently falling
    # through to CLIP. NOT derived via _extracted_set_id(extracted) — that
    # helper assumes the last '-' segment is a card number, which would
    # mangle a bare "jpn-<code>" string (no number suffix) into just "jpn".
    if jp_mode and not extracted_set_id and _last_unmapped_jp_setcode:
        extracted_set_id = f"jpn-{_last_unmapped_jp_setcode.lower()}"
        logger.info(f"[OCR-FIRST] unmapped JP set code {_last_unmapped_jp_setcode} -> {extracted_set_id}")
    if db_match:
        logger.info(f"[OCR-FIRST] direct DB match: {db_match}")
    return db_match, extracted_set_id


def _trusted_set_meta(set_id, set_metadata, expected_game):
    """Return set_metadata[set_id] only if its game matches expected_game.

    Guards against bare-id collisions where a Pokémon set_id (e.g. me1, me2,
    me3 — "Mega Evolution"/"Phantasmal Flames"/"Perfect Order") shares its
    key with an unrelated MTG set ("Masters Edition" I/II/III) in
    set_metadata.json. Without this check, callers would silently trust the
    wrong game's (null) totals for a real Pokémon SKU (15b-code fix).
    """
    if not set_id or not set_metadata:
        return None
    meta = set_metadata.get(set_id)
    if not meta or meta.get("game") != expected_game:
        return None
    return meta


def _denominator_blocks_promotion(candidate_sku, ocr_denominator, set_metadata, expected_game):
    """Fail-open veto. Returns True only when we have positive proof the OCR
    set is wrong: a parsed denominator, a known set total FOR THE EXPECTED
    GAME, and a mismatch against BOTH printed_total and total. Any
    missing/null/wrong-game data -> False (allow).
    """
    if not ocr_denominator or not candidate_sku or not set_metadata:
        return False
    set_id = candidate_sku.rsplit("-", 1)[0]
    meta = _trusted_set_meta(set_id, set_metadata, expected_game)
    if not meta:
        return False
    try:
        printed = meta.get("printed_total")
        full = meta.get("total")
        if printed is None and full is None:
            return False
        denom = int(ocr_denominator)
        # JPN cards print their base-set total (e.g. 63 for sv1v) while metadata
        # stores the full total incl. secret rares (e.g. 238); base <= full
        # always. Only a denominator that EXCEEDS the set total is genuinely
        # impossible, so block on denom > ceiling rather than strict inequality.
        ceiling = max(int(t) for t in (printed, full) if t is not None)
        if denom <= ceiling:
            return False
    except (TypeError, ValueError):
        return False
    return True  # denominator exceeds the set total -> wrong set -> block


def _denominator_confirms(candidate_sku, ocr_denominator, set_metadata, expected_game):
    """Positive-match counterpart of _denominator_blocks_promotion — True only
    when the OCR denominator matches the candidate's printed_total or total
    FOR THE EXPECTED GAME. Used by the 15c match-strength gate to tell a
    denominator-corroborated ("strong") OCR match from a bare-number
    ("weak") one. Any missing/null/wrong-game data -> False (not confirmed).
    """
    if not ocr_denominator or not candidate_sku or not set_metadata:
        return False
    set_id = candidate_sku.rsplit("-", 1)[0]
    meta = _trusted_set_meta(set_id, set_metadata, expected_game)
    if not meta:
        return False
    try:
        denom = int(ocr_denominator)
        printed = meta.get("printed_total")
        full = meta.get("total")
    except (TypeError, ValueError):
        return False
    if printed is not None and denom == int(printed):
        return True
    if full is not None and denom == int(full):
        return True
    return False


# Main entry point
# ─────────────────────────────────────────────────────────────

def ocr_confirm_ranking(
    results: List[dict],
    query_image_path: str,
    tcg: str,
    search_depth: int = 5,
    allow_pokemon_promote: bool = True,
    set_metadata: Optional[dict] = None,
    jpn_mode: bool = False,
) -> Tuple[List[dict], Dict]:
    """
    Attempt to confirm or correct the visual ranking using OCR.

    Args:
        results:          List of match dicts from _dinov2_tiebreak,
                          each with at least 'sku', 'score', 'rank'.
        query_image_path: Path to the saved query image on disk.
        tcg:              TCG identifier string — 'YUGIOH', 'POKEMON', 'MTG'.
        search_depth:     How many of the top results to check for an OCR match.
                          Default 5 — no point looking beyond rank 5.

    Returns:
        (results, ocr_info) — results may be reordered; ocr_info is diagnostic.
    """
    # JP mode: skip OCR for MTG/YGO — trust the CLIP visual match (there are
    # no JP MTG/YGO cards in the database at all, so there's nothing to gain).
    # POKEMON now falls through to the full OCR confirmation logic below,
    # since _JP_SETCODE_MAP lets it extract a real jpn-<set>-<num> key.
    if jpn_mode and (tcg or "").upper().strip() != "POKEMON":
        logger.debug("[OCR] jpn_mode=True — skipping OCR, returning visual rankings unchanged")
        return results, {"method": "jp_visual_only", "ocr_skipped": True}

    ocr_info: Dict = {
        "tcg": tcg,
        "extracted": None,
        "matched_sku": None,
        "promoted": False,
        "ocr_status": "not_run",
    }

    # Guard: nothing to do
    if not results:
        ocr_info["ocr_status"] = "skipped_no_results"
        return results, ocr_info

    if not query_image_path or not os.path.exists(query_image_path):
        ocr_info["ocr_status"] = "skipped_no_image"
        return results, ocr_info

    tcg_upper = (tcg or "").upper().strip()

    # Japanese scan? Suppress the EN SWSH printed-total fingerprint for this scan
    # so a JPN CLIP match can't be overridden by an EN set-total lookup. Trigger
    # on either signal (OR): explicit jpn_mode from the caller, or a top CLIP
    # candidate with a jpn- prefix. Set every call to avoid stale global carryover.
    global _suppress_swsh_for_jpn
    _top_sku = results[0].get("sku", "") if isinstance(results[0], dict) else getattr(results[0], "sku", "")
    _suppress_swsh_for_jpn = bool(jpn_mode) or bool(_top_sku and _top_sku.startswith("jpn-"))

    # Select extractor and matcher
    if tcg_upper == "YUGIOH":
        extractors = [(_extract_ygo_setcode, _ygo_matches)]
    elif tcg_upper == "POKEMON":
        extractors = [(_extract_pokemon_number, _pokemon_matches)]
    elif tcg_upper == "MTG":
        extractors = [(_extract_mtg_collector, _mtg_matches)]
    else:
        # No TCG specified — try extractors
        if allow_pokemon_promote:
            extractors = [
                (_extract_ygo_setcode,    _ygo_matches),
                (_extract_pokemon_number, _pokemon_matches),
                (_extract_mtg_collector,  _mtg_matches),
            ]
        else:
            extractors = [
                (_extract_ygo_setcode,   _ygo_matches),
                (_extract_mtg_collector, _mtg_matches),
            ]

    # Try each extractor until one works
    extracted = None
    matcher = None
    for _extractor, _matcher in extractors:
        try:
            _extracted = _extractor(query_image_path)
            if not _extracted:
                logger.debug(f"[OCR] {_extractor.__name__} found no text")
                continue
            # Check if this extracted value matches anything in DB
            _found = False
            for r in results[:10]:
                if _matcher(r.get("sku", "") if isinstance(r, dict) else r.sku, _extracted):
                    _found = True
                    break
            if _found:
                extracted = _extracted
                matcher = _matcher
                logger.debug(f"[OCR] {_extractor.__name__} matched: {_extracted}")
                if _last_ocr_confidence >= 0.85:
                    ocr_info["high_confidence"] = True
                    ocr_info["ocr_confidence"] = _last_ocr_confidence
                else:
                    ocr_info["high_confidence"] = False
                    ocr_info["ocr_confidence"] = _last_ocr_confidence
                break
            else:
                logger.debug(f"[OCR] {_extractor.__name__} extracted '{_extracted}' but no DB match, trying next")
                # Store even if not in top N — needed for direct DB lookup fallback
                if extracted is None:
                    extracted = _extracted
        except Exception as e:
            logger.warning(f"[OCR] extractor error: {e}")
            continue

    ocr_info["extracted"] = extracted
    ocr_denominator = _last_pkm_denominator if tcg_upper == "POKEMON" else None

    if not extracted:
        logger.info(f"[OCR] No text extracted")
        ocr_info["ocr_status"] = "no_text_found"
        return results, ocr_info

    # Search top N results for a matching SKU
    depth = min(search_depth, len(results))
    match_idx: Optional[int] = None

    for i in range(depth):
        sku = results[i].get("sku", "")
        if matcher and matcher(sku, extracted):
            match_idx = i
            ocr_info["matched_sku"] = sku
            logger.info(
                f"[OCR] Match found: '{extracted}' → {sku} "
                f"(was rank {i + 1})"
            )
            break

    if match_idx is None:
        logger.info(
            f"[OCR] '{extracted}' matched no SKU in top {depth} results"
        )
        # Try direct DB lookup by set code
        db_match = _lookup_sku_by_setcode(extracted, tcg_upper)
        if jpn_mode and db_match and not db_match.startswith("jpn-"):
            logger.warning(f"[OCR] rejecting EN direct match {db_match} in jp_mode")
            db_match = None
        if db_match:
            logger.info(f"[OCR] Direct DB match: {db_match}")
            if not allow_pokemon_promote and _is_pokemon_sku(db_match):
                ocr_info["ocr_status"] = "skipped_pokemon_promote"
                return results, ocr_info
            if _denominator_blocks_promotion(db_match, ocr_denominator, set_metadata, tcg_upper):
                logger.info("[OCR-P3] direct_lookup blocked: %s denom=%s != set total",
                            db_match, ocr_denominator)
                ocr_info["ocr_status"] = "denominator_mismatch"
                return results, ocr_info
            # Build a minimal result entry with profile loaded
            new_entry = {"sku": db_match, "score": 0.95, "similarity": 0.95, "prob": 0.95, "rank": 1, "ocr_promoted": True, "ocr_direct_lookup": True, "profile": {}}
            # Load profile inline so scanner gets price data
            try:
                import json as _json
                import os as _os
                from pathlib import Path as _Path
                _app_root = _os.path.dirname(_os.path.abspath(__file__))
                for pfn in [f"sku_profiles_{tcg_upper.lower()}.json", "sku_profiles.json"]:
                    _pp = _os.path.join(_app_root, pfn)
                    if _os.path.exists(_pp):
                        with open(_pp, "r", encoding="utf-8") as _pf:
                            _profiles = _json.load(_pf)
                        if db_match in _profiles:
                            new_entry["profile"] = _profiles[db_match]
                            break
            except Exception as _pe:
                logger.warning(f"[OCR] Direct profile load failed: {_pe}")
            results.insert(0, new_entry)
            for i, r in enumerate(results):
                r["rank"] = i + 1
            ocr_info["matched_sku"] = db_match
            ocr_info["promoted"] = True
            ocr_info["ocr_status"] = "direct_lookup"
        else:
            ocr_info["ocr_status"] = "no_sku_match"
        return results, ocr_info

    # Already rank 1 — confirm and return (but veto on denominator mismatch first,
    # same guard the promotion/direct-lookup paths use — closes the rank1_confirmed leak)
    if match_idx == 0:
        if _denominator_blocks_promotion(results[0]['sku'], ocr_denominator, set_metadata, tcg_upper):
            logger.info("[OCR-P3] rank1_confirm blocked: %s denom=%s != set total",
                        results[0]['sku'], ocr_denominator)
            ocr_info["ocr_status"] = "denominator_mismatch"
            return results, ocr_info
        logger.info(f"[OCR] Rank 1 confirmed by OCR: {results[0]['sku']}")
        ocr_info["ocr_status"] = "rank1_confirmed"
        return results, ocr_info

    # Promote matched result to rank 1
    matched_sku = results[match_idx].get("sku", "") if isinstance(results[match_idx], dict) else results[match_idx].sku
    if not allow_pokemon_promote and _is_pokemon_sku(matched_sku):
        ocr_info["ocr_status"] = "skipped_pokemon_promote"
        return results, ocr_info
    if _denominator_blocks_promotion(matched_sku, ocr_denominator, set_metadata, tcg_upper):
        logger.info("[OCR-P3] top5 promotion blocked: %s denom=%s != set total",
                    matched_sku, ocr_denominator)
        ocr_info["ocr_status"] = "denominator_mismatch"
        return results, ocr_info

    # ── 15c: era/format gate — a WEAK OCR match (bare number, no denominator
    # corroboration) must not override a CLIP rank1 that is itself confident.
    # A STRONG match (exact set-code+number, or denominator-confirmed via the
    # 15a fix) is never blocked here.
    has_set_code = isinstance(extracted, str) and "-" in extracted
    denom_confirmed = _denominator_confirms(matched_sku, ocr_denominator, set_metadata, tcg_upper)
    match_strength = "strong" if (has_set_code or denom_confirmed) else "weak"

    try:
        from app import CFG as _APP_CFG
        clip_promote_block_floor = float(_APP_CFG.get("clip_promote_block_floor", 0.55))
        clip_promote_block_gap = float(_APP_CFG.get("clip_promote_block_gap", 0.05))
    except Exception:
        clip_promote_block_floor = 0.55
        clip_promote_block_gap = 0.05

    clip0 = float(results[0].get("score", 0.0)) if results else 0.0
    clip1 = float(results[1].get("score", 0.0)) if len(results) > 1 else 0.0
    clip_gap = clip0 - clip1
    clip_confident = (clip0 >= clip_promote_block_floor) and (clip_gap >= clip_promote_block_gap)

    blocked_reason = None
    if match_strength == "weak" and clip_confident:
        blocked_reason = "weak_ocr_vs_confident_clip"

    logger.info(
        "[OCR-GATE] match_strength=%s clip0=%.4f clip1=%.4f clip_gap=%.4f promoted=%s blocked_reason=%s",
        match_strength, clip0, clip1, clip_gap, (blocked_reason is None), blocked_reason,
    )

    if blocked_reason:
        ocr_info["ocr_status"] = "clip_confident_block"
        ocr_info["match_strength"] = match_strength
        return results, ocr_info

    promoted = results.pop(match_idx)
    promoted["ocr_promoted"] = True
    results.insert(0, promoted)

    # Re-number ranks
    for i, r in enumerate(results):
        r["rank"] = i + 1

    ocr_info["promoted"]          = True
    ocr_info["promoted_from_rank"] = match_idx + 1
    ocr_info["ocr_status"]        = "promoted"

    logger.info(
        f"[OCR] Promoted rank {match_idx + 1} → rank 1: {promoted['sku']}"
    )
    return results, ocr_info


# ─────────────────────────────────────────────────────────────
# Standalone text-only parsers (no image needed)
# Used by /api/ocr-lookup to parse raw Tesseract output.
# ─────────────────────────────────────────────────────────────

def parse_pokemon_denominator(texts: list):
    """Return the printed denominator (TTT in NNN/TTT) from OCR text, or None.
    Mirrors parse_pokemon_text's _CARD_NUMBER_RE parse but yields the total only.
    Pure read, no globals, no side effects."""
    for text in texts:
        m = _CARD_NUMBER_RE.search(text)
        if m:
            try:
                t = int(m.group(2))
            except (TypeError, ValueError):
                continue
            if t and t <= 300:
                return t
    return None


def parse_pokemon_text(texts: list) -> Optional[str]:
    """Parse raw OCR text lines from a Pokémon card.
    Returns '{set_id}-{num}' (e.g. 'sv6-25') or plain num string, or None.
    """
    all_text = " ".join(texts).upper()
    num = None
    total = None
    set_code = None
    num_is_clean = False

    # Word-boundary match — bare substring matching false-positives on
    # flavour/attack text (PARALYZED→PAR, MEWTWO→MEW, PRESENT→PRE)
    for code, db_id in _PKM_SETCODE_MAP.items():
        if re.search(r'\b[A-Z]?' + re.escape(code) + r'[A-Z]?\b', all_text):
            set_code = db_id
            break

    _PLAIN_NUM_RE = re.compile(r'(?<![×xX*+\-])\b(\d{1,4})\b')
    for text in texts:
        m = _CARD_NUMBER_RE.search(text)
        if m and num is None:
            _n = int(m.group(1))
            _t = int(m.group(2))
            if _n > 400 or _t > 400:
                continue
            num = str(_n)
            total = _t
            num_is_clean = True

    _SKIP_STAT_WORDS = {'WEAKNESS', 'RESISTANCE', 'RETREAT'}
    if num is None:
        for text in texts:
            if any(w in text.upper() for w in _SKIP_STAT_WORDS):
                continue
            m = _PLAIN_NUM_RE.search(text)
            if m:
                candidate = str(int(m.group(1)))
                if 1 <= int(candidate) <= 500:
                    num = candidate
                    num_is_clean = False
                    break

    # Hyphenated (confident) identifiers require a clean NNN/NNN number —
    # a stray fallback digit may only return bare (which app.py rejects)
    if num and set_code and num_is_clean:
        return f"{set_code}-{num}"
    if total and total <= 300 and num and num_is_clean and not set_code:
        candidates = _SWSH_TOTAL_MAP.get(total, [])
        if len(candidates) == 1:
            return f"{candidates[0]}-{num}"
        elif candidates:
            return num
    if num:
        return num
    return None


def parse_mtg_text(texts: list) -> Optional[str]:
    """Parse raw OCR text lines from an MTG card.
    Returns '{set_code}-{num}' (e.g. 'snc-149') or plain num string, or None.
    """
    num = None
    set_code = None
    for text in texts:
        m = _CARD_NUMBER_RE.search(text)
        if m and num is None:
            num = str(int(m.group(1)))
        if set_code is None:
            for candidate in _MTG_SETCODE_RE.findall(text.upper()):
                if candidate in ('EN', 'DE', 'FR', 'IT', 'ES', 'PT', 'JP', 'KR',
                                 'C', 'U', 'R', 'M', 'S', 'A', 'T',
                                 'THE', 'AND', 'OF', 'IN', 'BY', 'AT',
                                 'ART', 'ILL', 'ILLUS', 'ARTIST', 'MAGIC'):
                    continue
                if not candidate.isalpha():
                    continue
                set_code = candidate.lower()
                break
    if num and set_code:
        return f"{set_code}-{num}"
    elif num:
        return num
    return None


def parse_ygo_text(texts: list) -> Tuple[Optional[str], Optional[str]]:
    """Parse raw OCR text lines from a YGO card.
    Returns (set_code_or_None, passcode_or_None).
    set_code: e.g. 'SDBT-EN011'
    passcode: e.g. '89631139' (8-digit number)
    """
    set_code = None
    passcode = None

    for text in texts:
        clean = text.replace(" ", "").replace("—", "-").replace("–", "-")
        clean = re.sub(r'([A-Z]{2})O(\d{2})', r'\g<1>0\2', clean)
        clean = re.sub(r'([A-Z]{2}\d)O(\d)', r'\g<1>0\2', clean)
        clean = re.sub(r'([A-Z]{2})I(\d{2})', r'\g<1>1\2', clean)
        clean = re.sub(r'([A-Z]{2}\d)I(\d)', r'\g<1>1\2', clean)
        clean = re.sub(r'([A-Z]{2}\d{2})I', r'\g<1>1', clean)
        clean = re.sub(r'([A-Z]{2})L(\d{2})', r'\g<1>1\2', clean)
        clean = re.sub(r'([A-Z]{2}\d)L(\d)', r'\g<1>1\2', clean)
        clean = re.sub(r'([A-Z]{2}\d{2})L', r'\g<1>1', clean)
        clean = re.sub(r'([A-Z]{2})[OIL]([OIL])(\d)', r'\g<1>0\g<2>\3', clean)
        clean = re.sub(r'([A-Z]{2}\d)[OIL]([OIL])', r'\g<1>0\g<2>', clean)
        clean = clean.replace('OOS', '005').replace('OOL', '001').replace('OOI', '001')
        clean = re.sub(r'([A-Z]{2}\d{2})S\b', r'\g<1>5', clean)
        clean = re.sub(r'([A-Z]{2}\d)S(\d)', r'\g<1>5\2', clean)
        if set_code is None:
            m = _YGO_SETCODE_RE.search(clean)
            if m:
                code = m.group(1).replace(" ", "").upper()
                code = re.sub(r'([A-Z0-9]{2,6})([A-Z]{2}[\d]{3})', r'\1-\2', code)
                set_code = code

    if passcode is None:
        joined = " ".join(texts)
        m = _YGO_PASSCODE_RE.search(joined.replace(" ", ""))
        if m:
            passcode = m.group(1)

    return set_code, passcode