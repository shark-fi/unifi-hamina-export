#!/usr/bin/env python3
r"""
radius_userid.py — Turn FreeRADIUS (eduroam) logins into Palo Alto User-ID
IP-to-user mappings, so firewall policy can match the eduroam identity.

The problem it solves: on 802.1X/eduroam the firewall never sees the login.
The only place that knows "10.20.30.40 is jdoe@uni.fi right now" is RADIUS
accounting. This bridge reads accounting events and pushes login/logout
entries into PAN-OS through the User-ID XML API.

Two input sources, use either or both:

  detail   Tail FreeRADIUS detail files (mods-available/detail). Survives a
           restart of this bridge — the file buffers while it is down.

  listen   Act as a RADIUS accounting server. Point FreeRADIUS's `replicate`
           module (or a second NAS accounting target) at it. No files, but
           events that arrive while the bridge is down are lost.

Subcommands:

  run      The bridge itself. Batches events and posts uid-messages.
  test     Push one mapping by hand — proves key, role and reachability.
  show     Dump the firewall's current IP-user mappings.
  keygen   Fetch a PAN-OS API key from username/password.

Examples:

  # tail detail files, push to two firewalls, 8 h mapping timeout
  python3 radius_userid.py run \
      --detail '/var/log/freeradius/radacct/*/detail-*' \
      --firewall https://fw1.uni.fi --firewall https://fw2.uni.fi \
      --realm-map uni.fi=UNI --timeout 480

  # accounting listener instead, tag visitors for a dynamic user group
  python3 radius_userid.py run --listen 0.0.0.0:1813 \
      --client 10.0.0.5=testing123 --firewall https://fw1.uni.fi \
      --visitors realm --tag-realm

  # see what a day of accounting would have produced, send nothing
  python3 radius_userid.py run --detail /var/log/freeradius/radacct/10.0.0.5/detail-20260727 \
      --from-start --once --dry-run --firewall https://fw1.uni.fi

The eduroam identity catch: accounting normally carries the *outer* identity
(anonymous@realm). See docs/palo-alto-user-id.md and freeradius/ for the
config that carries the inner identity out to accounting, and for what you
can and cannot know about visiting users from other institutions.

Stdlib only. Self-signed firewall certs are accepted unless --verify-tls.
"""

import argparse
import base64
import binascii
import getpass
import glob as globmod
import hashlib
import hmac
import http.cookiejar
import ipaddress
import json
import os
import re
import select
import signal
import socket
import ssl
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

VERSION = "1.0"

# PAN-OS limits: ip-user mapping timeout is in minutes, max 1440 (24 h);
# dynamic-user-group tag timeout is in seconds, max 2592000 (30 days).
MAX_MAPPING_TIMEOUT = 1440
MAX_TAG_TIMEOUT = 2592000

LOG_LEVELS = {"error": 0, "warn": 1, "info": 2, "debug": 3}
_log_level = LOG_LEVELS["info"]


def log(level, msg):
    if LOG_LEVELS[level] <= _log_level:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        print(f"{ts} {level:5s} {msg}", file=sys.stderr, flush=True)


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------
# RADIUS wire format (accounting only)
# --------------------------------------------------------------------------

# Only the attributes this bridge acts on. Everything else is skipped, so
# there is no dictionary to keep in sync.
RADIUS_ATTRS = {
    1: ("User-Name", "string"),
    4: ("NAS-IP-Address", "ipaddr"),
    8: ("Framed-IP-Address", "ipaddr"),
    25: ("Class", "octets"),
    30: ("Called-Station-Id", "string"),
    31: ("Calling-Station-Id", "string"),
    32: ("NAS-Identifier", "string"),
    40: ("Acct-Status-Type", "integer"),
    44: ("Acct-Session-Id", "string"),
    80: ("Message-Authenticator", "octets"),
    89: ("Chargeable-User-Identity", "octets"),
    95: ("NAS-IPv6-Address", "ipv6addr"),
    97: ("Framed-IPv6-Prefix", "ipv6prefix"),
    123: ("Delegated-IPv6-Prefix", "ipv6prefix"),
    126: ("Operator-Name", "string"),
    168: ("Framed-IPv6-Address", "ipv6addr"),
}

ACCT_STATUS = {1: "Start", 2: "Stop", 3: "Interim-Update",
               7: "Accounting-On", 8: "Accounting-Off"}

CODE_ACCOUNTING_REQUEST = 4
CODE_ACCOUNTING_RESPONSE = 5


def decode_attr(atype, raw):
    """Decode one attribute to the same textual form the detail parser uses,
    so both input paths hand the rest of the program identical records."""
    name, kind = RADIUS_ATTRS[atype]
    if kind == "string":
        return name, raw.decode("utf-8", "replace")
    if kind == "octets":
        return name, raw
    if kind == "integer":
        if len(raw) != 4:
            return name, None
        val = struct.unpack("!I", raw)[0]
        if name == "Acct-Status-Type":
            return name, ACCT_STATUS.get(val, str(val))
        return name, str(val)
    if kind == "ipaddr" and len(raw) == 4:
        return name, str(ipaddress.IPv4Address(raw))
    if kind == "ipv6addr" and len(raw) == 16:
        return name, str(ipaddress.IPv6Address(raw))
    if kind == "ipv6prefix" and len(raw) >= 2:
        # RFC 3162: reserved octet, prefix length, then the prefix bytes
        plen = raw[1]
        pfx = raw[2:] + b"\x00" * (16 - len(raw[2:]))
        return name, f"{ipaddress.IPv6Address(pfx[:16])}/{plen}"
    return name, None


