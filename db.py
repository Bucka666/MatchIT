"""
db.py — GrailSweep scan counter module (Phase 2).

modal.Dict is the source of truth for free-tier scan counts.
All Dict access is lazy — nothing runs at import time.

Key schema: "scans:free:{identifier}:{YYYY-MM}"
  identifier = fingerprint (MD5 of UA+lang+IP) or device_id (localStorage UUID)
"""

import hashlib
import time
from datetime import datetime, timezone

import modal

_SCANS_DICT_NAME     = "scan-counters"
FREE_TIER_MONTHLY_LIMIT = 150
SKU_DEDUP_WINDOW_SECONDS = 60


# ─────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────

def _current_ym() -> str:
    """Return current UTC year-month as 'YYYY-MM'."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _scan_key(identifier: str, ym: str | None = None) -> str:
    """
    Build a Dict key for the given identifier and month.
    Raises ValueError for empty/None identifier.
    """
    if not identifier:
        raise ValueError("identifier must be a non-empty string")
    return f"scans:free:{identifier}:{ym or _current_ym()}"


# ─────────────────────────────────────────────────────────────
# Public scan counter functions
# ─────────────────────────────────────────────────────────────

def read_free_scans(fingerprint: str | None, device_id: str | None) -> dict:
    """
    Read current month's free scan count for a user.

    count = max of fingerprint count and device_id count (whichever are present).

    Returns:
        {
            "ok": True,
            "count": int,
            "limit": int,
            "remaining": int,
            "at_limit": bool,
            "ym": str,
            "fingerprint_count": int | None,
            "device_id_count": int | None,
            "ms": float,
        }
    """
    t_start = time.monotonic()
    fp  = fingerprint or None
    did = device_id   or None

    if not fp and not did:
        return {"ok": False, "error": "no identifier provided", "count": 0,
                "ms": round((time.monotonic() - t_start) * 1000, 2)}
    try:
        d  = modal.Dict.from_name(_SCANS_DICT_NAME, create_if_missing=True)
        ym = _current_ym()

        fp_count  = d.get(_scan_key(fp,  ym), 0) if fp  else None
        did_count = d.get(_scan_key(did, ym), 0) if did else None

        count = max(v for v in (fp_count, did_count) if v is not None)

        return {
            "ok":                True,
            "count":             count,
            "limit":             FREE_TIER_MONTHLY_LIMIT,
            "remaining":         max(0, FREE_TIER_MONTHLY_LIMIT - count),
            "at_limit":          count >= FREE_TIER_MONTHLY_LIMIT,
            "ym":                ym,
            "fingerprint_count": fp_count,
            "device_id_count":   did_count,
            "ms":                round((time.monotonic() - t_start) * 1000, 2),
        }
    except Exception as exc:
        return {
            "ok":    False,
            "error": f"{type(exc).__name__}: {exc}",
            "count": 0,
            "ms":    round((time.monotonic() - t_start) * 1000, 2),
        }


def increment_free_scans(fingerprint: str | None, device_id: str | None) -> dict:
    """
    Increment free scan count for the current month.

    Reads the max of fingerprint and device_id counts, adds 1, then writes
    that new value back to every provided key so they stay synchronised.

    Returns:
        {
            "ok": True,
            "new_count": int,
            "limit": int,
            "remaining": int,
            "at_limit": bool,
            "limit_just_crossed": bool,
            "ym": str,
            "ms": float,
        }
    """
    t_start = time.monotonic()
    fp  = fingerprint or None
    did = device_id   or None

    if not fp and not did:
        return {"ok": False, "error": "no identifier provided",
                "ms": round((time.monotonic() - t_start) * 1000, 2)}
    try:
        d  = modal.Dict.from_name(_SCANS_DICT_NAME, create_if_missing=True)
        ym = _current_ym()

        fp_key  = _scan_key(fp,  ym) if fp  else None
        did_key = _scan_key(did, ym) if did else None

        fp_val  = d.get(fp_key,  0) if fp_key  else 0
        did_val = d.get(did_key, 0) if did_key else 0

        prev_max  = max(fp_val, did_val)
        new_count = prev_max + 1

        if fp_key:
            d.put(fp_key,  new_count)
        if did_key:
            d.put(did_key, new_count)

        return {
            "ok":                 True,
            "new_count":          new_count,
            "limit":              FREE_TIER_MONTHLY_LIMIT,
            "remaining":          max(0, FREE_TIER_MONTHLY_LIMIT - new_count),
            "at_limit":           new_count >= FREE_TIER_MONTHLY_LIMIT,
            "limit_just_crossed": (prev_max < FREE_TIER_MONTHLY_LIMIT) and (new_count >= FREE_TIER_MONTHLY_LIMIT),
            "ym":                 ym,
            "ms":                 round((time.monotonic() - t_start) * 1000, 2),
        }
    except Exception as exc:
        return {
            "ok":    False,
            "error": f"{type(exc).__name__}: {exc}",
            "ms":    round((time.monotonic() - t_start) * 1000, 2),
        }


# ────────────────────────────────────────────────────────────
# Top-up credit helpers (added Task 9b)
#
# Top-up credits are paid scan credits granted via TOPUP- code
# redemption. Stored in the same modal.Dict as free scan counts
# ("scan-counters") under a different key prefix to keep all
# per-user scan state in one queryable place.
#
# Key format: "topup:credits:{identifier}"
# No month suffix — credits never expire (Decision 3 = A).
#
# As with free scan counters, two identifier keys are written
# in sync (server_fp and device_id) so credits survive cookie
# clears or fingerprint shifts when the other identifier
# remains stable.
# ────────────────────────────────────────────────────────────

def _topup_key(identifier: str) -> str:
    """Build modal.Dict key for top-up credits balance."""
    return f"topup:credits:{identifier}"


def read_topup_credits(fingerprint: str | None, device_id: str | None) -> dict:
    """
    Read top-up credit balance for a user.

    Returns the max of fingerprint-keyed and device_id-keyed
    balances (same merge logic as read_free_scans).

    Returns:
        {"ok": True, "credits": int, "fingerprint_credits": int|None,
         "device_id_credits": int|None, "ms": float}
        or {"ok": False, "error": str, "credits": 0, "ms": float}
    """
    t_start = time.monotonic()
    fp  = fingerprint or None
    did = device_id   or None

    if not fp and not did:
        return {"ok": False, "error": "no identifier provided",
                "credits": 0, "ms": (time.monotonic() - t_start) * 1000}
    try:
        d = modal.Dict.from_name(_SCANS_DICT_NAME, create_if_missing=True)

        fp_credits  = d.get(_topup_key(fp),  0) if fp  else None
        did_credits = d.get(_topup_key(did), 0) if did else None

        credits = max(v for v in (fp_credits, did_credits) if v is not None)

        return {
            "ok": True,
            "credits": credits,
            "fingerprint_credits": fp_credits,
            "device_id_credits": did_credits,
            "ms": (time.monotonic() - t_start) * 1000,
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "credits": 0, "ms": (time.monotonic() - t_start) * 1000}


def add_topup_credits(fingerprint: str | None, device_id: str | None, amount: int) -> dict:
    """
    Add top-up credits to a user's balance.

    Used by /api/redeem-topup at code redemption time. Writes
    the new balance to BOTH fingerprint and device_id keys.

    Args:
        amount: credits to add (must be positive)

    Returns:
        {"ok": True, "new_balance": int, "ms": float}
        or {"ok": False, "error": str, "ms": float}
    """
    t_start = time.monotonic()
    fp  = fingerprint or None
    did = device_id   or None

    if not fp and not did:
        return {"ok": False, "error": "no identifier provided",
                "ms": (time.monotonic() - t_start) * 1000}
    if amount <= 0:
        return {"ok": False, "error": "amount must be positive",
                "ms": (time.monotonic() - t_start) * 1000}
    try:
        d = modal.Dict.from_name(_SCANS_DICT_NAME, create_if_missing=True)

        fp_key  = _topup_key(fp)  if fp  else None
        did_key = _topup_key(did) if did else None

        fp_val  = d.get(fp_key,  0) if fp_key  else 0
        did_val = d.get(did_key, 0) if did_key else 0

        prev_max    = max(fp_val, did_val)
        new_balance = prev_max + amount

        if fp_key:  d.put(fp_key,  new_balance)
        if did_key: d.put(did_key, new_balance)

        return {
            "ok": True,
            "new_balance": new_balance,
            "added": amount,
            "ms": (time.monotonic() - t_start) * 1000,
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "ms": (time.monotonic() - t_start) * 1000}


def consume_topup_credit(fingerprint: str | None, device_id: str | None) -> dict:
    """
    Consume one top-up credit from a user's balance.

    Called from check_and_record_scan() when free tier is at
    limit but top-up credits exist. Atomically decrements both
    fingerprint and device_id keys.

    If balance is 0 or negative, does NOT decrement and returns
    ok=False — caller should treat this as "no credits available."

    Returns:
        {"ok": True, "consumed": True, "remaining": int, "ms": float}
        {"ok": True, "consumed": False, "remaining": 0, "ms": float}  # no credits
        {"ok": False, "error": str, "ms": float}
    """
    t_start = time.monotonic()
    fp  = fingerprint or None
    did = device_id   or None

    if not fp and not did:
        return {"ok": False, "error": "no identifier provided",
                "consumed": False, "remaining": 0,
                "ms": (time.monotonic() - t_start) * 1000}
    try:
        d = modal.Dict.from_name(_SCANS_DICT_NAME, create_if_missing=True)

        fp_key  = _topup_key(fp)  if fp  else None
        did_key = _topup_key(did) if did else None

        fp_val  = d.get(fp_key,  0) if fp_key  else 0
        did_val = d.get(did_key, 0) if did_key else 0

        prev_max = max(fp_val, did_val)

        if prev_max <= 0:
            return {"ok": True, "consumed": False, "remaining": 0,
                    "ms": (time.monotonic() - t_start) * 1000}

        new_balance = prev_max - 1

        if fp_key:  d.put(fp_key,  new_balance)
        if did_key: d.put(did_key, new_balance)

        return {
            "ok": True,
            "consumed": True,
            "remaining": new_balance,
            "ms": (time.monotonic() - t_start) * 1000,
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "consumed": False, "remaining": 0,
                "ms": (time.monotonic() - t_start) * 1000}


def diagnostic_dump(fingerprint: str | None, device_id: str | None) -> dict:
    """
    Read-only inspection of a user's full scan counter state.
    Wraps read_free_scans with extra debugging fields.
    Never raises.
    """
    fp  = fingerprint or None
    did = device_id   or None
    ym  = _current_ym()

    result = read_free_scans(fp, did)
    result["dict_name"]           = _SCANS_DICT_NAME
    result["fingerprint_key"]     = _scan_key(fp,  ym) if fp  else None
    result["device_id_key"]       = _scan_key(did, ym) if did else None
    result["fingerprint_provided"] = fp  is not None
    result["device_id_provided"]   = did is not None
    return result


# ─────────────────────────────────────────────────────────────
# Phase 2 — enforcement helpers
# ─────────────────────────────────────────────────────────────

_LEGACY_TIER          = "legacy"
_PREMIUM_TIERS_EXEMPT = {"legacy", "lifetime"}       # truly no-billing — always free
_PREMIUM_TIERS_CAPPED = {"monthly", "annual", "founder_yearly"}  # paid tiers with monthly scan caps


# ─────────────────────────────────────────────────────────────
# Phase 3 — Tier cap helpers
# Key schema: "tier:used:{code}:{YYYY-MM}" (where YYYY-MM is the
# billing-anchor-aware period, not necessarily calendar month)
# ─────────────────────────────────────────────────────────────

def _anchor_day_for_subscription(sub: dict) -> int:
    """Day-of-month billing anchor derived from current_period_start or created_at.
    Falls back to 1 if neither is available or parseable."""
    from datetime import datetime
    for field in ("current_period_start", "created_at"):
        val = sub.get(field)
        if not val:
            continue
        try:
            if isinstance(val, (int, float)):
                return datetime.utcfromtimestamp(val).day
            return datetime.fromisoformat(val.replace("Z", "+00:00")).day
        except Exception:
            continue
    return 1


def _period_id_for_tier(anchor_day: int) -> str:
    """Returns YYYY-MM string for the current billing window given an anchor day.
    If today < anchor_day, we're still in the window that started last month."""
    from datetime import datetime
    now = datetime.utcnow()
    if anchor_day <= 1 or now.day >= anchor_day:
        return now.strftime("%Y-%m")
    # Today is before anchor_day — still in last month's window
    if now.month == 1:
        return f"{now.year - 1:04d}-12"
    return f"{now.year:04d}-{now.month - 1:02d}"


