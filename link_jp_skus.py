"""
Links jp_cardid_maps/{setcode}_map.json cardID->collector_number mappings to
existing SKUs in identifier_lookup.json, producing a cardID image -> SKU map.

Usage:
    python link_jp_skus.py                # run on all sets found in jp_cardid_maps/
    python link_jp_skus.py --sets s12a     # run on specific set(s) only
"""
import argparse
import json
import os

BASE_DIR = r"C:\MatchIT"
MAPS_DIR = os.path.join(BASE_DIR, "jp_cardid_maps")
IDENTIFIER_LOOKUP_PATH = os.path.join(BASE_DIR, "identifier_lookup.json")
CARDSDB_DIR = r"C:\CardsDB"
OUTPUT_LINKS_PATH = os.path.join(BASE_DIR, "jp_sku_links.json")
OUTPUT_REPORT_PATH = os.path.join(BASE_DIR, "jp_sku_links_report.txt")

UNMATCHED_THRESHOLD = 0.80


def discover_set_codes():
    codes = []
    for fname in sorted(os.listdir(MAPS_DIR)):
        if fname.endswith("_map.json"):
            codes.append(fname[: -len("_map.json")])
    return codes


def load_identifier_lookup():
    with open(IDENTIFIER_LOOKUP_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("pokemon", {})


def make_sku(setcode: str, number) -> str:
    return f"jpn-{setcode}-{str(number).zfill(3)}"


def sku_exists(setcode: str, number, pokemon_lookup: dict) -> bool:
    return make_sku(setcode, number) in pokemon_lookup


def link_set(setcode, pokemon_lookup):
    map_path = os.path.join(MAPS_DIR, f"{setcode}_map.json")
    with open(map_path, encoding="utf-8") as f:
        card_map = json.load(f)

    # "failed" (list of OCR-failure cardIDs) and "disagreements" (dict of
    # cardID -> per-engine OCR readings) are metadata keys, not cardID entries.
    META_KEYS = ("failed", "disagreements")
    card_entries = {k: v for k, v in card_map.items() if k not in META_KEYS}

    stats = {
        "total": len(card_entries),
        "linked": 0,
        "unmatched": 0,
        "skipped": 0,
        "collisions": 0,
    }
    linked = {}
    unmatched_skus = []

    # Sorted so collisions resolve deterministically (lowest cardID wins).
    for card_id, number in sorted(card_entries.items()):
        if number is None:
            stats["skipped"] += 1
            continue

        sku = make_sku(setcode, number)

        if sku_exists(setcode, number, pokemon_lookup):
            if sku in linked:
                stats["collisions"] += 1
                continue
            image_path = os.path.join(CARDSDB_DIR, f"jpn-{setcode}", f"{card_id}.jpg")
            linked[sku] = image_path.replace("\\", "/")
            stats["linked"] += 1
        else:
            stats["unmatched"] += 1
            unmatched_skus.append(sku)

    return linked, stats, unmatched_skus


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sets", nargs="+", default=None, help="Specific set codes to process")
    args = parser.parse_args()

    setcodes = args.sets if args.sets else discover_set_codes()
    pokemon_lookup = load_identifier_lookup()

    all_links = {}
    per_set_stats = {}
    low_match_sets = []

    for setcode in setcodes:
        linked, stats, unmatched_skus = link_set(setcode, pokemon_lookup)
        all_links.update(linked)
        per_set_stats[setcode] = stats

        mapped_total = stats["total"] - stats["skipped"]
        # "found a matching SKU" includes collisions (SKU existed, a duplicate
        # image just lost the tie-break) -- collisions aren't a naming mismatch.
        found_sku = stats["linked"] + stats["collisions"]
        match_rate = (found_sku / mapped_total) if mapped_total else 0.0
        if match_rate < UNMATCHED_THRESHOLD:
            low_match_sets.append((setcode, match_rate))

    with open(OUTPUT_LINKS_PATH, "w", encoding="utf-8") as f:
        json.dump(all_links, f, indent=2, ensure_ascii=False, sort_keys=True)

    grand_total = {"total": 0, "linked": 0, "unmatched": 0, "skipped": 0, "collisions": 0}
    lines = []
    lines.append("JP SKU Linking Report")
    lines.append("=" * 60)
    lines.append("")
    lines.append("Per-set breakdown:")
    lines.append("-" * 60)
    for setcode in setcodes:
        stats = per_set_stats[setcode]
        for k in grand_total:
            grand_total[k] += stats[k]
        lines.append(f"{setcode}:")
        lines.append(f"  Total cardID->number mappings: {stats['total']}")
        lines.append(f"  Linked (SKU found):            {stats['linked']}")
        lines.append(f"  Unmatched (SKU not in lookup):  {stats['unmatched']}")
        lines.append(f"  Skipped (None in map):          {stats['skipped']}")
        if stats["collisions"]:
            lines.append(f"  SKU collisions (dup number, extra image dropped): {stats['collisions']}")
        lines.append("")

    lines.append("=" * 60)
    lines.append("Grand totals across all sets:")
    lines.append(f"  Total cardID->number mappings: {grand_total['total']}")
    lines.append(f"  Linked (SKU found):            {grand_total['linked']}")
    lines.append(f"  Unmatched (SKU not in lookup):  {grand_total['unmatched']}")
    lines.append(f"  Skipped (None in map):          {grand_total['skipped']}")
    lines.append(f"  SKU collisions (dup number, extra image dropped): {grand_total['collisions']}")
    lines.append("")

    lines.append("=" * 60)
    lines.append(f"Sets below {int(UNMATCHED_THRESHOLD * 100)}% match rate (possible naming mismatch):")
    if low_match_sets:
        for setcode, rate in low_match_sets:
            lines.append(f"  {setcode}: {rate * 100:.1f}% matched")
    else:
        lines.append("  (none)")

    report_text = "\n".join(lines)
    with open(OUTPUT_REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report_text)

    print(report_text)
    print()
    print(f"Confirmed SKU->image links written: {len(all_links)}")


if __name__ == "__main__":
    main()
