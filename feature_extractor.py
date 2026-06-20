"""
feature_extractor.py — MatchIT Image Embedder
===============================================
Supports backends switchable via config.json "embedder_backend":
    "clip"      — OpenAI CLIP ViT-L-14 via PyTorch (768-dim) — default
    "clip_onnx" — Same CLIP model via ONNX Runtime (768-dim) — optional
    "dinov2"    — Meta DINOv2 ViT-L-14 (1024-dim) — better structural detail
    "ensemble"  — CLIP + DINOv2 concatenated (1792-dim) — both signals

All backends share the same preprocessing pipeline:
    open → resize → _fast_bg_crop → _key_aware_crops → batch embed → mean pool → l2norm

IMPORTANT: Switching backend requires re-embedding the entire DB
           (Admin → Re-embed All) since dimensions differ.
           EXCEPTION: "clip" and "clip_onnx" produce identical embeddings
           (same model, same weights) — no re-embed needed when switching
           between them.

PERF NOTES:
    - Multi-crop embedding is BATCHED — all crops in one forward pass
    - torch.set_num_threads uses all CPU cores
    - All operations include timing logs for profiling

Usage:
    from feature_extractor import ImageEmbedder
    emb = ImageEmbedder()                        # reads from config.json
    vec = emb.embed_path("key.jpg", multi_crop=True, suppress_bg=True)
"""

import json
import os
import time

# Silence HuggingFace Hub warnings (models are already cached locally)
os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

import numpy as np
from PIL import Image
import torch

# ─────────────────────────────────────────────
# Shared config
# ─────────────────────────────────────────────

DEFAULT_MAX_SIDE = 1024

def _read_backend_from_config() -> str:
    """Read embedder_backend from config.json. Defaults to 'clip'."""
    try:
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            return str(cfg.get("embedder_backend", "clip")).strip().lower()
    except Exception:
        pass
    return "clip"


# ─────────────────────────────────────────────
# Shared preprocessing (identical for all backends)
# ─────────────────────────────────────────────

