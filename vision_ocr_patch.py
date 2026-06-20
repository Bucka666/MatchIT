# ═══════════════════════════════════════════════════════════════
# REPLACEMENT FOR _crop_and_read IN ocr_confirm.py
# ═══════════════════════════════════════════════════════════════
#
# Give Claude Code this instruction:
#
# In ocr_confirm.py, replace the entire _crop_and_read function with this:

def _crop_and_read(
    image_path: str,
    region: tuple,
    min_width: int = 400,
) -> list:
    """
    Crop image to fractional region (left, top, right, bottom),
    send to Google Cloud Vision API, return list of text strings.
    Falls back to EasyOCR if Vision API key not configured.
    """
    try:
        import base64
        import json
        import urllib.request
        import numpy as np
        from PIL import Image, ImageFilter, ImageEnhance, ImageOps

        img = Image.open(image_path).convert("RGB")
        w, h = img.size
        left   = int(region[0] * w)
        top    = int(region[1] * h)
        right  = int(region[2] * w)
        bottom = int(region[3] * h)
        crop = img.crop((left, top, right, bottom))

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
            return _vision_api_read(crop, api_key)

        # Fallback to EasyOCR
        logger.warning("[OCR] Google Vision key not found, falling back to EasyOCR")
        return _easyocr_read(crop)

    except Exception as e:
        logger.warning(f"[OCR] crop_and_read error: {e}")
        return []


def _get_vision_api_key() -> str:
    """Load Google Vision API key from config.json."""
    try:
        import json
        import os
        # Try app root config.json
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


def _easyocr_read(crop_image) -> list:
    """PaddleOCR reader."""
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
                if text:
                    results.append(text)
        return results
    except Exception as e:
        logger.warning(f"[OCR-PADDLE] error: {e}")
        return []

# ═══════════════════════════════════════════════════════════════
# ALSO: Remove the existing _get_ocr_reader lazy init and SSL fix
# from the top of the file — EasyOCR is now only a fallback.
# Leave _get_ocr_reader in place but it will rarely be called.
# ═══════════════════════════════════════════════════════════════