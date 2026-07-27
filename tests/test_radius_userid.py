#!/usr/bin/env python3
"""Tests for radius_userid.py — run with: python3 -m unittest discover tests

Stdlib only, no network: the PAN-OS side is exercised through a stub sender.
"""

import hashlib
import hmac
import os
import struct
import sys
import tempfile
import unittest
from argparse import Namespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import radius_userid as ru  # noqa: E402


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def encode_attrs(attrs):
    out = b""
    for atype, value in attrs:
        if isinstance(value, str):
            value = value.encode()
        elif isinstance(value, int):
            value = struct.pack("!I", value)
        out += struct.pack("!BB", atype, len(value) + 2) + value
    return out


def acct_packet(attrs, secret=b"testing123", ident=7, with_ma=False):
    """Sign like FreeRADIUS does: Message-Authenticator first over a packet
    whose authenticator field is still zero, then the Request Authenticator
    over the finished packet."""
    body = encode_attrs(attrs)
    if with_ma:
        body += struct.pack("!BB", 80, 18) + b"\x00" * 16
    header = struct.pack("!BBH", 4, ident, 20 + len(body))
    if with_ma:
        ma = hmac.new(secret, header + b"\x00" * 16 + body, hashlib.md5).digest()
        body = body[:-16] + ma
    auth = hashlib.md5(header + b"\x00" * 16 + body + secret).digest()
    return header + auth + body


def args_for(**over):
    base = dict(realm_map=["uni.fi=UNI"], user_format="netbios",
                visitors="realm", visitor_prefix="eduroam-visitor@",
                class_prefix="uid:", cui_as_user=False, mac_users=False,
                timeout=480, refresh=30, tag_realm=False, tag_prefix="eduroam_",
                tag_timeout=0, untag=False)
    base.update(over)
    return Namespace(**base)


class StubSender:
    """Records what the bridge decided instead of talking to a firewall."""

    def __init__(self):
        self.calls = []

    def login(self, user, ip, timeout):
        self.calls.append(("login", user, ip, timeout))

    def logout(self, user, ip):
        self.calls.append(("logout", user, ip))

    def tag(self, user, tags, timeout):
        self.calls.append(("tag", user, tuple(tags), timeout))

    def untag(self, user, tags):
        self.calls.append(("untag", user, tuple(tags)))

    def flush(self):
        pass

    def tick(self, now=None):
        pass


# --------------------------------------------------------------------------
# RADIUS wire format
# --------------------------------------------------------------------------

class TestRadiusDecode(unittest.TestCase):
    def test_roundtrip(self):
        pkt = acct_packet([(1, "jdoe@uni.fi"), (40, 1), (8, bytes([10, 1, 2, 3])),
                           (44, "sess-1"), (31, "aa:bb:cc:dd:ee:ff"),
                           (4, bytes([192, 0, 2, 1]))])
        attrs, ident, auth = ru.radius_decode(pkt, b"testing123")
        self.assertEqual(attrs["User-Name"], "jdoe@uni.fi")
        self.assertEqual(attrs["Acct-Status-Type"], "Start")
        self.assertEqual(attrs["Framed-IP-Address"], "10.1.2.3")
        self.assertEqual(attrs["NAS-IP-Address"], "192.0.2.1")
        self.assertEqual(attrs["Acct-Session-Id"], "sess-1")
        self.assertEqual(ident, 7)
        self.assertEqual(len(auth), 16)

    def test_wrong_secret_rejected(self):
        pkt = acct_packet([(1, "jdoe@uni.fi"), (40, 1)])
        with self.assertRaises(ValueError):
            ru.radius_decode(pkt, b"wrong-secret")

    def test_message_authenticator(self):
        pkt = acct_packet([(1, "jdoe@uni.fi"), (40, 2)], with_ma=True)
        attrs, _, _ = ru.radius_decode(pkt, b"testing123")
        self.assertEqual(attrs["Acct-Status-Type"], "Stop")
        self.assertNotIn("Message-Authenticator", attrs)

    def test_tampered_message_authenticator(self):
        pkt = bytearray(acct_packet([(1, "jdoe@uni.fi"), (40, 2)], with_ma=True))
        pkt[-1] ^= 0xFF
        with self.assertRaises(ValueError):
            ru.radius_decode(bytes(pkt), b"testing123")

    def test_truncated_packet(self):
        with self.assertRaises(ValueError):
            ru.radius_decode(b"\x04\x01\x00\x14", b"testing123")

    def test_ipv6_attributes(self):
        addr = b"\x20\x01\x0d\xb8" + b"\x00" * 11 + b"\x01"
        pkt = acct_packet([(40, 1), (168, addr)])
        attrs, _, _ = ru.radius_decode(pkt, b"testing123")
        self.assertEqual(attrs["Framed-IPv6-Address"], "2001:db8::1")

    def test_response_authenticator(self):
        pkt = acct_packet([(40, 1)])
        _, ident, auth = ru.radius_decode(pkt, b"testing123")
        resp = ru.radius_accounting_response(ident, auth, b"testing123")
        code, rid, length = struct.unpack("!BBH", resp[:4])
        self.assertEqual((code, rid, length), (5, 7, 20))
        self.assertEqual(
            resp[4:], hashlib.md5(resp[:4] + auth + b"testing123").digest())


