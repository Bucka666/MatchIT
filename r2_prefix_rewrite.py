"""
r2_prefix_rewrite.py — Rewrite img_url prefixes in the 3 set-card-list sidecars.

The sidecars (pokemon/mtg/ygo_set_card_lists.json) already carry current card
data — only their img_url prefix is stale (/img/db/ instead of the R2 domain).
This rewrites the prefix in place; it does not touch card data and does not
re-read any profiles. Standalone, CPU-only Modal job — not a deploy.

Usage:
    modal run r2_prefix_rewrite.py::rewrite_prefixes --dry-run
    modal run r2_prefix_rewrite.py::rewrite_prefixes --no-dry-run
"""

import modal

VOLUME_NAME = "matchit-data-v2"
OLD_PREFIX = "/img/db/"
NEW_PREFIX = "https://images.grailsweep.com/"
FILES = [
    "pokemon_set_card_lists.json",
    "mtg_set_card_lists.json",
    "ygo_set_card_lists.json",
]

app = modal.App("grailsweep-prefix-rewrite")
vol = modal.Volume.from_name(VOLUME_NAME, version=2)
image = modal.Image.debian_slim(python_version="3.11")


@app.function(image=image, volumes={"/modal_data": vol}, timeout=600)
def rewrite_prefixes(dry_run: bool = True):
    import json
    import os

    vol.reload()
    results = {}

    for fname in FILES:
        path = os.path.join("/modal_data", fname)
        if not os.path.exists(path):
            print(f"[REWRITE] {fname}: MISSING, skipping", flush=True)
            results[fname] = {"status": "missing"}
            continue

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        total_cards = 0
        rewritten = 0
        already_new = 0
        anomalies = 0
        samples = []

        for set_id, entry in data.items():
            for card in entry.get("cards", []):
                total_cards += 1
                url = card.get("img_url", "")
                if url.startswith(OLD_PREFIX):
                    new_url = NEW_PREFIX + url[len(OLD_PREFIX):]
                    if len(samples) < 2:
                        samples.append((url, new_url))
                    card["img_url"] = new_url
                    rewritten += 1
                elif url.startswith(NEW_PREFIX):
                    already_new += 1
                else:
                    anomalies += 1

        # Sanity check on the in-memory rewritten data, before any write.
        bad_old = 0
        bad_other = 0
        for set_id, entry in data.items():
            for card in entry.get("cards", []):
                url = card.get("img_url", "")
                if url.startswith(OLD_PREFIX):
                    bad_old += 1
                elif not url.startswith(NEW_PREFIX):
                    bad_other += 1
        sanity_ok = bad_old == 0 and bad_other == 0

        print(
            f"[REWRITE] {fname}: total_cards={total_cards} rewritten={rewritten} "
            f"already_new={already_new} anomalies={anomalies} "
            f"sanity={'PASS' if sanity_ok else 'FAIL'}",
            flush=True,
        )
        for old_u, new_u in samples:
            print(f"[REWRITE]   sample: {old_u} -> {new_u}", flush=True)

        results[fname] = {
            "total_cards": total_cards,
            "rewritten": rewritten,
            "already_new": already_new,
            "anomalies": anomalies,
            "sanity_ok": sanity_ok,
        }

        if not sanity_ok:
            print(f"[REWRITE] {fname}: ABORTED (sanity check failed), not written", flush=True)
            continue

        if dry_run:
            continue

        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp_path, path)
        print(f"[REWRITE] {fname}: written", flush=True)

    if dry_run:
        print("[REWRITE] DRY-RUN complete, nothing written.", flush=True)
        return results

    try:
        vol.commit()
        print("[REWRITE] vol.commit() OK", flush=True)
        results["vol_commit"] = "OK"
    except Exception as e:
        print(f"[REWRITE] vol.commit() FAILED: {e}", flush=True)
        results["vol_commit"] = f"FAILED: {e}"

    return results
