"""Invitation interoperability and authenticated localhost API, without player startup."""
import ast
import base64
import json
import logging
import secrets
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlparse

from chat_service import ChatService, decode_invite, encode_invite

ROOT = Path(__file__).resolve().parents[1]


def raw_code(value):
    raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=True).encode("utf-8")
    return "LFCHAT1." + base64.urlsafe_b64encode(raw).decode().rstrip("=")


class InviteCodecTests(unittest.TestCase):
    def test_shared_mobile_fixtures(self):
        for item in json.loads((ROOT / "tests/fixtures/chat-invites.json").read_text(encoding="utf-8")):
            with self.subTest(url=item["url"]):
                self.assertEqual(encode_invite(item["url"], item["key"]), item["code"])
                self.assertEqual(decode_invite(" \n" + item["code"] + "\n"),
                                 {"server_url": item["url"], "access_code": item["key"]})

    def test_invalid_invites_do_not_touch_network(self):
        values = [None, "", "other", "LFCHAT2.abc", "LFCHAT1.A", "LFCHAT1.abc=", "LFCHAT1.!!!",
                  "LFCHAT1." + "a" * 4096, raw_code(b"\xff"), raw_code([]), raw_code({}),
                  raw_code(b'{"v":1,"v":1,"url":"http://host","key":"x"}')]
        for version in (True, 1.0, 2, "1", None):
            values.append(raw_code({"v": version, "url": "http://host", "key": "key"}))
        for url in ("", "file:///tmp", "http://user@host", "http://host/path", "http://host//",
                    "http://host?", "http://host#", "http://host:0", "http://host:65536",
                    "http://host:", "http://host\\path", "http://ho\nst", "http://ho st",
                    "http://[invalid", "http://\ud800:8786", "http://пример.рф", " http://host", 32):
            values.append(raw_code({"v": 1, "url": url, "key": "key"}))
        for key in (None, "", "x" * 129, "line\nbreak", "control\x85", "ключ", "🎵", 42):
            values.append(raw_code({"v": 1, "url": "http://host", "key": key}))
        values.append(raw_code({"v": 1, "url": "http://host", "key": "key", "extra": 1}))
        with patch("socket.getaddrinfo", side_effect=AssertionError("must not resolve")), \
                patch("socket.socket", side_effect=AssertionError("must not connect")):
            for value in values:
                with self.subTest(value=str(value)[:70]), self.assertRaises(ValueError):
                    decode_invite(value)
            self.assertEqual(decode_invite(encode_invite("http://not-resolved.invalid/", " key ")),
                             {"server_url": "http://not-resolved.invalid", "access_code": " key "})


class MemorySettings:
    def __init__(self):
        self.data = {"chat_enabled": False}

    def get(self):
        return dict(self.data)

    def update(self, patch):
        self.data.update(patch)
        return self.get()


class InviteApiTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / "player.py").read_text(encoding="utf-8"))
        handler = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == "Handler")
        self.temp = tempfile.TemporaryDirectory()
        state = SimpleNamespace(settings=MemorySettings(), token="local-player-token", last_seen=0)
        self.ns = {"BaseHTTPRequestHandler": BaseHTTPRequestHandler, "VERSION": "test", "STATE": state,
                   "logger": logging.getLogger("invite-test"), "json": json, "secrets": secrets,
                   "time": time, "urlparse": urlparse, "parse_qs": parse_qs, "unquote": unquote,
                   "MAX_JSON_BYTES": 65536, "Path": Path, "_CHAT_SERVICE": None}

        def configure(settings):
            if self.ns["_CHAT_SERVICE"] is None:
                self.ns["_CHAT_SERVICE"] = ChatService(self.temp.name)
            self.ns["_CHAT_SERVICE"].configure(settings)

        self.ns["configure_chat"] = configure
        exec(compile(ast.Module(body=[handler], type_ignores=[]), "player-handler", "exec"), self.ns)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.ns["Handler"])
        state.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{state.port}"

    def tearDown(self):
        if self.ns["_CHAT_SERVICE"] is not None:
            self.ns["_CHAT_SERVICE"].close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def post(self, action, data, token="local-player-token"):
        req = urllib.request.Request(self.base + "/api/chat/invite/" + action,
                                     json.dumps(data).encode(), {"Content-Type": "application/json", "X-Token": token})
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    def test_disabled_parse_and_join_validate_before_changing_settings(self):
        code = encode_invite("http://unreachable.invalid:8786", "friend-key")
        self.assertEqual(self.post("join", {"invite_code": code, "nickname": "Bob"}, token="bad")[0], 403)
        status, values = self.post("parse", {"invite_code": code})
        self.assertEqual(status, 200)
        self.assertEqual(values["access_code"], "friend-key")
        self.assertIsNone(self.ns["_CHAT_SERVICE"], "parsing while off must not create any chat service")
        self.assertEqual(self.post("join", {"invite_code": code, "nickname": "x"})[0], 400)
        self.assertFalse(self.ns["STATE"].settings.get()["chat_enabled"])
        self.assertEqual(self.post("join", {"invite_code": "bad", "nickname": "Bob"})[0], 400)
        with patch("socket.getaddrinfo", wraps=socket.getaddrinfo) as lookup:
            status, joined = self.post("join", {"invite_code": code, "nickname": "Bob"})
        self.assertTrue(all(call.args[0] == "127.0.0.1" for call in lookup.call_args_list))
        self.assertEqual(status, 200, joined)
        self.assertTrue(joined["settings"]["chat_enabled"])
        self.assertEqual(joined["settings"]["chat_server_url"], "http://unreachable.invalid:8786")
        self.assertIsNone(self.ns["_CHAT_SERVICE"].thread)
        self.assertIsNone(self.ns["_CHAT_SERVICE"].server)
        self.assertEqual(self.post("create", {"url": "http://host:8786"})[0], 409)

    def test_host_invitation_connects_peer_and_join_stops_old_host(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        settings = self.ns["STATE"].settings.update({"chat_enabled": True, "chat_host_enabled": True,
                                                     "chat_host_port": port, "chat_token": "shared-key"})
        self.ns["configure_chat"](settings)
        status, created = self.post("create", {"url": f"http://127.0.0.1:{port}"})
        self.assertEqual(status, 200, created)
        decoded = decode_invite(created["invite_code"])
        peer = ChatService(Path(self.temp.name) / "peer")
        try:
            peer.configure({"chat_enabled": True, "chat_server_url": decoded["server_url"],
                            "chat_token": decoded["access_code"]})
            status, session = peer.request("POST", "/api/chat/session",
                                           {"nickname": "Peer", "client_id": "peer-client-0123456789"})
            self.assertEqual(status, 200, session)
            sent = peer.request("POST", "/api/chat/messages", {"room": "general", "kind": "text",
                                 "text": "Invitation works", "nonce": "invite-message-12345678"}, session["token"])
            self.assertEqual(sent[0], 200, sent)
            code = encode_invite("http://new-friend.invalid:8786", "new-key")
            self.assertEqual(self.post("join", {"invite_code": code, "nickname": "Host"})[0], 200)
            self.assertIsNone(self.ns["_CHAT_SERVICE"].thread)
            with socket.socket() as probe:
                probe.settimeout(.5)
                self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)
        finally:
            peer.close()


if __name__ == "__main__":
    unittest.main()
