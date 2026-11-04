"""Optional user-hosted chat lifecycle and localhost API relay for the desktop player."""
import base64
import binascii
import json
import re
import socket
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from chat_server import ChatServer, ChatStore


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_server_url(value):
    value = str(value or "").strip()
    if not value:
        return ""
    if len(value) > 2000 or any(ch.isspace() or not 32 <= ord(ch) <= 126 or ch == "\\" for ch in value):
        raise ValueError("Некорректный адрес сервера")
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
    except ValueError:
        raise ValueError("Некорректный адрес сервера") from None
    if (parsed.scheme not in ("http", "https") or not hostname
            or parsed.username is not None or parsed.password is not None or parsed.path not in ("", "/")
            or "?" in value or "#" in value or parsed.netloc.endswith(":")):
        raise ValueError("Укажи адрес сервера вида http://IP:8786 или https://chat.example.com")
    try:
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
    except ValueError:
        raise ValueError("Некорректный порт сервера") from None
    return value.rstrip("/")


def validate_access_code(key, *, required=True):
    # Authorization headers carry this key directly; Unicode/control characters
    # cannot be represented consistently by desktop and Android HTTP clients.
    if (not isinstance(key, str) or len(key) > 128 or (required and not key)
            or any(not 32 <= ord(ch) <= 126 for ch in key)):
        raise ValueError("Ключ чата: 1–128 латинских букв, цифр или знаков ASCII")
    return key


def _invite_values(url, key):
    if not isinstance(url, str) or not url or url != url.strip():
        raise ValueError("В приглашении отсутствует адрес чата")
    url = validate_server_url(url)
    return {"server_url": url, "access_code": validate_access_code(key)}


def encode_invite(url, key):
    values = _invite_values(url, key)
    raw = json.dumps({"v": 1, "url": values["server_url"], "key": values["access_code"]},
                     ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "LFCHAT1." + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_invite(code):
    """Decode locally, without opening a socket, resolving a host or enabling chat."""
    if not isinstance(code, str) or len(code) > 4096:
        raise ValueError("Некорректный код приглашения")
    code = code.strip()
    if not code.startswith("LFCHAT1."):
        raise ValueError("Нужен полный код приглашения LFCHAT1.…")
    encoded = code[8:]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", encoded) or len(encoded) % 4 == 1:
        raise ValueError("Код приглашения повреждён")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Код приглашения повреждён")
            result[key] = value
        return result

    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, binascii.Error):
        raise ValueError("Код приглашения повреждён") from None
    if (not isinstance(data, dict) or set(data) != {"v", "url", "key"}
            or type(data["v"]) is not int or data["v"] != 1):
        raise ValueError("Версия приглашения не поддерживается")
    return _invite_values(data["url"], data["key"])


class ChatService:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.lock = threading.RLock()
        self.settings = {}
        self.server = None
        self.thread = None
        self.signature = None
        self.error = ""

    def configure(self, settings):
        values = {key: settings.get(key) for key in
                  ("chat_enabled", "chat_host_enabled", "chat_host_port", "chat_server_url", "chat_token")}
        values["chat_host_port"] = int(values.get("chat_host_port") or 8786)
        signature = (bool(values.get("chat_enabled")), bool(values.get("chat_host_enabled")),
                     values["chat_host_port"], str(values.get("chat_token") or ""))
        with self.lock:
            self.settings = values
            if signature == self.signature and (not signature[0] or not signature[1] or self.server is not None):
                return
            self._stop()
            self.signature = signature
            self.error = ""
            if not signature[0] or not signature[1]:
                return
            try:
                database = self.data_dir / "chat-data" / "chat.sqlite3"
                database.parent.mkdir(parents=True, exist_ok=True)
                self.server = ChatServer(("0.0.0.0", values["chat_host_port"]), ChatStore(database), str(values.get("chat_token") or ""))
                self.thread = threading.Thread(target=self.server.serve_forever, name="player-chat-host", daemon=True)
                self.thread.start()
            except (OSError, ValueError, sqlite3.Error, RuntimeError) as error:
                self.error = "Не удалось открыть порт чата: " + str(error)[:180]
                self._stop()

    def _stop(self):
        if self.server is not None:
            if self.thread is not None and self.thread.is_alive():
                self.server.shutdown()
            self.server.close_clients()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.server = self.thread = None

    def close(self):
        with self.lock:
            self._stop()
            self.signature = None
            self.settings = {}

    def status(self):
        with self.lock:
            settings, error = dict(self.settings), self.error
            hosting = self.server is not None
        addresses = []
        # No public IP lookup, background thread, or discovery traffic. Resolve the
        # machine's configured addresses only when the user requests host status.
        if hosting:
            try:
                addresses = sorted({item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET)
                                    if not item[4][0].startswith("127.")})
            except OSError:
                pass
        return {"enabled": bool(settings.get("chat_enabled")), "hosting": hosting,
                "port": int(settings.get("chat_host_port") or 8786),
                "server_url": str(settings.get("chat_server_url") or ""),
                "lan_addresses": addresses, "error": error}

    def request(self, method, path, payload=None, session_token=""):
        parsed_path = urlparse(path)
        if parsed_path.path == "/api/chat/status" and method == "GET":
            return 200, self.status()
        with self.lock:
            settings = dict(self.settings)
            hosting = self.server is not None
            error = self.error
        if not settings.get("chat_enabled"):
            return 403, {"error": "Чат выключен"}
        if parsed_path.path not in ("/api/chat/session", "/api/chat/rooms", "/api/chat/messages") or method not in ("GET", "POST"):
            return 404, {"error": "Адрес чата не найден"}
        if settings.get("chat_host_enabled"):
            if not hosting:
                return 503, {"error": error or "Хост чата не запущен"}
            base = "http://127.0.0.1:" + str(settings["chat_host_port"])
        else:
            try:
                base = validate_server_url(settings.get("chat_server_url"))
            except ValueError as error:
                return 400, {"error": str(error)}
            if not base:
                return 400, {"error": "Укажи адрес компьютера, который создал чат"}
        token = str(settings.get("chat_token") or "") if parsed_path.path == "/api/chat/session" else str(session_token or "")
        if len(token) > 512 or any(not 32 <= ord(ch) <= 126 for ch in token):
            return 400, {"error": "Некорректный ключ чата"}
        headers = {"Accept": "application/json", "Authorization": "Bearer " + token}
        body = None
        if method == "POST":
            body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
            if len(body) > 65536:
                return 413, {"error": "Сообщение слишком большое"}
            headers["Content-Type"] = "application/json"
        query = parse_qs(parsed_path.query)
        safe_query = urlencode({key: query[key][0] for key in ("room", "after", "limit") if key in query})
        request = urllib.request.Request(base + parsed_path.path + ("?" + safe_query if safe_query else ""), data=body, headers=headers, method=method)
        try:
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=8) as response:
                raw = response.read(1024 * 1024)
                result = json.loads(raw.decode("utf-8"))
                if not isinstance(result, dict):
                    raise ValueError("Некорректный ответ")
                return 200, result
        except urllib.error.HTTPError as error:
            try:
                result = json.loads(error.read(65536).decode("utf-8"))
                return error.code, {"error": str(result.get("error") or "Ошибка сервера чата")[:300]}
            except (ValueError, UnicodeError, AttributeError):
                return error.code, {"error": "Сервер чата отклонил запрос"}
            finally:
                error.close()
        except (OSError, ValueError, urllib.error.URLError):
            return 502, {"error": "Нет связи с хостом. Проверь адрес, открытый порт/VPN и запущен ли плеер у собеседника."}
