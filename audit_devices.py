#!/usr/bin/env python3
"""Read-only audit of the device shapes in an InnerSpace project.

Answers the question the console UI will not: for every AP shown on a floor
plan, is that MAC still an adopted device, and is it on more than one plan?

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

    shapes = [s for s in (proj.get("shapes") or []) if s.get("type") == "device"]
    by_mac = defaultdict(list)
    for s in shapes:
        by_mac[norm((s.get("meta") or {}).get("mac"))].append(s)

    print("\n%d device shape(s) across %d plan(s); %d adopted device(s) on %d site(s)\n"
          % (len(shapes), len(plans), len(adopted), len(sites)))

    hdr = "%-26s %-14s %-26s %s" % ("SHAPE TITLE", "MAC", "PLAN", "STATUS")
    print(hdr); print("-" * len(hdr))
    orphans, dupes = [], []
    for s in sorted(shapes, key=lambda x: (plans.get(x.get("planId"), ""),
                                           x.get("title") or "")):
        mac = norm((s.get("meta") or {}).get("mac"))
        plan = plans.get(s.get("planId"), s.get("planId") or "(no plan)")
        if not mac:
            status = "NO MAC"
        elif _is_placeholder_mac(mac):
            status = "synthesized placeholder"
        elif mac not in adopted:
            status = "NOT ADOPTED -> orphan"
            orphans.append(s)
        else:
            site, name, typ, model = adopted[mac]
            status = "adopted (%s, %s)" % (model or typ, site)
            if len(by_mac[mac]) > 1:
                status += "  ** on %d plans **" % len(by_mac[mac])
                dupes.append(s)
        print("%-26s %-14s %-26s %s"
              % ((s.get("title") or "(untitled)")[:26], mac or "-", plan[:26], status))

    print()
    if orphans:
        print("%d shape(s) reference a MAC no site has adopted — most likely a "
              "replaced or removed device still sitting on the plan." % len(orphans))
    if dupes:
        print("%d shape(s) place the same real device on more than one plan."
              % len(dupes))
    if not orphans and not dupes:
        print("Every shape maps to exactly one adopted device. Nothing stale here.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
