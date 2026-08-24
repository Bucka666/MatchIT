"""
jp_manual_link_queue.py — Queue + linking logic for the manual JP card-linking
tool (jp_manual_link_tool.py).

Aggregates the OCR-failed cardIDs recorded in jp_cardid_maps/*_map.json's
"failed" lists, and resolves a human-typed collector number to a SKU using the
exact same functions the automated pipeline uses:
  - link_jp_skus.sku_exists() / make_sku()   -- SKU lookup
  - insert_jp_sku_links.link_one()            -- confirmed link writer

No Flask here — this module has no UI concerns, only queue/state logic, so it
can be exercised directly from a REPL/tests without spinning up the tool.
"""
import glob
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone

import requests

from app import init_db, get_images_db_path, get_image_db_dir
from insert_jp_sku_links import link_one
from link_jp_skus import make_sku, sku_exists

BASE_DIR = r"C:\MatchIT"
MAPS_DIR = os.path.join(BASE_DIR, "jp_cardid_maps")
CARDSDB_DIR = r"C:\CardsDB"
IDENTIFIER_LOOKUP_PATH = os.path.join(BASE_DIR, "identifier_lookup.json")
SET_METADATA_PATH = os.path.join(BASE_DIR, "set_metadata.json")
SESSION_LOG_PATH = os.path.join(BASE_DIR, "jp_manual_link_log.jsonl")
SUPERTYPE_CACHE_PATH = os.path.join(BASE_DIR, "jp_cardid_supertypes.json")

META_KEYS = ("failed", "disagreements")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Referer": "https://www.pokemon-card.com/card-search/"}
RESULT_API = "https://www.pokemon-card.com/card-search/resultAPI.php"
THUMB_CODE_RE = re.compile(r"^\d+_([A-Za-z]+)_")


# ─────────────────────────────────────────────────────────────
# Supertype cache — resultAPI.php's cardThumbFile encodes a supertype code
# right after the cardID (.../{cardID}_P_..., _T_..., _E_...): P=Pokemon,
# T=Trainer, E=Energy. Confirmed across 840 cards / 3 sets with exactly
# those 3 codes, no ambiguity. Not persisted anywhere else in the pipeline
# (scrape_pokemon_card_jp.py's own docstring notes the API exposes no
# collector-number OR category field it keeps), so this cache is built by
# re-querying the live API once and reused after that.
# ─────────────────────────────────────────────────────────────

