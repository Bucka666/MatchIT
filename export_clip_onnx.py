"""
export_clip_onnx.py — One-time ONNX export for CLIP ViT-L-14
==============================================================
Run once to create clip_visual.onnx in the project directory.
After export, set "embedder_backend": "clip_onnx" in config.json.

Requirements:
    pip install onnx onnxruntime

Usage:
    python export_clip_onnx.py

Output:
    clip_visual.onnx  — ONNX model for CLIP visual encoder (L2-normalized)
"""

import os
import sys
import time
import numpy as np
import torch
import torch.nn as nn

def export_clip_onnx():
    print("=" * 60)
    print("CLIP ViT-L-14 → ONNX Export")
    print("=" * 60)

    # ── Step 1: Load CLIP model ──
    print("\n[1/4] Loading CLIP ViT-L-14 (laion2b_s32b_b82k)...")
    t0 = time.time()

    import open_clip
    model, _, preprocess = open_clip.create_model_and_transforms(
        "ViT-L-14", pretrained="laion2b_s32b_b82k"
    )
    model.eval()
    print(f"       Loaded in {time.time() - t0:.1f}s")

    # ── Step 2: Create wrapper that includes L2 norm ──
    class CLIPVisualNormed(nn.Module):
        def __init__(self, clip_model):
            super().__init__()
            self.visual = clip_model.visual

        def forward(self, x):
            features = self.visual(x)
            features = features / (features.norm(dim=-1, keepdim=True) + 1e-12)
            return features

    wrapper = CLIPVisualNormed(model)
    wrapper.eval()

    # ── Step 3: Export to ONNX ──
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clip_visual.onnx")
    print(f"\n[2/4] Exporting to ONNX: {out_path}")
    t0 = time.time()

    # CLIP ViT-L-14 input: (batch, 3, 224, 224)
    dummy = torch.randn(3, 3, 224, 224)  # batch=3 to match typical multi-crop

    # Force legacy TorchScript-based exporter (works on all PyTorch versions,
    # does NOT require onnxscript which is needed by the newer dynamo exporter)
    export_kwargs = dict(
        model=wrapper,
        args=(dummy,),
        f=out_path,
        input_names=["pixel_values"],
        output_names=["embeddings"],
        dynamic_axes={
            "pixel_values": {0: "batch_size"},
            "embeddings": {0: "batch_size"},
        },
        opset_version=14,
        do_constant_folding=True,
    )

    # PyTorch >= 2.6 added a 'dynamo' kwarg; set False to use legacy path
    try:
        torch.onnx.export(**export_kwargs, dynamo=False)
    except TypeError:
        # Older PyTorch without 'dynamo' kwarg — legacy is the only path
        torch.onnx.export(**export_kwargs)

    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(f"       Exported in {time.time() - t0:.1f}s ({size_mb:.0f} MB)")

    # ── Step 4: Verify ONNX output matches PyTorch ──
    print("\n[3/4] Verifying ONNX output matches PyTorch...")

    import onnxruntime as ort

    sess_opts = ort.SessionOptions()
    sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    session = ort.InferenceSession(out_path, sess_opts, providers=["CPUExecutionProvider"])

    test_input = torch.randn(1, 3, 224, 224)

    # PyTorch reference
    with torch.no_grad():
        pt_out = wrapper(test_input).numpy()

    # ONNX inference
    ort_out = session.run(None, {"pixel_values": test_input.numpy()})[0]

    max_diff = float(np.max(np.abs(pt_out - ort_out)))
    cos_sim = float(np.dot(pt_out.flatten(), ort_out.flatten()) /
                     (np.linalg.norm(pt_out) * np.linalg.norm(ort_out) + 1e-12))

    print(f"       Max absolute diff: {max_diff:.6f}")
    print(f"       Cosine similarity: {cos_sim:.6f}")

    if cos_sim < 0.999:
        print("       WARNING: Cosine similarity < 0.999 — check export quality")
    else:
        print("       OK — ONNX output matches PyTorch")

    # ── Step 5: Benchmark ──
    print("\n[4/4] Benchmarking (batch=3, like multi-crop)...")
    bench_input = torch.randn(3, 3, 224, 224)

    # PyTorch timing
    times_pt = []
    for _ in range(5):
        t0 = time.time()
        with torch.no_grad():
            _ = wrapper(bench_input)
        times_pt.append(time.time() - t0)

    # ONNX timing
    bench_np = bench_input.numpy()
    times_ort = []
    for _ in range(5):
        t0 = time.time()
        _ = session.run(None, {"pixel_values": bench_np})
        times_ort.append(time.time() - t0)

    pt_avg = sum(times_pt[1:]) / len(times_pt[1:])  # skip warmup
    ort_avg = sum(times_ort[1:]) / len(times_ort[1:])

    print(f"       PyTorch CPU:  {pt_avg:.2f}s per batch of 3")
    print(f"       ONNX Runtime: {ort_avg:.2f}s per batch of 3")
    print(f"       Speedup:      {pt_avg / max(ort_avg, 0.001):.1f}x")

    print(f"\n{'=' * 60}")
    print(f"Export complete: {out_path}")
    print(f"Now set \"embedder_backend\": \"clip_onnx\" in config.json")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    export_clip_onnx()