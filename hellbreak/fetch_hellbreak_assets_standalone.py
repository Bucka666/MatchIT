"""
fetch_hellbreak_assets_standalone.py
------------------------------------
Downloads the publicly-published Hellbreak "Dawn of Terror" card images from the
official Shopify CDN. No manifest file needed - the card list is embedded below.

Local corpus for embedding generation / matcher R&D only.
Images are (C) NBCUniversal Media, LLC and Spin Master. Do NOT rehost or serve
them from images.grailsweep.com without written permission.

Run:
    python fetch_hellbreak_assets_standalone.py
    python fetch_hellbreak_assets_standalone.py --out "D:\\somewhere\\else"
    python fetch_hellbreak_assets_standalone.py --force
"""

import argparse
import os
import sys
import time
import urllib.request

BASE = "https://cdn.shopify.com/s/files/1/0713/2115/7771/files/"
DEFAULT_OUT = r"C:\CardsDB\hellbreak\dawn_of_terror"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# (filename, card_name)
CARDS = [
    ('Dracula_TT_Lurking.jpg', 'Dracula (Lurking)'),
    ('Dracula_TT_Unleashed.jpg', 'Dracula (Unleashed)'),
    ('Aleera_11d9656c-9c3b-46e4-80bb-bf6220b8d1df.jpg', 'Aleera'),
    ('AncientWisdom.jpg', 'Ancient Wisdom'),
    ('BloodsuckingBat.jpg', 'Bloodsucking Bat'),
    ('CarriageDriver.jpg', 'Carriage Driver'),
    ('CountAlucard.jpg', 'Count Alucard'),
    ('CountessZaleska.jpg', 'Countess Zaleska'),
    ('CarpathianWildcat.jpg', 'Carpathian Wildcat'),
    ('CovenFeast.jpg', 'Coven Feast'),
    ('DrainLife.jpg', 'Drain Life'),
    ('FerociousWolfpack.jpg', 'Ferocious Wolfpack'),
    ('LucyWeston.jpg', 'Lucy Weston'),
    ('Marishka.jpg', 'Marishka'),
    ('MinaSeward.jpg', 'Mina Seward'),
    ('RenField.jpg', 'Ren Field'),
    ('SwarmOfRats.jpg', 'Swarm Of Rats'),
    ('TransylvanianWolf.jpg', 'Transylvanian Wolf'),
    ('VampiresCoffin.jpg', 'Vampires Coffin'),
    ('Verona.jpg', 'Verona'),
    ('Jaws_Scourge_Lurking.jpg', 'Jaws (Lurking)'),
    ('Jaws_Scourge_Unleashed.jpg', 'Jaws (Unleashed)'),
    ('APanicOnOurHands.jpg', 'A Panic On Our Hands'),
    ('Barracuda.jpg', 'Barracuda'),
    ('DeputyHendricks.jpg', 'Deputy Hendricks'),
    ('GiantOctopus.jpg', 'Giant Octopus'),
    ('KillerWhale.jpg', 'Killer Whale'),
    ('LarryVaughn.jpg', 'Larry Vaughn'),
    ('ManOWar.jpg', 'Man O War'),
    ('NarrowEscape.jpg', 'Narrow Escape'),
    ('Orca.jpg', 'Orca'),
    ('RavenousPredator.jpg', 'Ravenous Predator'),
    ('RogueShark.jpg', 'Rogue Shark'),
    ('RoughtailStingray.jpg', 'Roughtail Stingray'),
    ('SharkInThePond.jpg', 'Shark In The Pond'),
    ('SharkSpotter.jpg', 'Shark Spotter'),
    ('ThreatFromBelow.jpg', 'Threat From Below'),
    ('VeteranHarpooner.jpg', 'Veteran Harpooner'),
    ('TheBride_KBL_Lurking.jpg', 'The Bride (Lurking)'),
    ('TheBride_KBL_Unleashed.jpg', 'The Bride (Unleashed)'),
    ('AngryMob.png', 'Angry Mob'),
    ('BlackWidow.jpg', 'Black Widow'),
    ('Bloodhound.jpg', 'Bloodhound'),
    ('BodySnatcher.jpg', 'Body Snatcher'),
    ('Cosmic_Ray_Diffuser.jpg', 'Cosmic Ray Diffuser'),
    ('DeathstalkerScorpion.jpg', 'Deathstalker Scorpion'),
    ('GothicGargoyle.jpg', 'Gothic Gargoyle'),
    ('Karl.jpg', 'Karl'),
    ('Kennelmaster.jpg', 'Kennelmaster'),
    ('Ludwig.jpg', 'Ludwig'),
    ('PiercingScream.jpg', 'Piercing Scream'),
    ('StrangeApparition.jpg', 'Strange Apparition'),
    ('HeadlessHorseman_Lurking.jpg', 'Headless Horseman Lurking'),
    ('HeadlessHorseman_Unleashed.jpg', 'Headless Horseman Unleashed'),
    ('Daredevil.jpg', 'Daredevil'),
    ('DarkAvenger.jpg', 'Dark Avenger'),
    ('Defenestration.jpg', 'Defenestration'),
    ('HoodedFigure.jpg', 'Hooded Figure'),
    ('Intimidate.jpg', 'Intimidate'),
    ('LadyVanTassel.jpg', 'Lady Van Tassel'),
    ('MalevolentMist.jpg', 'Malevolent Mist'),
    ('MurderBegetsMurder.jpg', 'Murder Begets Murder'),
    ('SpectralAssassin.jpg', 'Spectral Assassin'),
    ('TreeOfTheDead.jpg', 'Tree Of The Dead'),
    ('VillageRifleman.jpg', 'Village Rifleman'),
    ('WardingSign.jpg', 'Warding Sign'),
    ('CarfaxAbbey.jpg', 'Carfax Abbey'),
    ('LightningTower.jpg', 'Lightning Tower'),
]


def fetch(url, dest, retries=3):
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            if len(data) < 1024:
                raise ValueError("suspiciously small response (%d bytes)" % len(data))
            with open(dest, "wb") as f:
                f.write(data)
            return len(data)
        except Exception:
            if attempt == retries:
                raise
            time.sleep(1.5 * attempt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT, help="output directory")
    ap.add_argument("--force", action="store_true", help="re-download existing files")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    total = len(CARDS)
    ok = skipped = failed = 0
    failures = []

    for i, (filename, card_name) in enumerate(CARDS, 1):
        dest = os.path.join(args.out, filename)
        if os.path.exists(dest) and not args.force:
            skipped += 1
            continue
        try:
            size = fetch(BASE + filename, dest)
            ok += 1
            print("[%3d/%3d] %-34s %7.1f KB" % (i, total, card_name, size / 1024))
        except Exception as e:
            failed += 1
            failures.append(card_name)
            print("[%3d/%3d] FAILED %s: %s" % (i, total, card_name, e))
        time.sleep(0.25)

    print("")
    print("Done. downloaded=%d skipped=%d failed=%d" % (ok, skipped, failed))
    print("Output: %s" % args.out)
    if failures:
        print("Failed: " + ", ".join(failures))
        print("Re-run to retry - existing files are skipped.")
        sys.exit(1)


if __name__ == "__main__":
    main()