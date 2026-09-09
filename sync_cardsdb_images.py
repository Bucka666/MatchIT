"""
sync_cardsdb_images.py — Onboard new CardsDB images into local images.db.

Standalone equivalent of app.py's /admin/sync_keysdb route (the "cards"
vertical branch), runnable without a live Flask session/admin login. Imports
the same helpers app.py already exposes (_iter_cardsdb_images,
normalize_uploaded_image, load_embedding_cache, upload_to_r2) so there is
exactly one implementation of "how a CardsDB image becomes an images.db row"
— this does not reimplement that logic, just drives it from the CLI.

For each new image found under CardsDB (skipping anything whose
original_filename is already in images.db):
  1. assigns a UUID image_id
  2. copies it to the local image_db/ dir as {image_id}.jpg
  3. normalizes it (app.py's normalize_uploaded_image)
  4. uploads it to R2 (non-fatal — failures are logged, the DB row still
     gets written; r2_image_upload.py's planner can backfill any gaps later)
  5. INSERT OR REPLACE INTO images(...) with embedding=NULL
     (embeddings are generated separately by incremental_embed.py)

Run from the project root (needs app.py importable):
    python sync_cardsdb_images.py --dry-run          # preview only, no writes
    python sync_cardsdb_images.py --limit 3           # sync just 3 new images (validation)
    python sync_cardsdb_images.py                      # sync all new images (no cap)
"""

import argparse
import os
import shutil
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional


def _iter_one_game_skus(cardsdb_root: Path, game_folder: str):
    """Like app.py's _iter_cardsdb_images() but walks a single game folder
    only, instead of every game under cardsdb_root, and does NOT resolve
    the front image -- just (sku, sku_dir), no filesystem stat calls beyond
    the one iterdir() of the game folder itself.

    _iter_cardsdb_images() walks all 4 games unconditionally and only lets
    a --game filter drop non-matching rows AFTER the fact (see its own
    CLAUDE.md gotcha: "ignores --game scoping and walks the entire CardsDB
    tree regardless"). Scoping the walk to one game wasn't enough on its
    own though -- a run scoped to pokemon (~36k SKU folders) still blew a
    1800s Modal timeout, because resolving front.png for every SKU costs
    one .exists() network round-trip per SKU against the volume. The real
    fix (matching the One Piece price-refresh timeout fix, commit
    a49721a) is splitting the cheap listing (this function) from the
    expensive per-SKU stat, so run_sync() can skip the stat entirely for
    SKUs already in images.db and parallelize it for the rest."""
    game_dir = cardsdb_root / game_folder
    if not game_dir.is_dir():
        return
    for sku_dir in sorted(d for d in game_dir.iterdir() if d.is_dir()):
        sku = sku_dir.name.strip()
        if not sku or sku.startswith("_"):
            continue
        yield sku, sku_dir


def _resolve_front_image(sku_dir: Path, is_image_file):
    """Front.png if present, else the first other image file in the SKU
    folder, else None. One or two .exists()/iterdir() round-trips -- the
    expensive part of onboarding a SKU over the network volume, which is
    why run_sync() only calls this for SKUs not already in images.db."""
    front = sku_dir / "front.png"
    if front.exists():
        return front
    candidates = sorted(
        (p for p in sku_dir.iterdir() if p.is_file() and is_image_file(p)),
        key=lambda p: p.name.lower()
    )
    return candidates[0] if candidates else None


