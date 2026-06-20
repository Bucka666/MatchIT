# C:\MatchIT\backfill_hobfra_profiles.py
import modal

app = modal.App("backfill-hobfra-profiles")
image = modal.Image.debian_slim().pip_install("requests")
vol = modal.Volume.from_name("matchit-data-v2")

@app.local_entrypoint()
def main():
    run.remote()

@app.function(image=image, volumes={"/data": vol}, timeout=900)
def run():
    import os, json, time, requests

    MTG_ROOT = "/data/CardsDB/mtg"

    # Scryfall set codes for our SKU prefixes
    # hob = Tales of Middle-earth: Holiday Release (set code "hob")
    # fra = ??? — need to confirm. Common candidates: "fdn-art", custom backfill. Let's just hit Scryfall.

    # Step 1: list every hob/fra SKU that has an embedding (we got 39 + 7 = 46 earlier)
    # We'll discover them by listing what was scraped. But since CardsDB has nothing,
    # we work backwards from a known range and let Scryfall tell us what exists.

    # Best approach: query Scryfall directly for set=hob and set=fra, then write each.
    def fetch_set(set_code):
        cards = []
        url = f"https://api.scryfall.com/cards/search?q=set:{set_code}&unique=prints"
        while url:
            r = requests.get(url, timeout=15)
            if r.status_code != 200:
                print(f"  ! Scryfall {set_code} returned {r.status_code}: {r.text[:200]}")
                break
            data = r.json()
            cards.extend(data.get("data", []))
            url = data.get("next_page") if data.get("has_more") else None
            time.sleep(0.1)  # Scryfall rate limit politeness
        return cards

    def map_color(colors_list):
        if not colors_list:
            return "COLORLESS"
        if len(colors_list) > 1:
            return "MULTICOLOR"
        return {"W": "WHITE", "U": "BLUE", "B": "BLACK", "R": "RED", "G": "GREEN"}.get(colors_list[0], "COLORLESS")

    def map_type(type_line):
        if not type_line:
            return "UNKNOWN"
        t = type_line.upper()
        for kw in ["CREATURE", "INSTANT", "SORCERY", "ENCHANTMENT", "ARTIFACT", "PLANESWALKER", "LAND"]:
            if kw in t:
                return kw
        return "UNKNOWN"

    def to_profile(c, set_code):
        sku = f"mtg-{set_code}-{c.get('collector_number')}"
        prices_raw = c.get("prices", {}) or {}
        prices = {}
        if prices_raw.get("usd"):
            prices.setdefault("tcgplayer", {})["normal"] = {"market": float(prices_raw["usd"])}
        if prices_raw.get("usd_foil"):
            prices.setdefault("tcgplayer", {})["foil"] = {"market": float(prices_raw["usd_foil"])}
        if prices_raw.get("eur"):
            prices.setdefault("cardmarket", {})["normal"] = {"trend": float(prices_raw["eur"])}
        if prices_raw.get("eur_foil"):
            prices.setdefault("cardmarket", {})["foil"] = {"trend": float(prices_raw["eur_foil"])}

        return sku, {
            "api_id": c.get("id"),
            "name": c.get("name"),
            "card_number": c.get("collector_number"),
            "set_id": set_code,
            "set_name": c.get("set_name"),
            "category": "MTG",
            "rarity": (c.get("rarity") or "").upper(),
            "mtg_color": map_color(c.get("colors", [])),
            "mtg_card_type": map_type(c.get("type_line", "")),
            "set_era": "MODERN_MTG",
            "language": "EN",
            "edition": "",
            "scryfall_id": c.get("id"),
            "scryfall_url": c.get("scryfall_uri"),
            "tcgplayer_id": c.get("tcgplayer_id"),
            "cardmarket_id": c.get("cardmarket_id"),
            "prices": prices,
            "tcgplayer_url": c.get("purchase_uris", {}).get("tcgplayer") if c.get("purchase_uris") else None,
            "cardmarket_url": c.get("purchase_uris", {}).get("cardmarket") if c.get("purchase_uris") else None,
            "artist": c.get("artist"),
            "type_line": c.get("type_line"),
            "mana_cost": c.get("mana_cost"),
            "oracle_text": c.get("oracle_text"),
            "power": c.get("power"),
            "toughness": c.get("toughness"),
            "cmc": c.get("cmc"),
        }

    total_written = 0
    for set_code in ("hob", "fra"):
        print(f"\n=== Fetching set '{set_code}' from Scryfall ===")
        cards = fetch_set(set_code)
        print(f"  Got {len(cards)} cards")
        if not cards:
            continue

        for c in cards:
            sku, profile = to_profile(c, set_code)
            card_dir = f"{MTG_ROOT}/{sku}"
            os.makedirs(card_dir, exist_ok=True)
            with open(f"{card_dir}/profile.json", "w") as f:
                json.dump(profile, f, indent=2, ensure_ascii=False)
            total_written += 1
            print(f"  ✓ {sku}: {profile['name']}")

    print(f"\n=== Done: wrote {total_written} profile.json files ===")
    vol.commit()
    print("Volume committed.")