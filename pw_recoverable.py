"""
Read-only: how many of the 5 PokeWallet sets' cards actually hold a
recoverable market value in the CURRENT (old) on-disk shape?

Old shape written by _fetch_pokewallet_prices pre-fix:
    {"tcgplayer": {"Holofoil": {"marketPrice": 4.31, ...}}, "cardmarket": {}}

Counts case-insensitively across variant keys and accepts either
market/marketPrice, so it can't miss either shape.

Throwaway. Ephemeral app, no vol.commit(), never writes.
Run:  $env:PYTHONIOENCODING="utf-8"; modal run pw_recoverable.py
"""
import modal

app = modal.App("grailsweep-pw-recoverable")
vol = modal.Volume.from_name("matchit-data-v2")

PW_SETS = ["swsh45sv", "swsh9tg", "swsh11tg", "swsh12tg", "swsh12pt5gg"]
CARDS = "/modal_data/CardsDB/pokemon"


@app.function(volumes={"/modal_data": vol}, timeout=900)
def count():
    import os, json
    from collections import Counter

    def market_of(prof):
        """Return (value, variant_key, field_name) for any usable market
        price under either shape, else (None, None, None)."""
        pr = prof.get("prices")
        if not isinstance(pr, dict):
            return None, None, None
        tcg = pr.get("tcgplayer")
        if not isinstance(tcg, dict):
            return None, None, None
        for vk, vd in tcg.items():
            if not isinstance(vd, dict):
                continue
            for fname in ("market", "marketPrice"):
                v = vd.get(fname)
                if isinstance(v, (int, float)) and v > 0:
                    return float(v), vk, fname
        return None, None, None

    folders = os.listdir(CARDS)
    grand = Counter()
    shapes = Counter()
    values = []
    print(f"{'set':<14}{'cards':>7}{'HAS_VALUE':>11}{'EMPTY':>8}   variant/field seen")
    for sid in PW_SETS:
        prefix = sid + "-"
        mine = [f for f in folders if f.startswith(prefix)]
        has = empty = 0
        seen = Counter()
        for f in mine:
            p = os.path.join(CARDS, f, "profile.json")
            try:
                prof = json.load(open(p, encoding="utf-8"))
            except Exception:
                empty += 1
                continue
            v, vk, fn = market_of(prof)
            if v is not None:
                has += 1
                seen[f"{vk}/{fn}"] += 1
                shapes[f"{vk}/{fn}"] += 1
                values.append((v, f))
            else:
                empty += 1
        grand["cards"] += len(mine)
        grand["has"] += has
        grand["empty"] += empty
        print(f"{sid:<14}{len(mine):>7}{has:>11}{empty:>8}   {dict(seen)}")

    print()
    print(f"{'TOTAL':<14}{grand['cards']:>7}{grand['has']:>11}{grand['empty']:>8}")
    print()
    print(f"  => {grand['has']} of {grand['cards']} hold recoverable prices, "
          f"{grand['empty']} genuinely empty.")
    print()
    print("  shape breakdown across all matched cards:", dict(shapes))
    if values:
        values.sort(reverse=True)
        tot = sum(v for v, _ in values)
        print(f"  market value: min=${values[-1][0]:.2f} max=${values[0][0]:.2f} "
              f"sum=${tot:,.2f} mean=${tot/len(values):.2f}")
        print("  top 8 by value:")
        for v, f in values[:8]:
            print(f"     {f:<22} ${v:,.2f}")
    print()
    print("Read-only. No vol.commit(), nothing written.")


@app.local_entrypoint()
def main():
    count.remote()
