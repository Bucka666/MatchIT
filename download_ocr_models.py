"""
One-time script to pre-download PaddleOCR models before deploying to Modal.
Run locally: python download_ocr_models.py
"""
import ssl
ssl._create_default_https_context = ssl._create_unverified_context

from paddleocr import PaddleOCR

print("[OCR] Downloading PaddleOCR models...")
reader = PaddleOCR(
    use_angle_cls=True,
    lang="en",
    use_gpu=False,
    show_log=True,
)
print("[OCR] PaddleOCR models downloaded successfully.")
