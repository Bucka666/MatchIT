"""
Read-only label/accuracy census over images.db on matchit-data-v2.
Standalone Modal app - does NOT touch matchit-api.
Run:  $env:PYTHONIOENCODING="utf-8"; modal run count_labels.py
"""
import modal

app = modal.App("grailsweep-label-count")
vol = modal.Volume.from_name("matchit-data-v2")

DB = "/modal_data/MatchITv2_ProductMatch_Data/cards/images.db"


@app.function(volumes={"/modal_data": vol}, timeout=900)
def census():
    import os
    import sqlite3

    if not os.path.exists(DB):
        print(f"!! DB NOT FOUND at {DB}")
        return

    size_mb = os.path.getsize(DB) / (1024 * 1024)
    print(f"DB found: {DB}  ({size_mb:.1f} MiB)\n")

    # read-only URI: cannot write, cannot create a journal
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = con.cursor()

    def q(label, sql):
        print(f"--- {label} ---")
        try:
            cur.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
            if cols:
                print(" | ".join(cols))
            for r in rows:
                print(" | ".join("NULL" if v is None else str(v) for v in r))
            if not rows:
                print("(no rows)")
        except Exception as e:
            print(f"FAILED: {type(e).__name__}: {e}")
        print()

    # sanity: what tables and columns actually exist
    q("TABLES", "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    for t in ("match_history", "match_feedback"):
        q(f"SCHEMA {t}", f"SELECT sql FROM sqlite_master WHERE name='{t}'")

    # 1. total scans recorded
    q("1. TOTAL SCANS", "SELECT COUNT(*) AS total_scans FROM match_history")

    # 2. total labels
    q("2. TOTAL LABELS (non-test)",
      "SELECT COUNT(*) AS labels FROM match_feedback WHERE is_test = 0")

    # 3. THE NUMBER - precision@1, precision@5, miss rate
    q("3. LABEL BREAKDOWN BY RANK",
      """SELECT confirmed_rank, verdict, COUNT(*) AS n
         FROM match_feedback
         WHERE is_test = 0
         GROUP BY confirmed_rank, verdict
         ORDER BY confirmed_rank""")

    q("3b. HEADLINE PRECISION",
      """SELECT
           COUNT(*) AS labelled,
           SUM(CASE WHEN verdict='correct' AND confirmed_rank=1
                    THEN 1 ELSE 0 END) AS rank1_correct,
           SUM(CASE WHEN verdict='correct' AND confirmed_rank BETWEEN 1 AND 5
                    THEN 1 ELSE 0 END) AS in_top5,
           SUM(CASE WHEN confirmed_rank=0 OR verdict='incorrect'
                    THEN 1 ELSE 0 END) AS complete_miss
         FROM match_feedback WHERE is_test = 0""")

    # 4. is the labelled subset representative?
    q("4. SCORE BIAS CHECK",
      """SELECT
           (SELECT ROUND(AVG(top_score),4) FROM match_history) AS all_scans,
           (SELECT ROUND(AVG(h.top_score),4)
              FROM match_history h
              JOIN match_feedback f ON f.query_filename = h.query_filename
             WHERE f.is_test = 0) AS labelled_only""")

    # 5. free signal across ALL scans, no labels needed
    q("5. LOW_CERT DISTRIBUTION (all scans)",
      "SELECT low_cert, COUNT(*) AS n FROM match_history GROUP BY low_cert")

    # bonus: join yield + date range
    q("6. JOIN YIELD",
      """SELECT COUNT(*) AS joinable
         FROM match_feedback f
         JOIN match_history h ON h.query_filename = f.query_filename
        WHERE f.is_test = 0""")

    q("7. DATE RANGE",
      """SELECT MIN(matched_at) AS first_scan,
                MAX(matched_at) AS last_scan FROM match_history""")

    # ── Join integrity: the post-stratified figure is only meaningful if
    #    query_filename is a unique key. If filenames are reused, the join
    #    fans out and every derived precision number is garbage. ──
    q("0a. JOIN KEY UNIQUENESS — match_history",
      """SELECT COUNT(*) AS rows,
                COUNT(DISTINCT query_filename) AS distinct_fn,
                SUM(CASE WHEN query_filename IS NULL OR query_filename=''
                         THEN 1 ELSE 0 END) AS blank_fn
         FROM match_history""")

    q("0b. JOIN KEY UNIQUENESS — match_feedback (non-test)",
      """SELECT COUNT(*) AS rows,
                COUNT(DISTINCT query_filename) AS distinct_fn,
                SUM(CASE WHEN query_filename IS NULL OR query_filename=''
                         THEN 1 ELSE 0 END) AS blank_fn
         FROM match_feedback WHERE is_test=0""")

    q("0c. WORST FILENAME COLLISIONS in match_history",
      """SELECT query_filename, COUNT(*) AS n FROM match_history
         GROUP BY query_filename HAVING n > 1 ORDER BY n DESC LIMIT 5""")

    q("0d. ROW-FANOUT: does the join multiply rows?",
      """SELECT COUNT(*) AS join_rows
         FROM match_feedback f
         JOIN match_history h ON h.query_filename = f.query_filename
        WHERE f.is_test = 0""")

    # ── Post-stratification: bound true precision, don't leave it unknown ──
    BAND_SQL = ("CASE WHEN {c} >= 0.95 THEN '1_0.95+' "
                "WHEN {c} >= 0.90 THEN '2_0.90-0.95' "
                "WHEN {c} >= 0.85 THEN '3_0.85-0.90' "
                "ELSE '4_below_0.85' END")

    q("A. PRECISION WITHIN SCORE BANDS (labelled, joinable)",
      f"""SELECT {BAND_SQL.format(c='h.top_score')} AS band,
                 COUNT(*) AS labelled,
                 SUM(CASE WHEN f.verdict='correct' AND f.confirmed_rank=1
                          THEN 1 ELSE 0 END) AS rank1_correct
          FROM match_feedback f
          JOIN match_history h ON h.query_filename = f.query_filename
         WHERE f.is_test = 0
         GROUP BY band ORDER BY band""")

    q("B. POPULATION DISTRIBUTION ACROSS SAME BANDS",
      f"""SELECT {BAND_SQL.format(c='top_score')} AS band, COUNT(*) AS n
          FROM match_history GROUP BY band ORDER BY band""")

    q("C. THE CLEANEST SINGLE CUT (low_cert)",
      """SELECT h.low_cert,
                COUNT(*) AS labelled,
                SUM(CASE WHEN f.verdict='correct' AND f.confirmed_rank=1
                         THEN 1 ELSE 0 END) AS rank1_correct
         FROM match_feedback f
         JOIN match_history h ON h.query_filename = f.query_filename
        WHERE f.is_test = 0
        GROUP BY h.low_cert""")

    # Reweighted estimate, computed here so the arithmetic is auditable
    print("--- D. POST-STRATIFIED PRECISION@1 ESTIMATE ---")
    try:
        cur.execute(f"""SELECT {BAND_SQL.format(c='h.top_score')} AS band,
                        COUNT(*), SUM(CASE WHEN f.verdict='correct'
                                      AND f.confirmed_rank=1 THEN 1 ELSE 0 END)
                        FROM match_feedback f
                        JOIN match_history h ON h.query_filename=f.query_filename
                        WHERE f.is_test=0 GROUP BY band""")
        lab = {b: (n, c) for b, n, c in cur.fetchall()}
        cur.execute(f"""SELECT {BAND_SQL.format(c='top_score')} AS band, COUNT(*)
                        FROM match_history GROUP BY band""")
        pop = dict(cur.fetchall())
        pop_total = sum(pop.values())
        print(f"{'band':<14}{'pop_share':>10}{'labelled':>10}{'band_P@1':>10}{'contrib':>10}")
        est, covered = 0.0, 0
        for band in sorted(pop):
            share = pop[band] / pop_total
            n, corr = lab.get(band, (0, 0))
            if n:
                p = corr / n
                est += share * p
                covered += pop[band]
                print(f"{band:<14}{share:>10.3f}{n:>10}{p:>10.3f}{share*p:>10.4f}")
            else:
                print(f"{band:<14}{share:>10.3f}{0:>10}{'NO DATA':>10}{'--':>10}")
        print(f"\n  reweighted P@1 over covered bands only : {est:.4f}")
        print(f"  population coverage of those bands      : {covered/pop_total:.1%}")
        print("  NOTE: bands with NO DATA are unmeasured — the reweighted figure")
        print("        applies only to the covered share, so treat it as an upper")
        print("        bound on true precision, not a point estimate.")
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
    print()

    con.close()

    # ── True scan total: match_history undercounts (see /api/v1/match) ──
    print("--- E. TRUE SCAN TOTAL RECONCILIATION ---")
    import json as _json
    try:
        sp = "/modal_data/stats.json"
        if os.path.exists(sp):
            s = _json.load(open(sp, encoding="utf-8"))
            print(f"  stats.json total_scans : {s.get('total_scans')}")
            print(f"  stats.json today_scans : {s.get('today_scans')} ({s.get('today_date')})")
        else:
            print("  stats.json: not found")
    except Exception as e:
        print(f"  stats.json FAILED: {type(e).__name__}: {e}")

    for dname, keys in (("scan-source-counters", ("modal", "ondevice")),):
        try:
            d = modal.Dict.from_name(dname, create_if_missing=True)
            print(f"  Dict {dname!r}: " +
                  ", ".join(f"{k}={d.get(k, 0)}" for k in keys))
        except Exception as e:
            print(f"  Dict {dname!r} FAILED: {type(e).__name__}: {e}")

    for dname in ("scan-counters", "sku-scan-freq"):
        try:
            d = modal.Dict.from_name(dname, create_if_missing=True)
            n = 0
            tot = 0
            for _k, v in d.items():          # modal.Dict has no len()
                n += 1
                if isinstance(v, (int, float)):
                    tot += v
                elif isinstance(v, dict):
                    for _vk, vv in v.items():
                        if isinstance(vv, (int, float)):
                            tot += vv
            print(f"  Dict {dname!r}: {n} keys, numeric-value sum={tot}")
        except Exception as e:
            print(f"  Dict {dname!r} FAILED: {type(e).__name__}: {e}")
    print()

    # ── F. IDENTITY / USER COUNT — the decisive unknown ──
    print("--- F1. IDENTITY STORES (volume JSON) ---")

    def _peek(path, label):
        try:
            if not os.path.exists(path):
                print(f"  {label:<24} NOT PRESENT")
                return None
            d = _json.load(open(path, encoding="utf-8"))
            if isinstance(d, dict):
                print(f"  {label:<24} dict, {len(d)} top-level keys")
            elif isinstance(d, list):
                print(f"  {label:<24} list, {len(d)} entries")
            else:
                print(f"  {label:<24} {type(d).__name__}")
            return d
        except Exception as e:
            print(f"  {label:<24} FAILED: {type(e).__name__}: {e}")
            return None

    subs = _peek("/modal_data/subscriptions.json", "subscriptions.json")
    cols = _peek("/modal_data/collections.json", "collections.json")
    push = _peek("/modal_data/push_subscriptions.json", "push_subscriptions.json")
    refs = _peek("/modal_data/referrals.json", "referrals.json")
    print()

    print("--- F2. SUBSCRIPTIONS BY STATUS/TIER (all statuses incl. expired) ---")
    if isinstance(subs, dict):
        from collections import Counter
        st = Counter(); ti = Counter()
        for _k, v in subs.items():
            if isinstance(v, dict):
                st[str(v.get("status"))] += 1
                ti[str(v.get("tier"))] += 1
        print(f"  total identities: {len(subs)}")
        print(f"  by status: {dict(st)}")
        print(f"  by tier  : {dict(ti)}")
    print()

    print("--- F3. COLLECTIONS: identities holding a NONZERO collection ---")
    if isinstance(cols, dict):
        nonzero = 0
        sizes = []
        for _k, v in cols.items():
            n = len(v) if isinstance(v, (list, dict)) else 0
            if n > 0:
                nonzero += 1
                sizes.append(n)
        sizes.sort()
        print(f"  identities with a collection key : {len(cols)}")
        print(f"  identities with >=1 saved card   : {nonzero}")
        if sizes:
            print(f"  cards saved: min={sizes[0]} median={sizes[len(sizes)//2]} max={sizes[-1]} total={sum(sizes)}")
    print()

    print("--- F4. PUSH / REFERRALS ---")
    if isinstance(push, (dict, list)):
        print(f"  push subscription entries : {len(push)}")
    if isinstance(refs, dict):
        print(f"  referrals top-level keys  : {len(refs)}")
    print()

    print("--- F5. scan-counters Dict: key shape (identifiers truncated) ---")
    try:
        d = modal.Dict.from_name("scan-counters", create_if_missing=True)
        for k, v in d.items():
            ks = str(k)
            ks = ks if len(ks) <= 28 else ks[:14] + "..." + ks[-8:]
            if isinstance(v, dict):
                print(f"  {ks:<32} dict keys={sorted(v.keys())[:6]}")
            else:
                print(f"  {ks:<32} {type(v).__name__}={v}")
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
    print()

    print("--- F6. query/ dir: file count + distinct filename prefixes ---")
    try:
        qd = "/modal_data/query"
        if os.path.isdir(qd):
            names = os.listdir(qd)
            print(f"  files in query/: {len(names)}")
            print(f"  sample names   : {names[:5]}")
            import re as _re
            prefixes = set()
            for n in names:
                m = _re.match(r"^([A-Za-z0-9]+?)[-_.]", n)
                prefixes.add(m.group(1) if m else n)
            print(f"  distinct prefixes: {len(prefixes)}")
        else:
            print("  query/ not present")
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
    print()

    # ── G. TEMPORAL PATTERN: burst-testing vs organic spread ──
    con2 = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c2 = con2.cursor()
    print("--- G1. DAILY SCAN PATTERN (summary) ---")
    try:
        c2.execute("""SELECT date(matched_at) AS d, COUNT(*) n
                      FROM match_history GROUP BY d ORDER BY d""")
        days = c2.fetchall()
        counts = sorted(n for _d, n in days)
        print(f"  distinct days with >=1 scan : {len(days)}")
        print(f"  calendar span               : {days[0][0]} -> {days[-1][0]}")
        print(f"  scans/day  min={counts[0]} median={counts[len(counts)//2]} max={counts[-1]}")
        top = sorted(days, key=lambda r: -r[1])[:8]
        print(f"  busiest days: {top}")
        heavy = sum(n for _d, n in top)
        print(f"  share of all scans in those 8 days: {heavy/sum(counts):.1%}")
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
    print()

    print("--- G3. FULL DAILY SERIES (every day with >=1 scan) ---")
    try:
        c2.execute("""SELECT date(matched_at) AS d, COUNT(*) n
                      FROM match_history GROUP BY d ORDER BY d""")
        rows = c2.fetchall()
        run = 0
        total = sum(n for _d, n in rows)
        for d, n in rows:
            run += n
            print(f"  {d}  {n:>4}   cum={run:>4} ({run/total:>5.1%})")
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
    print()

    # Play release history (from Play Console):
    #   v1.0.0   28 May 2026 19:45  <- track first open to testers
    #   v1.0.0.2 10 Jun 13:49 | v1.0.0.3 11 Jun 21:16 | v1.0.0.6 12 Jun 00:12
    #   v1.0.0.7 23 Jun 00:38 | v1.0.0.8  4 Jul 00:33 | current 26 Jul 10:35
    PLAY_VERSIONS = [
        ("2026-05-28", "v1.0.0   track opens"),
        ("2026-06-10", "v1.0.0.2"),
        ("2026-06-11", "v1.0.0.3"),
        ("2026-06-12", "v1.0.0.6"),
        ("2026-06-23", "v1.0.0.7"),
        ("2026-07-04", "v1.0.0.8"),
        ("2026-07-26", "current build"),
    ]
    print("--- J. SCANS SINCE EACH PLAY RELEASE ---")
    try:
        for anchor, label in PLAY_VERSIONS:
            c2.execute("""SELECT COUNT(*), COUNT(DISTINCT date(matched_at))
                          FROM match_history WHERE matched_at >= ?""", (anchor,))
            n, days = c2.fetchone()
            print(f"  since {anchor} ({label:<18}) scans={n:<5} active_days={days}")
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
    print()

    print("--- J2. DAILY SERIES SINCE TRACK OPENED (2026-05-28) ---")
    try:
        c2.execute("""SELECT date(matched_at), COUNT(*)
                      FROM match_history
                      WHERE matched_at >= '2026-05-28'
                      GROUP BY 1 ORDER BY 1""")
        rows = c2.fetchall()
        marks = {a: l for a, l in PLAY_VERSIONS}
        tot = sum(n for _d, n in rows)
        for d, n in rows:
            m = f"   <- {marks[d]}" if d in marks else ""
            print(f"  {d}  {n:>4}{m}")
        print(f"  TOTAL since track opened: {tot}")
        spikes = sorted((n for _d, n in rows), reverse=True)[:3]
        print(f"  top-3 days = {sum(spikes)} of {tot} ({sum(spikes)/tot:.1%})")
        print(f"  remainder  = {tot - sum(spikes)} over {len(rows)-3} days "
              f"({(tot-sum(spikes))/(len(rows)-3):.1f}/active day)")
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
    print()

    print("--- G2. HOUR-OF-DAY HISTOGRAM (UTC) ---")
    try:
        c2.execute("""SELECT strftime('%H', matched_at) h, COUNT(*) n
                      FROM match_history GROUP BY h ORDER BY h""")
        for h, n in c2.fetchall():
            print(f"  {h}:00  {'#' * min(60, n)} {n}")
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
    con2.close()
    print()

    print("--- H. PAID vs COMPED: does any sub have a real Stripe id? ---")
    try:
        s = _json.load(open("/modal_data/subscriptions.json", encoding="utf-8"))
        paid = comped = 0
        emails = set()
        created = []
        for k, v in s.items():
            if not isinstance(v, dict):
                continue
            sid = v.get("stripe_subscription_id")
            if sid:
                paid += 1
            else:
                comped += 1
            if v.get("email"):
                emails.add(str(v["email"]).strip().lower())
            if v.get("created_at"):
                created.append(str(v["created_at"])[:10])
        print(f"  with stripe_subscription_id : {paid}")
        print(f"  without (comp/access code)  : {comped}")
        print(f"  distinct emails             : {len(emails)}")
        created.sort()
        if created:
            print(f"  created_at range            : {created[0]} -> {created[-1]}")
            from collections import Counter
            print(f"  signups by month            : {dict(sorted(Counter(c[:7] for c in created).items()))}")
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
    print()

    print("--- I. collections.json key shape (identity type) ---")
    try:
        c = _json.load(open("/modal_data/collections.json", encoding="utf-8"))
        for k, v in c.items():
            ks = str(k)
            ks = ks if len(ks) <= 26 else ks[:12] + "..." + ks[-8:]
            n = len(v) if isinstance(v, (list, dict)) else 0
            print(f"  {ks:<30} {n} cards")
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
    print()

    print("Census complete. Nothing was written.")


@app.local_entrypoint()
def main():
    census.remote()