# --------------------------------------------------------------------------
# detail files
# --------------------------------------------------------------------------

DETAIL_RECORD = """Mon Jul 27 06:38:00 2026
\tAcct-Status-Type = Start
\tUser-Name = "jdoe@uni.fi"
\tCalling-Station-Id = "AA-BB-CC-DD-EE-FF"
\tFramed-IP-Address = 10.20.30.40
\tAcct-Session-Id = "5C4E1B00-00000001"
\tNAS-IP-Address = 192.0.2.10
\tClass = 0x7569643a6a646f6540756e692e6669
\tTimestamp = 1753598280

"""


class TestDetailParsing(unittest.TestCase):
    def test_record(self):
        attrs = ru.parse_detail_record(DETAIL_RECORD.strip("\n").split("\n"))
        self.assertEqual(attrs["User-Name"], "jdoe@uni.fi")
        self.assertEqual(attrs["Framed-IP-Address"], "10.20.30.40")
        self.assertEqual(attrs["Class"], b"uid:jdoe@uni.fi")
        self.assertEqual(attrs["Acct-Status-Type"], "Start")

    def test_quoted_escapes(self):
        self.assertEqual(ru.unquote_detail(r'"a\"b\\c"'), 'a"b\\c')
        self.assertEqual(ru.unquote_detail('"tab\\there"'), "tab\there")
        self.assertEqual(ru.unquote_detail("0xdeadbeef"), b"\xde\xad\xbe\xef")
        self.assertEqual(ru.unquote_detail("Start"), "Start")

    def test_tail_follows_appends_and_partial_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "detail-20260727")
            open(path, "w").close()
            tail = ru.DetailTail([path], {}, from_start=True)
            self.assertEqual(tail.poll(), [])

            with open(path, "a") as fh:
                fh.write(DETAIL_RECORD)
            recs = tail.poll()
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["User-Name"], "jdoe@uni.fi")

            # a record that is still being written must not be emitted yet
            half = DETAIL_RECORD.replace('jdoe@uni.fi', 'asmith@uni.fi')
            with open(path, "a") as fh:
                fh.write(half[:60])
            self.assertEqual(tail.poll(), [])
            with open(path, "a") as fh:
                fh.write(half[60:])
            recs = tail.poll()
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["User-Name"], "asmith@uni.fi")

    def test_tail_handles_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "detail-20260727")
            with open(path, "w") as fh:
                fh.write(DETAIL_RECORD)
            tail = ru.DetailTail([os.path.join(tmp, "detail-*")], {},
                                 from_start=True)
            self.assertEqual(len(tail.poll()), 1)
            # same name, new inode — the file was rotated and replaced
            fresh = os.path.join(tmp, "fresh")
            with open(fresh, "w") as fh:
                fh.write(DETAIL_RECORD.replace("jdoe", "bnew"))
            os.rename(fresh, path)
            recs = tail.poll()
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["User-Name"], "bnew@uni.fi")

    def test_tail_handles_truncation_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "detail-20260727")
            with open(path, "w") as fh:
                fh.write(DETAIL_RECORD)
            tail = ru.DetailTail([path], {}, from_start=True)
            self.assertEqual(len(tail.poll()), 1)
            with open(path, "w") as fh:      # logrotate copytruncate
                fh.write(DETAIL_RECORD.replace("jdoe", "ctrunc"))
            recs = tail.poll()
            self.assertEqual([r["User-Name"] for r in recs], ["ctrunc@uni.fi"])

    def test_tail_starts_at_end_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "detail-20260727")
            with open(path, "w") as fh:
                fh.write(DETAIL_RECORD)
            tail = ru.DetailTail([path], {}, from_start=False)
            self.assertEqual(tail.poll(), [])


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------

