"""
ocr_confirm.py — PaddleOCR-based set code / card number confirmation for MatchIT
=================================================================================
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
Add to Modal pip_install: "paddleocr>=2.7.0", "paddlepaddle>=2.6.0"
"""

import logging
import os
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# PaddleOCR reader — lazy loaded, cached per process
# ─────────────────────────────────────────────────────────────

_ocr_reader = None
_ocr_init_attempted = False
_last_ocr_confidence = 0.0

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
    # Scarlet & Violet era — only SV cards have printed text codes
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
    "BLK": "sv11b",     # Black Bolt (not yet in DB)
    "WHT": "sv11w",     # White Flare (not yet in DB)
    "ASC": "me2pt5",    # Ascended Heroes (Mega Evolution series)
}


def _get_ocr_reader():
    """Return cached PaddleOCR reader, initialising once on first call."""
    global _ocr_reader, _ocr_init_attempted
    if _ocr_init_attempted:
        return _ocr_reader
    try:
        from paddleocr import PaddleOCR
        _ocr_reader = PaddleOCR(
            use_angle_cls=True,
            lang="en",
        )
        _ocr_init_attempted = True
        logger.info("[OCR] PaddleOCR ready")
        return _ocr_reader
    except Exception as e:
        logger.error(f"[OCR] PaddleOCR could not be initialised: {e}")
        return None

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
    Falls back to PaddleOCR if Vision API key not configured.
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

        # Fallback to PaddleOCR if Vision API key not configured
        logger.warning("[OCR] Google Vision key not found, falling back to PaddleOCR")
        paddle_results = _paddle_ocr_read(crop)
        return [text for text, conf in paddle_results]

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


def _paddle_ocr_read(crop_image) -> list:
    """PaddleOCR reader — returns text results from crop image."""
    global _last_ocr_confidence
    import numpy as np
    reader = _get_ocr_reader()
    if reader is None:
        return []
    try:
        crop_arr = np.array(crop_image)
        raw = reader.ocr(crop_arr, cls=True)
        results = []
        if raw and raw[0]:
            for line in raw[0]:
                text = str(line[1][0]).strip().upper()
                confidence = float(line[1][1])
                if text:
                    results.append((text, confidence))
        if results:
            _last_ocr_confidence = max(conf for _, conf in results)
        else:
            _last_ocr_confidence = 0.0
        return results
    except Exception as e:
        logger.warning(f"[OCR-PADDLE] error: {e}")
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
    # Plain number pattern for energy cards e.g. "015" without total
    _PLAIN_NUM_RE = re.compile(r'\b(\d{1,4})\b')
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
    if num is None:
        for text in texts:
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
        return combined
    # SWSH fingerprint — use total to identify set
    # Reject impossible totals (no Pokémon set has more than ~300 cards)
    if total and total > 300:
        total = None
    if num and total and not set_code:
        candidates = _SWSH_TOTAL_MAP.get(total, [])
        if len(candidates) == 1:
            combined = f"{candidates[0]}-{num}"
            logger.info(f"[OCR-PKM] SWSH fingerprint: total={total} → {combined}")
            return combined
        elif len(candidates) > 1:
            logger.info(f"[OCR-PKM] SWSH ambiguous total={total}, candidates={candidates}")
            return num
    if num:
        logger.info(f"[OCR-PKM] extracted card number only: {num}")
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


def ocr_direct_lookup(image_path: str, tcg: str) -> Optional[str]:
    """
    OCR-first lookup for YGO and MTG — runs before CLIP.
    Extracts set code / collector number directly from the card image
    and looks it up in the DB. Returns matched SKU or None.
    """
    tcg_upper = tcg.upper()
    if tcg_upper == "YUGIOH":
        extracted = _extract_ygo_setcode(image_path)
    elif tcg_upper == "MTG":
        extracted = _extract_mtg_collector(image_path)
    else:
        return None
    if not extracted:
        return None
    logger.info(f"[OCR-FIRST] extracted: {extracted} for {tcg_upper}")
    db_match = _lookup_sku_by_setcode(extracted, tcg_upper)
    if db_match:
        logger.info(f"[OCR-FIRST] direct DB match: {db_match}")
    return db_match


# Main entry point
# ─────────────────────────────────────────────────────────────

def ocr_confirm_ranking(
    results: List[dict],
    query_image_path: str,
    tcg: str,
    search_depth: int = 5,
    allow_pokemon_promote: bool = True,
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
        if db_match:
            logger.info(f"[OCR] Direct DB match: {db_match}")
            if not allow_pokemon_promote and _is_pokemon_sku(db_match):
                ocr_info["ocr_status"] = "skipped_pokemon_promote"
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

    # Already rank 1 — confirm and return
    if match_idx == 0:
        logger.info(f"[OCR] Rank 1 confirmed by OCR: {results[0]['sku']}")
        ocr_info["ocr_status"] = "rank1_confirmed"
        return results, ocr_info

    # Promote matched result to rank 1
    matched_sku = results[match_idx].get("sku", "") if isinstance(results[match_idx], dict) else results[match_idx].sku
    if not allow_pokemon_promote and _is_pokemon_sku(matched_sku):
        ocr_info["ocr_status"] = "skipped_pokemon_promote"
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