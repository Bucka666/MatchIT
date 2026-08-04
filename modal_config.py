"""
modal_config.py — Shared Modal volume and image definition
==========================================================
Import image and vol from here in any Modal-enabled script.
Keeping them in one place avoids circular imports when scripts
like incremental_embed.py are run directly via `modal run`.
"""

import modal
import subprocess as _sp

VOLUME_NAME = "matchit-data-v2"

vol = modal.Volume.from_name(VOLUME_NAME, version=2)


def _git_sha() -> str:
    """Short HEAD hash, resolved LOCALLY at deploy time and baked into the
    image as GS_GIT_SHA. app.py prints it at startup as [VERSION].

    .git is in the add_local_dir ignore list, so the container cannot work
    this out for itself — it has to be passed in. Deploy verification twice
    this week had to fall back on circumstantial evidence (cold-start
    duration, byte-size deltas) because the changes were on silent code
    paths; one baked-in line removes that permanently.

    Falls back to "unknown" rather than raising: a deploy must never fail
    because git is unavailable.
    """
    try:
        sha = _sp.check_output(["git", "rev-parse", "--short", "HEAD"],
                               cwd="C:/MatchIT", stderr=_sp.DEVNULL,
                               timeout=10).decode().strip()
        try:
            _sp.check_output(["git", "diff", "--quiet", "HEAD"], cwd="C:/MatchIT",
                             stderr=_sp.DEVNULL, timeout=10)
        except _sp.CalledProcessError:
            sha += "-dirty"      # uncommitted changes are in this image
        return sha or "unknown"
    except Exception:
        return "unknown"


GIT_SHA = _git_sha()

image = (
    modal.Image.debian_slim(python_version="3.11")
    .run_commands(
        "apt-get update && apt-get install -y libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*"
    )
    .pip_install(
        "flask==3.0.0",
        "flask-cors==4.0.0",
        "Pillow>=10.0.0",
        "numpy>=1.24.0",
        "requests>=2.31.0",
        "open_clip_torch>=2.24.0",
        "torch>=2.1.0",
        "torchvision>=0.16.0",
        "transformers>=4.36.0",
        "timm>=0.9.12",
        "huggingface_hub>=0.20.0",
        "stripe>=7.0.0",
        "anthropic>=0.25.0",
        "pywebpush>=2.3.0",
        "boto3",
        "tcgdex-sdk==2.3.0",
        "google-auth>=2.23.0",
    )
    .run_commands(
        "python -c \"import open_clip; open_clip.create_model_and_transforms('ViT-L-14', pretrained='laion2b_s32b_b82k')\"",
        "python -c \"from transformers import AutoModel, AutoImageProcessor; AutoModel.from_pretrained('facebook/dinov2-large'); AutoImageProcessor.from_pretrained('facebook/dinov2-large')\"",
    )
    .env({
        "HF_HUB_OFFLINE": "1",
        "PYTHONWARNINGS": "ignore::UserWarning",
        # Resolved locally at deploy time — see _git_sha(). Printed by
        # app.py at startup as [VERSION], so a deploy can be verified
        # directly instead of by inference.
        "GS_GIT_SHA": GIT_SHA,
    })
    .add_local_dir(
        "C:/MatchIT",
        remote_path="/app",
        ignore=[
            # existing
            "*.txt", "*.md", "__pycache__", "*.pyc", ".git", ".venv", "*.log",
            # P-3: scratch/dev/test dirs — recon-confirmed unreferenced at runtime
            ".cache_mobileclip_test",
            ".wrangler",
            ".claude",
            "fp16_drift_run",
            "ondevice_index_v1_test",
            "regression_queries",
            "test_queries",
            "_snapshots",
            "ondevice_index_v1",
            "web_spike",
            # Rescued scan images + blind labelling tool (2026-08-02). ~630 MB
            # of JPEGs, a 22 MB reference index and a 7 MB inline-index HTML.
            # Local analysis artefacts only — nothing here is imported or read
            # at runtime, and .gitignore does not cover the Modal mount.
            "eval_rescue",
            # Sidecars of config.json are copies of the same secrets.
            # config.json.preocrflag / .prefallbackflag were baked into
            # every deployed image up to v5. This pattern needs a suffix,
            # so config.json ITSELF is untouched and still uploads —
            # app.py reads it at runtime.
            "config.json.*",
            "*_pre_*",
            "*_preocr*",
            "*.bak*",
        ],
    )
)
