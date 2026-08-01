"""
PokeWallet 403 diagnostic — establish the REAL cause.
Read-only: makes GET requests only, never writes to the volume.
Throwaway. Run:  $env:PYTHONIOENCODING="utf-8"; modal run pw_403_diag.py
"""
import modal

app = modal.App("grailsweep-pw-403-diag")
vol = modal.Volume.from_name("matchit-data-v2")
image = modal.Image.debian_slim().pip_install("requests")

BASE = "https://api.pokewallet.io"
SWSH_SETS = {
    "2781": "swsh45sv  Shining Fates: Shiny Vault",
    "3020": "swsh9tg   Brilliant Stars Trainer Gallery",
    "17689": "swsh12pt5gg Crown Zenith: Galarian Gallery",
}


@app.function(
    image=image,
    volumes={"/modal_data": vol},
    secrets=[modal.Secret.from_name("pokewallet-credentials")],
    timeout=600,
)
def diag():
    import os, json, requests

    key = os.environ.get("POKEWALLET_API_KEY", "")
    print("=" * 72)
    print("KEY: present=%s  len=%d  prefix=%r" % (bool(key), len(key), key[:4]))
    print("=" * 72)

    H = {"X-API-Key": key}

    def show(label, url, headers=H):
        print("\n--- %s ---" % label)
        print("  GET %s" % url)
        try:
            r = requests.get(url, headers=headers, timeout=20)
        except Exception as e:
            print("  EXCEPTION: %s: %s" % (type(e).__name__, e))
            return None
        print("  HTTP %s" % r.status_code)
        interesting = ("www-authenticate", "x-plan", "x-plan-required", "x-ratelimit",
                       "x-ratelimit-limit", "x-ratelimit-remaining", "retry-after",
                       "content-type", "x-request-id", "server")
        for k, v in r.headers.items():
            if any(k.lower().startswith(p) for p in interesting):
                print("    %s: %s" % (k, v))
        body = r.text or ""
        print("  BODY (first 600 chars):")
        print("    " + (body[:600].replace("\n", "\n    ") if body else "(empty)"))
        return r

    # ── 1. the failing SWSH set ────────────────────────────────────────────
    print("\n" + "#" * 72)
    print("# 1. SWSH SETS (the ones reported 403)")
    print("#" * 72)
    for sid, name in SWSH_SETS.items():
        show("SWSH %s  (%s)" % (sid, name), "%s/prices/%s" % (BASE, sid))

    # ── 2. what CAN this key see? enumerate sets ───────────────────────────
    print("\n" + "#" * 72)
    print("# 2. /sets — what does the key have access to?")
    print("#" * 72)
    r = show("list sets", "%s/sets" % BASE)
    normal_ids = []
    if r is not None and r.status_code == 200:
        try:
            data = r.json()
            rows = data.get("data") if isinstance(data, dict) else data
            if isinstance(rows, list):
                print("\n  total sets returned: %d" % len(rows))
                swsh_listed = [x for x in rows
                               if str(x.get("id")) in SWSH_SETS]
                print("  are the SWSH ids present in the list? %s"
                      % ([str(x.get('id')) for x in swsh_listed] or "NO"))
                for x in rows[:12]:
                    print("    id=%-8s name=%r" % (x.get("id"), x.get("name")))
                # pick non-SWSH ids for the comparison test
                normal_ids = [str(x.get("id")) for x in rows
                              if str(x.get("id")) not in SWSH_SETS][:3]
        except Exception as e:
            print("  (could not parse set list: %s)" % e)

    # ── 3. NON-SWSH comparison — auth problem vs set-gating ────────────────
    print("\n" + "#" * 72)
    print("# 3. NON-SWSH SETS (comparison — THE decisive test)")
    print("#" * 72)
    if not normal_ids:
        print("  set list unavailable; trying a few plausible ids blind")
        normal_ids = ["1", "100", "1000"]
    for sid in normal_ids:
        show("normal set %s" % sid, "%s/prices/%s" % (BASE, sid))

    # ── 4. auth-shape probes: is it the key or the resource? ───────────────
    print("\n" + "#" * 72)
    print("# 4. AUTH-SHAPE PROBES")
    print("#" * 72)
    show("no key at all (expect 401 if auth works normally)",
         "%s/prices/2781" % BASE, headers={})
    show("deliberately WRONG key (expect 401/403 — compare to real-key 403)",
         "%s/prices/2781" % BASE, headers={"X-API-Key": "definitely-not-valid"})
    show("Bearer instead of X-API-Key (is the header name right?)",
         "%s/prices/2781" % BASE, headers={"Authorization": "Bearer %s" % key})

    # ── 5. timeline: when were the 282 last successfully priced? ───────────
    print("\n" + "#" * 72)
    print("# 5. TIMELINE — last successful PokeWallet write per set")
    print("#" * 72)
    root = "/modal_data/CardsDB/pokemon"
    from collections import Counter
    for prefix in ("swsh45sv", "swsh9tg", "swsh11tg", "swsh12tg", "swsh12pt5gg"):
        stamps = Counter()
        n = 0
        try:
            for f in os.listdir(root):
                if not f.startswith(prefix + "-"):
                    continue
                p = os.path.join(root, f, "profile.json")
                if not os.path.isfile(p):
                    continue
                try:
                    prof = json.load(open(p, encoding="utf-8"))
                except Exception:
                    continue
                n += 1
                stamps[str(prof.get("prices_updated"))[:10]] += 1
        except Exception as e:
            print("  %s: %s" % (prefix, e))
            continue
        print("  %-12s cards=%-4d prices_updated dates: %s"
              % (prefix, n, dict(stamps)))

    print("\nDiagnostic complete. GET requests only — nothing written.")


@app.local_entrypoint()
def main():
    diag.remote()