def _period_bounds_for_tier(anchor_day: int) -> tuple:
    """Returns (period_start_iso, period_end_iso) for the current billing window."""
    from datetime import datetime, timedelta
    from calendar import monthrange
    now = datetime.utcnow()
    anchor = max(anchor_day, 1)
    if now.day >= anchor:
        start_year, start_month = now.year, now.month
    else:
        if now.month == 1:
            start_year, start_month = now.year - 1, 12
        else:
            start_year, start_month = now.year, now.month - 1
    start_day = min(anchor, monthrange(start_year, start_month)[1])
    period_start = datetime(start_year, start_month, start_day)
    if start_month == 12:
        end_year, end_month = start_year + 1, 1
    else:
        end_year, end_month = start_year, start_month + 1
    end_day = min(anchor, monthrange(end_year, end_month)[1])
    period_end = datetime(end_year, end_month, end_day) - timedelta(seconds=1)
    return period_start.isoformat(), period_end.isoformat()


def get_tier_cap(tier: str) -> int | None:
    """Returns int scan cap for a capped tier from config, or None if uncapped."""
    try:
        from app import CFG
        return CFG.get("tier_caps", {}).get(tier)
    except Exception:
        return None


def read_tier_state(code: str, tier: str, subscriptions_obj: dict) -> dict | None:
    """Read-only tier usage state for display. Never increments.
    Returns None for uncapped tiers (free, legacy, lifetime)."""
    if not code or tier not in _PREMIUM_TIERS_CAPPED:
        return None
    sub = subscriptions_obj.get(code, {})
    anchor_day = _anchor_day_for_subscription(sub)
    period_id = _period_id_for_tier(anchor_day)
    period_start, period_end = _period_bounds_for_tier(anchor_day)
    cap = get_tier_cap(tier) or 0
    try:
        d = modal.Dict.from_name(_SCANS_DICT_NAME, create_if_missing=True)
        tier_used = d.get(f"tier:used:{code}:{period_id}", 0) or 0
    except Exception:
        tier_used = 0
    warned = bool(sub.get("tier_warned_80pct", False))
    transition_warned = bool(sub.get("tier_transition_warned", False))
    # Both flags reset when a new billing period begins
    recorded_end = sub.get("tier_period_end")
    if recorded_end and recorded_end != period_end:
        warned = False
        transition_warned = False
    return {
        "tier_used":               tier_used,
        "tier_limit":              cap,
        "tier_period_start":       period_start,
        "tier_period_end":         period_end,
        "tier_warned_80pct":       warned,
        "tier_transition_warned":  transition_warned,
        "tier_remaining":          max(0, cap - tier_used),
    }


