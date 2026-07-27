# eduroam identities in Palo Alto policy

How to get `UNI\jdoe` — not `10.20.30.40` — into your firewall rules, source
of truth being FreeRADIUS.

```
  client ──802.1X──▶ UniFi AP ──RADIUS──▶ FreeRADIUS
                                              │ accounting (Start/Interim/Stop)
                                              ▼
                                    detail file  or  UDP 1813
                                              │
                                     radius_userid.py
                                              │ User-ID XML API (HTTPS)
                                              ▼
                                     PAN-OS  ip → user table
                                              │
                                     security policy, logs, reports
```

The firewall never sees an 802.1X exchange, so it cannot learn who is behind
an address on its own. RADIUS accounting is the only place that knows, and
PAN-OS accepts exactly that kind of feed through the User-ID XML API.
`radius_userid.py` is the piece in between.

## The identity problem, stated honestly

On eduroam there are two different questions, and they have different
answers.

**Your own users (you are the IdP).** With PEAP or TTLS the supplicant sends
an anonymous outer identity — `anonymous@uni.fi` — and only reveals the real
username inside the TLS tunnel. Your FreeRADIUS sees it; the AP does not; so
plain accounting carries the anonymous name. Fixable, and the fix is in
[`freeradius/sites-available/eduroam-userid.conf`](../freeradius/sites-available/eduroam-userid.conf):
copy the inner name into a `Class` attribute in the Access-Accept. RFC 2865
requires the NAS to echo `Class` back in every accounting packet for that
session, so the real identity returns to you untouched, without changing what
the NAS thinks the username is.

**Visitors from other institutions (you are the SP).** Their authentication
is proxied to their home IdP. Nobody on your network — not FreeRADIUS, not
the firewall, not you — is told who they are. That is not a limitation to
work around; it is what eduroam is. Identity disclosure happens after the
fact, through the federation's abuse process, not in real time. What you
*can* know is the realm, because it is the routing information in the outer
identity. So the bridge maps a visitor to a per-realm pseudo-user
(`eduroam-visitor@fh-koeln.de`) and, optionally, tags them for a dynamic user
group. Policy on "visitors from realm X", yes. Policy on a named visitor, no.

| Who | What the firewall can match |
|---|---|
| Your users, PEAP/TTLS, with the `Class` config | the real username |
| Your users, EAP-TLS or non-tunnelled | the real username, no extra config |
| Inbound eduroam roamers | their realm only |
| MAC-authenticated devices | skipped by default (`--mac-users` to keep) |

## Prerequisites

- FreeRADIUS 3.0 or 3.2 doing accounting for the wireless NAS.
- PAN-OS 9.x–11.x. Any version with the User-ID XML API works; the `timeout`
  attribute used here has been there since PAN-OS 5.0.
- A host that can reach the firewall's management interface over HTTPS —
  the FreeRADIUS server itself is the obvious one.
- Python 3.7+. No packages.

## 1. FreeRADIUS

Install the accounting feed:

```bash
cp freeradius/mods-available/detail_userid /etc/freeradius/3.0/mods-available/
ln -s ../mods-available/detail_userid /etc/freeradius/3.0/mods-enabled/
mkdir -p /var/log/freeradius/radacct/userid
chown freerad:freeradius /var/log/freeradius/radacct/userid
```

Add `detail_userid` to the `accounting {}` section of the virtual server your
APs talk to, then apply the `Class` and `Acct-Interim-Interval` snippets from
[`eduroam-userid.conf`](../freeradius/sites-available/eduroam-userid.conf).
Restart, authenticate a test client, and confirm the file fills up:

```bash
tail -f /var/log/freeradius/radacct/userid/detail-$(date +%Y%m%d)
```