class TestIdentity(unittest.TestCase):
    def ident(self, **over):
        return ru.Identity(args_for(**over))

    def test_class_beats_anonymous_outer_identity(self):
        rec = {"User-Name": "anonymous@uni.fi", "Class": b"uid:jdoe@uni.fi"}
        user, realm, kind = self.ident().resolve(rec)
        self.assertEqual((user, realm, kind), ("UNI\\jdoe", "uni.fi", "known"))

    def test_class_base64(self):
        rec = {"User-Name": "anonymous@uni.fi", "Class": b"uid:64:amRvZUB1bmkuZmk="}
        user, _, kind = self.ident().resolve(rec)
        self.assertEqual((user, kind), ("UNI\\jdoe", "known"))

    def test_foreign_class_ignored(self):
        rec = {"User-Name": "anonymous@other.ac.uk", "Class": b"\x01\x02\x03"}
        user, realm, kind = self.ident().resolve(rec)
        self.assertEqual((user, realm, kind),
                         ("eduroam-visitor@other.ac.uk", "other.ac.uk", "visitor"))

    def test_plain_inner_identity(self):
        user, _, kind = self.ident().resolve({"User-Name": "jdoe@uni.fi"})
        self.assertEqual((user, kind), ("UNI\\jdoe", "known"))

    def test_user_formats(self):
        rec = {"User-Name": "jdoe@uni.fi"}
        self.assertEqual(self.ident(user_format="upn").resolve(rec)[0], "jdoe@uni.fi")
        self.assertEqual(self.ident(user_format="bare").resolve(rec)[0], "jdoe")
        self.assertEqual(
            self.ident(realm_map=[]).resolve(rec)[0], "jdoe@uni.fi")

    def test_visitor_modes(self):
        rec = {"User-Name": "anonymous@fh-koeln.de"}
        self.assertIsNone(self.ident(visitors="skip").resolve(rec)[0])
        self.assertEqual(self.ident(visitors="anon").resolve(rec)[0],
                         "eduroam-visitor")
        self.assertEqual(self.ident(visitors="realm").resolve(rec)[0],
                         "eduroam-visitor@fh-koeln.de")

    def test_no_realm_anonymous_is_unmapped(self):
        self.assertIsNone(self.ident().resolve({"User-Name": "anonymous"})[0])
        self.assertIsNone(self.ident().resolve({})[0])

    def test_mac_auth_usernames_skipped(self):
        for name in ("aabbccddeeff", "AA-BB-CC-DD-EE-FF", "aa:bb:cc:dd:ee:ff"):
            self.assertIsNone(self.ident().resolve({"User-Name": name})[0], name)
        self.assertEqual(self.ident(mac_users=True).resolve(
            {"User-Name": "aabbccddeeff"})[0], "aabbccddeeff")

    def test_cui_fallback(self):
        rec = {"User-Name": "anonymous@uni.fi",
               "Chargeable-User-Identity": b"jdoe@uni.fi"}
        self.assertIsNone(self.ident().resolve(rec)[2] == "known" or None)
        self.assertEqual(self.ident(cui_as_user=True).resolve(rec)[0], "UNI\\jdoe")

    def test_cui_null_request_is_not_an_identity(self):
        rec = {"User-Name": "anonymous@uni.fi", "Chargeable-User-Identity": b"\x00"}
        user, _, kind = self.ident(cui_as_user=True).resolve(rec)
        self.assertEqual(kind, "visitor")

    def test_realm_is_after_the_last_at(self):
        self.assertEqual(ru.split_realm("odd@name@uni.fi"), ("odd@name", "uni.fi"))
        self.assertEqual(ru.split_realm("UNI\\jdoe"), ("jdoe", "uni"))

    def test_windows_style_names_keep_their_domain(self):
        rec = {"User-Name": "UNI\\jdoe"}
        self.assertEqual(self.ident(realm_map=[]).resolve(rec)[0], "UNI\\jdoe")
        self.assertEqual(self.ident(user_format="bare").resolve(rec)[0], "jdoe")
        self.assertEqual(self.ident(user_format="upn").resolve(rec)[0], "jdoe@uni")
        # an explicit mapping still wins over what the supplicant sent
        self.assertEqual(
            self.ident(realm_map=["uni=EXAMPLE"]).resolve(rec)[0], "EXAMPLE\\jdoe")

    def test_realm_tag_slug(self):
        self.assertEqual(ru.realm_tag("fh-koeln.de", "eduroam_"),
                         "eduroam_fh_koeln_de")