def check_and_consume_tier(code: str, tier: str, subscriptions_obj: dict) -> dict:
    """Atomic check-and-increment for capped premium tiers.
    Returns allowed=True and increments if under cap, allowed=False if exhausted.
    FAIL-OPEN: counter errors allow the scan rather than block a paying user."""
    if not code or tier not in _PREMIUM_TIERS_CAPPED:
        return {"allowed": False, "reason": "not_capped_tier"}
    sub = subscriptions_obj.get(code, {})
    anchor_day = _anchor_day_for_subscription(sub)
    period_id = _period_id_for_tier(anchor_day)
    cap = get_tier_cap(tier)
    if not cap:
        print(f"[TIER] WARNING: no cap configured for tier={tier}, allowing scan", flush=True)
        return {"allowed": True, "consumed": "tier_no_cap", "tier_used_after": 0}
    counter_key = f"tier:used:{code}:{period_id}"
    try:
        d = modal.Dict.from_name(_SCANS_DICT_NAME, create_if_missing=True)
        current = d.get(counter_key, 0) or 0
        if current >= cap:
            return {
                "allowed":    False,
                "reason":     "tier_limit_exceeded",
                "tier_used":  current,
                "tier_limit": cap,
            }
        new_val = current + 1
        d.put(counter_key, new_val)
        return {
            "allowed":         True,
            "consumed":        "tier",
            "tier_used_after": new_val,
            "tier_limit":      cap,
        }
    except Exception as e:
        print(f"[TIER] ERROR in check_and_consume_tier code={code}: {e}", flush=True)
        return {"allowed": True, "consumed": "tier_error_fallback"}


