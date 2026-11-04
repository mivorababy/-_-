#!/usr/bin/env python3
"""Shared LAST FREQUENCY chat relay. Run behind an HTTPS reverse proxy."""
import argparse
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
import unicodedata
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

MAX_BODY = 65536
SESSION_TTL = 7 * 86400
MUSIC_HOSTS = ("youtube.com", "youtu.be", "soundcloud.com", "spotify.com", "apple.com", "music.yandex.ru", "music.yandex.com", "music.yandex.kz")


def clean_text(value, limit, *, required=False):
    if not isinstance(value, str):
        raise ValueError("Ожидается строка")
    value = unicodedata.normalize("NFC", value).strip()
    value = "".join(ch for ch in value if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))
    if len(value) > limit or (required and not value):
        raise ValueError(f"Допустимая длина: 1–{limit} символов")
    return value


def music_url(value, *, required=True):
    value = clean_text(value or "", 2000)
    if not value and not required:
        return ""
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("Нужна HTTPS-ссылка на музыкальную площадку")
    if not any(host == item or host.endswith("." + item) for item in MUSIC_HOSTS):
        raise ValueError("Поддерживаются ссылки YouTube, SoundCloud, Spotify, Apple Music и Яндекс Музыки")
    return value


def clean_attachment(kind, value):
    if kind == "text":
        return None
    if kind not in ("track", "playlist") or not isinstance(value, dict):
        raise ValueError("Неподдерживаемое вложение")
    title = clean_text(value.get("title") or "Без названия", 180, required=True)
    if kind == "track":
        return {"title": title, "artist": clean_text(value.get("artist") or "", 180),
                "url": music_url(value.get("url")), "provider": clean_text(value.get("provider") or "", 40)}
    tracks = value.get("tracks") or []
    if not isinstance(tracks, list) or len(tracks) > 100:
        raise ValueError("В плейлисте должно быть не более 100 треков")
    result = {"title": title, "url": music_url(value.get("url"), required=False),
              "tracks": [clean_attachment("track", item) for item in tracks]}
    if not result["url"] and not result["tracks"]:
        raise ValueError("В плейлисте нет доступных ссылок")
    return result


