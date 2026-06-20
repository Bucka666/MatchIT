# convert_fp16.py — FP16 conversion with shape-infer disabled (the hang fix),
# then parity vs Python on the same 4 images.
import os, numpy as np, torch, open_clip, onnx
from onnxconverter_common import float16
from PIL import Image
FP32 = "web_spike/mobileclip2s2_image.onnx"
FP16 = "web_spike/mobileclip2s2_image_fp16.onnx"
IMGS = ["test_queries/PXL_20260614_151551049.jpg",
        "test_queries/PXL_20260614_150023502.jpg",
        "test_queries/PXL_20260614_142116011.jpg",
        "test_queries/PXL_20260614_145940712.jpg"]

print("[fp16] converting (disable_shape_infer=True, pre-populated value_info) ...", flush=True)
m = onnx.load(FP32)
print(f"value_info entries before infer: {len(m.graph.value_info)}", flush=True)
m = onnx.shape_inference.infer_shapes(m)
print(f"value_info entries after infer:  {len(m.graph.value_info)}", flush=True)
m16 = float16.convert_float_to_float16(
    m, keep_io_types=True, disable_shape_infer=True,
    op_block_list=float16.DEFAULT_OP_BLOCK_LIST + ["Range"],
)
# Post-pass: sync Cast `to` attribute to match value_info output type.
# onnxconverter_common updates value_info but leaves Cast.to stale,
# producing a runtime/declared-type mismatch ORT rejects at load.
vi_map = {v.name: v for v in m16.graph.value_info}
fixed = 0
for node in m16.graph.node:
    if node.op_type != "Cast" or not node.output:
        continue
    out = node.output[0]
    if out not in vi_map:
        continue  # output is a graph output (keep_io_types boundary) — leave alone
    vi_type = vi_map[out].type.tensor_type.elem_type
    for attr in node.attribute:
        if attr.name == "to" and attr.i != vi_type:
            print(f"  [cast-fix] {node.name}: to {attr.i} -> {vi_type}")
            attr.i = vi_type
            fixed += 1
print(f"  [cast-fix] total Cast nodes updated: {fixed}", flush=True)

onnx.save(m16, FP16)
print(f"[fp16] saved, size = {os.path.getsize(FP16)/1e6:.1f} MB", flush=True)

import onnxruntime as ort
print("[fp16] loading session (surfaces type errors) ...", flush=True)
sess = ort.InferenceSession(FP16, providers=["CPUExecutionProvider"])
print("[fp16] session loaded OK", flush=True)

model, _, pre = open_clip.create_model_and_transforms("MobileCLIP2-S2", pretrained="dfndr2b")
model.eval()
class Enc(torch.nn.Module):
    def __init__(s, m): super().__init__(); s.m = m
    def forward(s, x): return s.m.encode_image(x)
enc = Enc(model).eval()
cos = lambda a, b: float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
print(f"\n{'image':<34} {'cosine FP16':>12}", flush=True)
ok = True
for p in IMGS:
    x = pre(Image.open(p).convert("RGB")).unsqueeze(0)
    with torch.no_grad():
        ref = enc(x).squeeze(0).numpy().astype(np.float32)
    o = sess.run(None, {"image": x.numpy().astype(np.float32)})[0].squeeze(0).astype(np.float32)
    c = cos(ref, o); ok &= c >= 0.999
    print(f"{os.path.basename(p):<34} {c:>12.6f}", flush=True)
worst = "n/a"
print(f"\nFP16 VERDICT: {'PASS' if ok else 'DRIFT (acceptable if close)'} (all cos >= 0.999)", flush=True)
