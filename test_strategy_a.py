"""
test_strategy_a.py — Live verification of Strategy A fallback fix.

Tests three scan cases against the deployed /match endpoint:
  Test 1: me4 card (scheduler-added, should hit PROFILE-FALLBACK)
  Test 2: hob MTG card (CardsDB backfill, should NOT hit fallback)
  Test 3: base1-4 Pokemon (original 135K, should NOT hit fallback)

Runs as a Modal function so it can download images from the
volume's image_db directly, bypassing the need for real card photos.
"""
import modal
import sys

app = modal.App("test-strategy-a")
vol  = modal.Volume.from_name("matchit-data-v2")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("requests")
)

API_URL = "https://c-a-buckley--matchit-api-serve.modal.run/match"
API_KEY = "gs_live_YPlAHIiZ-HeVtFXGq9TZNclTIfxAv2O0fL6ASB9eJZQ"


@app.function(image=image, volumes={"/modal_data": vol}, timeout=120)
def run_tests():
    import os, requests, sqlite3, json
    from pathlib import Path

    DB_PATH   = "/modal_data/MatchITv2_ProductMatch_Data/cards/images.db"
    IMG_DIR   = "/modal_data/MatchITv2_ProductMatch_Data/cards/image_db"

    def get_jpeg_for_sku(sku: str):
        """Return path to the JPEG thumbnail for a SKU from images.db."""
        conn = sqlite3.connect(DB_PATH)
        row  = conn.execute(
            "SELECT path FROM images WHERE sku = ? AND original_filename LIKE '%FRONT%' LIMIT 1",
            (sku,)
        ).fetchone()
        conn.close()
        if row and os.path.exists(row[0]):
            return row[0]
        # fallback: any row
        conn = sqlite3.connect(DB_PATH)
        row  = conn.execute("SELECT path FROM images WHERE sku = ? LIMIT 1", (sku,)).fetchone()
        conn.close()
        return row[0] if row and os.path.exists(row[0]) else None

    def post_scan(label: str, sku: str):
        print(f"\n{'='*60}")
        print(f"TEST: {label}  (SKU: {sku})")
        print(f"{'='*60}")

        jpeg_path = get_jpeg_for_sku(sku)
        if not jpeg_path:
            print(f"  ERROR: no JPEG found in image_db for {sku}")
            return

        print(f"  Image path: {jpeg_path}")
        with open(jpeg_path, "rb") as f:
            img_bytes = f.read()

        resp = requests.post(
            API_URL,
            files={"image": ("card.jpg", img_bytes, "image/jpeg")},
            allow_redirects=True,
            timeout=60,
        )
        print(f"  HTTP status: {resp.status_code}")

        body = resp.text
        # Extract key profile fields from rendered HTML
        def find_text(marker_start, marker_end, text):
            try:
                s = text.index(marker_start) + len(marker_start)
                e = text.index(marker_end, s)
                return text[s:e].strip()
            except ValueError:
                return None

        # The results page renders profile.name in res-card-name divs
        # and set_name in res-card-set divs
        import re
        names    = re.findall(r'class="res-card-name">(.*?)</div>', body)
        sets     = re.findall(r'class="res-card-set">(.*?)</div>', body)
        prices   = re.findall(r'class="res-price-tag">(.*?)</span>', body)
        ia_name  = re.findall(r'class="res-ia-name">(.*?)</div>', body)
        ia_set   = re.findall(r'class="res-ia-set">(.*?)</div>', body)

        print(f"  Instant-answer name:  {ia_name[:1]}")
        print(f"  Instant-answer set:   {ia_set[:1]}")
        print(f"  Card names (top-5):   {names[:3]}")
        print(f"  Card sets  (top-5):   {sets[:3]}")
        print(f"  Prices found:         {prices[:3]}")

        if not names and not ia_name:
            print("  !! PROFILE MISSING — no card name in response HTML")
        elif "Chaos Rising" in body or "Weedle" in body or "Ho-Oh" in body:
            print("  OK — me4 set_name / card name present in response")
        elif names or ia_name:
            print("  OK — profile name present")

    # ── Test 1: me4 scheduler-added Pokémon (fix target) ──
    # Find any me4 SKU that has an embedding in images.db
    conn = sqlite3.connect(DB_PATH)
    me4_row = conn.execute(
        "SELECT sku FROM images WHERE sku LIKE 'me4-%' AND embedding IS NOT NULL LIMIT 1"
    ).fetchone()
    conn.close()
    me4_sku = me4_row[0] if me4_row else "me4-1"
    post_scan("Test 1 — me4 (Chaos Rising, scheduler-added)", me4_sku)

    # ── Test 2: hob MTG (CardsDB backfill) ──
    conn = sqlite3.connect(DB_PATH)
    hob_row = conn.execute(
        "SELECT sku FROM images WHERE sku LIKE 'mtg-hob-%' AND embedding IS NOT NULL LIMIT 1"
    ).fetchone()
    conn.close()
    hob_sku = hob_row[0] if hob_row else "mtg-hob-29"
    post_scan("Test 2 — hob MTG (CardsDB backfill, primary path)", hob_sku)

    # ── Test 3: base1 original Pokémon ──
    conn = sqlite3.connect(DB_PATH)
    b1_row = conn.execute(
        "SELECT sku FROM images WHERE sku LIKE 'base1-%' AND embedding IS NOT NULL LIMIT 1"
    ).fetchone()
    conn.close()
    b1_sku = b1_row[0] if b1_row else "base1-4"
    post_scan("Test 3 — base1 original Pokémon (primary path)", b1_sku)

    print(f"\n{'='*60}")
    print("All tests complete. Check Modal logs for [PROFILE-FALLBACK] entries.")


@app.local_entrypoint()
def main():
    run_tests.remote()