# --------------------------------------------------------------------------
# uid-message
# --------------------------------------------------------------------------

class TestUidMessage(unittest.TestCase):
    def test_login_logout(self):
        msg = ru.build_uid_message(
            logins=[("UNI\\jdoe", "10.0.0.1", 480)],
            logouts=[("UNI\\asmith", "10.0.0.2", None)])
        self.assertIn('<login><entry name="UNI\\jdoe" ip="10.0.0.1" timeout="480" />',
                      msg.replace(" />", " />"))
        self.assertIn('<logout>', msg)
        self.assertIn('ip="10.0.0.2"', msg)
        self.assertIn("<version>1.0</version>", msg)
        self.assertIn("<type>update</type>", msg)

    def test_logout_without_name(self):
        msg = ru.build_uid_message(logouts=[(None, "10.0.0.3", None)])
        self.assertIn('<entry ip="10.0.0.3"', msg)
        self.assertNotIn("name=", msg)

    def test_xml_escaping(self):
        msg = ru.build_uid_message(logins=[('UNI\\a"b&c<d', "10.0.0.4", 5)])
        self.assertNotIn('"b&c<d"', msg)
        self.assertIn("&amp;", msg)
        self.assertIn("&quot;", msg)

    def test_register_user_tags(self):
        msg = ru.build_uid_message(register=[("UNI\\jdoe", ["eduroam_uni_fi"], 3600)])
        self.assertIn("<register-user>", msg)
        self.assertIn('<member timeout="3600">eduroam_uni_fi</member>', msg)


# --------------------------------------------------------------------------
# batching
# --------------------------------------------------------------------------

class RecordingSender(ru.Sender):
    def __init__(self, **kw):
        super().__init__([], dry_run=True, **kw)
        self.messages = []

    def flush(self):
        if self._queued():
            self.messages.append(ru.build_uid_message(
                self.logins, self.logouts, self.register, self.unregister))
        super().flush()


class TestSender(unittest.TestCase):
    def test_batches_into_one_message(self):
        s = RecordingSender(batch_seconds=1e9)
        s.login("a", "10.0.0.1", 60)
        s.login("b", "10.0.0.2", 60)
        s.flush()
        self.assertEqual(len(s.messages), 1)
        self.assertEqual(s.messages[0].count("<entry"), 2)

    def test_conflicting_ip_forces_a_separate_message(self):
        # PAN-OS applies the login section before the logout section, so a
        # logout for an IP with a queued login must go in its own message.
        s = RecordingSender(batch_seconds=1e9)
        s.login("a", "10.0.0.1", 60)
        s.logout("a", "10.0.0.1")
        s.login("b", "10.0.0.1", 60)
        s.flush()
        self.assertEqual(len(s.messages), 3)
        self.assertIn("<login>", s.messages[0])
        self.assertIn("<logout>", s.messages[1])
        self.assertIn("<login>", s.messages[2])

    def test_max_batch_flushes(self):
        s = RecordingSender(batch_seconds=1e9, max_batch=2)
        s.login("a", "10.0.0.1", 60)
        s.login("b", "10.0.0.2", 60)
        self.assertEqual(len(s.messages), 1)


