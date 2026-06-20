# compare_browser_embedding.py — Step 4 verification.
# Cosine between the BROWSER-exported embedding and the open_clip Python
# embedding for the SAME image. PASS = cosine >= 0.999 (proves JS preprocessing
# + ORT-web reproduce the validation embeddings).
#
# Usage:
#   1. On the phone, Run inference, tap "Copy embedding JSON", paste it into
#      a file e.g. browser_embedding.json (or save the textarea contents).
#   2. python compare_browser_embedding.py --json browser_embedding.json \
#          --image test_queries/PXL_20260614_151551049.jpg
#
import argparse, json
import numpy as np, torch, open_clip
from PIL import Image

ap = argparse.ArgumentParser()
ap.add_argument("--json", default="browser_embedding.json",
                help="file containing the browser 'Copy embedding JSON' output")
ap.add_argument("--image", required=True, help="the SAME image embedded in the browser")
args = ap.parse_args()

with open(args.json, encoding="utf-8") as f:
    data = json.load(f)
browser = np.asarray(data["embedding"], dtype=np.float32)
browser /= np.linalg.norm(browser)          # ensure normalized
print(f"browser embedding: dim={browser.shape[0]}  (image tag: {data.get('image')})")

model, _, preprocess = open_clip.create_model_and_transforms("MobileCLIP2-S2", pretrained="dfndr2b")
model.eval()
x = preprocess(Image.open(args.image).convert("RGB")).unsqueeze(0)
with torch.no_grad():
    ref = model.encode_image(x).squeeze(0).numpy().astype(np.float32)
ref /= np.linalg.norm(ref)

cos = float(np.dot(browser, ref))
print(f"\ncosine(browser, open_clip Python) = {cos:.6f}")
print("VERDICT:", "PASS (>=0.999) — JS preprocessing reproduces the embedding"
      if cos >= 0.999 else
      f"DRIFT ({cos:.4f}) — JS preprocessing is off; resize/crop is the likely cause")