def _l2norm(x: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(x) + 1e-12
    return x / n


def _resize_max_side(pil_img: Image.Image, max_side: int) -> Image.Image:
    w, h = pil_img.size
    m = max(w, h)
    if m <= max_side:
        return pil_img
    scale = max_side / float(m)
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    return pil_img.resize((nw, nh), Image.BICUBIC)


def _fast_bg_crop(pil_img: Image.Image, pad_frac: float = 0.06) -> Image.Image:
    try:
        arr = np.asarray(pil_img, dtype=np.uint8)
        h, w = arr.shape[:2]
        if h < 80 or w < 80:
            return pil_img
        gray = (
            0.299 * arr[:, :, 0].astype(np.float32)
            + 0.587 * arr[:, :, 1].astype(np.float32)
            + 0.114 * arr[:, :, 2].astype(np.float32)
        )
        p95 = float(np.percentile(gray, 95))
        thr = min(245.0, max(150.0, p95 - 18.0))
        mask = (gray < thr).astype(np.uint8)
        mp = np.pad(mask, ((1, 1), (1, 1)), mode="edge")
        neigh = (
            mp[:-2, :-2] + mp[:-2, 1:-1] + mp[:-2, 2:]
            + mp[1:-1, :-2] + mp[1:-1, 1:-1] + mp[1:-1, 2:]
            + mp[2:, :-2]  + mp[2:, 1:-1]  + mp[2:, 2:]
        )
        mask2 = neigh >= 3
        ys, xs = np.where(mask2)
        if xs.size < 50 or ys.size < 50:
            return pil_img
        x0, x1 = int(xs.min()), int(xs.max())
        y0, y1 = int(ys.min()), int(ys.max())
        crop_w = x1 - x0 + 1
        crop_h = y1 - y0 + 1
        if crop_w < 60 or crop_h < 60:
            return pil_img
        if (crop_w * crop_h) < 0.04 * (w * h):
            return pil_img
        padx = int(crop_w * pad_frac)
        pady = int(crop_h * pad_frac)
        x0 = max(0, x0 - padx)
        y0 = max(0, y0 - pady)
        x1 = min(w - 1, x1 + padx)
        y1 = min(h - 1, y1 + pady)
        return pil_img.crop((x0, y0, x1 + 1, y1 + 1))
    except Exception:
        return pil_img


def _key_aware_crops(img: Image.Image):
    """
    Bow and tip crops for portrait keys only (landscape/SIDE_C no longer used).
    Full image always included as first element.
    Blade zone intentionally excluded — differs between cut key and blank.
    """
    w, h = img.size
    crops = [img]
    aspect = h / max(float(w), 1.0)

    if aspect >= 1.4:
        # Portrait — cylinder / mortice
        bow_crop = img.crop((0, 0,          w, int(h * 0.38)))
        tip_crop = img.crop((0, int(h * 0.62), w, h))
        for c in [bow_crop, tip_crop]:
            cw, ch = c.size
            if cw >= 32 and ch >= 32:
                crops.append(c)
    else:
        # Square-ish — quadrant crops
        cx, cy = w // 2, h // 2
        ov_x, ov_y = int(w * 0.12), int(h * 0.12)
        for c in [
            img.crop((0,         0,         cx + ov_x, cy + ov_y)),
            img.crop((cx - ov_x, 0,         w,         cy + ov_y)),
            img.crop((0,         cy - ov_y, cx + ov_x, h        )),
            img.crop((cx - ov_x, cy - ov_y, w,         h        )),
        ]:
            cw, ch = c.size
            if cw >= 32 and ch >= 32:
                crops.append(c)

    return crops


# ─────────────────────────────────────────────
# Backend: CLIP (PyTorch — default)
# ─────────────────────────────────────────────

class _CLIPBackend:
    """OpenAI CLIP ViT-L-14 via open_clip + PyTorch. 768-dim embeddings."""

    def __init__(self, device: str,
                 model_name: str = "ViT-L-14",
                 pretrained: str = "laion2b_s32b_b82k"):
        import open_clip
        self.device = device
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(
            model_name, pretrained=pretrained)
        self.model = self.model.to(self.device).eval()
        self.dim = 768
        self.name = f"clip/{model_name}/{pretrained}"

    @torch.no_grad()
    def embed_pil(self, img: Image.Image) -> np.ndarray:
        x = self.preprocess(img).unsqueeze(0).to(self.device)
        feat = self.model.encode_image(x)
        feat = feat / feat.norm(dim=-1, keepdim=True)
        return feat.squeeze(0).detach().cpu().numpy().astype(np.float32)

    @torch.no_grad()
    def batch_embed_pils(self, imgs: list) -> np.ndarray:
        """Embed multiple PIL images in a single forward pass. Returns (N, dim)."""
        if len(imgs) == 1:
            return np.expand_dims(self.embed_pil(imgs[0]), 0)
        tensors = [self.preprocess(img) for img in imgs]
        batch = torch.stack(tensors).to(self.device)
        feats = self.model.encode_image(batch)
        feats = feats / feats.norm(dim=-1, keepdim=True)
        return feats.detach().cpu().numpy().astype(np.float32)


# ─────────────────────────────────────────────
# Backend: CLIP ONNX (optional, for machines where it helps)
# ─────────────────────────────────────────────

class _CLIPOnnxBackend:
    """
    CLIP ViT-L-14 via ONNX Runtime. Same 768-dim embeddings as PyTorch backend.

    Requires:
        pip install onnxruntime
        python export_clip_onnx.py   (one-time, creates clip_visual.onnx)

    Produces IDENTICAL embeddings to _CLIPBackend — safe to switch without re-embedding DB.
    """

    def __init__(self, device: str,
                 model_name: str = "ViT-L-14",
                 pretrained: str = "laion2b_s32b_b82k"):
        import onnxruntime as ort

        self.device = device
        self.dim = 768
        self.name = f"clip_onnx/{model_name}/{pretrained}"

        onnx_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clip_visual.onnx")
        if not os.path.exists(onnx_path):
            raise FileNotFoundError(
                f"ONNX model not found at {onnx_path}. "
                f"Run 'python export_clip_onnx.py' first to export the model."
            )

        sess_opts = ort.SessionOptions()
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        n_threads = os.cpu_count() or 4
        sess_opts.intra_op_num_threads = n_threads
        sess_opts.inter_op_num_threads = 1
        sess_opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL

        self.session = ort.InferenceSession(
            onnx_path, sess_opts, providers=["CPUExecutionProvider"]
        )
        self.input_name = self.session.get_inputs()[0].name

        import open_clip
        _, _, self.preprocess = open_clip.create_model_and_transforms(
            model_name, pretrained=pretrained
        )

        print(f"[EMBEDDER] ONNX session loaded: {onnx_path} "
              f"({os.path.getsize(onnx_path) / 1024 / 1024:.0f} MB, "
              f"threads={n_threads})", flush=True)

    def embed_pil(self, img: Image.Image) -> np.ndarray:
        x = self.preprocess(img).unsqueeze(0).numpy()
        out = self.session.run(None, {self.input_name: x})[0]
        return out.squeeze(0).astype(np.float32)

    def batch_embed_pils(self, imgs: list) -> np.ndarray:
        if len(imgs) == 1:
            return np.expand_dims(self.embed_pil(imgs[0]), 0)
        tensors = [self.preprocess(img) for img in imgs]
        batch = torch.stack(tensors).numpy()
        out = self.session.run(None, {self.input_name: batch})[0]
        return out.astype(np.float32)


# ─────────────────────────────────────────────
# Backend: DINOv2
# ─────────────────────────────────────────────

class _DINOv2Backend:
    """Meta DINOv2 ViT-L-14 via transformers. 1024-dim embeddings."""

    def __init__(self, device: str,
                 model_name: str = "facebook/dinov2-large"):
        from transformers import AutoImageProcessor, AutoModel
        self.device = device
        self.processor = AutoImageProcessor.from_pretrained(model_name)
        self.model = AutoModel.from_pretrained(model_name)
        self.model = self.model.to(self.device).eval()
        self.dim = 1024
        self.name = f"dinov2/{model_name}"

    @torch.no_grad()
    def embed_pil(self, img: Image.Image) -> np.ndarray:
        inputs = self.processor(images=img, return_tensors="pt").to(self.device)
        outputs = self.model(**inputs)
        cls = outputs.last_hidden_state[:, 0]
        cls = cls / cls.norm(dim=-1, keepdim=True)
        return cls.squeeze(0).detach().cpu().numpy().astype(np.float32)

    @torch.no_grad()
    def batch_embed_pils(self, imgs: list) -> np.ndarray:
        if len(imgs) == 1:
            return np.expand_dims(self.embed_pil(imgs[0]), 0)
        inputs = self.processor(images=imgs, return_tensors="pt").to(self.device)
        outputs = self.model(**inputs)
        cls = outputs.last_hidden_state[:, 0]
        cls = cls / cls.norm(dim=-1, keepdim=True)
        return cls.detach().cpu().numpy().astype(np.float32)


# ─────────────────────────────────────────────
# Backend: Ensemble (CLIP + DINOv2 concatenated)
# ─────────────────────────────────────────────

class _EnsembleBackend:
    """Concatenated CLIP + DINOv2 embeddings. 1792-dim (768 + 1024)."""

    def __init__(self, device: str):
        self._clip = _CLIPBackend(device)
        self._dino = _DINOv2Backend(device)
        self.dim = self._clip.dim + self._dino.dim
        self.name = f"ensemble/{self._clip.name}+{self._dino.name}"

    @torch.no_grad()
    def embed_pil(self, img: Image.Image) -> np.ndarray:
        clip_emb = self._clip.embed_pil(img)
        dino_emb = self._dino.embed_pil(img)
        combined = np.concatenate([clip_emb, dino_emb])
        return _l2norm(combined).astype(np.float32)

    @torch.no_grad()
    def batch_embed_pils(self, imgs: list) -> np.ndarray:
        if len(imgs) == 1:
            return np.expand_dims(self.embed_pil(imgs[0]), 0)
        clip_batch = self._clip.batch_embed_pils(imgs)
        dino_batch = self._dino.batch_embed_pils(imgs)
        combined = np.concatenate([clip_batch, dino_batch], axis=1)
        norms = np.linalg.norm(combined, axis=1, keepdims=True) + 1e-12
        return (combined / norms).astype(np.float32)


# ─────────────────────────────────────────────
# Public API: ImageEmbedder
# ─────────────────────────────────────────────

class ImageEmbedder:
    """
    MatchIT image embedder with pluggable backend.

    Pipeline: open → resize → bg_crop → key_aware_crops → batch embed → mean pool → l2norm
    """

    def __init__(self, device: str | None = None, backend: str | None = None):
        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = device

        torch.manual_seed(0)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(0)

        # Optimize PyTorch CPU threading
        if device == "cpu":
            n_threads = os.cpu_count() or 4
            torch.set_num_threads(n_threads)

        # Resolve backend
        if backend is None:
            backend = _read_backend_from_config()
        backend = backend.strip().lower()

        if backend == "clip_onnx":
            try:
                self._backend = _CLIPOnnxBackend(device)
            except (ImportError, FileNotFoundError) as e:
                print(f"[EMBEDDER] ONNX backend failed ({e}), falling back to PyTorch CLIP", flush=True)
                self._backend = _CLIPBackend(device)
        elif backend == "dinov2":
            self._backend = _DINOv2Backend(device)
        elif backend == "ensemble":
            self._backend = _EnsembleBackend(device)
        else:
            self._backend = _CLIPBackend(device)

        self.backend_name = self._backend.name
        self.embedding_dim = self._backend.dim
        print(f"[EMBEDDER] Backend: {self.backend_name} ({self.embedding_dim}-dim) on {device}")

    @torch.no_grad()
    def _embed_pil(self, img: Image.Image) -> np.ndarray:
        return self._backend.embed_pil(img)

    @torch.no_grad()
    def embed_path(self, path: str, multi_crop: bool = True,
                   suppress_bg: bool = True, max_side: int | None = None) -> np.ndarray:
        t_start = time.time()

        img = Image.open(path).convert("RGB")
        ms = max(256, int(max_side) if max_side is not None else DEFAULT_MAX_SIDE)
        img = _resize_max_side(img, ms)

        if suppress_bg:
            img = _fast_bg_crop(img)

        t_preproc = time.time()

        if not multi_crop:
            result = self._embed_pil(img)
            t_model = time.time()
            print(f"[EMBEDDER-TIMING] preproc={t_preproc - t_start:.3f}s "
                  f"model={t_model - t_preproc:.3f}s "
                  f"total={t_model - t_start:.3f}s (single-crop)", flush=True)
            return result

        crops = _key_aware_crops(img)
        if not crops:
            result = self._embed_pil(img)
            t_model = time.time()
            print(f"[EMBEDDER-TIMING] preproc={t_preproc - t_start:.3f}s "
                  f"model={t_model - t_preproc:.3f}s "
                  f"total={t_model - t_start:.3f}s (no crops)", flush=True)
            return result

        t_crops = time.time()

        # ── BATCHED INFERENCE: all crops in one forward pass ──
        feats = self._backend.batch_embed_pils(crops)  # (N_crops, dim)
        mean_feat = np.mean(feats, axis=0).astype(np.float32)
        result = _l2norm(mean_feat)

        t_model = time.time()
        print(f"[EMBEDDER-TIMING] preproc={t_preproc - t_start:.3f}s "
              f"crops={len(crops)} "
              f"model={t_model - t_crops:.3f}s "
              f"total={t_model - t_start:.3f}s", flush=True)

        return result


# ─────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────

def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    a, b = a.astype(np.float32), b.astype(np.float32)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)