def radius_decode(packet, secret, verify=True):
    """Parse an Accounting-Request. Returns (attrs, identifier, authenticator)
    or raises ValueError. Verifies the Request Authenticator, and the
    Message-Authenticator when the packet carries one (RFC 3579)."""
    if len(packet) < 20:
        raise ValueError("short packet")
    code, ident, length = struct.unpack("!BBH", packet[:4])
    if code != CODE_ACCOUNTING_REQUEST:
        raise ValueError(f"not an Accounting-Request (code {code})")
    if length > len(packet) or length < 20:
        raise ValueError("bad length field")
    packet = packet[:length]
    authenticator = packet[4:20]
    body = packet[20:]

    attrs = {}
    ma_offset = None
    pos = 0
    while pos + 2 <= len(body):
        atype, alen = body[pos], body[pos + 1]
        if alen < 2 or pos + alen > len(body):
            raise ValueError("malformed attribute")
        raw = body[pos + 2:pos + alen]
        if atype in RADIUS_ATTRS:
            if atype == 80:
                ma_offset = pos + 2
            name, value = decode_attr(atype, raw)
            if value is not None and name not in attrs:
                attrs[name] = value
        pos += alen

    if verify:
        expect = hashlib.md5(packet[:4] + b"\x00" * 16 + body + secret).digest()
        if not hmac.compare_digest(expect, authenticator):
            raise ValueError("bad Request Authenticator (wrong shared secret?)")
        if ma_offset is not None:
            # RFC 3579 §3.2: the HMAC covers the packet with its own field
            # zeroed, and — for Accounting-Request — with the authenticator
            # field zeroed too, since that is signed afterwards.
            got = attrs.get("Message-Authenticator", b"")
            zeroed = bytearray(body)
            zeroed[ma_offset:ma_offset + 16] = b"\x00" * 16
            want = hmac.new(secret, packet[:4] + b"\x00" * 16 + bytes(zeroed),
                            hashlib.md5).digest()
            if not hmac.compare_digest(want, got):
                raise ValueError("bad Message-Authenticator")

    attrs.pop("Message-Authenticator", None)
    return attrs, ident, authenticator


def radius_accounting_response(ident, request_authenticator, secret):
    header = struct.pack("!BBH", CODE_ACCOUNTING_RESPONSE, ident, 20)
    digest = hashlib.md5(header + request_authenticator + secret).digest()
    return header + digest


