"""Pre-deploy regression test for the profile loader + sync pipeline.

Run before every deploy:
    modal run regression_tests.py::test_profile_pipeline

Covers the bug class fixed by Strategy A2 + Strategy C:
scheduler-added sets returning blank profiles.

Template note — three issues corrected from original spec:
  1. modal_config has no 'app' — standalone modal.App used instead.
  2. Test 5 HTTP endpoint replaced with direct _load_card_profile_for_sku
     calls: no valid API key is available to the test function, and importing
     the full Flask app pulls in CLIP/DINOv2/PaddleOCR (60-90s cold start).
     The direct loader test is more targeted and proves Strategy A2+C.
  3. get_db_root() / get_data_dir() depend on load_vertical() having been
     called — which only 'serve()' does. Paths are hardcoded for Modal.
"""

import sys
sys.path.insert(0, "/app")

import modal
from modal_config import vol, image

modal_app = modal.App("grailsweep-regression")

# Modal volume paths — hardcoded because get_db_root() / get_data_dir()
# require load_vertical() to have been called first (only serve() does that).
_CARDSDB_ROOT = "/modal_data/CardsDB"
_SCRAPE_ROOT  = "/modal_data/MatchITv2_ProductMatch_Data/cards"


@modal_app.function(
    image=image,
    volumes={"/modal_data": vol},
    timeout=300,
)
def _run_test_suite() -> dict:
    import json
    import os
    from pathlib import Path
    from profile_utils import _load_card_profile_for_sku
    from sync_profiles import sync_profiles_to_cardsdb

    os.environ["LOCALAPPDATA"] = "/modal_data"

    db_root     = _CARDSDB_ROOT
    scrape_root = _SCRAPE_ROOT

    results = {"passed": 0, "failed": 0, "tests": []}

    def record(name, condition, detail=""):
        status = "PASS" if condition else "FAIL"
        results["tests"].append({"name": name, "status": status, "detail": detail})
        if condition:
            results["passed"] += 1
        else:
            results["failed"] += 1
        print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""), flush=True)

    print("\n=== Profile pipeline regression suite ===\n", flush=True)

    # ── Test group 1: CardsDB primary path (scheduler-synced sets) ──────────
    print("Test group 1: CardsDB primary path (scheduler-synced sets)", flush=True)
    for sku in ["me4-1", "me4-10", "me4-100"]:
        path = Path(f"{db_root}/pokemon/{sku}/profile.json")
        if not path.exists():
            record(f"{sku}: CardsDB file exists", False, f"missing: {path}")
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                prof = json.load(f)
            has_name   = bool(prof.get("name"))
            has_set    = bool(prof.get("set_name"))
            has_prices = bool(prof.get("prices"))
            record(
                f"{sku}: profile populated",
                has_name and has_set and has_prices,
                f"name={prof.get('name')!r} set={prof.get('set_name')!r} "
                f"prices={list(prof.get('prices', {}).keys())}",
            )
        except Exception as e:
            record(f"{sku}: profile populated", False, f"load error: {e}")

    # ── Test group 2: Pre-existing CardsDB cards (regression guard) ─────────
    print("\nTest group 2: Pre-existing CardsDB cards (regression guard)", flush=True)
    for sku in ["base1-1", "mtg-hob-110"]:
        prof = _load_card_profile_for_sku(sku, db_root, scrape_root)
        record(
            f"{sku}: loader returns populated profile",
            bool(prof.get("name") and prof.get("prices")),
            f"name={prof.get('name')!r}",
        )

    # ── Test group 3: Helper safety (missing SKU) ────────────────────────────
    print("\nTest group 3: Helper safety", flush=True)
    try:
        prof = _load_card_profile_for_sku(
            "definitely-not-a-real-sku-xyz123", db_root, scrape_root
        )
        record(
            "missing SKU returns {} without raising",
            prof == {},
            f"got: {prof!r}",
        )
    except Exception as e:
        record("missing SKU returns {} without raising", False, f"raised: {e}")

    # ── Test group 4: Sync function safety (fake set code is a no-op) ────────
    print("\nTest group 4: Sync function safety", flush=True)
    try:
        res = sync_profiles_to_cardsdb({"pokemon": ["fake-set-xyz-doesnt-exist"]})
        record(
            "sync with fake set is no-op",
            res["copied"] == 0 and res["updated"] == 0 and not res["errors"],
            f"got: {res}",
        )
    except Exception as e:
        record("sync with fake set is no-op", False, f"raised: {e}")

    # ── Test group 5: End-to-end profile loader for Strategy A2+C ───────────
    # Exercises the full chain: scrape → sync → CardsDB → loader → populated data.
    # (Replaces the HTTP /api/v1/match test from the original spec — see module
    # docstring for why.)
    print("\nTest group 5: Profile loader end-to-end for me4 (Strategy A2+C)", flush=True)
    for sku in ["me4-1", "me4-10", "me4-100"]:
        prof = _load_card_profile_for_sku(sku, db_root, scrape_root)
        record(
            f"{sku}: loader returns populated profile (Strategy A2+C)",
            bool(prof.get("name") and prof.get("prices")),
            f"name={prof.get('name')!r}",
        )

    # ── Summary ──────────────────────────────────────────────────────────────
    total = results["passed"] + results["failed"]
    print(f"\n=== Summary: {results['passed']}/{total} passed ===", flush=True)
    if results["failed"]:
        print("FAILED tests:", flush=True)
        for t in results["tests"]:
            if t["status"] == "FAIL":
                print(f"  - {t['name']}: {t['detail']}", flush=True)

    return results


@modal_app.local_entrypoint()
def test_profile_pipeline():
    """Run the regression suite against the live Modal volume.

    Exits non-zero if any test fails, so it can be wired into CI or a
    pre-deploy script.
    """
    import json
    results = _run_test_suite.remote()
    print(json.dumps(results, indent=2))
    if results["failed"] > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    print("Run via: modal run regression_tests.py::test_profile_pipeline")
