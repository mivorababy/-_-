"""Real localhost integration checks; no external server or network access needed."""
import socket
import tempfile
import unittest
from pathlib import Path

from chat_service import ChatService, decode_invite, encode_invite, validate_server_url
from chat_server import clean_attachment


class ChatIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.host = ChatService(Path(self.temp.name) / "host")
        self.peer = ChatService(Path(self.temp.name) / "peer")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.config = {"chat_enabled": True, "chat_host_enabled": True,
                       "chat_host_port": self.port, "chat_token": "test-long-access-code"}
        self.host.configure(self.config)
        self.peer.configure({**self.config, "chat_host_enabled": False,
                             "chat_server_url": f"http://127.0.0.1:{self.port}"})

    def tearDown(self):
        self.peer.close()
        self.host.close()
        self.temp.cleanup()

    def session(self, service, nickname, client):
        status, payload = service.request("POST", "/api/chat/session", {"nickname": nickname, "client_id": client})
        self.assertEqual(status, 200, payload)
        return payload["token"]

    def test_two_clients_messages_attachments_and_nonce(self):
        # The same single code used by Windows and Android supplies the peer config.
        invite = decode_invite(encode_invite(f"http://127.0.0.1:{self.port}", self.config["chat_token"]))
        self.peer.configure({"chat_enabled": True, "chat_host_enabled": False,
                             "chat_server_url": invite["server_url"], "chat_token": invite["access_code"]})
        alice = self.session(self.host, "Alice", "alice-client-0123456789")
        bob = self.session(self.peer, "Боб", "bob-client-0123456789")
        message = {"room": "general", "text": "Послушай!", "kind": "track",
                   "attachment": {"title": "Song", "artist": "Artist", "url": "https://www.youtube.com/watch?v=abcdefghijk", "provider": "youtube"},
                   "nonce": "message-nonce-0123456789"}
        first = self.host.request("POST", "/api/chat/messages", message, alice)
        duplicate = self.host.request("POST", "/api/chat/messages", message, alice)
        self.assertEqual(first[0], 200, first)
        self.assertEqual(first[1]["message"]["id"], duplicate[1]["message"]["id"])
        status, messages = self.peer.request("GET", "/api/chat/messages?room=general&after=0&t=local-secret", session_token=bob)
        self.assertEqual(status, 200, messages)
        self.assertEqual(len(messages["items"]), 1)
        self.assertEqual(messages["items"][0]["nickname"], "Alice")
        self.assertEqual(messages["items"][0]["attachment"]["artist"], "Artist")
        self.assertNotIn("client", messages["items"][0])

    def test_wrong_code_unauthorized_and_disabled_shutdown(self):
        self.peer.configure({**self.config, "chat_host_enabled": False,
                             "chat_server_url": f"http://127.0.0.1:{self.port}", "chat_token": "wrong"})
        status, _ = self.peer.request("POST", "/api/chat/session", {"nickname": "Bob", "client_id": "bob-client-0123456789"})
        self.assertEqual(status, 403)
        self.assertEqual(self.peer.request("GET", "/api/chat/messages")[0], 401)
        self.host.configure({**self.config, "chat_enabled": False})
        self.assertFalse(self.host.status()["hosting"])
        self.assertIsNone(self.host.thread)
        self.assertEqual(self.host.request("GET", "/api/chat/messages")[0], 403)
        with socket.socket() as probe:
            probe.settimeout(.5)
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", self.port)), 0)

    def test_room_and_playlist_share(self):
        token = self.session(self.host, "Alice", "alice-client-0123456789")
        status, room = self.host.request("POST", "/api/chat/rooms", {"name": "Рекомендации"}, token)
        self.assertEqual(status, 200, room)
        self.assertEqual(self.peer.request("GET", "/api/chat/rooms", session_token=token)[0], 200)
        payload = {"room": room["room"]["id"], "kind": "playlist", "text": "", "nonce": "playlist-nonce-123456789",
                   "attachment": {"title": "Подборка", "tracks": [{"title": "Song", "url": "https://soundcloud.com/artist/song"}]}}
        status, sent = self.host.request("POST", "/api/chat/messages", payload, token)
        self.assertEqual(status, 200, sent)
        self.assertEqual(sent["message"]["attachment"]["tracks"][0]["title"], "Song")

    def test_payload_validation(self):
        for url in ("file:///C:/secret.mp3", "javascript:alert(1)", "https://example.org/file", "https://user:password@youtube.com/watch?v=x"):
            with self.assertRaises(ValueError):
                clean_attachment("track", {"title": "Bad", "url": url})
        for value in ("https://name:password@host", "http://host/path", "http://host?secret=1"):
            with self.assertRaises(ValueError):
                validate_server_url(value)

    def test_corrupt_database_does_not_crash_player(self):
        self.host.close()
        database = Path(self.temp.name) / "host" / "chat-data" / "chat.sqlite3"
        database.write_bytes(b"not a sqlite database")
        self.host.configure(self.config)
        self.assertFalse(self.host.status()["hosting"])
        self.assertTrue(self.host.status()["error"])


if __name__ == "__main__":
    unittest.main()
