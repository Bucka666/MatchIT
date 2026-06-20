# export_phase0.py — Phase 0 Step 1+2
# Export MobileCLIP2-S2/dfndr2b IMAGE encoder to ONNX (FP32 + FP16) and verify
# parity vs the open_clip Python embedding on 4 real test images.
# Standalone — touches nothing in the app.
import os, json
import numpy as np
import torch, open_clip
from PIL import Image

OUT = "web_spike"; os.makedirs(OUT, exist_ok=True)
FP32 = os.path.join(OUT, "mobileclip2s2_image.onnx")
FP16 = os.path.join(OUT, "mobileclip2s2_image_fp16.onnx")
TEST_IMGS = [
    "test_queries/PXL_20260614_151551049.jpg",   # me1-112 (a disputed card)
    "test_queries/PXL_20260614_150023502.jpg",   # sv8pt5-16
    "test_queries/PXL_20260614_142116011.jpg",   # ygo
    "test_queries/PXL_20260614_145940712.jpg",   # mtg-snc-36
]

print("[load] MobileCLIP2-S2/dfndr2b ...")
model, _, preprocess = open_clip.create_model_and_transforms("MobileCLIP2-S2", pretrained="dfndr2b")
model.eval()


class ImageEncoder(torch.nn.Module):
    def __init__(self, m): super().__init__(); self.m = m
    def forward(self, x): return self.m.encode_image(x)   # raw 512-d, no L2 norm


enc = ImageEncoder(model).eval()

print("[export] FP32 ONNX (opset 17) ...")
torch.onnx.export(
    enc, torch.randn(1, 3, 256, 256), FP32,
    input_names=["image"], output_names=["embedding"],
    dynamic_axes={"image": {0: "batch"}, "embedding": {0: "batch"}},
    opset_version=17, do_constant_folding=True,
)

print("[export] FP16 ONNX ...")
import onnx
from onnxconverter_common import float16
fp16_ok = False
try:
    m16 = float16.convert_float_to_float16(
        onnx.load(FP32), keep_io_types=True,                 # IO stays fp32
        op_block_list=float16.DEFAULT_OP_BLOCK_LIST + ["Range", "Cast"],
    )
    onnx.save(m16, FP16)
    fp16_ok = True
except Exception as e:
    print(f"  FP16 export FAILED: {str(e)[:160]}  -> keeping FP32 only")

sz32 = os.path.getsize(FP32) / 1e6
sz16 = os.path.getsize(FP16) / 1e6 if fp16_ok and os.path.isfile(FP16) else 0.0
print(f"\n[size] FP32 = {sz32:.1f} MB" + (f"   FP16 = {sz16:.1f} MB" if fp16_ok else "   FP16 = (failed)"))

# ── parity ──
import onnxruntime as ort
def session(p): return ort.InferenceSession(p, providers=["CPUExecutionProvider"])
s32 = session(FP32)
s16 = session(FP16) if fp16_ok else None

# load FP16 session up front to surface any load-time type errors before the loop
if fp16_ok:
    try:
        _ = s16
    except Exception as e:
        print(f"  FP16 session load FAILED: {str(e)[:160]}"); fp16_ok = False; s16 = None

def cos(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

print("\n========== STEP 2 PARITY (cosine vs open_clip Python embedding) ==========")
print(f"  {'image':<34} {'FP32':>9} {'FP16':>9}")
rows, pass32, pass16 = [], True, True
for p in TEST_IMGS:
    if not os.path.isfile(p):
        print(f"  {os.path.basename(p):<34}  MISSING"); continue
    x = preprocess(Image.open(p).convert("RGB")).unsqueeze(0)
    with torch.no_grad():
        ref = enc(x).squeeze(0).numpy().astype(np.float32)
    xn = x.numpy().astype(np.float32)
    o32 = s32.run(None, {"image": xn})[0].squeeze(0).astype(np.float32)
    c32 = cos(ref, o32); pass32 &= c32 >= 0.999
    if fp16_ok:
        o16 = s16.run(None, {"image": xn})[0].squeeze(0).astype(np.float32)
        c16 = cos(ref, o16); pass16 &= c16 >= 0.999
        print(f"  {os.path.basename(p):<34} {c32:>9.6f} {c16:>9.6f}")
    else:
        c16 = None
        print(f"  {os.path.basename(p):<34} {c32:>9.6f} {'n/a':>9}")
    rows.append({"image": os.path.basename(p), "cos_fp32": c32, "cos_fp16": c16})

print("\n  VERDICT:")
print(f"    FP32: {'PASS' if pass32 else 'FAIL'} (all cos >= 0.999)")
if fp16_ok:
    print(f"    FP16: {'PASS' if pass16 else 'DRIFT'} (all cos >= 0.999)")
    if not pass16:
        worst = min(r['cos_fp16'] for r in rows if r['cos_fp16'] is not None)
        print(f"          worst FP16 cosine = {worst:.6f}  (drift = {1-worst:.6f})")
else:
    print("    FP16: unavailable (export/load failed) — FP32 is the shippable artifact")

with open(os.path.join(OUT, "parity_step2.json"), "w", encoding="utf-8") as f:
    json.dump({"fp32_mb": round(sz32, 1), "fp16_mb": round(sz16, 1),
               "fp16_ok": fp16_ok, "rows": rows,
               "pass_fp32": pass32, "pass_fp16": pass16 if fp16_ok else None}, f, indent=2)
print(f"\n[saved] {OUT}/parity_step2.json")