def _fetch_set_cardlist(setcode):
    cards = []
    page = 1
    while True:
        params = {
            "pg": setcode, "se_ta": "", "page": str(page),
            "regulation_sidebar_form": "all", "sm_and_keyword": "true",
        }
        r = requests.get(RESULT_API, params=params, headers=HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
        page_cards = data.get("cardList", [])
        if not page_cards:
            break
        cards.extend(page_cards)
        max_page = data.get("maxPage", page)
        if page >= max_page:
            break
        page += 1
        time.sleep(0.3)
    return cards


def build_supertype_cache(setcodes):
    """
    Query resultAPI.php once per setcode and record {cardID: code} for every
    card returned (P/T/E). Merges into and overwrites SUPERTYPE_CACHE_PATH.
    """
    cache = {}
    if os.path.exists(SUPERTYPE_CACHE_PATH):
        with open(SUPERTYPE_CACHE_PATH, encoding="utf-8") as f:
            cache = json.load(f)

    for setcode in setcodes:
        cards = _fetch_set_cardlist(setcode)
        set_map = {}
        for c in cards:
            raw_id = c.get("cardID", "")
            try:
                card_id = f"{int(raw_id):06d}"
            except (TypeError, ValueError):
                continue
            m = THUMB_CODE_RE.match(os.path.basename(c.get("cardThumbFile", "")))
            if m:
                set_map[card_id] = m.group(1)
        cache[setcode] = set_map

    with open(SUPERTYPE_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    return cache


def _load_supertype_cache():
    if not os.path.exists(SUPERTYPE_CACHE_PATH):
        return {}
    with open(SUPERTYPE_CACHE_PATH, encoding="utf-8") as f:
        return json.load(f)


# ─────────────────────────────────────────────────────────────
# Queue
# ─────────────────────────────────────────────────────────────

def build_full_queue():
    """
    Every OCR-failure cardID across all jp_cardid_maps/*_map.json, minus
    Basic/foil Energy reprints (supertype "E") -- those never carry a
    collector number and are not worth indexing. Returns a list of
    {setcode, card_id, local_path}, sorted by (setcode, card_id) so a
    session works through one set at a time.
    """
    supertypes = _load_supertype_cache()
    items = []
    energy_excluded = 0
    for fp in sorted(glob.glob(os.path.join(MAPS_DIR, "*_map.json"))):
        setcode = os.path.basename(fp)[: -len("_map.json")]
        with open(fp, encoding="utf-8") as f:
            data = json.load(f)
        set_codes = supertypes.get(setcode, {})
        for card_id in data.get("failed", []):
            if set_codes.get(card_id) == "E":
                energy_excluded += 1
                continue
            local_path = os.path.join(CARDSDB_DIR, f"jpn-{setcode}", f"{card_id}.jpg")
            items.append({"setcode": setcode, "card_id": card_id, "local_path": local_path})
    items.sort(key=lambda x: (x["setcode"], x["card_id"]))
    return items


def _strip_front_suffix(filename):
    """
    link_one() now writes original_filename as "{cardID}_FRONT.jpg" (see
    project_front_matrix_view_bug memory -- an explicit view marker so
    _infer_view_from_orig() doesn't depend on its default). Older rows
    linked before that change still carry the bare "{cardID}.jpg" form.
    Normalize both to the bare cardID-based filename so resume-skip
    recognizes either.
    """
    stem, ext = os.path.splitext(filename)
    if stem.upper().endswith("_FRONT"):
        stem = stem[: -len("_FRONT")]
    return stem + ext


def _linked_original_filenames():
    """
    original_filename values already present in images.db for jpn- SKUs,
    normalized to the bare cardID form. This is the resume-skip key: a
    card's cardID *is* its source filename (see insert_jp_sku_links.link_one),
    and that identity is fixed regardless of which SKU the number eventually
    resolves to. Keyed off images.db reality, not the session log.
    """
    db_path = get_images_db_path()
    if not os.path.exists(db_path):
        return set()
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT original_filename FROM images WHERE sku LIKE 'jpn-%'"
        ).fetchall()
    finally:
        conn.close()
    return {_strip_front_suffix(r[0]) for r in rows if r[0]}


def get_pending_queue():
    """Full queue minus anything already linked in images.db in a prior session."""
    linked = _linked_original_filenames()
    return [
        item for item in build_full_queue()
        if os.path.basename(item["local_path"]) not in linked
    ]


def already_imaged_skus(setcode):
    """SKUs for this set that already have an images.db row (any image)."""
    db_path = get_images_db_path()
    if not os.path.exists(db_path):
        return set()
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT sku FROM images WHERE sku LIKE ?",
            (f"jpn-{setcode}-%",),
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def sku_has_image(sku):
    db_path = get_images_db_path()
    if not os.path.exists(db_path):
        return False
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT 1 FROM images WHERE sku = ? LIMIT 1", (sku,)).fetchone()
    finally:
        conn.close()
    return row is not None


# ─────────────────────────────────────────────────────────────
# identifier_lookup.json / set_metadata.json — in-memory cache
# ─────────────────────────────────────────────────────────────

class LookupStore:
    """
    identifier_lookup.json is ~6MB — reload once per tool process, write
    through to disk on Class B mutations only, keep the in-memory copy (and
    the per-set number index) authoritative so a multi-hour session isn't
    reparsing 6MB on every card.
    """

    def __init__(self):
        with open(IDENTIFIER_LOOKUP_PATH, encoding="utf-8") as f:
            self.id_lookup = json.load(f)
        self.pokemon_lookup = self.id_lookup["pokemon"]

        with open(SET_METADATA_PATH, encoding="utf-8") as f:
            self.set_meta = json.load(f)

        self._by_set = {}
        for k in self.pokemon_lookup:
            if not k.startswith("jpn-"):
                continue
            rest = k[len("jpn-"):]
            setcode, _, num = rest.rpartition("-")
            if setcode and num.isdigit():
                self._by_set.setdefault(setcode, []).append(int(num))
        for nums in self._by_set.values():
            nums.sort()

    def set_numbers(self, setcode):
        return list(self._by_set.get(setcode, []))

    def printed_total(self, setcode):
        meta = self.set_meta.get(f"jpn-{setcode}")
        return meta.get("printed_total") if meta else None

    def mint_sku(self, setcode, number):
        """
        Class B write: identity entry into identifier_lookup.json
        ("jpn-s8b-071": "jpn-s8b-071"), plus set_metadata printed_total kept
        consistent with how it was originally derived (max of known
        numbers) -- bumped if exceeded, seeded if it was never set (some
        early-batch sets, e.g. jpn-s8b, currently sit at printed_total=null).
        """
        sku = make_sku(setcode, number)
        self.pokemon_lookup[sku] = sku
        self._by_set.setdefault(setcode, [])
        if number not in self._by_set[setcode]:
            self._by_set[setcode].append(number)
            self._by_set[setcode].sort()
        with open(IDENTIFIER_LOOKUP_PATH, "w", encoding="utf-8") as f:
            json.dump(self.id_lookup, f, ensure_ascii=False, indent=2)

        key = f"jpn-{setcode}"
        meta = self.set_meta.get(key)
        if meta is not None:
            current = meta.get("printed_total")
            if current is None or int(number) > int(current):
                meta["printed_total"] = int(number)
                with open(SET_METADATA_PATH, "w", encoding="utf-8") as f:
                    json.dump(self.set_meta, f, ensure_ascii=False, indent=2)
        return sku


_store = None


def get_store():
    global _store
    if _store is None:
        _store = LookupStore()
    return _store


# ─────────────────────────────────────────────────────────────
# Session log (own file — never jp_sku_links.json / _report.txt,
# which every batch run overwrites)
# ─────────────────────────────────────────────────────────────

def _append_log(entry):
    entry = dict(entry)
    entry["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(SESSION_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ─────────────────────────────────────────────────────────────
# Submit — the one entry point the UI calls
# ─────────────────────────────────────────────────────────────

def submit_number(setcode, card_id, local_path, number, confirm_new=False):
    """
    Resolve a human-typed collector number for one card.

    Returns a dict with "outcome" in:
      needs_confirm   -- number has no existing SKU; caller must re-call with
                          confirm_new=True to actually mint + link it
      linked          -- written through to images.db via link_one()
      already_covered -- SKU already has an image (e.g. a base non-holo
                          already covers this number); nothing written, by
                          design (mirrors insert_jp_sku_links.py's own
                          dedup rule: only SKUs with no existing row get a row)
      error           -- bad input or link_one() failed
    """
    try:
        number = int(str(number).strip())
    except (TypeError, ValueError):
        return {"outcome": "error", "error": f"{number!r} is not a valid collector number"}
    if number <= 0:
        return {"outcome": "error", "error": "collector number must be positive"}

    store = get_store()
    sku = make_sku(setcode, number)
    is_known = sku_exists(setcode, number, store.pokemon_lookup)

    if not is_known and not confirm_new:
        nums = store.set_numbers(setcode)
        return {
            "outcome": "needs_confirm",
            "sku": sku,
            "setcode": setcode,
            "number": number,
            "known_range": f"{nums[0]:03d}-{nums[-1]:03d}" if nums else "none yet",
        }

    cls = "A" if is_known else "B"
    if not is_known:
        store.mint_sku(setcode, number)

    if sku_has_image(sku):
        _append_log({
            "setcode": setcode, "card_id": card_id, "number": number, "sku": sku,
            "class": cls, "action": "already_covered",
        })
        return {"outcome": "already_covered", "sku": sku, "class": cls}

    init_db()
    conn = sqlite3.connect(get_images_db_path())
    try:
        result = link_one(sku, local_path, conn, get_image_db_dir())
        conn.commit()
    finally:
        conn.close()

    _append_log({
        "setcode": setcode, "card_id": card_id, "number": number, "sku": sku,
        "class": cls, "action": result["status"],
        "image_id": result.get("image_id"), "error": result.get("error"),
    })

    if result["status"] != "inserted":
        return {"outcome": "error", "sku": sku, "class": cls, "error": result.get("error")}
    return {"outcome": "linked", "sku": sku, "class": cls, "image_id": result["image_id"]}
