# export_mobileclip_onnx.py
# Export MobileCLIP2-S2/dfndr2b IMAGE encoder to ONNX (FP32 + FP16) and verify
# parity against the Python embedding. Outputs go to web_spike/.
import os, time, json
import numpy as np
import torch, open_clip
from PIL import Image

OUT_DIR = "web_spike"
os.makedirs(OUT_DIR, exist_ok=True)
FP32 = os.path.join(OUT_DIR, "mobileclip2_s2_fp32.onnx")
FP16 = os.path.join(OUT_DIR, "mobileclip2_s2_fp16.onnx")
TEST_IMG = "test_queries/PXL_20260614_151551049.jpg"   # a real card photo

print("[load] MobileCLIP2-S2/dfndr2b ...")
model, _, preprocess = open_clip.create_model_and_transforms("MobileCLIP2-S2", pretrained="dfndr2b")
model.eval()


class ImageEncoder(torch.nn.Module):
    """Preprocessed image tensor -> raw 512-d image embedding (no L2 norm)."""
    def __init__(self, m): super().__init__(); self.m = m
    def forward(self, x): return self.m.encode_image(x)


enc = ImageEncoder(model).eval()
dummy = torch.randn(1, 3, 256, 256)

print("[export] FP32 ONNX ...")
torch.onnx.export(
    enc, dummy, FP32,
    input_names=["image"], output_names=["embedding"],
    dynamic_axes={"image": {0: "batch"}, "embedding": {0: "batch"}},
    opset_version=17, do_constant_folding=True,
)

# ── parity reference (Python torch) ──
img = Image.open(TEST_IMG).convert("RGB")
x = preprocess(img).unsqueeze(0)                     # identical preprocessing both sides
with torch.no_grad():
    ref = enc(x).squeeze(0).numpy().astype(np.float32)

import onnxruntime as ort
def run(path):
    s = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
    return s.run(None, {"image": x.numpy().astype(np.float32)})[0].squeeze(0).astype(np.float32)

def cos(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

o32 = run(FP32)
print("\n========== PARITY (cosine sim vs Python torch embedding) ==========")
print(f"  ONNX FP32 vs Python : {cos(ref, o32):.6f}")

# FP16 conversion (INT8 broke the export per the browser dev; FP16 expected fine).
# keep_io_types=True keeps inputs/outputs fp32; block Cast/ops that otherwise
# produce a type-mismatch on load.
print("\n[export] FP16 ONNX ...")
import onnx
from onnxconverter_common import float16
m32 = onnx.load(FP32)
o16 = None
for strat, kw in [
    ("block Range+Cast", dict(keep_io_types=True,
        op_block_list=float16.DEFAULT_OP_BLOCK_LIST + ["Range", "Cast"])),
    ("default block list", dict(keep_io_types=True)),
]:
    try:
        m16 = float16.convert_float_to_float16(onnx.load(FP32), **kw)
        onnx.save(m16, FP16)
        o16 = run(FP16)
        print(f"  FP16 OK via [{strat}] -> cosine vs Python: {cos(ref, o16):.6f}")
        break
    except Exception as e:
        print(f"  FP16 [{strat}] failed: {str(e)[:120]}")

sz32 = os.path.getsize(FP32) / 1e6
sz16 = os.path.getsize(FP16) / 1e6 if os.path.isfile(FP16) and o16 is not None else 0.0
print(f"\n[size] FP32 = {sz32:.1f} MB   FP16 = {sz16:.1f} MB" +
      ("" if o16 is not None else "  (FP16 unusable — spike will use FP32)"))
print(f"  ref[:6]    = {np.round(ref[:6], 4).tolist()}")
print(f"  onnx32[:6] = {np.round(o32[:6], 4).tolist()}")
if o16 is not None:
    print(f"  onnx16[:6] = {np.round(o16[:6], 4).tolist()}")

# Save a reference embedding + the exact preprocessed input for the JS sanity check
np.save(os.path.join(OUT_DIR, "ref_embedding.npy"), ref)
ref_meta = {
    "test_image": os.path.basename(TEST_IMG),
    "embedding_dim": int(ref.shape[0]),
    "ref_first6": np.round(ref[:6], 6).tolist(),
    "preprocess": {"resize": 256, "center_crop": 256, "scale": "x/255",
                   "mean": [0, 0, 0], "std": [1, 1, 1], "interpolation": "bilinear"},
    "input_layout": "NCHW float32 [1,3,256,256]",
}
with open(os.path.join(OUT_DIR, "ref_meta.json"), "w", encoding="utf-8") as f:
    json.dump(ref_meta, f, indent=2)
print(f"\n[saved] {OUT_DIR}/ref_embedding.npy + ref_meta.json")