class AcctListener:
    """UDP RADIUS accounting server. FreeRADIUS's `replicate` module or a
    second NAS accounting target feeds it; it answers so the sender's queue
    drains, then hands records to the bridge."""

    def __init__(self, addr, port, clients, verify=True):
        self.clients = clients          # {ip: secret bytes}, "*" = any source
        self.verify = verify
        family = socket.AF_INET6 if ":" in addr else socket.AF_INET
        self.sock = socket.socket(family, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((addr, port))
        self.sock.setblocking(False)
        log("info", f"listening for RADIUS accounting on {addr}:{port}")

    def fileno(self):
        return self.sock.fileno()

    def secret_for(self, ip):
        return self.clients.get(ip) or self.clients.get("*")

    def read(self):
        """Drain the socket. Returns a list of attribute dicts."""
        out = []
        while True:
            try:
                data, peer = self.sock.recvfrom(8192)
            except BlockingIOError:
                return out
            except OSError as e:
                log("warn", f"accounting socket: {e}")
                return out
            src = peer[0]
            if src.startswith("::ffff:"):
                src = src[7:]
            secret = self.secret_for(src)
            if secret is None:
                log("warn", f"accounting packet from unknown client {src}, dropped")
                continue
            try:
                attrs, ident, auth = radius_decode(data, secret, self.verify)
            except ValueError as e:
                log("warn", f"accounting packet from {src}: {e}")
                continue
            try:
                self.sock.sendto(radius_accounting_response(ident, auth, secret), peer)
            except OSError as e:
                log("warn", f"could not answer {src}: {e}")
            attrs.setdefault("Client-IP-Address", src)
            out.append(attrs)


# --------------------------------------------------------------------------
# FreeRADIUS detail files
# --------------------------------------------------------------------------

DETAIL_LINE = re.compile(r"^\s+([A-Za-z0-9-]+(?::[A-Za-z0-9-]+)?)\s*=\s*(.*)$")


def unquote_detail(value):
    """Detail files quote strings and hex-print octets."""
    value = value.strip()
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        body = value[1:-1]
        out, i = [], 0
        while i < len(body):
            c = body[i]
            if c == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                i += 2
            else:
                out.append(c)
                i += 1
        return "".join(out)
    if value.startswith("0x"):
        try:
            return binascii.unhexlify(value[2:])
        except binascii.Error:
            return value
    return value


RADIUS_ATTRS_BY_NAME = {name for name, _ in RADIUS_ATTRS.values()}
# Attributes the detail module adds that never travel on the wire.
DETAIL_EXTRA = {"Client-IP-Address", "Timestamp", "Acct-Unique-Session-Id",
                "Packet-Src-IP-Address"}


def parse_detail_record(lines):
    """One detail record (timestamp line + indented attributes) -> dict.
    The timestamp line is not indented, so the regex ignores it."""
    attrs = {}
    for line in lines:
        m = DETAIL_LINE.match(line.rstrip("\n"))
        if not m:
            continue
        name, value = m.group(1), unquote_detail(m.group(2))
        if name in RADIUS_ATTRS_BY_NAME or name in DETAIL_EXTRA:
            attrs.setdefault(name, value)
    return attrs


class DetailTail:
    """Follows one or more detail files, including files that appear later
    (FreeRADIUS rolls them per client and per day) and files that are
    replaced under the same name. Only whole records are emitted; a partial
    trailing record stays in the file until it is finished."""

    def __init__(self, patterns, state, from_start=False):
        self.patterns = patterns
        self.state = state              # {path: {"ino": int, "pos": int}}
        self.from_start = from_start
        self.buffers = {}

    @staticmethod
    def _head_sig(path, count):
        """Fingerprint of the first bytes of a file. Appending never changes
        it, so a change means the file was replaced — which catches a rotation
        that reuses the inode and lands on the same size."""
        if count <= 0:
            return ""
        try:
            with open(path, "rb") as fh:
                return hashlib.md5(fh.read(count)).hexdigest()
        except OSError:
            return ""

    def _files(self):
        seen = []
        for pat in self.patterns:
            if any(ch in pat for ch in "*?["):
                seen.extend(globmod.glob(pat))
            elif os.path.exists(pat):
                seen.append(pat)
        return sorted(set(seen))

    def poll(self):
        """Returns a list of attribute dicts for every complete new record."""
        records = []
        for path in self._files():
            try:
                st = os.stat(path)
            except OSError:
                continue
            entry = self.state.get(path)
            if entry is None:
                pos = 0 if self.from_start else st.st_size
                entry = {"ino": st.st_ino, "pos": pos, "sig": "", "sig_len": 0}
                self.state[path] = entry
                log("debug", f"tracking {path} from offset {pos}")
            else:
                reason = None
                if entry["ino"] != st.st_ino:
                    reason = "was replaced"
                elif st.st_size < entry["pos"]:
                    reason = "shrank"
                elif (entry.get("sig_len") and st.st_size >= entry["sig_len"]
                      and self._head_sig(path, entry["sig_len"]) != entry["sig"]):
                    reason = "was rewritten"
                if reason:
                    log("info", f"{path} {reason}, reading from the start")
                    entry.update(ino=st.st_ino, pos=0, sig="", sig_len=0)
                    self.buffers.pop(path, None)

            want = min(256, st.st_size)
            if want > entry.get("sig_len", 0):
                entry["sig"] = self._head_sig(path, want)
                entry["sig_len"] = want

            if st.st_size <= entry["pos"]:
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(entry["pos"])
                    chunk = fh.read()
                    entry["pos"] = fh.tell()
            except OSError as e:
                log("warn", f"cannot read {path}: {e}")
                continue
            records.extend(self._consume(path, chunk))
        self._prune()
        return records

    def _consume(self, path, chunk):
        buf = self.buffers.get(path, "") + chunk
        records, current = [], []
        # A record ends at a blank line. Anything after the last blank line is
        # still being written, so it is carried over to the next poll.
        parts = buf.split("\n")
        tail = parts.pop() if not buf.endswith("\n") else ""
        for line in parts:
            if line.strip() == "":
                if current:
                    attrs = parse_detail_record(current)
                    if attrs:
                        records.append(attrs)
                    current = []
            else:
                current.append(line)
        if current:
            # No terminating blank line yet — put it back and wait.
            tail = "\n".join(current) + ("\n" if not tail else "\n" + tail)
        self.buffers[path] = tail
        return records

    def _prune(self):
        for path in list(self.state):
            if not os.path.exists(path):
                del self.state[path]
                self.buffers.pop(path, None)


# --------------------------------------------------------------------------
# Identity: pulling the real eduroam name out of an accounting record
# --------------------------------------------------------------------------

ANON_LOCALPARTS = {"anonymous", "anon", "eduroam", "@", ""}
MAC_USER = re.compile(r"^[0-9a-fA-F]{12}$|^([0-9a-fA-F]{2}[:.-]){5}[0-9a-fA-F]{2}$")


def split_realm(name):
    """eduroam names are user@realm; NAI keeps the realm after the *last* @."""
    if "@" in name:
        local, _, realm = name.rpartition("@")
        return local, realm.lower()
    if "\\" in name:
        domain, _, local = name.partition("\\")
        return local, domain.lower()
    return name, ""


def name_domain(name):
    """The domain of a DOMAIN\\user name, exactly as the supplicant wrote it
    (Windows supplicants send this form and PAN-OS wants it kept)."""
    return name.partition("\\")[0] if "\\" in name else ""


def is_anonymous(name):
    if not name:
        return True
    local, _realm = split_realm(name)
    return local.lower() in ANON_LOCALPARTS


def decode_class(blob, prefix):
    """The Class attribute is echoed verbatim by the NAS in accounting
    (RFC 2865 §5.25), which makes it the reliable way to carry the inner
    tunnel identity out of the Access-Accept. Two encodings, both emitted by
    the shipped FreeRADIUS snippet: '<prefix>name' and '<prefix>64:base64'."""
    if not blob:
        return None
    if isinstance(blob, str):
        blob = blob.encode("utf-8", "replace")
    pre = prefix.encode()
    if not blob.startswith(pre):
        return None
    rest = blob[len(pre):]
    if rest.startswith(b"64:"):
        try:
            rest = base64.b64decode(rest[3:], validate=True)
        except (binascii.Error, ValueError):
            return None
    name = rest.decode("utf-8", "replace").strip("\x00").strip()
    return name or None


class Identity:
    """Turns raw RADIUS names into the string PAN-OS should match on, and
    decides what to do about users from other eduroam realms."""

    def __init__(self, args):
        self.realm_map = {}
        for item in args.realm_map or []:
            realm, _, domain = item.partition("=")
            if not realm or not domain:
                die(f"--realm-map wants realm=DOMAIN, got {item!r}")
            self.realm_map[realm.strip().lower()] = domain.strip()
        self.user_format = args.user_format
        self.visitors = args.visitors
        self.visitor_prefix = args.visitor_prefix
        self.class_prefix = args.class_prefix
        self.cui_as_user = args.cui_as_user
        self.skip_mac_users = not args.mac_users

    def raw_name(self, rec):
        """Best available identity, and where it came from."""
        name = decode_class(rec.get("Class"), self.class_prefix)
        if name:
            return name, "class"
        user = rec.get("User-Name") or ""
        if user and not is_anonymous(user):
            return user, "user-name"
        if self.cui_as_user:
            cui = rec.get("Chargeable-User-Identity")
            if isinstance(cui, bytes):
                cui = cui.decode("utf-8", "replace")
            cui = (cui or "").strip("\x00").strip()
            # A single NUL is the "please send me a CUI" request, not a value.
            if cui and not is_anonymous(cui):
                return cui, "cui"
        return user, "outer" if user else "none"

    def resolve(self, rec):
        """Returns (username_for_panos, realm, kind).

        kind is 'known' when the record carries a real identity, 'visitor'
        when all we have is the realm of an inbound roamer, and the user is
        None when there is nothing worth mapping."""
        raw, _source = self.raw_name(rec)
        local, realm = split_realm(raw)

        if self.skip_mac_users and MAC_USER.match(local):
            return None, realm, "mac-auth"

        if raw and not is_anonymous(raw):
            return self.format_known(local, realm, name_domain(raw)), realm, "known"

        # No usable identity: an inbound eduroam visitor who authenticated at
        # their home IdP. The realm is all anyone outside that IdP may know —
        # eduroam deliberately does not disclose the inner identity to the
        # visited network.
        if self.visitors == "skip" or not realm:
            return None, realm, "anonymous"
        if self.visitors == "anon":
            return self.visitor_prefix.rstrip("@"), realm, "visitor"
        return f"{self.visitor_prefix}{realm}", realm, "visitor"

    def format_known(self, local, realm, sent_domain=""):
        if self.user_format == "bare":
            return local
        if self.user_format == "upn":
            return f"{local}@{realm}" if realm else local
        # netbios: DOMAIN\user, which is what LDAP group mapping usually wants
        domain = self.realm_map.get(realm) or sent_domain
        if domain:
            return f"{domain}\\{local}"
        return f"{local}@{realm}" if realm else local


def realm_tag(realm, prefix):
    """PAN-OS tags allow a limited character set; keep it boring."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", realm or "unknown").strip("_").lower()
    return f"{prefix}{slug}"


# --------------------------------------------------------------------------
# PAN-OS XML API
# --------------------------------------------------------------------------

class PanOS:
    """One firewall (or Panorama with --target). Only User-ID updates and
    read-only op commands — nothing here touches the configuration."""

    def __init__(self, url, api_key, vsys=None, target=None, verify=True,
                 timeout=30):
        self.url = url.rstrip("/")
        if not self.url.startswith("http"):
            self.url = "https://" + self.url
        self.api_key = api_key
        self.vsys = vsys
        self.target = target
        self.timeout = timeout
        handlers = []
        if not verify:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        handlers.append(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.opener = urllib.request.build_opener(*handlers)

    def __str__(self):
        return self.url

    def call(self, params):
        """POST to /api/. PAN-OS wants GET only for tiny requests; a uid-message
        batch is well past that, so everything goes in the body."""
        body = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(
            f"{self.url}/api/", data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(f"HTTP {e.code} from {self.url}: {detail}") from None
        except (urllib.error.URLError, ssl.SSLError, socket.timeout) as e:
            raise RuntimeError(f"{self.url}: {e}") from None
        try:
            root = ET.fromstring(payload)
        except ET.ParseError:
            raise RuntimeError(f"{self.url}: unparseable response") from None
        if root.get("status") != "success":
            msg = " ".join(t.strip() for t in root.itertext() if t.strip())
            raise RuntimeError(f"{self.url}: {msg or 'API call failed'}")
        return root

    def user_id(self, uid_message):
        params = {"type": "user-id", "action": "set", "key": self.api_key,
                  "cmd": uid_message}
        if self.vsys:
            params["vsys"] = self.vsys
        if self.target:
            params["target"] = self.target
        return self.call(params)

    def op(self, cmd):
        params = {"type": "op", "cmd": cmd, "key": self.api_key}
        if self.vsys:
            params["vsys"] = self.vsys
        if self.target:
            params["target"] = self.target
        return self.call(params)


def keygen(url, user, password, verify=True):
    fw = PanOS(url, "", verify=verify)
    root = fw.call({"type": "keygen", "user": user, "password": password})
    key = root.findtext("./result/key")
    if not key:
        raise RuntimeError(f"{url}: no key in keygen response")
    return key


def build_uid_message(logins=(), logouts=(), register=(), unregister=()):
    """logins/logouts: (user, ip, timeout_minutes|None).
    register/unregister: (user, [tags], timeout_seconds|None)."""
    root = ET.Element("uid-message")
    ET.SubElement(root, "version").text = "1.0"
    ET.SubElement(root, "type").text = "update"
    payload = ET.SubElement(root, "payload")

    if logins:
        el = ET.SubElement(payload, "login")
        for user, ip, timeout in logins:
            entry = ET.SubElement(el, "entry", name=user, ip=ip)
            if timeout:
                entry.set("timeout", str(timeout))
    if logouts:
        el = ET.SubElement(payload, "logout")
        for user, ip, _ in logouts:
            attrs = {"ip": ip}
            if user:
                attrs["name"] = user
            ET.SubElement(el, "entry", **attrs)
    for section, items in (("register-user", register), ("unregister-user", unregister)):
        if not items:
            continue
        el = ET.SubElement(payload, section)
        for user, tags, timeout in items:
            entry = ET.SubElement(el, "entry", user=user)
            tag_el = ET.SubElement(entry, "tag")
            for tag in tags:
                member = ET.SubElement(tag_el, "member")
                if timeout and section == "register-user":
                    member.set("timeout", str(timeout))
                member.text = tag
    return ET.tostring(root, encoding="unicode")


class Sender:
    """Queues User-ID updates and posts them in batches. PAN-OS processes the
    login section before the logout section of the same message, so a logout
    that collides with a queued login for the same IP forces a flush first —
    otherwise the message would delete the mapping it just created."""

    def __init__(self, firewalls, batch_seconds=2.0, max_batch=500,
                 retries=3, dry_run=False, queue_limit=20000):
        self.firewalls = firewalls
        self.batch_seconds = batch_seconds
        self.max_batch = max_batch
        self.retries = retries
        self.dry_run = dry_run
        self.queue_limit = queue_limit
        self.logins, self.logouts = [], []
        self.register, self.unregister = [], []
        self.pending_ips = set()
        self.last_flush = time.time()
        self.stats = {"login": 0, "logout": 0, "batches": 0, "failed": 0,
                      "dropped": 0}

    def _queued(self):
        return len(self.logins) + len(self.logouts) + len(self.register) + len(self.unregister)

    def login(self, user, ip, timeout):
        if ip in self.pending_ips:
            self.flush()
        self.logins.append((user, ip, timeout))
        self.pending_ips.add(ip)
        self.stats["login"] += 1
        self._maybe_flush()

    def logout(self, user, ip):
        if ip in self.pending_ips:
            self.flush()
        self.logouts.append((user, ip, None))
        self.pending_ips.add(ip)
        self.stats["logout"] += 1
        self._maybe_flush()

    def tag(self, user, tags, timeout):
        self.register.append((user, list(tags), timeout))
        self._maybe_flush()

    def untag(self, user, tags):
        self.unregister.append((user, list(tags), None))
        self._maybe_flush()

    def _maybe_flush(self):
        if self._queued() >= self.max_batch:
            self.flush()
        elif self._queued() > self.queue_limit:
            log("error", "update queue overflowed, dropping oldest entries")
            self.stats["dropped"] += self._queued()
            self.logins, self.logouts = [], []
            self.register, self.unregister = [], []
            self.pending_ips.clear()

    def tick(self, now=None):
        now = now or time.time()
        if self._queued() and now - self.last_flush >= self.batch_seconds:
            self.flush()

    def flush(self):
        if not self._queued():
            self.last_flush = time.time()
            return
        msg = build_uid_message(self.logins, self.logouts,
                                self.register, self.unregister)
        counts = (f"{len(self.logins)} login, {len(self.logouts)} logout, "
                  f"{len(self.register)} tag, {len(self.unregister)} untag")
        self.logins, self.logouts = [], []
        self.register, self.unregister = [], []
        self.pending_ips.clear()
        self.last_flush = time.time()
        self.stats["batches"] += 1

        if self.dry_run:
            log("info", f"[dry-run] {counts}: {msg}")
            return
        log("debug", f"sending {counts}")
        for fw in self.firewalls:
            self._send_one(fw, msg, counts)

    def _send_one(self, fw, msg, counts):
        delay = 1.0
        for attempt in range(1, self.retries + 1):
            try:
                fw.user_id(msg)
                log("debug", f"{fw}: accepted {counts}")
                return
            except RuntimeError as e:
                if attempt == self.retries:
                    self.stats["failed"] += 1
                    log("error", f"giving up after {attempt} attempts: {e}")
                    return
                log("warn", f"{e} (retry {attempt}/{self.retries - 1})")
                time.sleep(delay)
                delay *= 2


# --------------------------------------------------------------------------
# MAC -> IP fallback via the UniFi controller
# --------------------------------------------------------------------------

class UniFiClients:
    """Wireless accounting often carries no Framed-IP-Address: the NAS sends
    the Start before the client has finished DHCP, and many controllers never
    fill it in at all. When that happens the client's MAC is still in
    Calling-Station-Id, and the UniFi controller knows which IP that MAC
    holds. Read-only, and only consulted when the IP is missing."""

    def __init__(self, host, username, password, site="default", ttl=15,
                 verify=False):
        self.base = host.rstrip("/")
        self.username = username
        self.password = password
        self.site = site
        self.ttl = ttl
        self.verify = verify
        self.prefix = None
        self.cache = {}
        self.fetched = 0.0
        self.opener = None

    def _connect(self):
        handlers = [urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())]
        if not self.verify:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)
        self.headers = {"Accept": "application/json",
                        "Content-Type": "application/json"}
        body = json.dumps({"username": self.username,
                           "password": self.password}).encode()
        for path, prefix in (("/api/auth/login", "/proxy/network"), ("/api/login", "")):
            req = urllib.request.Request(self.base + path, data=body,
                                         headers=self.headers, method="POST")
            try:
                with self.opener.open(req, timeout=20) as resp:
                    csrf = resp.headers.get("X-CSRF-Token")
                    if csrf:
                        self.headers["X-CSRF-Token"] = csrf
                self.prefix = prefix
                log("info", f"UniFi login OK ({self.base}{prefix or ''})")
                return
            except Exception:
                continue
        raise RuntimeError("UniFi login failed")

    def refresh(self):
        if self.opener is None:
            self._connect()
        url = f"{self.base}{self.prefix}/api/s/{self.site}/stat/sta"
        req = urllib.request.Request(url, headers=self.headers, method="GET")
        with self.opener.open(req, timeout=20) as resp:
            data = json.loads(resp.read() or b"null") or {}
        cache = {}
        for sta in data.get("data", []):
            mac = (sta.get("mac") or "").lower()
            ip = sta.get("ip") or ""
            if mac and ip:
                cache[mac] = ip
        self.cache = cache
        self.fetched = time.time()
        log("debug", f"UniFi client table: {len(cache)} entries with an IP")

    def lookup(self, mac):
        mac = normalize_mac(mac)
        if not mac:
            return None
        if time.time() - self.fetched > self.ttl:
            try:
                self.refresh()
            except Exception as e:
                # Session expired or console restarted — reconnect next time.
                log("warn", f"UniFi lookup failed: {e}")
                self.opener = None
                self.fetched = time.time()  # do not hammer a broken console
                return None
        return self.cache.get(mac)


def normalize_mac(value):
    if not value:
        return None
    hexes = re.sub(r"[^0-9a-fA-F]", "", value)
    if len(hexes) != 12:
        return None
    return ":".join(hexes[i:i + 2] for i in range(0, 12, 2)).lower()


# --------------------------------------------------------------------------
# The bridge
# --------------------------------------------------------------------------

def pick_ip(rec, resolver):
    for attr in ("Framed-IP-Address", "Framed-IPv6-Address"):
        val = rec.get(attr)
        if val and val not in ("0.0.0.0", "::"):
            return val
    prefix = rec.get("Framed-IPv6-Prefix") or ""
    if prefix.endswith("/128"):
        return prefix[:-4]
    if resolver:
        return resolver.lookup(rec.get("Calling-Station-Id"))
    return None


class Bridge:
    def __init__(self, args, sender, identity, resolver=None, state=None):
        self.args = args
        self.sender = sender
        self.identity = identity
        self.resolver = resolver
        self.state = state if state is not None else {}
        self.sessions = self.state.setdefault("sessions", {})
        self.counters = {"records": 0, "skipped": 0, "no-ip": 0, "logins": 0,
                         "logouts": 0}

    @staticmethod
    def session_key(rec):
        nas = (rec.get("NAS-IP-Address") or rec.get("NAS-IPv6-Address")
               or rec.get("NAS-Identifier") or rec.get("Client-IP-Address") or "?")
        sid = rec.get("Acct-Session-Id") or normalize_mac(rec.get("Calling-Station-Id")) or "?"
        return f"{nas}|{sid}"

    def handle(self, rec):
        self.counters["records"] += 1
        status = rec.get("Acct-Status-Type") or ""

        if status in ("Accounting-On", "Accounting-Off"):
            self.nas_reset(rec)
            return
        if status not in ("Start", "Interim-Update", "Stop"):
            return

        key = self.session_key(rec)
        if status == "Stop":
            self.stop(key, rec)
            return

        user, realm, kind = self.identity.resolve(rec)
        if not user:
            self.counters["skipped"] += 1
            log("debug", f"{key}: no usable identity ({kind}), skipped")
            return

        ip = pick_ip(rec, self.resolver)
        if not ip:
            self.counters["no-ip"] += 1
            log("debug", f"{key}: {user} has no IP yet, waiting for an update")
            # Remember the user so a later update (or the Stop) can act on it.
            sess = self.sessions.setdefault(key, {})
            sess.update(user=user, realm=realm, kind=kind, seen=time.time())
            return

        sess = self.sessions.get(key)
        now = time.time()
        if sess and sess.get("ip") == ip and sess.get("user") == user:
            if now - sess.get("sent", 0) < self.args.refresh * 60:
                sess["seen"] = now
                return
        elif sess and sess.get("ip") and sess["ip"] != ip:
            # The client changed address inside one accounting session.
            self.sender.logout(sess["user"], sess["ip"])
            self.counters["logouts"] += 1

        self.sender.login(user, ip, self.args.timeout)
        self.counters["logins"] += 1
        log("info", f"login {user} -> {ip} ({kind}, session {key})")
        if self.args.tag_realm:
            tags = [realm_tag(realm, self.args.tag_prefix)]
            if kind == "visitor":
                tags.append(f"{self.args.tag_prefix}visitor")
            self.sender.tag(user, tags, self.args.tag_timeout)
        else:
            tags = []
        self.sessions[key] = {"user": user, "ip": ip, "realm": realm,
                              "kind": kind, "tags": tags, "sent": now,
                              "seen": now}

    def stop(self, key, rec):
        sess = self.sessions.pop(key, None)
        user = sess.get("user") if sess else None
        ip = (sess or {}).get("ip") or pick_ip(rec, self.resolver)
        if not user:
            # No session on file — only name the user when the Stop itself
            # carries a real identity. Guessing wrong would delete somebody
            # else's mapping; an IP-only logout is the safer form.
            candidate, _, kind = self.identity.resolve(rec)
            user = candidate if kind == "known" else None
        if not ip:
            log("debug", f"{key}: Stop with no known IP, nothing to remove")
            return
        self.sender.logout(user, ip)
        self.counters["logouts"] += 1
        log("info", f"logout {user or '(any)'} -> {ip} (session {key})")
        if sess and sess.get("tags") and self.args.tag_realm and self.args.untag:
            self.sender.untag(user, sess["tags"])

    def nas_reset(self, rec):
        """Accounting-On/Off means the NAS just (re)started: every session it
        was holding is gone, and no Stop will ever arrive for them."""
        nas = (rec.get("NAS-IP-Address") or rec.get("NAS-IPv6-Address")
               or rec.get("NAS-Identifier") or rec.get("Client-IP-Address") or "?")
        gone = [k for k in self.sessions if k.startswith(f"{nas}|")]
        if not gone:
            return
        log("info", f"{nas} restarted, removing {len(gone)} mappings")
        for k in gone:
            sess = self.sessions.pop(k)
            if sess.get("ip"):
                self.sender.logout(sess.get("user"), sess["ip"])
                self.counters["logouts"] += 1

    def expire(self):
        """Drop sessions we have not heard about in a long time. The firewall
        entry expires on its own via the timeout attribute; this only keeps
        the state file from growing without limit."""
        cutoff = time.time() - max(self.args.timeout * 60 * 2, 3600)
        stale = [k for k, s in self.sessions.items() if s.get("seen", 0) < cutoff]
        for k in stale:
            del self.sessions[k]
        if stale:
            log("debug", f"expired {len(stale)} idle sessions from state")


# --------------------------------------------------------------------------
# state file
# --------------------------------------------------------------------------

def load_state(path):
    if not path or not os.path.exists(path):
        return {"files": {}, "sessions": {}}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError) as e:
        log("warn", f"cannot read state file {path}: {e}, starting fresh")
        return {"files": {}, "sessions": {}}
    state.setdefault("files", {})
    state.setdefault("sessions", {})
    return state


def save_state(path, state):
    if not path:
        return
    tmp = f"{path}.tmp"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    except OSError as e:
        log("warn", f"cannot write state file {path}: {e}")


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------

def resolve_key(args):
    key = args.api_key or os.environ.get("PANOS_API_KEY")
    if not key and args.api_key_file:
        try:
            with open(args.api_key_file, "r", encoding="utf-8") as fh:
                key = fh.read().strip()
        except OSError as e:
            die(f"cannot read --api-key-file: {e}")
    if not key and args.panos_user:
        password = args.panos_password or getpass.getpass(
            f"PAN-OS password for {args.panos_user}: ")
        try:
            key = keygen(args.firewall[0], args.panos_user, password,
                         verify=args.verify_tls)
        except RuntimeError as e:
            die(str(e))
        log("info", "obtained an API key with keygen")
    if not key:
        die("no API key: pass --api-key, set PANOS_API_KEY, use --api-key-file, "
            "or give --panos-user for keygen")
    return key


def firewalls_from(args):
    if not args.firewall:
        die("--firewall is required")
    key = resolve_key(args)
    return [PanOS(url, key, vsys=args.vsys, target=args.target,
                  verify=args.verify_tls) for url in args.firewall]


def load_clients(args):
    clients = {}
    for item in args.client or []:
        ip, _, secret = item.partition("=")
        if not secret:
            die(f"--client wants ip=secret, got {item!r}")
        clients[ip.strip()] = secret.strip().encode()
    if args.secret_file:
        try:
            with open(args.secret_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.split("#", 1)[0].strip()
                    if not line:
                        continue
                    ip, _, secret = line.partition("=")
                    if not secret:
                        die(f"bad line in --secret-file: {line!r}")
                    clients[ip.strip()] = secret.strip().encode()
        except OSError as e:
            die(f"cannot read --secret-file: {e}")
    return clients


def run_bridge(args):
    if not args.detail and not args.listen:
        die("nothing to read: give --detail and/or --listen")
    if args.timeout > MAX_MAPPING_TIMEOUT:
        log("warn", f"--timeout capped at {MAX_MAPPING_TIMEOUT} minutes by PAN-OS")
        args.timeout = MAX_MAPPING_TIMEOUT
    if args.tag_timeout and args.tag_timeout > MAX_TAG_TIMEOUT:
        log("warn", f"--tag-timeout capped at {MAX_TAG_TIMEOUT} seconds by PAN-OS")
        args.tag_timeout = MAX_TAG_TIMEOUT

    if not args.firewall and not args.dry_run:
        die("--firewall is required unless --dry-run")
    firewalls = [] if args.dry_run else firewalls_from(args)
    sender = Sender(firewalls, batch_seconds=args.batch_seconds,
                    max_batch=args.max_batch, retries=args.retries,
                    dry_run=args.dry_run)
    identity = Identity(args)
    state = load_state(args.state)

    resolver = None
    if args.unifi_host:
        if not args.unifi_user:
            die("--unifi-host also needs --unifi-user")
        password = args.unifi_password or os.environ.get("UNIFI_PASSWORD")
        if not password:
            password = getpass.getpass(f"UniFi password for {args.unifi_user}: ")
        resolver = UniFiClients(args.unifi_host, args.unifi_user, password,
                                site=args.unifi_site, ttl=args.unifi_cache,
                                verify=args.verify_tls)

    bridge = Bridge(args, sender, identity, resolver, state)
    tail = DetailTail(args.detail, state["files"], args.from_start) if args.detail else None

    listener = None
    if args.listen:
        addr, _, port = args.listen.rpartition(":")
        addr = addr.strip("[]")
        if not port.isdigit():
            die("--listen wants [address:]port")
        clients = load_clients(args)
        if not clients:
            die("--listen needs at least one --client ip=secret or --secret-file")
        listener = AcctListener(addr or "0.0.0.0", int(port), clients,
                                verify=not args.no_verify_radius)

    stopping = {"flag": False}

    def on_signal(signum, _frame):
        log("info", f"signal {signum}, shutting down")
        stopping["flag"] = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    log("info", f"radius_userid {VERSION} started: "
                f"{len(firewalls)} firewall(s), timeout {args.timeout} min"
                f"{', dry run' if args.dry_run else ''}")

    last_save = last_expire = time.time()
    try:
        while not stopping["flag"]:
            records = []
            if listener:
                ready, _, _ = select.select([listener], [], [], args.poll_interval)
                if ready:
                    records.extend(listener.read())
            elif not args.once:
                time.sleep(args.poll_interval)
            if tail:
                records.extend(tail.poll())
            for rec in records:
                try:
                    bridge.handle(rec)
                except Exception as e:      # one bad record must not stop the bridge
                    log("error", f"record failed: {e}")
            sender.tick()

            now = time.time()
            if now - last_expire > 300:
                bridge.expire()
                last_expire = now
            if args.state and now - last_save > args.save_interval:
                save_state(args.state, state)
                last_save = now
            if args.once:
                break
    finally:
        sender.flush()
        save_state(args.state, state)
        log("info", "counters: " + ", ".join(f"{k}={v}" for k, v in
                                             {**bridge.counters, **sender.stats}.items()))
    return 0


def run_test(args):
    firewalls = firewalls_from(args)
    msg = (build_uid_message(logouts=[(args.user, args.ip, None)])
           if args.logout else
           build_uid_message(logins=[(args.user, args.ip, args.timeout)]))
    if args.dry_run:
        print(msg)
        return 0
    rc = 0
    for fw in firewalls:
        try:
            fw.user_id(msg)
            action = "logout" if args.logout else "login"
            print(f"{fw}: {action} {args.user} {args.ip} accepted")
        except RuntimeError as e:
            print(e, file=sys.stderr)
            rc = 1
    return rc


def run_show(args):
    cmd = "<show><user><ip-user-mapping><all/></ip-user-mapping></user></show>"
    rc = 0
    for fw in firewalls_from(args):
        try:
            root = fw.op(cmd)
        except RuntimeError as e:
            print(e, file=sys.stderr)
            rc = 1
            continue
        entries = root.findall(".//entry")
        rows = []
        for e in entries:
            user = e.findtext("user") or ""
            if args.filter and args.filter.lower() not in user.lower():
                continue
            rows.append((e.findtext("ip") or "", user, e.findtext("type") or "",
                         e.findtext("timeout") or ""))
        print(f"# {fw} — {len(rows)} mapping(s)")
        for ip, user, kind, timeout in sorted(rows):
            print(f"{ip:<40} {user:<40} {kind:<12} {timeout}")
    return rc


def run_keygen(args):
    if not args.firewall:
        die("--firewall is required")
    if not args.panos_user:
        die("--panos-user is required")
    password = args.panos_password or getpass.getpass(
        f"PAN-OS password for {args.panos_user}: ")
    try:
        print(keygen(args.firewall[0], args.panos_user, password,
                     verify=args.verify_tls))
    except RuntimeError as e:
        die(str(e))
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def add_panos_args(p):
    # -v/-q are accepted on either side of the subcommand; SUPPRESS keeps the
    # subparser from overwriting a value given before it.
    p.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS,
                   help="more logging (repeat for debug)")
    p.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS,
                   help="errors only")
    g = p.add_argument_group("firewall")
    g.add_argument("--firewall", action="append", metavar="URL", default=[],
                   help="PAN-OS management URL, repeatable for several firewalls")
    g.add_argument("--api-key", help="PAN-OS API key (or set PANOS_API_KEY)")
    g.add_argument("--api-key-file", help="file holding the API key")
    g.add_argument("--panos-user", help="username for keygen when no key is given")
    g.add_argument("--panos-password", help="password for keygen (prompted if omitted)")
    g.add_argument("--vsys", help="target vsys, e.g. vsys1 (multi-vsys firewalls)")
    g.add_argument("--target", help="serial of a managed device, when talking to Panorama")
    g.add_argument("--verify-tls", action="store_true",
                   help="verify the firewall certificate (off by default)")


def main():
    ap = argparse.ArgumentParser(
        description="Feed FreeRADIUS/eduroam logins into Palo Alto User-ID.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples:")[1] if "Examples:" in __doc__ else None)
    ap.add_argument("-v", "--verbose", action="count", default=0)
    ap.add_argument("-q", "--quiet", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # run ------------------------------------------------------------------
    run = sub.add_parser("run", help="bridge RADIUS accounting to User-ID")
    add_panos_args(run)
    src = run.add_argument_group("accounting sources")
    src.add_argument("--detail", action="append", metavar="PATH",
                     help="FreeRADIUS detail file or glob, repeatable")
    src.add_argument("--from-start", action="store_true",
                     help="read detail files from the beginning on first sight")
    src.add_argument("--listen", metavar="[ADDR:]PORT",
                     help="also act as a RADIUS accounting server, e.g. 0.0.0.0:1813")
    src.add_argument("--client", action="append", metavar="IP=SECRET",
                     help="accounting client allowed to send to --listen")
    src.add_argument("--secret-file", help="file of ip=secret lines for --listen")
    src.add_argument("--no-verify-radius", action="store_true",
                     help="skip authenticator checks on received packets (debug only)")

    ident = run.add_argument_group("identity")
    ident.add_argument("--realm-map", action="append", metavar="REALM=DOMAIN",
                       help="map an eduroam realm to a domain, e.g. uni.fi=UNI")
    ident.add_argument("--user-format", choices=["netbios", "upn", "bare"],
                       default="netbios",
                       help="DOMAIN\\user (default), user@realm, or user")
    ident.add_argument("--visitors", choices=["skip", "realm", "anon"],
                       default="realm",
                       help="inbound roamers, whose real name nobody outside "
                            "their home IdP knows: map one pseudo-user per "
                            "realm (default), one bucket for all of them "
                            "(anon), or leave them unmapped (skip)")
    ident.add_argument("--visitor-prefix", default="eduroam-visitor@",
                       help="prefix for visitor pseudo-users (default eduroam-visitor@)")
    ident.add_argument("--class-prefix", default="uid:",
                       help="prefix marking an identity carried in Class (default uid:)")
    ident.add_argument("--cui-as-user", action="store_true",
                       help="fall back to Chargeable-User-Identity as the username")
    ident.add_argument("--mac-users", action="store_true",
                       help="do not skip MAC-authentication usernames")

    beh = run.add_argument_group("behaviour")
    beh.add_argument("--timeout", type=int, default=480, metavar="MIN",
                     help="mapping timeout in minutes, max 1440 (default 480)")
    beh.add_argument("--refresh", type=int, default=30, metavar="MIN",
                     help="re-send an unchanged mapping this often (default 30)")
    beh.add_argument("--tag-realm", action="store_true",
                     help="also tag users with their realm for dynamic user groups")
    beh.add_argument("--tag-prefix", default="eduroam_",
                     help="tag name prefix (default eduroam_)")
    beh.add_argument("--tag-timeout", type=int, default=0, metavar="SEC",
                     help="tag timeout in seconds, 0 = never expires")
    beh.add_argument("--untag", action="store_true",
                     help="remove tags on logout as well")
    beh.add_argument("--batch-seconds", type=float, default=2.0,
                     help="how long to accumulate updates before posting")
    beh.add_argument("--max-batch", type=int, default=500,
                     help="post immediately once this many entries are queued")
    beh.add_argument("--retries", type=int, default=3, help="API attempts per batch")
    beh.add_argument("--poll-interval", type=float, default=1.0,
                     help="detail-file poll interval in seconds")
    beh.add_argument("--state", default="", metavar="PATH",
                     help="state file, keeps file offsets and sessions across restarts")
    beh.add_argument("--save-interval", type=float, default=10.0)
    beh.add_argument("--once", action="store_true",
                     help="one pass over the detail files, then exit")
    beh.add_argument("--dry-run", action="store_true",
                     help="log the uid-messages instead of sending them")

    unifi = run.add_argument_group("MAC->IP fallback (UniFi)")
    unifi.add_argument("--unifi-host", help="controller URL, e.g. https://192.168.1.1")
    unifi.add_argument("--unifi-user", default="", help="local admin username")
    unifi.add_argument("--unifi-password", help="or set UNIFI_PASSWORD")
    unifi.add_argument("--unifi-site", default="default")
    unifi.add_argument("--unifi-cache", type=float, default=15.0,
                       help="seconds to reuse the client table (default 15)")

    # test -----------------------------------------------------------------
    test = sub.add_parser("test", help="push a single mapping by hand")
    add_panos_args(test)
    test.add_argument("--user", required=True, help=r"e.g. UNI\jdoe")
    test.add_argument("--ip", required=True)
    test.add_argument("--timeout", type=int, default=10, metavar="MIN")
    test.add_argument("--logout", action="store_true", help="remove instead of add")
    test.add_argument("--dry-run", action="store_true", help="print the XML only")

    # show -----------------------------------------------------------------
    show = sub.add_parser("show", help="list current IP-user mappings")
    add_panos_args(show)
    show.add_argument("--filter", help="only show users containing this text")

    # keygen ---------------------------------------------------------------
    kg = sub.add_parser("keygen", help="fetch a PAN-OS API key")
    add_panos_args(kg)

    args = ap.parse_args()

    global _log_level
    _log_level = 0 if getattr(args, "quiet", False) else min(
        LOG_LEVELS["info"] + getattr(args, "verbose", 0), LOG_LEVELS["debug"])

    if args.cmd == "run":
        return run_bridge(args)
    if args.cmd == "test":
        return run_test(args)
    if args.cmd == "show":
        return run_show(args)
    if args.cmd == "keygen":
        return run_keygen(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
