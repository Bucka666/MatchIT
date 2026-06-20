# parity_fp32.py — fast FP32-only parity using the already-exported ONNX.
import os, numpy as np, torch, open_clip
from PIL import Image
FP32 = "web_spike/mobileclip2s2_image.onnx"
IMGS = ["test_queries/PXL_20260614_151551049.jpg",
        "test_queries/PXL_20260614_150023502.jpg",
        "test_queries/PXL_20260614_142116011.jpg",
        "test_queries/PXL_20260614_145940712.jpg"]
print("[load] open_clip MobileCLIP2-S2/dfndr2b ...", flush=True)
model, _, pre = open_clip.create_model_and_transforms("MobileCLIP2-S2", pretrained="dfndr2b")
model.eval()
class Enc(torch.nn.Module):
    def __init__(s, m): super().__init__(); s.m = m
    def forward(s, x): return s.m.encode_image(x)
enc = Enc(model).eval()
import onnxruntime as ort
sess = ort.InferenceSession(FP32, providers=["CPUExecutionProvider"])
cos = lambda a, b: float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
print(f"[size] FP32 = {os.path.getsize(FP32)/1e6:.1f} MB\n", flush=True)
print(f"{'image':<34} {'cosine FP32':>12}", flush=True)
ok = True
for p in IMGS:
    x = pre(Image.open(p).convert("RGB")).unsqueeze(0)
    with torch.no_grad():
        ref = enc(x).squeeze(0).numpy().astype(np.float32)
    o = sess.run(None, {"image": x.numpy().astype(np.float32)})[0].squeeze(0).astype(np.float32)
    c = cos(ref, o); ok &= c >= 0.999
    print(f"{os.path.basename(p):<34} {c:>12.6f}", flush=True)
print(f"\nFP32 VERDICT: {'PASS' if ok else 'FAIL'} (all cos >= 0.999)", flush=True)