You are looking for three things in a `Start` record: `Class = 0x7569643a…`
(the `uid:` marker in hex), `Framed-IP-Address`, and `Acct-Session-Id`. If
`Class` is missing the inner-identity config did not take. If
`Framed-IP-Address` is missing, see [No IP address](#no-ip-address-in-accounting)
below — it is the most common snag and it has three separate fixes.

## 2. The firewall

**An admin account for the API.** Device → Admin Roles → add a role whose
XML API tab grants *User-ID Agent* (and *Operational Requests* if you want
`radius_userid.py show` to work). Everything else off — this integration
never touches configuration. Device → Administrators → new admin with that
role. Then fetch its key:

```bash
python3 radius_userid.py keygen --firewall https://fw1.example.edu \
    --panos-user userid-api
install -m 0400 -o radius-userid /dev/stdin /etc/radius-userid/api.key
```

**Enable User-ID on the zone.** Network → Zones → your wireless zone →
*Enable User Identification*. Without this the mappings arrive and are stored
but policy never consults them. Restrict the *Include List* to the wireless
client subnets so the firewall does not try to map addresses it has no
business mapping.

**Group mapping, if you want groups in policy.** Device → User Identification
→ Group Mapping Settings, pointed at your LDAP/AD. The usernames the bridge
sends must be in the same shape as the ones group mapping produces —
that is what `--realm-map uni.fi=UNI` and `--user-format` are for. If group
mapping yields `uni\jdoe`, send `UNI\jdoe` (case is not significant to
PAN-OS here); if it yields UPNs, use `--user-format upn`.

**Prove the path before involving RADIUS:**

```bash
python3 radius_userid.py test --firewall https://fw1.example.edu \
    --api-key-file /etc/radius-userid/api.key --user 'UNI\jdoe' --ip 10.20.30.40
python3 radius_userid.py show --firewall https://fw1.example.edu \
    --api-key-file /etc/radius-userid/api.key --filter jdoe
```

or on the firewall CLI: `show user ip-user-mapping all type XMLAPI`.

## 3. The bridge

Dry run first — it prints exactly what it would send and touches nothing:

```bash
python3 radius_userid.py run --dry-run --from-start --once \
    --detail "/var/log/freeradius/radacct/userid/detail-$(date +%Y%m%d)" \
    --realm-map uni.fi=UNI
```

Read the `login`/`logout` lines and the closing counter line. `skipped` and
`no-ip` counts that dwarf `logins` mean the identity or the address is not
arriving, and no amount of firewall configuration will fix that.

Then install it for real:

```bash
install -m 0755 radius_userid.py /usr/local/bin/
useradd --system --no-create-home --groups freeradius radius-userid
cp freeradius/systemd/radius-userid.service /etc/systemd/system/
# edit the ExecStart line: firewall URL, realms, detail path
systemctl daemon-reload && systemctl enable --now radius-userid
journalctl -u radius-userid -f
```

Useful flags once it is running:

| Flag | Why |
|---|---|
| `--timeout 480` | mapping lifetime in minutes, max 1440. Long enough to cover a working day, short enough that a missed `Stop` self-heals. |
| `--refresh 30` | re-send an unchanged mapping this often, so the timeout keeps sliding while the session is alive. |
| `--firewall` ×N | push to several firewalls; the alternative is PAN-OS User-ID redistribution from one of them. |
| `--vsys vsys1` | multi-vsys firewalls. |
| `--state /var/lib/radius-userid/state.json` | survive restarts without re-reading or forgetting live sessions. |
| `--tag-realm` | also register realm tags for dynamic user groups (see below). |

## 4. Writing policy

With mappings flowing, the *User* column in Policies → Security accepts
usernames and (with group mapping) groups, and the traffic log shows the
eduroam identity next to every session.

Per-realm policy for visitors is nicer through **dynamic user groups**, which
match on tags rather than a directory. Run the bridge with `--tag-realm`, and
each mapping also registers `eduroam_<realm>` — plus `eduroam_visitor` for
inbound roamers — against the user. Objects → Dynamic User Groups → new
group with match `'eduroam_visitor'`, then use it as the source user of a
rule that, say, allows web and VPN and nothing else. Tags default to never
expiring (`--tag-timeout SECONDS` to change that, max 30 days) and are
removed on logout only with `--untag`.

Two rules worth having early: source user `known-user` for anything that
should require an identity, and a catch-all `unknown` rule that logs, so you
can see how much of the wireless traffic the mapping is actually covering.

## No IP address in accounting

This is the failure mode that bites everyone. The NAS sends
Accounting-Start when the 802.1X session comes up, which is *before* the
client has a DHCP lease, so `Framed-IP-Address` is absent — and many
wireless controllers never fill it in later either. Three fixes, in order of
preference:

1. **Interim updates.** Reply with `Acct-Interim-Interval := 600` and make
   sure the controller has accounting updates enabled. Once the client has an
   address, the next interim carries it and the bridge maps it then. Costs
   you up to one interval of unmapped traffic.
2. **MAC lookup against the controller.** `--unifi-host https://192.168.1.1
   --unifi-user <local-admin>` lets the bridge resolve the client MAC from
   `Calling-Station-Id` against the UniFi client table when accounting has no
   address. Read-only, cached, and only consulted when the address is missing.
   Use a local admin account — a ui.com cloud account hits MFA and cannot log
   in from a script.
3. **DHCP-side mapping.** If your DHCP server logs leases, a syslog parse
   profile on the firewall can map address to MAC while User-ID maps user to
   address. More moving parts; only worth it if the first two fail.

## Alternative: syslog, no bridge

PAN-OS can parse syslog directly with its integrated User-ID agent, which
removes this script from the picture entirely.
[`freeradius/mods-available/linelog_userid`](../freeradius/mods-available/linelog_userid)
emits one line per accounting event and carries the matching parse-profile
regexes in its comments. Configure it under Device → User Identification →
User Mapping → Palo Alto Networks User-ID Agent Setup → Syslog Filters, add
FreeRADIUS as a monitored server, and permit *User-ID Syslog Listener* in the
interface management profile of the interface receiving it.

What you give up: realm rewriting, visitor pseudo-users, the `Class`
inner-identity trick (the firewall cannot decode it — you would need
`use_tunneled_reply = yes` and a NAS that honours it), MAC-to-IP fallback,
and buffering. A syslog packet that is dropped is a mapping that never
happens. A single parse profile can identify login events or logout events,
never both, so logout needs a second profile.

What you gain: nothing to install, nothing to keep running.

## Troubleshooting

| Symptom | Where to look |
|---|---|
| `no usable identity` in the log | `Class` is not reaching accounting. Check a `Start` record in the detail file; check the inner-tunnel `post-auth` snippet is in the *inner* server. |
| Users appear as `eduroam-visitor@…` | Same cause, or the realm is genuinely foreign. `-v` logs the identity source per session. |
| `has no IP yet` for every session | See [No IP address](#no-ip-address-in-accounting). |
| API returns "Invalid credential" | Key belongs to an admin without the *User-ID Agent* XML API permission, or was generated on a different firewall — keys are per-device. |
| Mappings exist but policy still misses | User Identification not enabled on the zone, or the address is outside the zone's include list. `show user ip-user-mapping all` proves the mapping; the traffic log proves the match. |
| Username matches but group does not | Format mismatch with group mapping. Compare `show user ip-user-mapping all` against `show user group name <group>` and adjust `--realm-map` / `--user-format`. |
| Mappings linger after users leave | Missing `Stop` records — check accounting reaches FreeRADIUS at all, and lower `--timeout` so stale entries expire sooner. |
| Firewall unreachable for a while | Each batch is retried (`--retries`) and then dropped; the mapping returns on the next refresh (`--refresh`, default 30 min) or the next accounting update, and nothing goes stale because entries expire on their own. A *stopped* bridge is different: the detail file buffers and it resumes at its saved offset. `--listen` buffers nothing in either case. |

Firewall-side commands worth knowing:

```
show user ip-user-mapping all type XMLAPI
show user ip-user-mapping-mp all
show user group name "cn=staff,ou=groups,dc=uni,dc=fi"
clear user-cache ip 10.20.30.40
```

## Privacy and policy notes

- The inner identity of a **visiting** user is not yours to have. Do not try
  to extract it. The `Class` mechanism here only ever carries identities your
  own IdP authenticated.
- Mapping identity to traffic makes firewall logs personal data. Under the
  eduroam policy you already log authentications; a User-ID feed extends that
  to browsing, which is a different sensitivity. Match your retention on the
  firewall to whatever your institution's privacy notice actually says, and
  involve whoever owns that notice before turning this on.
- Shared and lab machines map to whoever authenticated the wireless session,
  which may not be who is at the keyboard. Do not use these mappings as
  evidence of individual conduct without corroboration.
- `Class` travels to the AP in clear text. On your own infrastructure that is
  usually fine; if it is not, the base64 form is obfuscation, not encryption,
  and the honest answer is to not send it.

## References

- [Send User Mappings to User-ID Using the XML API](https://docs.paloaltonetworks.com/pan-os/11-1/pan-os-admin/user-id/map-ip-addresses-to-users/send-user-mappings-to-user-id-using-the-xml-api)
- [Configure the PAN-OS Integrated User-ID Agent as a Syslog Listener](https://docs.paloaltonetworks.com/ngfw/administration/user-id/map-ip-addresses-to-users/configure-user-id-to-monitor-syslog-senders-for-user-mapping/configure-the-pan-os-integrated-user-id-agent-as-a-syslog-listener)
- [Use Dynamic User Groups in Policy](https://docs.paloaltonetworks.com/pan-os/11-1/pan-os-admin/policy/use-dynamic-user-groups-in-policy)
- [eduroam FreeRADIUS IdP configuration](https://wiki.geant.org/display/H2eduroam/freeradius-idp)
- RFC 2865 §5.25 (Class), RFC 2866 (accounting), RFC 3579 §3.2
  (Message-Authenticator), RFC 4372 (Chargeable-User-Identity)
