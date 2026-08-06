"""
justtcg_graded_probe.py — THROWAWAY probe: SKU -> JustTCG slug -> graded prices
================================================================================
Tests the proposed "check my graded slab" flow end-to-end against real
GrailSweep cards:
    user picks a card (SKU known) -> resolve SKU to a JustTCG v2 card_id
    (slug) via search -> fetch graded prices for that card_id.

This script is disposable. It does not import from or modify any other
GrailSweep module, does not touch Modal, does not write to CardsDB, and does
not deploy anything. It only reads local profile.json files under C:\\CardsDB
(the local mirror of the Modal volume's CardsDB — see CLAUDE.md) and makes
read-only GET requests to the JustTCG v2 API.

Usage:
    $env:JUSTTCG_API_KEY = "tcg_..."      # PowerShell
    python justtcg_graded_probe.py

Hard cap: 30 JustTCG requests total. The script aborts loudly if it would
exceed that, and still writes out whatever partial results it collected.

Output:
    - plain-text tables to console
    - full JSON dump at C:\\MatchIT\\justtcg_graded_probe_results.json
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import statistics
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CARDSDB_ROOT = Path(r"C:\CardsDB")
OUTPUT_JSON = Path(r"C:\MatchIT\justtcg_graded_probe_results.json")

JUSTTCG_BASE = "https://api.justtcg.com/v2"
REQUEST_CAP = 30

# TEST_SKUS category strings ("pokemon"/"mtg"/"yugioh") are used directly
# as the JustTCG `game` query param below. These are best-guess values, NOT
# verified against JustTCG docs — if a whole category comes back MISS on
# every card, a wrong game slug here is the first thing to suspect (that's
# part of what this probe exists to find out).

# 12 real SKUs pulled from CardsDB. Only the path is hardcoded — every
# field used below (name / set / rarity / number) is read live from each
# card's actual profile.json, not retyped here.
TEST_SKUS = [
    # 3x EN Pokemon WOTC-era holo
    ("pokemon", "base1-4"),    # Charizard, Base Set
    ("pokemon", "base1-2"),    # Blastoise, Base Set
    ("pokemon", "base1-15"),   # Venusaur, Base Set
    # 3x EN Pokemon modern SV-era chase
    ("pokemon", "sv3pt5-199"),  # Charizard ex, 151 (full art)
    ("pokemon", "sv4pt5-240"),  # Wo-Chien ex, Paldean Fates (rainbow)
    ("pokemon", "sv1-254"),     # Koraidon ex, Scarlet & Violet (rainbow)
    # 2x EN Pokemon SWSH-era alt art
    ("pokemon", "swsh7-215"),   # Umbreon VMAX, Evolving Skies (alt art)
    ("pokemon", "swsh12-202"),  # Lugia VSTAR, Silver Tempest (alt art)
    # 2x MTG (one Reserved List, one modern mythic)
    ("mtg", "mtg-lea-232"),     # Black Lotus, Limited Edition Alpha (Reserved List)
    ("mtg", "mtg-ltr-246"),     # The One Ring, LOTR: Tales of Middle-earth (modern mythic)
    # 2x Yu-Gi-Oh staple
    ("yugioh", "ygo-LOB-001-89631139"),  # Blue-Eyes White Dragon, LOB
    ("yugioh", "ygo-LOB-005-36996508"),  # Dark Magician, LOB
]


class RequestBudget:
    def __init__(self, cap: int = REQUEST_CAP):
        self.cap = cap
        self.count = 0
        self.log: list[str] = []

    def use(self, url: str) -> None:
        if self.count >= self.cap:
            raise RuntimeError(
                f"HARD CAP REACHED: {self.cap} JustTCG requests already used. "
                f"Aborting before issuing: {url}"
            )
        self.count += 1
        self.log.append(url)
        print(f"[REQUEST {self.count}/{self.cap}] {url}")


def load_api_key() -> str:
    # Same pattern matchit_modal.py uses to load this key in production
    # (scrape_pokemon_jpn.py itself takes api_key as a plain function arg —
    # the env-var loading lives in the caller).
    key = os.environ.get("JUSTTCG_API_KEY", "").strip()
    if not key:
        print("ERROR: JUSTTCG_API_KEY is not set in the environment.")
        print('  PowerShell:  $env:JUSTTCG_API_KEY = "tcg_..."')
        sys.exit(1)
    return key


def load_profile(sku: str, category: str) -> dict:
    if category == "pokemon":
        path = CARDSDB_ROOT / "pokemon" / sku / "profile.json"
    elif category == "mtg":
        path = CARDSDB_ROOT / "mtg" / sku / "profile.json"
    elif category == "yugioh":
        path = CARDSDB_ROOT / "yugioh" / sku / "profile.json"
    else:
        raise ValueError(f"unknown category {category}")
    if not path.exists():
        raise FileNotFoundError(f"profile.json not found for {sku} at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def rarity_keywords(raw: str) -> list[str]:
    if not raw:
        return []
    s = raw.upper()
    for suffix in ("_YGO", "_MTG"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return [w for w in s.replace("_", " ").strip().lower().split() if w]


def set_matches(our_set_name: str, their_set_name: str) -> bool:
    a, b = norm(our_set_name), norm(their_set_name)
    if not a or not b:
        return False
    return a in b or b in a


def search_card(card: dict, api_key: str, budget: RequestBudget) -> dict:
    """STEP 1 — resolve a card via JustTCG search. 1 request."""
    q = card["name"]
    game = card["_category"]
    url = f"{JUSTTCG_BASE}/cards?q={urllib.parse.quote(q)}&game={game}&limit=20"
    budget.use(url)
    resp = requests.get(url, headers={"x-api-key": api_key}, timeout=15)
    try:
        data = resp.json()
    except Exception:
        data = {"error": f"non-JSON response, status {resp.status_code}", "raw": resp.text[:500]}

    results = data.get("data", []) if isinstance(data, dict) else []

    # Every result observed, for the set-slug mapping table — using the
    # structured `set` object JustTCG returns rather than fragile string
    # splitting of the slug (splitting "pokemon-base-set-charizard-holo-rare"
    # into components is ambiguous; the API already hands us set.id/set.name).
    observed_sets = [
        {"justtcg_set_id": r.get("set", {}).get("id"), "justtcg_set_name": r.get("set", {}).get("name")}
        for r in results
    ]
    all_result_ids = [
        {"id": r.get("id"), "slug": r.get("slug"), "name": r.get("name"),
         "set_name": r.get("set", {}).get("name"), "rarity": r.get("rarity"), "number": r.get("number")}
        for r in results
    ]

    our_name_norm = norm(card["name"])
    plausible = [r for r in results if norm(r.get("name", "")) == our_name_norm
                 and set_matches(card["set_name"], r.get("set", {}).get("name", ""))]

    our_kw = set(rarity_keywords(card["rarity"]))

    if not results:
        classification, picked, rule = "MISS", None, "search returned zero results"
    elif not plausible:
        classification, picked, rule = "MISS", None, "no result matched both normalized name and set name"
    elif len(plausible) == 1:
        classification, picked, rule = "EXACT", plausible[0], "unique name+set match"
    else:
        def score(r):
            hay = f"{r.get('rarity','')} {r.get('slug','')}".lower()
            return sum(1 for k in our_kw if k and k != "rare" and k in hay)
        ranked = sorted(plausible, key=score, reverse=True)
        classification, picked = "AMBIGUOUS", ranked[0]
        rule = (f"{len(plausible)} results matched name+set; tiebroke by rarity-keyword "
                f"overlap (our rarity keywords={sorted(our_kw)})")

    rarity_in_slug = None
    if picked is not None:
        hay = (picked.get("slug") or "").lower()
        rarity_in_slug = any(k in hay for k in our_kw if k and k != "rare")

    return {
        "sku": card["_sku"],
        "query": q,
        "game_param": game,
        "num_results": len(results),
        "all_results": all_result_ids,
        "observed_sets": observed_sets,
        "num_plausible": len(plausible),
        "classification": classification,
        "picked": {
            "id": picked.get("id"), "slug": picked.get("slug"), "name": picked.get("name"),
            "set_name": picked.get("set", {}).get("name"), "rarity": picked.get("rarity"),
        } if picked else None,
        "rule": rule,
        "rarity_field_in_chosen_slug": rarity_in_slug,
    }


def fetch_graded(slug: str, api_key: str, budget: RequestBudget) -> dict:
    """STEP 2 — fetch graded-only prices for a resolved card_id. 1 request."""
    url = f"{JUSTTCG_BASE}/cards?card_id={urllib.parse.quote(slug)}&graded=only"
    budget.use(url)
    resp = requests.get(url, headers={"x-api-key": api_key}, timeout=15)
    try:
        return resp.json()
    except Exception:
        return {"error": f"non-JSON response, status {resp.status_code}", "raw": resp.text[:500]}


def parse_graded(raw: dict) -> dict:
    cards = raw.get("data", []) if isinstance(raw, dict) else []
    if not cards:
        return {"has_data": False, "companies": {}, "markets_shape_sample": None,
                "updated_ats": [], "raw_error": raw.get("error") if isinstance(raw, dict) else None}

    companies: dict[str, list[dict]] = {}
    updated_ats: list[int] = []
    markets_shape_sample = None

    for c in cards:
        for v in c.get("variants", []):
            grading = v.get("grading")
            if not grading:
                continue
            company = grading.get("company", "UNKNOWN")
            markets = v.get("markets") or []
            price = markets[0].get("price") if markets else None
            updated_at = markets[0].get("updated_at") if markets else None
            if updated_at:
                updated_ats.append(updated_at)
            if markets_shape_sample is None and markets:
                m = markets[0]
                markets_shape_sample = {
                    "region": m.get("region"), "currency": m.get("currency"),
                    "field_names": sorted(m.keys()),
                }
            companies.setdefault(company, []).append({
                "grade": grading.get("canonical"), "price": price, "updated_at": updated_at,
            })

    return {
        "has_data": bool(companies),
        "companies": companies,
        "markets_shape_sample": markets_shape_sample,
        "updated_ats": updated_ats,
    }


def age_days(unix_ts: int) -> float:
    now = datetime.now(timezone.utc).timestamp()
    return round((now - unix_ts) / 86400.0, 1)


def main():
    api_key = load_api_key()
    budget = RequestBudget(REQUEST_CAP)

    cards = []
    print("=" * 100)
    print("TEST SET — 12 SKUs, fields read live from CardsDB")
    print("=" * 100)
    for category, sku in TEST_SKUS:
        profile = load_profile(sku, category)
        card = {
            "_sku": sku,
            "_category": category,
            "name": profile.get("name"),
            "set_id": profile.get("set_id"),
            "set_name": profile.get("set_name"),
            "rarity": profile.get("rarity"),
            "card_number": profile.get("card_number"),
        }
        cards.append(card)
        print(f"{sku:<28} name={card['name']!r:<28} set={card['set_name']!r:<32} "
              f"rarity={card['rarity']:<20} number={card['card_number']}")

    resolutions = []
    graded_results = []

    try:
        print("\n" + "=" * 100)
        print("STEP 1 — RESOLUTION")
        print("=" * 100)
        for card in cards:
            res = search_card(card, api_key, budget)
            resolutions.append(res)
            print(f"\n[{res['sku']}] query={res['query']!r} game={res['game_param']} "
                  f"-> {res['num_results']} results, {res['num_plausible']} plausible "
                  f"=> {res['classification']}")
            print(f"  rule: {res['rule']}")
            if res["picked"]:
                print(f"  picked: {res['picked']['slug']}  "
                      f"(name={res['picked']['name']!r} set={res['picked']['set_name']!r} "
                      f"rarity={res['picked']['rarity']!r})")
                print(f"  our rarity field present in chosen slug: {res['rarity_field_in_chosen_slug']}")
            for r in res["all_results"]:
                print(f"    candidate: {r['slug']}  | set={r['set_name']!r} rarity={r['rarity']!r} number={r['number']!r}")

        print("\n" + "=" * 100)
        print("STEP 2 — GRADED FETCH")
        print("=" * 100)
        for card, res in zip(cards, resolutions):
            if res["picked"] is None:
                print(f"\n[{res['sku']}] skipped (MISS at resolution — no card_id to fetch)")
                graded_results.append({"sku": res["sku"], "skipped": True})
                continue
            slug = res["picked"]["slug"]
            raw = fetch_graded(slug, api_key, budget)
            parsed = parse_graded(raw)
            parsed["sku"] = res["sku"]
            parsed["category"] = card["_category"]
            parsed["slug"] = slug
            graded_results.append(parsed)

            print(f"\n[{res['sku']}] card_id={slug}")
            if not parsed["has_data"]:
                print("  no graded data returned")
                continue
            for company, grades in parsed["companies"].items():
                grade_strs = ", ".join(f"{g['grade']}=${g['price']}" for g in grades)
                print(f"  {company}: {grade_strs}")
            if parsed["updated_ats"]:
                ages = [age_days(t) for t in parsed["updated_ats"]]
                print(f"  updated_at age (days): newest={min(ages)} oldest={max(ages)}")
            if parsed["markets_shape_sample"]:
                print(f"  markets[0] shape: {parsed['markets_shape_sample']}")

    except RuntimeError as e:
        print(f"\n!!! ABORTED: {e}")
        print(f"!!! Requests used before abort: {budget.count}/{budget.cap}")

    # ── Summary ──────────────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)

    n = len(resolutions)
    exact = sum(1 for r in resolutions if r["classification"] == "EXACT")
    ambiguous = sum(1 for r in resolutions if r["classification"] == "AMBIGUOUS")
    miss = sum(1 for r in resolutions if r["classification"] == "MISS")
    print(f"Resolution: EXACT {exact}/{n}, AMBIGUOUS {ambiguous}/{n}, MISS {miss}/{n}")

    any_graded = sum(1 for g in graded_results if g.get("has_data"))
    print(f"Cards with ANY graded data: {any_graded}/{n}")

    has_psa = sum(1 for g in graded_results if "PSA" in g.get("companies", {}))
    print(f"Cards with PSA data specifically: {has_psa}/{n}")

    # canonical grade strings look like "PSA 9" / "PSA 10"
    has_psa_9_10 = sum(
        1 for g in graded_results
        if {"PSA 9", "PSA 10"}.issubset({gr["grade"] for gr in g.get("companies", {}).get("PSA", [])})
    )
    print(f"Cards with PSA 9 AND PSA 10: {has_psa_9_10}/{n}")

    print("\nGraded coverage by game:")
    for cat in ("pokemon", "mtg", "yugioh"):
        cat_cards = [g for g in graded_results if g.get("category") == cat]
        cat_with_data = sum(1 for g in cat_cards if g.get("has_data"))
        print(f"  {cat}: {cat_with_data}/{len(cat_cards)}")

    all_companies = set()
    for g in graded_results:
        all_companies.update(g.get("companies", {}).keys())
    print(f"\nDistinct grading companies seen: {sorted(all_companies)}")

    all_ages = []
    for g in graded_results:
        all_ages.extend(age_days(t) for t in g.get("updated_ats", []))
    if all_ages:
        print(f"Median age of graded price data: {statistics.median(all_ages)} days "
              f"(n={len(all_ages)} price points)")
    else:
        print("Median age of graded price data: n/a (no graded price points)")

    print("\nSet-slug mapping table (our set_id -> observed JustTCG set.id / set.name):")
    seen_pairs = set()
    for card, res in zip(cards, resolutions):
        for s in res["observed_sets"]:
            key = (card["set_id"], s["justtcg_set_id"], s["justtcg_set_name"])
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            print(f"  {card['set_id']:<14} -> {s['justtcg_set_id']!r:<32} ({s['justtcg_set_name']})")

    print(f"\nTotal requests used: {budget.count}/{budget.cap}")

    # ── Write full JSON ──────────────────────────────────────────────────
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "requests_used": budget.count,
        "request_cap": budget.cap,
        "request_log": budget.log,
        "test_cards": cards,
        "resolutions": resolutions,
        "graded_results": graded_results,
        "summary": {
            "resolution": {"EXACT": exact, "AMBIGUOUS": ambiguous, "MISS": miss, "total": n},
            "any_graded_data": any_graded,
            "psa_data": has_psa,
            "psa_9_and_10": has_psa_9_10,
            "distinct_grading_companies": sorted(all_companies),
            "median_age_days": statistics.median(all_ages) if all_ages else None,
        },
    }
    OUTPUT_JSON.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nFull JSON written to {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