# --------------------------------------------------------------------------
# bridge behaviour
# --------------------------------------------------------------------------

class TestBridge(unittest.TestCase):
    def setUp(self):
        self.sender = StubSender()
        self.args = args_for()
        self.bridge = ru.Bridge(self.args, self.sender, ru.Identity(self.args))

    def rec(self, status, **over):
        base = {"Acct-Status-Type": status, "User-Name": "anonymous@uni.fi",
                "Class": b"uid:jdoe@uni.fi", "Acct-Session-Id": "s1",
                "NAS-IP-Address": "192.0.2.10",
                "Calling-Station-Id": "AA-BB-CC-DD-EE-FF"}
        base.update(over)
        return base

    def test_start_then_stop(self):
        self.bridge.handle(self.rec("Start", **{"Framed-IP-Address": "10.0.0.5"}))
        self.bridge.handle(self.rec("Stop", **{"Framed-IP-Address": "10.0.0.5"}))
        self.assertEqual(self.sender.calls,
                         [("login", "UNI\\jdoe", "10.0.0.5", 480),
                          ("logout", "UNI\\jdoe", "10.0.0.5")])

    def test_start_without_ip_then_interim_with_ip(self):
        # The common wireless case: the Start beats DHCP, so no IP yet.
        self.bridge.handle(self.rec("Start"))
        self.assertEqual(self.sender.calls, [])
        self.bridge.handle(self.rec("Interim-Update",
                                    **{"Framed-IP-Address": "10.0.0.6"}))
        self.assertEqual(self.sender.calls,
                         [("login", "UNI\\jdoe", "10.0.0.6", 480)])

    def test_stop_uses_remembered_ip(self):
        self.bridge.handle(self.rec("Start", **{"Framed-IP-Address": "10.0.0.7"}))
        self.bridge.handle(self.rec("Stop"))  # Stop often omits the address
        self.assertEqual(self.sender.calls[-1], ("logout", "UNI\\jdoe", "10.0.0.7"))

    def test_repeat_interim_does_not_resend(self):
        for _ in range(3):
            self.bridge.handle(self.rec("Interim-Update",
                                        **{"Framed-IP-Address": "10.0.0.8"}))
        self.assertEqual(len(self.sender.calls), 1)

    def test_address_change_within_a_session(self):
        self.bridge.handle(self.rec("Start", **{"Framed-IP-Address": "10.0.0.9"}))
        self.bridge.handle(self.rec("Interim-Update",
                                    **{"Framed-IP-Address": "10.0.0.10"}))
        self.assertEqual(self.sender.calls[1], ("logout", "UNI\\jdoe", "10.0.0.9"))
        self.assertEqual(self.sender.calls[2], ("login", "UNI\\jdoe", "10.0.0.10", 480))

    def test_stop_without_session_logs_out_by_ip_only(self):
        # No Start was seen and the record has no real identity: naming a
        # guessed user could delete the wrong mapping.
        self.bridge.handle({"Acct-Status-Type": "Stop",
                            "User-Name": "anonymous@other.ac.uk",
                            "Framed-IP-Address": "10.0.0.11"})
        self.assertEqual(self.sender.calls, [("logout", None, "10.0.0.11")])

    def test_accounting_off_clears_the_nas(self):
        self.bridge.handle(self.rec("Start", **{"Framed-IP-Address": "10.0.0.12"}))
        self.bridge.handle({"Acct-Status-Type": "Accounting-Off",
                            "NAS-IP-Address": "192.0.2.10"})
        self.assertEqual(self.sender.calls[-1], ("logout", "UNI\\jdoe", "10.0.0.12"))
        self.assertEqual(self.bridge.sessions, {})

    def test_tags_for_dynamic_user_groups(self):
        args = args_for(tag_realm=True, untag=True, tag_timeout=3600)
        bridge = ru.Bridge(args, self.sender, ru.Identity(args))
        bridge.handle(self.rec("Start", **{"Framed-IP-Address": "10.0.0.13"}))
        self.assertIn(("tag", "UNI\\jdoe", ("eduroam_uni_fi",), 3600),
                      self.sender.calls)
        bridge.handle(self.rec("Stop"))
        self.assertIn(("untag", "UNI\\jdoe", ("eduroam_uni_fi",)),
                      self.sender.calls)

    def test_visitor_gets_realm_and_visitor_tags(self):
        args = args_for(tag_realm=True)
        bridge = ru.Bridge(args, self.sender, ru.Identity(args))
        bridge.handle({"Acct-Status-Type": "Start",
                       "User-Name": "anonymous@fh-koeln.de",
                       "Acct-Session-Id": "s2", "NAS-IP-Address": "192.0.2.10",
                       "Framed-IP-Address": "10.0.0.14"})
        self.assertEqual(
            self.sender.calls,
            [("login", "eduroam-visitor@fh-koeln.de", "10.0.0.14", 480),
             ("tag", "eduroam-visitor@fh-koeln.de",
              ("eduroam_fh_koeln_de", "eduroam_visitor"), 0)])

    def test_mac_resolver_used_only_when_ip_missing(self):
        class Resolver:
            def __init__(self):
                self.hits = 0

            def lookup(self, mac):
                self.hits += 1
                return "10.0.0.15" if ru.normalize_mac(mac) else None

        resolver = Resolver()
        bridge = ru.Bridge(self.args, self.sender, ru.Identity(self.args), resolver)
        bridge.handle(self.rec("Start", **{"Framed-IP-Address": "10.0.0.16"}))
        self.assertEqual(resolver.hits, 0)
        bridge.handle(self.rec("Start", **{"Acct-Session-Id": "s9"}))
        self.assertEqual(resolver.hits, 1)
        self.assertEqual(self.sender.calls[-1], ("login", "UNI\\jdoe", "10.0.0.15", 480))

    def test_unusable_records_are_ignored(self):
        self.bridge.handle({"Acct-Status-Type": "Start",
                            "User-Name": "aabbccddeeff",
                            "Framed-IP-Address": "10.0.0.17"})
        self.bridge.handle({"Acct-Status-Type": "Start",
                            "User-Name": "jdoe@uni.fi",
                            "Framed-IP-Address": "0.0.0.0"})
        self.assertEqual(self.sender.calls, [])

    def test_sessions_expire_from_state(self):
        self.bridge.handle(self.rec("Start", **{"Framed-IP-Address": "10.0.0.18"}))
        for sess in self.bridge.sessions.values():
            sess["seen"] = 0
        self.bridge.expire()
        self.assertEqual(self.bridge.sessions, {})