def compute_server_fingerprint(user_agent: str, accept_language: str, remote_addr: str) -> str:
    """
    Reproduce the fingerprint pattern used by validate_premium():
    MD5(user_agent | accept_language | first-two-IP-octets).
    Never raises — returns "unknown_fp" on any error.
    """
    try:
        addr   = remote_addr or ""
        parts  = addr.split(".")
        prefix = ".".join(parts[:2]) if len(parts) >= 2 else ""
        raw    = f"{user_agent}|{accept_language}|{prefix}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()
    except Exception:
        return "unknown_fp"


def check_and_record_scan(
    server_fp: str,
    device_id: str | None,
    tier: str | None,
    code: str | None = None,
    subscriptions_obj: dict | None = None,
    sku: str | None = None,
) -> dict:
    """
    Single enforcement decision: check tier → check count → (if ok) increment.

    FAIL-OPEN: on any error, allowed=True so a counter bug never blocks /match.

    Args:
        server_fp: MD5 fingerprint of UA+lang+IP
        device_id: localStorage UUID from matchit_device_id_v1 cookie
        tier: resolved tier string ("legacy", "monthly", "annual", etc.) or None
        code: GRAIL-XXXX-XXXX access code (required for capped premium tiers)
        subscriptions_obj: pre-loaded dict from _load_subs() (avoids circular import)
        sku: matched card SKU, if known. When provided, a repeat of the same
            SKU by the same identity within SKU_DEDUP_WINDOW_SECONDS is allowed
            but not charged against quota (allowed=True, counted=False).

    Returns:
        {
            "ok": bool,
            "allowed": bool,
            "counted": bool,
            "reason": str,
            "tier": str | None,
            "limit": int | None,
            "count": int | None,
            "remaining": int | None,
            "limit_just_crossed": bool,
            "ms": float,
        }
    """
    t_start = time.monotonic()
    try:
        # Branch 1 — truly exempt tiers (legacy config codes, lifetime purchasers)
        if tier in _PREMIUM_TIERS_EXEMPT:
            return {
                "ok":                True,
                "allowed":           True,
                "counted":           False,
                "reason":            "premium_exempt",
                "tier":              tier,
                "limit":             None,
                "count":             None,
                "remaining":         None,
                "limit_just_crossed": False,
                "ms":                round((time.monotonic() - t_start) * 1000, 2),
            }

        # Branch 1b — per-SKU dedupe: a repeat of the same card by the same
        # identity within the window is allowed but not charged again.
        # FAIL-OPEN: any error here falls through to the normal charge logic
        # below rather than blocking or mis-skipping a legitimate scan.
        if sku:
            try:
                _dd = modal.Dict.from_name(_SCANS_DICT_NAME, create_if_missing=True)
                _dedup_key = f"dedup:{server_fp}:{device_id}:{sku}"
                _last = _dd.get(_dedup_key)
                _now = time.time()
                if _last is not None and (_now - _last) < SKU_DEDUP_WINDOW_SECONDS:
                    if tier in _PREMIUM_TIERS_CAPPED and code and subscriptions_obj is not None:
                        _tier_state = read_tier_state(code, tier, subscriptions_obj)
                    else:
                        _tier_state = None
                    if _tier_state:
                        _dd_limit = _tier_state["tier_limit"]
                        _dd_count = _tier_state["tier_used"]
                    else:
                        _free_state = read_free_scans(server_fp, device_id)
                        _dd_limit = FREE_TIER_MONTHLY_LIMIT
                        _dd_count = _free_state.get("count", 0) if _free_state.get("ok") else 0
                    _dd_remaining = max(0, _dd_limit - _dd_count) if _dd_limit is not None else None
                    return {
                        "ok":                True,
                        "allowed":           True,
                        "counted":           False,
                        "reason":            "sku_deduped",
                        "deduped":           True,
                        "tier":              tier,
                        "limit":             _dd_limit,
                        "count":             _dd_count,
                        "remaining":         _dd_remaining,
                        "limit_just_crossed": False,
                        "ms":                round((time.monotonic() - t_start) * 1000, 2),
                    }
            except Exception:
                pass

        # Branch 2 — capped premium tiers (monthly, annual, founder_yearly)
        if tier in _PREMIUM_TIERS_CAPPED:
            if not code or subscriptions_obj is None:
                # Caller didn't supply code/subs — degrade gracefully, allow but log
                print(f"[TIER] WARNING: capped tier={tier} but code/subs missing — allowing", flush=True)
                return {
                    "ok": True, "allowed": True, "counted": False,
                    "reason": "tier_missing_code", "tier": tier,
                    "limit": None, "count": None, "remaining": None,
                    "limit_just_crossed": False,
                    "ms": round((time.monotonic() - t_start) * 1000, 2),
                }
            tier_result = check_and_consume_tier(code, tier, subscriptions_obj)
            if tier_result.get("allowed"):
                if sku:
                    try:
                        modal.Dict.from_name(_SCANS_DICT_NAME, create_if_missing=True).put(
                            f"dedup:{server_fp}:{device_id}:{sku}", time.time()
                        )
                    except Exception:
                        pass
                return {
                    "ok":                True,
                    "allowed":           True,
                    "counted":           True,
                    "reason":            "tier_within_limit",
                    "tier":              tier,
                    "tier_used":         tier_result.get("tier_used_after"),
                    "tier_limit":        tier_result.get("tier_limit"),
                    "limit":             tier_result.get("tier_limit"),
                    "count":             tier_result.get("tier_used_after"),
                    "remaining":         None,
                    "limit_just_crossed": False,
                    "ms":                round((time.monotonic() - t_start) * 1000, 2),
                }
            # Tier exhausted — try top-up fallback before blocking
            topup_result = consume_topup_credit(server_fp, device_id)
            if topup_result.get("ok") and topup_result.get("consumed"):
                return {
                    "ok":                True,
                    "allowed":           True,
                    "counted":           False,
                    "reason":            "topup_consumed_premium",
                    "tier":              tier,
                    "limit":             tier_result.get("tier_limit"),
                    "count":             tier_result.get("tier_used"),
                    "remaining":         0,
                    "limit_just_crossed": False,
                    "topup_remaining":   topup_result.get("remaining", 0),
                    "ms":                round((time.monotonic() - t_start) * 1000, 2),
                }
            return {
                "ok":                True,
                "allowed":           False,
                "counted":           False,
                "reason":            "tier_limit_exceeded",
                "tier":              tier,
                "tier_used":         tier_result.get("tier_used"),
                "tier_limit":        tier_result.get("tier_limit"),
                "limit":             tier_result.get("tier_limit"),
                "count":             tier_result.get("tier_used"),
                "remaining":         0,
                "limit_just_crossed": False,
                "ms":                round((time.monotonic() - t_start) * 1000, 2),
            }

        read_result = read_free_scans(server_fp, device_id)
        if not read_result.get("ok"):
            raise Exception(read_result.get("error", "read_free_scans failed"))

        current_count = read_result["count"]

        if current_count >= FREE_TIER_MONTHLY_LIMIT:
            # Free tier exhausted — try top-up credits before blocking.
            topup_result = consume_topup_credit(server_fp, device_id)
            if topup_result.get("ok") and topup_result.get("consumed"):
                return {
                    "ok":                True,
                    "allowed":           True,
                    "counted":           False,  # not counted against free tier
                    "reason":            "topup_consumed",
                    "tier":              "free",
                    "limit":             FREE_TIER_MONTHLY_LIMIT,
                    "count":             current_count,
                    "remaining":         0,
                    "limit_just_crossed": False,
                    "topup_remaining":   topup_result.get("remaining", 0),
                    "ms":                round((time.monotonic() - t_start) * 1000, 2),
                }
            # No top-up credits available — block.
            return {
                "ok":                True,
                "allowed":           False,
                "counted":           False,
                "reason":            "free_limit_exceeded",
                "tier":              "free",
                "limit":             FREE_TIER_MONTHLY_LIMIT,
                "count":             current_count,
                "remaining":         0,
                "limit_just_crossed": False,
                "topup_remaining":   0,
                "ms":                round((time.monotonic() - t_start) * 1000, 2),
            }

        inc_result = increment_free_scans(server_fp, device_id)
        if not inc_result.get("ok"):
            raise Exception(inc_result.get("error", "increment_free_scans failed"))

        new_count = inc_result["new_count"]
        if sku:
            try:
                modal.Dict.from_name(_SCANS_DICT_NAME, create_if_missing=True).put(
                    f"dedup:{server_fp}:{device_id}:{sku}", time.time()
                )
            except Exception:
                pass
        return {
            "ok":                True,
            "allowed":           True,
            "counted":           True,
            "reason":            "free_within_limit",
            "tier":              "free",
            "limit":             FREE_TIER_MONTHLY_LIMIT,
            "count":             new_count,
            "remaining":         max(0, FREE_TIER_MONTHLY_LIMIT - new_count),
            "limit_just_crossed": inc_result.get("limit_just_crossed", False),
            "ms":                round((time.monotonic() - t_start) * 1000, 2),
        }
    except Exception as exc:
        return {
            "ok":                False,
            "allowed":           True,  # FAIL-OPEN: never block on counter errors
            "counted":           False,
            "reason":            "error",
            "error":             f"{type(exc).__name__}: {exc}",
            "tier":              tier,
            "limit":             None,
            "count":             None,
            "remaining":         None,
            "limit_just_crossed": False,
            "ms":                round((time.monotonic() - t_start) * 1000, 2),
        }


def resolve_tier_from_code(access_code: str | None, cfg_premium_codes: list, subs: dict) -> str | None:
    """
    Resolve the tier string for an access code. Never raises.

    Returns: "legacy" | "monthly" | "annual" | "lifetime" | None
    """
    try:
        if not access_code:
            return None
        if access_code in cfg_premium_codes:
            return "legacy"
        entry = subs.get(access_code)
        if entry and entry.get("status") == "active":
            return entry.get("tier")
        return None
    except Exception:
        return None
