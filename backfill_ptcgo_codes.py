"""
backfill_ptcgo_codes.py — Standalone one-shot ptcgoCode backfill
==================================================================
Modal function that backfills the 'ptcgoCode' field onto POKEMON
entries in /modal_data/set_metadata.json on the matchit-data-v2
volume, sourced from a single pokemontcg.io API call.

This is intentionally standalone and does NOT touch
rebuild_lookup_files() in matchit_modal.py. No CardsDB access, no
folder scan — set_metadata.json in, set_metadata.json out.

Run:
    modal run backfill_ptcgo_codes.py
"""
import copy
import sys

sys.path.insert(0, "/app")
from modal_config import vol, image

import modal

app = modal.App("matchit-ptcgo-backfill")


@app.function(
    image=image,
    volumes={"/modal_data": vol},
    timeout=300,
)
def backfill_ptcgo_codes():
    import json
    import os
    import urllib.request

    meta_path = "/modal_data/set_metadata.json"
    LOG = "[PTCGO-BACKFILL]"

    # ── 1. Load set_metadata.json ──────────────────────────────────────
    with open(meta_path, "r", encoding="utf-8") as f:
        old_meta = json.load(f)
    print(f"{LOG} Loaded set_metadata.json: {len(old_meta)} entries", flush=True)

    # ── 2. Fetch ptcgoCode from pokemontcg.io — one endpoint, up to 3 tries ──
    import time

    req = urllib.request.Request(
        "https://api.pokemontcg.io/v2/sets?select=id,ptcgoCode",
        headers={
            "User-Agent": "GrailSweep/1.0 contact@grailsweep.com",
            "Accept": "application/json",
        },
    )
    data = None
    for attempt in range(1, 4):
        print(f"{LOG} fetch attempt {attempt}/3", flush=True)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            break
        except Exception as e:
            print(f"{LOG} fetch attempt {attempt}/3 FAILED ({e})", flush=True)
            if attempt < 3:
                time.sleep(2)
    if data is None:
        print(f"{LOG} API fetch FAILED after 3 attempts — aborting WITHOUT writing", flush=True)
        return

    api_sets = data.get("data") if isinstance(data, dict) else None
    if not api_sets:
        print(f"{LOG} API fetch returned no data — aborting WITHOUT writing", flush=True)
        return

    # ── Pagination guard — a partial page would write None over sets that
    # legitimately have codes, so a short read must abort, not partially apply.
    total_count = data.get("totalCount") if isinstance(data, dict) else None
    print(f"{LOG} API returned {len(api_sets)} of {total_count} sets", flush=True)
    if total_count is None or len(api_sets) != total_count:
        print(f"{LOG} ABORT: partial/paginated response ({len(api_sets)} != {total_count}) "
              f"— not writing", flush=True)
        return

    # ── 3. Build id -> ptcgoCode lookup ─────────────────────────────────
    id_to_ptcgo = {}
    for s in api_sets:
        sid = s.get("id", "")
        if sid:
            id_to_ptcgo[sid.lower()] = s.get("ptcgoCode")
    print(f"{LOG} Fetched {len(id_to_ptcgo)} set codes from pokemontcg.io", flush=True)

    # ── 4. Apply ptcgoCode to POKEMON entries only ──────────────────────
    new_meta = copy.deepcopy(old_meta)
    updated = 0
    left_none = 0
    for set_id, entry in new_meta.items():
        if not isinstance(entry, dict) or entry.get("game") != "POKEMON":
            continue
        if set_id.startswith("jpn-"):
            entry["ptcgoCode"] = None
            left_none += 1
            continue
        code = id_to_ptcgo.get(set_id.lower())
        entry["ptcgoCode"] = code
        if code is None:
            left_none += 1
        else:
            updated += 1

    # ── 6. Assert the ONLY change on any entry is the 'ptcgoCode' key ──
    for set_id, old_entry in old_meta.items():
        new_entry = new_meta.get(set_id)
        if not isinstance(old_entry, dict) or not isinstance(new_entry, dict):
            if old_entry != new_entry:
                print(f"{LOG} ABORT: entry {set_id!r} changed shape — not writing", flush=True)
                return
            continue

        old_keys = set(old_entry.keys())
        new_keys = set(new_entry.keys())
        removed_keys = old_keys - new_keys
        added_keys = new_keys - old_keys
        if removed_keys:
            print(f"{LOG} ABORT: entry {set_id!r} lost key(s) {sorted(removed_keys)} — not writing", flush=True)
            return
        if added_keys - {"ptcgoCode"}:
            print(f"{LOG} ABORT: entry {set_id!r} gained unexpected key(s) "
                  f"{sorted(added_keys - {'ptcgoCode'})} — not writing", flush=True)
            return

        changed_values = [k for k in old_keys if old_entry[k] != new_entry[k]]
        bad_changes = [k for k in changed_values if k != "ptcgoCode"]
        if bad_changes:
            print(f"{LOG} ABORT: entry {set_id!r} changed field(s) other than "
                  f"ptcgoCode: {bad_changes} — not writing", flush=True)
            return

    # ── 7. Assert entry count is unchanged ──────────────────────────────
    if set(new_meta.keys()) != set(old_meta.keys()) or len(new_meta) != len(old_meta):
        print(f"{LOG} ABORT: entry count/keys changed ({len(old_meta)} -> {len(new_meta)}) "
              f"— not writing", flush=True)
        return

    print(f"{LOG} Assertions passed: only 'ptcgoCode' differs, "
          f"entry count unchanged at {len(new_meta)}", flush=True)
    print(f"{LOG} {updated} entries updated with a code, {left_none} entries left None", flush=True)

    # ── Verification output — must pass BEFORE the write ─────────────────

    # (a) me4 sanity check — the single fact this whole exercise exists to
    # establish. If this doesn't hold, nothing else here can be trusted.
    me4_entry = new_meta.get("me4")
    me4_code = me4_entry.get("ptcgoCode") if isinstance(me4_entry, dict) else None
    print(f"{LOG} me4 -> ptcgoCode={me4_code!r}", flush=True)
    if me4_code != "CRI":
        print(f"{LOG} ABORT: me4 resolved to {me4_code!r}, expected 'CRI' — not writing", flush=True)
        return

    # (b) Collision report across EN (non-jpn) POKEMON entries — the
    # collision surface Stage 2 (denominator disambiguation) has to handle.
    en_entries = {
        sid: e for sid, e in new_meta.items()
        if isinstance(e, dict) and e.get("game") == "POKEMON" and not sid.startswith("jpn-")
    }
    code_to_sids = {}
    for sid, e in en_entries.items():
        code = e.get("ptcgoCode")
        if code:
            code_to_sids.setdefault(code, []).append(sid)
    collisions = {code: sids for code, sids in code_to_sids.items() if len(sids) > 1}
    print(f"{LOG} Collision report: {len(collisions)} code(s) shared by more than one set", flush=True)
    for code in sorted(collisions):
        sids = collisions[code]
        parts = ", ".join(f"{sid} ({en_entries[sid].get('printed_total')})" for sid in sids)
        print(f"{LOG}   {code} -> {parts}", flush=True)
        for i in range(len(sids)):
            for j in range(i + 1, len(sids)):
                ti = en_entries[sids[i]].get("printed_total")
                tj = en_entries[sids[j]].get("printed_total")
                if ti is not None and ti == tj:
                    print(
                        f"{LOG}   UNRESOLVABLE BY DENOMINATOR: {code} — "
                        f"{sids[i]} and {sids[j]} share printed_total={ti}",
                        flush=True,
                    )

    # (c) EN coded vs None counts.
    en_coded = [sid for sid, e in en_entries.items() if e.get("ptcgoCode")]
    en_none = sorted(sid for sid, e in en_entries.items() if not e.get("ptcgoCode"))
    print(f"{LOG} EN entries with a code: {len(en_coded)}", flush=True)
    print(f"{LOG} EN entries left None ({len(en_none)}): {en_none}", flush=True)

    # ── 8. Write back + commit ──────────────────────────────────────────
    tmp_path = meta_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(new_meta, f)
    os.replace(tmp_path, meta_path)
    vol.commit()
    print(f"{LOG} Wrote set_metadata.json and committed volume.", flush=True)


@app.local_entrypoint()
def main():
    backfill_ptcgo_codes.remote()