def run_sync(
    game: str = "",
    dry_run: bool = False,
    limit: int = 0,
    commit_cb: Optional[Callable[[], None]] = None,
    checkpoint_every: int = 200,
) -> dict:
    """Core sync logic, callable directly (used by main() below and by the
    Modal entry point at the bottom of this file) as well as from the CLI.

    app.py / vertical_loader / r2_util are imported HERE, not at module
    top-level -- `modal run sync_cardsdb_images.py` imports this module
    locally first (to discover the App/entrypoint) before anything remote
    happens, and a module-level `from app import ...` would drag app.py's
    entire import chain (Flask, CLIP, DINOv2, preloads) into that local
    discovery step. incremental_embed.py's Modal entrypoint uses the same
    deferred-import pattern for exactly this reason (see its `from app
    import get_embedder` inside run_incremental_embed(), not at module
    scope) -- mirrored here rather than just structurally resembled.

    commit_cb / checkpoint_every: same checkpointing pattern as
    incremental_embed.py's run_incremental_embed() (mirrors commit a49721a,
    the One Piece price-refresh timeout fix) -- every checkpoint_every
    images, the SQLite transaction is committed and commit_cb() (pass
    Modal's vol.commit) is called, instead of a single commit at the very
    end. A run that times out mid-batch now loses at most one
    checkpoint_every-sized chunk, not everything processed since the run
    started. commit_cb=None for local/non-Modal runs (nothing to commit
    to)."""
    from app import (
        init_db, get_images_db_path, get_image_db_dir,
        normalize_uploaded_image, load_embedding_cache, _iter_cardsdb_images,
        _is_image_file,
    )
    from vertical_loader import get_db_root
    from r2_util import upload_to_r2

    init_db()
    db_path = get_images_db_path()
    img_dir = get_image_db_dir()
    db_root = Path(get_db_root())

    print(f"images.db : {db_path}")
    print(f"image_db  : {img_dir}")
    print(f"CardsDB   : {db_root}\n")

    if not db_root.exists():
        print(f"[ERROR] DB root not found: {db_root}")
        return {"status": "error", "error": f"DB root not found: {db_root}"}

    # Dedup key: sku, not original_filename (fixed 2026-09-09). This table
    # is one row per SKU by design (no view/category column -- every row
    # is a FRONT entry, confirmed by direct query: zero legitimate
    # multi-row-per-sku cases across any of the 4 games). original_filename
    # matching broke silently because it depends on the exact string every
    # historical ingestion pipeline happened to write there -- confirmed
    # live that ALL 8,788 pre-existing jpn- rows checked used one of two
    # older formats ('{sku}_FRONT.ext' or '{scrape_id}_FRONT.ext', from
    # insert_jp_sku_links.py and similar), none matching this script's own
    # f"{game}/{sku}/{sku}_FRONT" construction -- so every one of them
    # looked "new" on every run, and re-syncing an already-registered SKU
    # creates a genuine duplicate row (image_id is the only primary key;
    # sku has an index, not a uniqueness constraint, so INSERT OR REPLACE
    # never collides with the existing row). SKU existence is
    # format-independent and is what "already synced" actually means here.
    existing = set()
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT sku FROM images WHERE sku IS NOT NULL"
        ).fetchall()
        for (sku,) in rows:
            if sku:
                existing.add(str(sku).strip().lower())

    to_process = []
    skipped_existing = 0
    skipped_bad = 0

    game_filter = game.strip().lower()
    skipped_other_game = 0

    if game_filter:
        # Two-phase scoped scan: (1) cheap directory listing only, skip
        # anything already in images.db WITHOUT ever stat-ing it, (2)
        # resolve front.png only for the SKUs that actually need it, in
        # parallel. See _iter_one_game_skus()'s docstring -- this is what
        # actually fixed the 1800s timeout; scoping the walk to one game
        # alone wasn't enough (still ~36k SKUs to stat for pokemon).
        pending = []
        for sku, sku_dir in _iter_one_game_skus(db_root, game_filter):
            if sku.strip().lower() in existing:
                skipped_existing += 1
                continue
            # rel_id is still computed and stored as original_filename on
            # INSERT (informational/for any future consumer that reads it),
            # it's just no longer what dedup itself checks against.
            rel_id = f"{game_filter}/{sku}/{sku}_FRONT"
            pending.append((sku, sku_dir, rel_id))

        print(f"[SCAN] {len(pending)} SKU(s) not yet in images.db, {skipped_existing} already present "
              f"-- resolving image files (8 workers)...")

        def _resolve(item):
            sku, sku_dir, rel_id = item
            return sku, _resolve_front_image(sku_dir, _is_image_file), rel_id

        with ThreadPoolExecutor(max_workers=8) as pool:
            for sku, src_path, rel_id in pool.map(_resolve, pending):
                if src_path is None:
                    skipped_bad += 1
                    continue
                to_process.append((sku, src_path, rel_id))
    else:
        # Unscoped (sync every game) -- unchanged full-catalog walk, same
        # sku-based dedup as the scoped path above.
        for sku, src_path, rel_id in _iter_cardsdb_images(db_root):
            if not rel_id:
                skipped_bad += 1
                continue
            if sku.strip().lower() in existing:
                skipped_existing += 1
                continue
            to_process.append((sku, src_path, rel_id))

    print(f"[SCAN] {len(to_process)} new images found, {skipped_existing} already present, {skipped_bad} bad"
          + (f", {skipped_other_game} outside --game {game!r}" if game_filter else ""))

    if limit > 0:
        to_process = to_process[:limit]
        print(f"[LIMIT] processing only {len(to_process)} (--limit {limit})")

    if dry_run:
        print("\n[DRY-RUN] sample of what would be synced:")
        for sku, src_path, rel_id in to_process[:10]:
            print(f"  {sku}  {src_path}  ->  {rel_id}")
        print(f"\n[DRY-RUN] {len(to_process)} would be inserted, 0 written (dry run)")
        return {"status": "dry_run", "would_insert": len(to_process),
                "skipped_existing": skipped_existing, "skipped_bad": skipped_bad,
                "skipped_other_game": skipped_other_game}

    inserted = 0
    r2_failed = []
    insert_failed = []

    def _process_one(item):
        """Runs in a worker thread -- copy + normalize + R2 upload only,
        NEVER touches the SQLite connection. Mirrors the scan phase's own
        _resolve()/pool.map() pattern above: worker threads return a
        result, the actual write to shared state (there: to_process.append,
        here: conn.execute) happens back in the main thread as results
        stream in via pool.map(). This is what makes 8-way threading safe
        with a single sqlite3.Connection -- the connection is only ever
        used from the thread that created it."""
        sku, src_path, rel_id = item
        try:
            image_id = str(uuid.uuid4())
            dst_path = os.path.join(img_dir, f"{image_id}.jpg")
            shutil.copy2(str(src_path), dst_path)
            normalize_uploaded_image(dst_path)
            r2_ok = upload_to_r2(image_id, dst_path)
            return {"ok": True, "sku": sku, "src_path": src_path, "image_id": image_id,
                    "dst_path": dst_path, "rel_id": rel_id, "r2_ok": r2_ok}
        except Exception as e:
            return {"ok": False, "sku": sku, "src_path": src_path, "error": str(e)}

    conn = sqlite3.connect(db_path)
    try:
        # 8 workers: each image's copy+normalize+R2-upload is I/O-bound
        # (two network-volume round trips for the copy, one R2 network
        # call), not CPU-bound, so this parallelizes well -- and matches
        # r2_util.py's own boto3 Config(max_pool_connections=8), which was
        # already sized for exactly this concurrency level. Serial
        # processing measured ~1.9s/image; a ~4,845-image batch at that
        # rate needs ~2.5h, which checkpointing bounds the LOSS from but
        # does nothing to reduce -- this is the actual throughput fix.
        with ThreadPoolExecutor(max_workers=8) as pool:
            for idx, result in enumerate(pool.map(_process_one, to_process)):
                if result["ok"]:
                    if not result["r2_ok"]:
                        r2_failed.append(result["sku"])
                    try:
                        # OR IGNORE, not OR REPLACE (2026-09-09): with
                        # idx_images_sku_unique in place, OR REPLACE would
                        # resolve a sku conflict by silently DELETING the
                        # existing row (with its real embedding) and
                        # inserting this one (embedding=NULL) -- no
                        # exception, no trace. If a future dedup-check gap
                        # (like tonight's) ever let an already-registered
                        # SKU reach this point again, that's silent,
                        # unrecoverable loss of an embedded row, not a
                        # recoverable duplicate. OR IGNORE instead leaves
                        # the existing row untouched and drops the attempted
                        # insert -- the just-uploaded R2 object becomes a
                        # harmless orphan (unreferenced, cleanable later),
                        # never worse than that. No code path in this
                        # script (or any other caller, confirmed) currently
                        # relies on REPLACE semantics -- the dedup check
                        # above unconditionally skips already-registered
                        # SKUs before they ever reach this insert, so there
                        # was never a legitimate "re-sync a corrected image"
                        # flow depending on this default overwriting.
                        cur = conn.execute(
                            """
                            INSERT OR IGNORE INTO images
                                (image_id, sku, description, original_filename, path, added_at, embedding)
                            VALUES (?, ?, ?, ?, ?, ?, NULL)
                            """,
                            (result["image_id"], result["sku"], "", result["rel_id"],
                             result["dst_path"], datetime.utcnow().isoformat()),
                        )
                        if cur.rowcount == 0:
                            # sku already existed (idx_images_sku_unique
                            # conflict) -- dedup should have caught this
                            # upstream. Not silent: visible in run output
                            # even though nothing was lost.
                            insert_failed.append(f"{result['sku']}: sku already existed, insert skipped")
                            print(f"  [WARN] {result['sku']}: SKU already existed in images.db -- "
                                  f"dedup check should have caught this, insert skipped to protect "
                                  f"existing row (orphaned R2 object: {result['image_id']}.jpg)")
                        else:
                            inserted += 1
                            print(f"  [OK] {result['sku']} -> {result['image_id']}.jpg"
                                  + ("" if result["r2_ok"] else "  (R2 upload failed, row inserted anyway)"))
                    except sqlite3.Error as e:
                        insert_failed.append(f"{result['sku']}: {e}")
                        print(f"  [FAIL] {result['sku']} (sqlite error on insert): {e}")
                else:
                    insert_failed.append(f"{result['sku']}: {result['error']}")
                    print(f"  [FAIL] {result['sku']} ({result['src_path']}): {result['error']}")

                done = idx + 1
                if checkpoint_every > 0 and done % checkpoint_every == 0:
                    conn.commit()
                    if commit_cb is not None:
                        try:
                            commit_cb()
                        except Exception as e:
                            print(f"  [WARN] commit_cb() failed at checkpoint: {e}")
                    print(f"  [CHECKPOINT] {done}/{len(to_process)} (sqlite committed, volume committed)")

        conn.commit()
        if commit_cb is not None:
            try:
                commit_cb()
            except Exception as e:
                print(f"  [WARN] commit_cb() failed at final commit: {e}")
    finally:
        conn.close()

    load_embedding_cache(force=True)

    print(f"\nSync complete. Inserted={inserted}, skipped_existing={skipped_existing}, skipped_bad={skipped_bad}")
    if r2_failed:
        print(f"R2 upload failures ({len(r2_failed)}, rows still inserted, backfillable via r2_image_upload.py): {r2_failed[:10]}")
    if insert_failed:
        print(f"Insert failures ({len(insert_failed)}): {insert_failed[:10]}")

    return {"status": "ok", "inserted": inserted, "skipped_existing": skipped_existing,
            "skipped_bad": skipped_bad, "skipped_other_game": skipped_other_game,
            "r2_failed": r2_failed, "insert_failed": insert_failed}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="max new images to sync (0 = unlimited)")
    parser.add_argument("--dry-run", action="store_true", help="preview only, no writes")
    parser.add_argument("--game", default="", help="only sync this CardsDB top-level folder (e.g. onepiece)")
    args = parser.parse_args()
    run_sync(game=args.game, dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()


# ─────────────────────────────────────────────────────────────
# Modal entry point — modal run sync_cardsdb_images.py --game pokemon
#
# sync_cardsdb_images.py was previously local-only; every other
# volume-touching maintenance script in this repo (incremental_embed.py,
# backfill_*.py, etc.) follows the same pattern of a small modal.App
# appended at the bottom of its own file, so this mirrors that rather
# than introducing a new standalone runner script.
# ─────────────────────────────────────────────────────────────

import sys
sys.path.insert(0, "/app")
import modal
from modal_config import image, vol

sync_app = modal.App("grailsweep-sync-cardsdb-images")


def _fix_vertical_config():
    """Mirrors matchit_modal.py's helper of the same name -- duplicated
    here (not imported) so this file's Modal entry point stays
    self-contained, matching incremental_embed.py's convention of pulling
    only from modal_config, never from matchit_modal.py."""
    import json
    vpath = "/app/verticals/cards/vertical.json"
    with open(vpath, "r") as f:
        vcfg = json.load(f)
    vcfg["db_root"] = "/modal_data/CardsDB"
    with open(vpath, "w") as f:
        json.dump(vcfg, f, indent=2)
    os.makedirs("/modal_data/CardsDB", exist_ok=True)


@sync_app.function(
    image=image,
    volumes={"/modal_data": vol},
    # r2-credentials was missing entirely -- run_sync() calls upload_to_r2()
    # (r2_util.py), which needs R2_ENDPOINT/R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY/
    # R2_BUCKET as env vars. r2_util.py is deliberately non-fatal on missing
    # creds (catches the KeyError, logs, returns False) so this failed
    # silently-ish: every row still got INSERTed with a real image_db path,
    # just never mirrored to R2. Matches every other R2-uploading function in
    # this repo (matchit_modal.py, r2_image_upload.py, smart_upload.py).
    secrets=[modal.Secret.from_name("r2-credentials")],
    # Was 1800 -- a real ~5,123-image pokemon batch confirmed timed out at
    # that ceiling before completing. run_sync() now checkpoints (SQLite
    # commit + vol.commit every checkpoint_every images, see run_sync()'s
    # docstring), so a timeout no longer loses everything processed since
    # the run started -- this is headroom for one invocation to make real
    # progress on a big batch, not a substitute for that safety net. 3600
    # matches r2_image_upload.py's own proven ceiling for the same class of
    # work (a loop of per-image R2 uploads), not a fresh guess -- mirrors
    # how incremental_embed.py's 1800->3600 bump was justified tonight.
    timeout=3600,
)
def run_sync_remote(game: str = "", dry_run: bool = False, limit: int = 0):
    os.chdir("/app")
    sys.path.insert(0, "/app")
    os.environ["LOCALAPPDATA"] = "/modal_data"
    vol.reload()
    # _fix_vertical_config() MUST run before calling run_sync() -- run_sync()
    # imports app.py internally (deferred import, see its docstring), which
    # caches db_root at import time (see matchit_modal.py comment).
    _fix_vertical_config()
    from sync_cardsdb_images import run_sync as _run
    # commit_cb is only ever invoked from inside the (non-dry-run) insert
    # loop -- run_sync()'s dry-run branch returns before reaching it, and
    # its own final commit (after the loop) covers the tail, so no separate
    # vol.commit() is needed here (that was a confirmed-redundant duplicate
    # in incremental_embed.py's equivalent wrapper, fixed the same way).
    result = _run(game=game, dry_run=dry_run, limit=limit, commit_cb=vol.commit)
    return result


@sync_app.local_entrypoint()
def run_sync_cli(game: str = "", dry_run: bool = False, limit: int = 0):
    import json as _json
    result = run_sync_remote.remote(game=game, dry_run=dry_run, limit=limit)
    print(_json.dumps(result, indent=2))