class TestMisc(unittest.TestCase):
    def test_normalize_mac(self):
        for value in ("AA-BB-CC-DD-EE-FF", "aabb.ccdd.eeff", "aa:bb:cc:dd:ee:ff"):
            self.assertEqual(ru.normalize_mac(value), "aa:bb:cc:dd:ee:ff")
        self.assertIsNone(ru.normalize_mac("not-a-mac"))
        self.assertIsNone(ru.normalize_mac(""))

    def test_pick_ip_prefers_v4_then_v6(self):
        self.assertEqual(ru.pick_ip({"Framed-IP-Address": "10.0.0.1"}, None),
                         "10.0.0.1")
        self.assertEqual(ru.pick_ip({"Framed-IPv6-Address": "2001:db8::1"}, None),
                         "2001:db8::1")
        self.assertEqual(ru.pick_ip({"Framed-IPv6-Prefix": "2001:db8::2/128"}, None),
                         "2001:db8::2")
        self.assertIsNone(ru.pick_ip({"Framed-IPv6-Prefix": "2001:db8::/64"}, None))

    def test_state_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "state.json")
            ru.save_state(path, {"files": {"/a": {"ino": 1, "pos": 2}},
                                 "sessions": {"k": {"user": "u"}}})
            state = ru.load_state(path)
            self.assertEqual(state["files"]["/a"]["pos"], 2)
            self.assertEqual(state["sessions"]["k"]["user"], "u")

    def test_corrupt_state_file_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w") as fh:
                fh.write("{not json")
            self.assertEqual(ru.load_state(path), {"files": {}, "sessions": {}})


if __name__ == "__main__":
    unittest.main()
