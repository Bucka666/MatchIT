# diff_fetch_vs_filepick.py — Step 4 control: prove the fetch loader changed NOTHING.
# Diffs the new fetch-path embeddings against the validated file-picker embeddings
# (embeddings_desktop.json). Same bytes, same canvas, same model => expect
# bit-identical / within floating-point noise. Any real divergence = the loader
# disturbed something, and we fix it before touching the Pixel.
import json, sys
import numpy as np

A = "embeddings_desktop.json"          # control (file-picker)
B = sys.argv[1] if len(sys.argv) > 1 else "embeddings_fetch.json"   # new fetch path

def load(p):
    d = json.load(open(p, encoding="utf-8"))
    return {r["image"]: np.asarray(r["embedding"], np.float32)
            for r in d["embeddings"] if not r.get("error")}, d.get("execution_provider")

a, epa = load(A); b, epb = load(B)
common = sorted(set(a) & set(b))
print(f"[{A}] ep={epa}  rows={len(a)}")
print(f"[{B}] ep={epb}  rows={len(b)}")
print(f"joined on filename: {len(common)}  (A-only {len(set(a)-set(b))}, B-only {len(set(b)-set(a))})")

cos, maxabs = [], []
for fn in common:
    va, vb = a[fn], b[fn]
    cos.append(float(np.dot(va, vb) / (np.linalg.norm(va) * np.linalg.norm(vb))))
    maxabs.append(float(np.max(np.abs(va - vb))))
cos = np.array(cos); maxabs = np.array(maxabs)
print(f"\ncosine(fetch, filepick): min={cos.min():.8f}  mean={cos.mean():.8f}")
print(f"max |elementwise diff| : mean={maxabs.mean():.3e}  max={maxabs.max():.3e}")
print(f"rows bit-identical (maxabs==0): {int((maxabs==0).sum())}/{len(common)}")
worst = common[int(np.argmin(cos))]
print(f"worst-cosine row: {worst}  cos={cos.min():.8f}")
VERDICT = "CLEAN (loader did not disturb embeddings)" if cos.min() >= 0.99999 else \
          "DIVERGENCE — investigate loader before Pixel run"
print(f"\nVERDICT: {VERDICT}")
