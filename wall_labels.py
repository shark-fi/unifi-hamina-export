#!/usr/bin/env python3
"""wall_labels.py — check our wall-type spellings against a real Hamina export.

Hamina drops any wall_type it does not recognise, silently, and it matches on the
strings ITS OWN exporter writes — not the display names in its wall-type picker.
Guessing from the picker cost a 122-wall plan 109 of its walls. So a label is
verified only by finding it in an export.

    # in Hamina: draw one wall of each type you want to confirm, export OpenIntent
    python3 wall_labels.py hamina-export.zip

Reports three things:
  * labels we emit that this export confirms      -> safe
  * labels Hamina uses that we never emit         -> we cannot round-trip them
  * labels we emit that no export has shown       -> still a guess

Read-only. Standard library only.
"""
import collections
import json
import sys
import zipfile

from unifi_export import WALL_VARIANTS
from openintent_import import WALL_LABEL_TO_VARIANT


def labels_in(path):
    """wall_type -> count, over every floorplan in an OpenIntent zip."""
    counts = collections.Counter()
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not name.endswith(".json"):
                continue
            def walk(o):
                if isinstance(o, dict):
                    if "wall_type" in o:
                        counts[o["wall_type"]] += 1
                    for v in o.values():
                        walk(v)
                elif isinstance(o, list):
                    for v in o:
                        walk(v)
            walk(json.loads(z.read(name)))
    return counts


def classify(seen):
    """(confirmed, theirs_only, unverified) given the labels an export contains.

    `theirs_only` is the interesting one: a material Hamina uses that we never
    produce means a wall survives the trip but comes back as something else.
    """
    ours = {label for label, _att in WALL_VARIANTS.values()}
    confirmed = sorted(l for l in seen if l in ours)
    theirs_only = sorted(l for l in seen if l not in ours)
    unverified = sorted(ours - set(seen))
    return confirmed, theirs_only, unverified


def main(argv=None):
    argv = argv or sys.argv[1:]
    if not argv:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    seen = labels_in(argv[0])
    if not seen:
        print("no wall segments in %s — draw some walls before exporting" % argv[0],
              file=sys.stderr)
        return 1
    confirmed, theirs_only, unverified = classify(seen)

    print("\n%d wall(s), %d distinct label(s) in %s\n" % (sum(seen.values()), len(seen), argv[0]))
    for label in confirmed:
        print("  CONFIRMED   %-24s %4d  we spell it identically" % (label, seen[label]))
    for label in theirs_only:
        known = "" if label in WALL_LABEL_TO_VARIANT else "  (and we cannot import it)"
        print("  THEIRS      %-24s %4d  Hamina uses this, we never emit it%s"
              % (label, seen[label], known))
    if unverified:
        print("\n  still unverified — no export has contained these, so each is a"
              "\n  guess that may be silently dropping walls:")
        for label in unverified:
            print("    %s" % label)
    print("\nAdd a label to WALL_VARIANTS only from output like this, never from"
          "\nHamina's picker: the picker shows display names, the importer matches"
          "\nthe export's names, and they are not the same strings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