class ChatStore:
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.Lock()
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS rooms (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE);
                INSERT OR IGNORE INTO rooms VALUES ('general', 'Общий чат');
                CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, client TEXT NOT NULL, nickname TEXT NOT NULL, expires INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT NOT NULL, nickname TEXT NOT NULL,
                    kind TEXT NOT NULL, text TEXT NOT NULL, attachment TEXT, created_at INTEGER NOT NULL,
                    client TEXT NOT NULL, nonce TEXT NOT NULL, UNIQUE(client, nonce));
                CREATE INDEX IF NOT EXISTS messages_room_id ON messages(room,id);
            """)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def new_session(self, nickname, client):
        nickname = clean_text(nickname, 32, required=True)
        if len(nickname) < 2:
            raise ValueError("Ник должен содержать хотя бы 2 символа")
        if not isinstance(client, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", client):
            raise ValueError("Некорректный идентификатор клиента")
        token = secrets.token_urlsafe(32)
        expires = int(time.time()) + SESSION_TTL
        with self.lock, self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires < ? OR client = ?", (int(time.time()), client))
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), client, nickname, expires))
        return {"token": token, "expires_at": expires, "nickname": nickname, "rooms": self.rooms()}

    def session(self, token):
        if not token or len(token) > 200:
            return None
        with self.connect() as db:
            row = db.execute("SELECT client,nickname,expires FROM sessions WHERE token=? AND expires>?", (hashlib.sha256(token.encode()).hexdigest(), int(time.time()))).fetchone()
        return dict(row) if row else None

    def rooms(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,name FROM rooms ORDER BY id!='general',name LIMIT 200")]

    def add_room(self, name):
        name = clean_text(name, 48, required=True)
        with self.lock, self.connect() as db:
            existing = db.execute("SELECT id,name FROM rooms WHERE name=?", (name,)).fetchone()
            if existing:
                return dict(existing)
            if db.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] >= 200:
                raise ValueError("Лимит комнат достигнут")
            room = {"id": secrets.token_urlsafe(12), "name": name}
            db.execute("INSERT INTO rooms VALUES (?,?)", (room["id"], room["name"]))
            return room

    @staticmethod
    def message(row):
        return {"id": row["id"], "room": row["room"], "nickname": row["nickname"],
                "kind": row["kind"], "text": row["text"], "created_at": row["created_at"],
                "attachment": json.loads(row["attachment"]) if row["attachment"] else None}

    def messages(self, room, after=0, limit=100):
        limit = max(1, min(int(limit), 100))
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM rooms WHERE id=?", (room,)).fetchone():
                raise ValueError("Комната не найдена")
            if after:
                rows = db.execute("SELECT * FROM messages WHERE room=? AND id>? ORDER BY id LIMIT ?", (room, after, limit)).fetchall()
            else:
                rows = list(reversed(db.execute("SELECT * FROM messages WHERE room=? ORDER BY id DESC LIMIT ?", (room, limit)).fetchall()))
        return {"items": [self.message(row) for row in rows], "cursor": rows[-1]["id"] if rows else after}

    def send(self, session, data):
        room = clean_text(data.get("room") or "general", 80, required=True)
        kind = data.get("kind") or "text"
        text = clean_text(data.get("text") or "", 2000)
        attachment = clean_attachment(kind, data.get("attachment"))
        nonce = data.get("nonce")
        if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", nonce):
            raise ValueError("Некорректный идентификатор сообщения")
        if not text and attachment is None:
            raise ValueError("Сообщение пустое")
        with self.lock, self.connect() as db:
            if not db.execute("SELECT 1 FROM rooms WHERE id=?", (room,)).fetchone():
                raise ValueError("Комната не найдена")
            db.execute("INSERT OR IGNORE INTO messages(room,nickname,kind,text,attachment,created_at,client,nonce) VALUES (?,?,?,?,?,?,?,?)",
                       (room, session["nickname"], kind, text, json.dumps(attachment, ensure_ascii=False) if attachment else None, int(time.time()), session["client"], nonce))
            row = db.execute("SELECT * FROM messages WHERE client=? AND nonce=?", (session["client"], nonce)).fetchone()
            db.execute("DELETE FROM messages WHERE id < (SELECT COALESCE(MAX(id),0)-20000 FROM messages)")
        return {"message": self.message(row)}


class ChatHandler(BaseHTTPRequestHandler):
    server_version = "LastFrequencyChat/1"

    def log_message(self, format, *args):
        # Never log query strings, nicknames, message bodies, or session credentials.
        return

    def respond(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_OPTIONS(self):
        self.respond(200, {})

    def do_GET(self):
        self.dispatch(False)

    def do_POST(self):
        self.dispatch(True)

    def dispatch(self, post):
        try:
            path = urlparse(self.path)
            if not post and path.path == "/health":
                return self.respond(200, {"ok": True, "service": "last-frequency-chat"})
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            data = {}
            if post:
                size = int(self.headers.get("Content-Length") or 0)
                if not 0 < size <= MAX_BODY:
                    return self.respond(413, {"error": "Сообщение слишком большое или пустое"})
                if self.headers.get_content_type() != "application/json":
                    return self.respond(415, {"error": "Ожидается JSON"})
                data = json.loads(self.rfile.read(size).decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("Ожидается JSON-объект")
            if post and path.path == "/api/chat/session":
                key = self.server.shared_key
                if key and not hmac.compare_digest(token.encode("utf-8"), key.encode("utf-8")):
                    return self.respond(403, {"error": "Неверный ключ сервера"})
                if not self.server.allow("session:" + self.client_address[0], 20, 60):
                    return self.respond(429, {"error": "Слишком много подключений. Подожди минуту."})
                return self.respond(200, self.server.store.new_session(data.get("nickname"), data.get("client_id")))
            session = self.server.store.session(token)
            if not session:
                return self.respond(401, {"error": "Сессия истекла. Подключись заново."})
            if not self.server.allow("read:" + session["client"], 90, 60):
                return self.respond(429, {"error": "Слишком много запросов. Подожди минуту."})
            if path.path == "/api/chat/rooms":
                if post:
                    if not self.server.allow("rooms:" + session["client"], 5, 60):
                        return self.respond(429, {"error": "Слишком много новых комнат"})
                    return self.respond(200, {"room": self.server.store.add_room(data.get("name"))})
                return self.respond(200, {"rooms": self.server.store.rooms()})
            if path.path == "/api/chat/messages":
                if post:
                    if not self.server.allow("send:" + session["client"], 20, 60):
                        return self.respond(429, {"error": "Можно отправить до 20 сообщений в минуту"})
                    return self.respond(200, self.server.store.send(session, data))
                query = parse_qs(path.query)
                return self.respond(200, self.server.store.messages((query.get("room") or ["general"])[0], max(0, int((query.get("after") or ["0"])[0])), int((query.get("limit") or ["100"])[0])))
            return self.respond(404, {"error": "Адрес не найден"})
        except (ValueError, TypeError, UnicodeError, OverflowError):
            self.respond(400, {"error": "Некорректные данные сообщения или ссылки"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.respond(500, {"error": "Сервер временно недоступен"})


class ChatServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, store, shared_key=""):
        super().__init__(address, ChatHandler)
        self.store = store
        self.shared_key = shared_key
        self.rates = defaultdict(deque)
        self.rate_lock = threading.Lock()
        self.connections = threading.BoundedSemaphore(32)
        self.client_sockets = set()
        self.client_lock = threading.Lock()

    def process_request(self, request, client_address):
        if not self.connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(15)
        with self.client_lock:
            self.client_sockets.add(connection)
        return connection, address

    def close_request(self, request):
        with self.client_lock:
            self.client_sockets.discard(request)
        super().close_request(request)

    def close_clients(self):
        with self.client_lock:
            clients = list(self.client_sockets)
        for connection in clients:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

    def allow(self, key, count, interval):
        now = time.monotonic()
        with self.rate_lock:
            if len(self.rates) > 10000:
                for stale in [name for name, values in self.rates.items() if not values or values[-1] < now - 60]:
                    self.rates.pop(stale, None)
                if len(self.rates) > 10000:
                    return False
            values = self.rates[key]
            while values and values[0] <= now - interval:
                values.popleft()
            if len(values) >= count:
                return False
            values.append(now)
            return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--database", default="chat-data/chat.sqlite3")
    args = parser.parse_args()
    database = Path(args.database).expanduser().resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    server = ChatServer((args.host, args.port), ChatStore(database), os.environ.get("CHAT_SERVER_KEY", ""))
    print(f"LAST FREQUENCY chat listening on {args.host}:{args.port}; expose through HTTPS reverse proxy")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
