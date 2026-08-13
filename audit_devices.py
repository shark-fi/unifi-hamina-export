#!/usr/bin/env python3
"""Read-only audit of the device shapes in an InnerSpace project.

Answers what the console UI will not: which device shapes exist, which plan
each one belongs to, and which of them are redundant.

What this can and cannot see. The adopted-device list comes from the NETWORK
app only. Protect cameras, Access readers and plain clients are not in it and
never will be, so "not in the Network app" is the normal state for a large part
of a real project -- on the console this was written against, 39 of 55 shapes.
It is not evidence of anything on its own. An earlier version of this script
called them all orphans, which would have meant deleting forty legitimate
entries had anyone acted on it.

The one signal that IS actionable: the same MAC appearing twice, once placed on
a live plan and once stranded without one. That second copy is redundant, and
`openintent_import.py --purge-placeholders` removes exactly those.

Writes nothing. Run it from the unifi-hamina-export checkout:

    python3 audit_devices.py --host https://192.168.5.254 --username marko
"""
import argparse
import getpass
import sys
from collections import defaultdict

from unifi_export import Http, legacy_login
from openintent_import import (
    INNERSPACE_API, NETWORK_API, apply_csrf, _is_placeholder_mac,
    classify_device_shapes,
)


def norm(mac):
    return (mac or "").replace(":", "").replace("-", "").upper()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", required=True)
    p.add_argument("--username", required=True)
    p.add_argument("--password")
    p.add_argument("--site", default="default")
    p.add_argument("--verify-tls", action="store_true", default=False)
    a = p.parse_args()

    base = a.host.rstrip("/")
    http_ = Http(verify=a.verify_tls)
    pw = a.password or getpass.getpass("password for %s: " % a.username)
    legacy_login(http_, base, a.username, pw)
    apply_csrf(http_)

    proj = http_.get_json("%s%s/project?mode=2D" % (base, INNERSPACE_API)).get("data", {})
    plans = {pl.get("id"): (pl.get("title") or pl.get("id"))
             for pl in proj.get("plans") or []}

    # every device the Network app currently has adopted, on every site
    adopted = {}
    try:
        sites = [s.get("name") for s in
                 http_.get_json("%s/api/self/sites" % base).get("data") or []]
    except Exception:
        sites = [a.site]
    for site in filter(None, sites):
        try:
            devs = http_.get_json("%s%s/s/%s/stat/device"
                                  % (base, NETWORK_API, site)).get("data") or []
        except Exception as e:
            print("  (site %s unreadable: %s)" % (site, e))
            continue
        for d in devs:
            if norm(d.get("mac")):
                adopted[norm(d.get("mac"))] = (
                    site, d.get("name") or "", d.get("type") or "", d.get("model") or "")

    # The same rule the purge applies, imported rather than reimplemented, so
    # the two can never report different sets for one project. When they DID
    # differ it was the project changing between runs -- worth being able to
    # conclude that immediately instead of suspecting both tools.
    removable, _kept, _total = classify_device_shapes(proj)
    why_by_id = {id(sh): why for sh, why in removable}

    shapes = [s for s in (proj.get("shapes") or []) if s.get("type") == "device"]
    by_mac = defaultdict(list)
    for s in shapes:
        by_mac[norm((s.get("meta") or {}).get("mac"))].append(s)

    print("\n%d device shape(s) across %d plan(s); %d adopted device(s) on %d "
          "site(s); %d removable\n"
          % (len(shapes), len(plans), len(adopted), len(sites), len(removable)))

    hdr = "%-26s %-14s %-26s %s" % ("SHAPE TITLE", "MAC", "PLAN", "STATUS")
    print(hdr); print("-" * len(hdr))
    unknown, dupes = [], []
    for s in sorted(shapes, key=lambda x: (plans.get(x.get("planId"), ""),
                                           x.get("title") or "")):
        mac = norm((s.get("meta") or {}).get("mac"))
        plan = plans.get(s.get("planId"), s.get("planId") or "(no plan)")
        if not mac:
            status = "NO MAC"
        elif _is_placeholder_mac(mac):
            status = "synthesized placeholder"
        elif mac not in adopted:
            # NOT a fault. The Network app does not know about Protect, Access
            # or client devices, and those are placed on floor plans routinely.
            status = "not a Network device (Protect/Access/client?)"
            unknown.append(s)
        else:
            site, name, typ, model = adopted[mac]
            status = "adopted (%s, %s)" % (model or typ, site)
            if id(s) in why_by_id:
                status += "   ** REMOVABLE: %s **" % why_by_id[id(s)]
                dupes.append(s)
        print("%-26s %-14s %-26s %s"
              % ((s.get("title") or "(untitled)")[:26], mac or "-", plan[:26], status))

    print()
    if unknown:
        print("%d shape(s) are not Network devices. Expected: Protect cameras, "
              "Access readers and clients live in other applications. A few "
              "may be genuinely removed hardware — this script cannot tell "
              "which, so it does not guess." % len(unknown))
    if removable:
        print("%d shape(s) are removable (%s). Remove with:\n"
              "  python3 openintent_import.py --purge-placeholders --host ... "
              "--username ...   (add --commit)"
              % (len(removable), ", ".join(sorted({w for _s, w in removable}))))
    else:
        print("Nothing removable. Every device is listed once.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
