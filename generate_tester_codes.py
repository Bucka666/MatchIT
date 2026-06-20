"""
Generate 15 GRAIL-XXXX-XXXX tester codes for Phase 8 closed beta.

Usage:
    python generate_tester_codes.py

Reads:  C:/Users/c_a_b/grailsweep-backups/subscriptions_20260527_173040.json
Writes: subscriptions_with_testers.json  (to be uploaded to Modal)
        tester_codes.csv                  (for Craig to send via WhatsApp DMs)
"""
import csv
import json
import secrets
from datetime import datetime, timedelta
from pathlib import Path

SOURCE = Path("C:/Users/c_a_b/grailsweep-backups/subscriptions_20260527_173040.json")
OUTPUT_JSON = Path("subscriptions_with_testers.json")
OUTPUT_CSV = Path("tester_codes.csv")

NUM_CODES = 15
TODAY = datetime.utcnow()
EXPIRY = (TODAY + timedelta(days=365)).isoformat()
CREATED = TODAY.isoformat()


def gen_code(existing: set) -> str:
    for _ in range(1000):
        part1 = secrets.token_hex(2).upper()
        part2 = secrets.token_hex(2).upper()
        code = f"GRAIL-{part1}-{part2}"
        if code not in existing:
            return code
    raise RuntimeError("Could not generate unique code after 1000 attempts")


def main():
    with open(SOURCE, encoding="utf-8") as f:
        subs = json.load(f)

    original_count = len(subs)
    existing_codes = set(subs.keys())
    print(f"Loaded {original_count} existing entries from {SOURCE.name}")

    new_entries = []
    for i in range(NUM_CODES):
        code = gen_code(existing_codes)
        existing_codes.add(code)
        entry = {
            "email": "",
            "tier": "annual",
            "stripe_subscription_id": None,
            "created_at": CREATED,
            "expires_at": EXPIRY,
            "status": "active",
            "devices": [],
            "device_fingerprint_limit": 3,
            "is_tester": True,
            "is_reviewer": False,
            "source": "closed_beta_tester",
            "notes": f"Phase 8 closed beta tester — generated {TODAY.strftime('%Y-%m-%d')}",
        }
        subs[code] = entry
        new_entries.append((code, EXPIRY[:10]))
        print(f"  [{i+1:02d}] {code}  expires {EXPIRY[:10]}")

    final_count = len(subs)
    assert final_count == original_count + NUM_CODES, (
        f"Entry count mismatch: expected {original_count + NUM_CODES}, got {final_count}"
    )

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(subs, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {OUTPUT_JSON} ({final_count} total entries, +{NUM_CODES} new)")

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Name", "Code", "Email", "Tier", "Expiry", "Status"])
        for code, expiry in new_entries:
            writer.writerow(["", code, "", "annual", expiry, ""])
    print(f"Wrote {OUTPUT_CSV} ({NUM_CODES} rows)")


if __name__ == "__main__":
    main()
