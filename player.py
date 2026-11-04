#!/usr/bin/env python3
"""NOT soundLARP 100% — локальный музыкальный плеер.

Маленький веб-сервер на стандартной библиотеке (127.0.0.1) + веб-интерфейс из папки web/.
Окно открывается в режиме приложения Edge/Chrome. Слушает треки из папок-источников
(по умолчанию — папка бота downloads/SoundCloud и папка music), показывает текущий трек
в Discord (Rich Presence, нужен pypresence) и умеет менять тему и фон.
"""

import base64
import hashlib
import difflib
import filecmp
import html
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import zipfile
import webbrowser
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse, urljoin
from mini_player import FloatingMiniPlayer

logger = logging.getLogger("player")

VERSION = "1.32-last-frequency"
APP_NAME = "NOT soundLARP 100%"
APP_EXE_NAME = APP_NAME + ".exe"
APP_SETUP_NAME = APP_NAME + "-Setup.exe"
MAX_LAYERS = 8  # сколько треков можно играть одновременно в режиме «мульти-треки»
APP_ID = "last-frequency"
DEFAULT_PORT = 8775
DISCORD_CLIENT_ID = "1558464674985410580"
_INSTANCE_MUTEX = None
_ACTIVE_PERFORMANCE_MODE = "eco_offline"
_ACTIVE_CLIPS_ENABLED = True
_AUTO_OFFLINE_FALLBACK = False
PERFORMANCE_MODES = ("eco_online", "online", "eco_offline", "offline")
ECO_MODES = ("eco_online", "eco_offline")
ONLINE_MODES = ("eco_online", "online")
DOWNLOAD_MODES = ONLINE_MODES  # онлайн-поиск/скачивание доступны только в режимах с интернетом

# Online streams are proxied through localhost so WebAudio/CORS cannot mute otherwise valid sources.
ONLINE_STREAMS = {}
ONLINE_STREAMS_LOCK = threading.Lock()
ONLINE_STREAM_TTL = 60 * 60
ONLINE_RESOLVE_CACHE = {}
ONLINE_RESOLVE_LOCKS = {}
ONLINE_RESOLVE_GUARD = threading.Lock()
ONLINE_FAILURE_LOGGED = {}
_ONLINE_STREAM_HOSTS = (
    "googlevideo.com", "youtube.com", "youtu.be", "soundcloud.com", "sndcdn.com",
    "soundcloud.cloud",
    "deezer.com", "dzcdn.net", "itunes.apple.com", "apple.com", "mzstatic.com",
)
_LYRICS_CACHE: dict[tuple, tuple[float, dict]] = {}
_LYRICS_CACHE_LOCK = threading.Lock()
GITHUB_RELEASES_URL = "https://api.github.com/repos/mivorababy/-_-/releases/latest"
GITHUB_REPO_URL = "https://github.com/mivorababy/-_-"
_UPDATE_CACHE = {"checked_at": 0.0, "payload": None}
_UPDATE_CACHE_LOCK = threading.Lock()
_UPDATE_DOWNLOAD_JOBS = {}
_UPDATE_DOWNLOAD_JOBS_LOCK = threading.Lock()
_CHAT_SERVICE = None


def configure_chat(settings: dict) -> None:
    """No chat module, network listener or polling is created while chat is disabled."""
    global _CHAT_SERVICE
    if not settings.get("chat_enabled", False):
        if _CHAT_SERVICE is not None:
            _CHAT_SERVICE.close()
            _CHAT_SERVICE = None
        return
    if _CHAT_SERVICE is None:
        from chat_service import ChatService
        _CHAT_SERVICE = ChatService(PLAYER_DIR)
    try:
        _CHAT_SERVICE.configure(settings)
    except Exception:
        logger.exception("Не удалось включить необязательный чат")
        _CHAT_SERVICE.close()
        _CHAT_SERVICE = None


class _OnlineStreamDrmError(RuntimeError):
    """The extractor explicitly reported a DRM-protected source."""
RESTART_SETTLE_SECONDS = 1.5    # короткая пауза после выхода старого процесса: порт, mutex и WebView2 успевают освободиться
RESTART_WAIT_LIMIT = 20         # сколько ждать закрытия старого процесса, прежде чем завершить его принудительно
FORCE_EXIT_SECONDS = 8          # страховка: если выход завис, процесс завершается сам
DOWNLOAD_IDLE_SHUTDOWN_SECONDS = 60   # загрузчик MP3 выключается сам через столько секунд после последней загрузки
VISUALIZER_FPS_CHOICES = (10, 15, 20, 30, 45, 60)
MAX_VOLUME_BOOST = 25           # верхняя граница усилителя громкости, ×

FROZEN = bool(getattr(sys, "frozen", False))  # True, когда запущен собранный .exe
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))  # web/ лежит внутри .exe


def _data_dir() -> Path:
    """Хранить настройки и музыку рядом с приложением, чтобы удаление папки удаляло данные."""
    if not FROZEN:
        return Path(__file__).resolve().parent
    target = Path(sys.executable).resolve().parent
    target.mkdir(parents=True, exist_ok=True)
    legacy = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "SoundLarpPlayer-Rewritten"
    try:
        if legacy.resolve() != target.resolve() and legacy.is_dir():
            def copy_missing(source, destination):
                destination = Path(destination)
                if destination.exists():
                    return str(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                return shutil.copy2(source, destination)

            # Merge older profiles without overwriting newer files already beside the EXE.
            for item in legacy.iterdir():
                destination = target / item.name
                if item.is_dir():
                    shutil.copytree(item, destination, dirs_exist_ok=True, copy_function=copy_missing)
                elif item.is_file():
                    copy_missing(item, destination)
            # All old files are now beside the EXE; uninstalling the app removes the whole profile.
            shutil.rmtree(legacy)
    except OSError as error:
        # Do not silently fall back to AppData: the requested profile location is beside the EXE.
        raise RuntimeError(f"Не удалось перенести профиль плеера рядом с приложением: {error}") from error
    return target


PLAYER_DIR = _data_dir()
WEB_DIR = RESOURCE_DIR / "web"
BG_DIR = PLAYER_DIR / "backgrounds"
CLIPS_DIR = PLAYER_DIR / "clips"
SETTINGS_FILE = PLAYER_DIR / "player_settings.json"
CACHE_FILE = PLAYER_DIR / "library_cache.json"
COVER_URL_FILE = PLAYER_DIR / "discord_covers.json"   # кэш: обложка -> временная ссылка
PLAY_HISTORY_FILE = PLAYER_DIR / "play_history.json"   # статистика и недавние прослушивания
CUSTOM_PLAYLISTS_FILE = PLAYER_DIR / "custom_playlists.json"
REMOTE_FAVORITES_FILE = PLAYER_DIR / "remote_favorites.json"
LOG_DIR = PLAYER_DIR / "logs"
LOG_FILE = LOG_DIR / "player.log"
MUSIC_DIR = PLAYER_DIR / "music"  # сюда можно просто кидать свои треки
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".aac"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
COVER_NAMES = {"cover", "folder", "front", "album", "artwork"}
CONTENT_TYPES = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".flac": "audio/flac", ".ogg": "audio/ogg",
    ".opus": "audio/ogg", ".wav": "audio/wav", ".aac": "audio/aac",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
    ".gif": "image/gif", ".mp4": "video/mp4",
}
MAX_BACKGROUND_BYTES = 25 * 1024 * 1024

def _ensure_bundled_sample_playlist() -> None:
    sample = (RESOURCE_DIR / "sample-playlist") if FROZEN else (Path(__file__).resolve().parent / "assets" / "sample-playlist")
    destination = MUSIC_DIR / "Добро и позитив"
    if not sample.is_dir():
        return
    try:
        marker = destination / ".last_frequency_sample_playlist"
        if destination.is_dir():
            if not marker.is_file():
                bundled_files = [item for item in sample.iterdir() if item.is_file()]
                existing_files = [item for item in destination.iterdir() if item.is_file()]
                # A migrated folder is deletable as a sample only if every bundled file matches byte-for-byte.
                if bundled_files and len(existing_files) == len(bundled_files) and \
                        {item.name for item in existing_files} == {item.name for item in bundled_files} and all((destination / item.name).is_file() and
                                         filecmp.cmp(destination / item.name, item, shallow=False)
                                         for item in bundled_files):
                    marker.write_text("bundled sample\n", encoding="utf-8")
            return
        MUSIC_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copytree(sample, destination)
        marker.write_text("bundled sample\n", encoding="utf-8")
    except OSError as error:
        logger.warning("Не удалось установить тестовый плейлист: %s", error)

_ensure_bundled_sample_playlist()
MAX_JSON_BYTES = 8 * 1024 * 1024

# id темы -> (цвет фона, тёмная ли тема, акцент). Заголовок окна красится от АКЦЕНТА темы,
# слегка притемнённого фоном: розовый для «Сердечки», оранжевый для «Огонь» и т.д.
_WINDOWS_THEME_NAMES = {
    "midnight": ("#0f1020", True, "#8b95ff"),
    "neon": ("#07020f", True, "#ff3df0"),
    "sunset": ("#1f0f1d", True, "#ff8a5c"),
    "forest": ("#07130f", True, "#3ddc97"),
    "ocean": ("#04121c", True, "#38bdf8"),
    "rose": ("#1c0b14", True, "#fb7185"),
    "hearts": ("#2b1024", True, "#ff8fc8"),
    "mono": ("#0a0a0a", True, "#ffffff"),
    "fire": ("#050302", True, "#ff7a18"),
    "zxc": ("#08080b", True, "#ff1744"),
    "omori": ("#050505", True, "#ffffff"),
    "maretu": ("#080808", True, "#f2f2f2"),
    "dota": ("#0b1016", True, "#c99b55"),
    "deadcells": ("#101214", True, "#50d5bd"),
    "daylight": ("#f4f6fb", False, "#4f5bd5"),
    "paper": ("#faf6ee", False, "#c2410c"),
    "spotify": ("#121212", True, "#1ed760"),
    "discord": ("#1e1f22", True, "#5865f2"),
    "youtube": ("#0f0f0f", True, "#ff0033"),
    "soundcloud": ("#160f0a", True, "#ff5500"),
    "blood": ("#100306", True, "#ff2438"),
}
_HEX6 = re.compile(r"^#[0-9a-fA-F]{6}$")


def _hex_rgb_int(value: str) -> int:
    value = value.lstrip("#")
    try:
        r, g, b = int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
        return r | (g << 8) | (b << 16)      # COLORREF = 0x00BBGGRR
    except Exception:
        return 0x101010


def _rgb(value: str):
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def window_caption_colors(theme_id: str, accent: str = ""):
    """Цвет заголовка окна и цвет его текста для темы. Формулу повторяет JS (windowCaptionColor)."""
    bg, dark, theme_accent = _WINDOWS_THEME_NAMES.get(theme_id, _WINDOWS_THEME_NAMES["midnight"])
    base = accent if _HEX6.match(accent or "") else theme_accent
    ar, ag, ab = _rgb(base)
    br, bgc, bb = _rgb(bg)
    r, g, b = (int(x * .75 + y * .25 + .5) for x, y in ((ar, br), (ag, bgc), (ab, bb)))
    luminance = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
    return "#%02x%02x%02x" % (r, g, b), ("#1a1a1a" if luminance > .6 else "#ffffff"), dark


_PLAYER_HWND = None
_NATIVE_WINDOW = None
_NATIVE_HWND = None
_APP_ICON_HANDLE = None
_NATIVE_IS_MAXIMIZED = False
_EXIT_REQUESTED = False


def _apply_native_window_icon(hwnd):
    """Apply the bundled application icon to the native window and task switcher."""
    global _APP_ICON_HANDLE
    if os.name != "nt" or not hwnd:
        return
    icon_path = RESOURCE_DIR / "icon.ico"
    if not icon_path.is_file():
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        if not _APP_ICON_HANDLE:
            user32.LoadImageW.argtypes = (wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                          ctypes.c_int, ctypes.c_int, wintypes.UINT)
            user32.LoadImageW.restype = wintypes.HANDLE
            handle = user32.LoadImageW(None, str(icon_path), 1, 0, 0, 0x00000010 | 0x00000040)
            if not handle:
                return
            _APP_ICON_HANDLE = int(handle.value if hasattr(handle, "value") else handle)
        user32.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.SendMessageW(wintypes.HWND(hwnd), 0x0080, 0, _APP_ICON_HANDLE)  # WM_SETICON, ICON_SMALL
        user32.SendMessageW(wintypes.HWND(hwnd), 0x0080, 1, _APP_ICON_HANDLE)  # WM_SETICON, ICON_BIG
    except Exception:
        logger.debug("Не удалось применить значок окна из icon.ico", exc_info=True)


def find_player_hwnd(include_hidden=False):
    """HWND окна плеера (Edge/Chrome в режиме приложения).

    Заголовок окна меняется («Трек · NOT soundLARP 100%»), поэтому ищем по вхождению, а не по началу
    строки, и запоминаем найденное окно."""
    global _PLAYER_HWND
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    if _PLAYER_HWND and user32.IsWindow(_PLAYER_HWND) and (include_hidden or user32.IsWindowVisible(_PLAYER_HWND)):
        return _PLAYER_HWND
    found = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def enum_proc(candidate, _):
        if not include_hidden and not user32.IsWindowVisible(candidate):
            return True
        length = user32.GetWindowTextLengthW(candidate)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(candidate, buf, length + 1)
        if APP_NAME in buf.value or "LAST FREQUENCY" in buf.value:
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(candidate, cls, 256)
            if cls.value.startswith(("Chrome_WidgetWin", "HwndWrapper", "WindowsForms10.Window")):
                found.append(candidate)
                return False
        return True
    user32.EnumWindows(EnumWindowsProc(enum_proc), 0)
    _PLAYER_HWND = found[0] if found else None
    return _PLAYER_HWND


def set_windows_window_theme(theme_id: str, accent: str = "", hwnd=None):
    """Тёмный/светлый режим системного заголовка окна и (Windows 11) его цвет. True, если окно найдено."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        dwmapi = ctypes.windll.dwmapi
        if hwnd is None:
            hwnd = _NATIVE_HWND or find_player_hwnd()
        if not hwnd:
            logger.debug("Цвет заголовка: окно ещё не найдено (тема %s)", theme_id)
            return False
        caption, text, is_dark = window_caption_colors(theme_id, accent)
        results = {}
        for name, attr, value in (
            ("dark", 20, wintypes.BOOL(bool(is_dark))),              # DWMWA_USE_IMMERSIVE_DARK_MODE
            ("caption", 35, wintypes.DWORD(_hex_rgb_int(caption))),  # Windows 11
            ("text", 36, wintypes.DWORD(_hex_rgb_int(text))),        # Windows 11
        ):
            results[name] = dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
        logger.info("Цвет заголовка: тема=%s цвет=%s hwnd=%s, тёмный режим=%s",
                    theme_id, caption, hwnd, "включён" if results.get("dark") == 0 else "не поддерживается")
        if results.get("caption", 0) != 0 or results.get("text", 0) != 0:
            logger.debug("Windows не поддерживает отдельные цвета заголовка; используется цвет системной темы")
        return True
    except Exception:
        logger.warning("Не удалось изменить цвет окна Windows", exc_info=True)
        return False


def apply_windows_theme_repeated(theme_id: str, accent: str = "", attempts=40):
    """Повторно применяет тему после запуска Edge/Chrome: HWND появляется не сразу."""
    if os.name != "nt":
        return
    for _ in range(attempts):
        if set_windows_window_theme(theme_id, accent):
            time.sleep(1.5)  # Chromium иногда перерисовывает рамку после первого показа — применяем ещё раз
            set_windows_window_theme(theme_id, accent)
            return
        time.sleep(0.35)
    logger.warning("Не удалось найти окно браузера для смены темы Windows после %s попыток", attempts)



# ======================================================================================
#  Настройки
# ======================================================================================
BUILTIN_LABELS = {"music": "Папка music"}


def find_bot_folder():
    """Ищет рядом папку ReClip-Bot (где лежит bot.py): либо плеер лежит внутри неё, либо рядом."""
    parent = PLAYER_DIR.parent
    candidates = [parent]
    try:
        candidates += sorted(d for d in parent.iterdir() if d.is_dir() and d != PLAYER_DIR)[:80]
    except OSError:
        pass
    for folder in candidates:
        try:
            if (folder / "bot.py").is_file():
                return folder
        except OSError:
            continue
    return None


def bot_tracks_path(settings: dict) -> str:
    """Папка с треками бота: заданная вручную, найденная автоматически или пустая строка."""
    custom = str(settings.get("bot_folder") or "").strip()
    if custom:
        base = Path(custom)
        for candidate in (base / "downloads" / "SoundCloud", base / "SoundCloud"):
            if candidate.is_dir():
                return str(candidate)
        return str(base)
    found = find_bot_folder()
    return str(found / "downloads" / "SoundCloud") if found else ""


def builtin_paths(settings: dict) -> dict:
    return {"bot": bot_tracks_path(settings), "music": str(MUSIC_DIR)}

DEFAULT_SETTINGS = {
    "theme": "midnight",
    "accent": "",
    "maretu_bg_opacity": 20,
    "background": {"type": "theme", "image": "", "color": "#12131c", "blur": 18, "dim": 35, "animate": True, "color2": "#1a1d3a", "gradient_style": "linear", "gradient_angle": 135, "crop_x": 50, "crop_y": 50, "zoom": 100},
    "glass": 10,
    "panel_blur": 18,
    "ui_scale": 100,
    "dvd_enabled": False,
    "dvd_image": "",
    "dvd_images": [],
    "dvd_size": 112,
    "dvd_rotate": False,
    "dvd_rotation_speed": 1,
    "radius": 32,
    "row_scale": 100,
    "font": "bold",
    "search_provider": "all",
    "radio_provider": "all",
    "radio_exclude_remixes": True,
    "radio_exclude_ai_slop": True,
    "radio_exclude_instrumentals": True,
    "radio_recommendation_count": 30,
    "radio_seed_count": 5,
    "radio_history_days": 10.5,
    "radio_blocked_tracks": [],
    "hidden_tracks": [],
    "hidden_playlist_tracks": {},
    "chat_enabled": False,
    "chat_nickname": "",
    "chat_server_url": "",
    "chat_token": "",
    "chat_host_enabled": False,
    "chat_host_port": 8786,
    "playlist_track_order": {},
    "density": "comfortable",
    "visualizer": "bars",
    "visualizer_position": "background",   # background — на фоне окна, bar — в нижней панели между управлением и громкостью
    "visualizer_fps": 20,
    "visualizer_color_mode": "theme",
    "visualizer_color": "#8b95ff",
    "visualizer_opacity_mode": "normal",
    "visualizer_opacity": 46,
    "visualizer_glow": 60,
    "track_scale": 130,
    "performance_mode": "eco_offline",
    "clips_enabled": True,
    "clips_online_enabled": False,
    "clip_video_quality": 1080,
    "mp3_downloader_enabled": False,
    "download_last_completed_at": 0,
    "music_effects": {"hearts": False, "pulse": True, "style": "default", "mode": "beat", "color_mode": "theme", "color": "#8b95ff"},
    "playlist_icon_mode": "cover",
    "setup_complete": False,
    "download_path": "",
    "custom_theme": {"bg1": "#12131c", "bg2": "#1a1d3a", "bg3": "#2b1d47", "panel": "#16182e", "text": "#eceef8", "muted": "#9aa0bd", "accent": "#8b95ff"},
    "sources": [
        {"id": "music", "enabled": True},
    ],
    "volume": 0.8,
    "volume_boost": 1.0,
    "shuffle": False,
    "repeat": "off",
    "last": {"id": "", "pos": 0.0},
    "favorites": [],
    "discord": {"enabled": True, "client_id": DISCORD_CLIENT_ID, "show_elapsed": True, "source_mode": "player", "large_image": "", "show_cover": True, "show_artist_image": True, "show_paused": True, "show_link": True, "status_format": "track_artist"},
    "multi": {"enabled": False, "loop_default": True, "layers": []},
    "mini_player": {"enabled": True, "style": "standard", "opacity": 92, "scale": 100, "position": "bottom-right", "x": 0, "y": 0, "lyrics_enabled": True, "lyrics_lines": 3, "lyrics_x": 0, "lyrics_y": 0},
    "autoclose": True,
    "hotkeys": {"show": "ctrl+alt+o", "hide": "ctrl+alt+h", "quit": "ctrl+alt+q"},
    "port": DEFAULT_PORT,
}

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_ID_RE = re.compile(r"^[0-9a-f]{8,32}$")
_FILE_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_ALLOWED_THEMES = {"midnight", "neon", "ocean", "mono", "custom", "spotify", "discord", "youtube", "soundcloud", "blood", "maretu"}


def _num(value, low, high, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _version_numbers(value: str) -> tuple[int, ...]:
    parts = [int(part) for part in re.findall(r"\d+", str(value or ""))]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _update_asset_key(name: str) -> str:
    """GitHub may replace spaces and '%' in uploaded asset names with punctuation."""
    name = str(name or "")
    return re.sub(r"[^a-z0-9]", "", name[:-4].casefold()) if name.casefold().endswith(".exe") else ""


def _select_update_asset(assets: list, frozen: bool, executable_name: str = ""):
    supported = {_update_asset_key(item.get("name")): item for item in reversed(assets)
                 if isinstance(item, dict)}
    new_player, new_setup = _update_asset_key(APP_EXE_NAME), _update_asset_key(APP_SETUP_NAME)
    if frozen and executable_name.casefold() == APP_EXE_NAME.casefold():
        order = (new_player, new_setup, "lastfrequencysetup", "lastfrequency")
    elif frozen:
        # Installation migrates the legacy executable name and its shortcuts.
        order = (new_setup, "lastfrequencysetup")
    else:
        order = (new_setup, "lastfrequencysetup", new_player, "lastfrequency")
    return next((supported[key] for key in order if key in supported), None)


def _is_setup_update(path: Path) -> bool:
    key = _update_asset_key(path.name)
    return key.startswith((_update_asset_key(APP_SETUP_NAME), "lastfrequencysetup"))


def github_update_status(force: bool = False) -> dict:
    """Return the newest public GitHub release, cached to avoid repeated requests."""
    now = time.monotonic()
    with _UPDATE_CACHE_LOCK:
        if not force and _UPDATE_CACHE["payload"] is not None and now - _UPDATE_CACHE["checked_at"] < 6 * 60 * 60:
            return dict(_UPDATE_CACHE["payload"])
        payload = {"available": False, "current": VERSION, "latest": "", "url": GITHUB_REPO_URL,
                   "download_url": "", "name": "", "notes": ""}
        try:
            request = urllib.request.Request(GITHUB_RELEASES_URL, headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"LastFrequency/{VERSION}",
                "X-GitHub-Api-Version": "2022-11-28",
            })
            with urllib.request.urlopen(request, timeout=5) as response:
                release = json.loads(response.read(1024 * 1024).decode("utf-8"))
            tag = str(release.get("tag_name") or "").strip()
            current_numbers, latest_numbers = _version_numbers(VERSION), _version_numbers(tag)
            payload.update({
                "available": bool(current_numbers and latest_numbers and latest_numbers > current_numbers),
                "latest": tag,
                "url": str(release.get("html_url") or GITHUB_REPO_URL),
                "name": str(release.get("name") or tag)[:180],
                "notes": str(release.get("body") or "")[:6000],
            })
            assets = [item for item in release.get("assets", []) if isinstance(item, dict)]
            preferred = _select_update_asset(assets, FROZEN, Path(sys.executable).name)
            download_url = str((preferred or {}).get("browser_download_url") or "")
            if download_url.startswith("https://github.com/"):
                payload["download_url"] = download_url
        except Exception as error:
            logger.info("Проверка обновлений GitHub не удалась: %s", type(error).__name__)
        _UPDATE_CACHE.update({"checked_at": now, "payload": payload})
        return dict(payload)


def _update_download_snapshot(job_id: str) -> dict:
    with _UPDATE_DOWNLOAD_JOBS_LOCK:
        job = _UPDATE_DOWNLOAD_JOBS.get(job_id)
        if not job:
            raise ValueError("Загрузка обновления не найдена")
        return {key: value for key, value in job.items() if key not in ("path", "url")}


def _launch_update_asset_after_exit(path: Path, player_pid: int) -> None:
    """Wait for this player process to exit, then open the downloaded installer normally."""
    if os.name != "nt":
        raise OSError("Автоматический запуск обновления доступен только в Windows")
    powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        found = shutil.which("powershell.exe")
        if not found:
            raise OSError("Не найден Windows PowerShell для запуска установщика после выхода")
        powershell = Path(found)
    safe_path = str(path.resolve()).replace("'", "''")
    player_dir = str(PLAYER_DIR.resolve()).replace("'", "''")
    target = Path(sys.executable).resolve() if FROZEN else PLAYER_DIR / APP_EXE_NAME
    target_path = str(target).replace("'", "''")
    update_log = str(PLAYER_DIR / "logs" / "update.log").replace("'", "''")
    if _is_setup_update(path):
        install_arg = ('"/DIR=' + str(PLAYER_DIR.resolve()) + '"').replace("'", "''")
        action = f"Start-Process -FilePath '{safe_path}' -ArgumentList '{install_arg}'"
    else:
        action = (f"$lfTarget='{target_path}'; $lfNew=$lfTarget+'.update-new'; $lfBackup=$lfTarget+'.previous'; "
                  "if (Test-Path -LiteralPath $lfTarget) { Copy-Item -LiteralPath $lfTarget -Destination $lfBackup -Force }; "
                  "try { "
                  f"Copy-Item -LiteralPath '{safe_path}' -Destination $lfNew -Force; "
                  "$lfDeadline=[DateTime]::UtcNow.AddSeconds(20); "
                  "while ($true) { try { Move-Item -LiteralPath $lfNew -Destination $lfTarget -Force; break } "
                  "catch { if ([DateTime]::UtcNow -ge $lfDeadline) { throw }; Start-Sleep -Milliseconds 300 } }; "
                  f"$lfLaunched=Start-Process -FilePath $lfTarget -WorkingDirectory '{player_dir}' -PassThru; "
                  "if ($lfLaunched.WaitForExit(2000) -and $lfLaunched.ExitCode -ne 0) { throw 'Updated player failed to start' } "
                  "} catch { $lfFailure=$_; "
                  "if (Test-Path -LiteralPath $lfBackup) { Copy-Item -LiteralPath $lfBackup -Destination $lfTarget -Force; "
                  f"Start-Process -FilePath $lfTarget -WorkingDirectory '{player_dir}' }}; throw $lfFailure }}")
    script = (f"while (Get-Process -Id {int(player_pid)} -ErrorAction SilentlyContinue) "
              "{ Start-Sleep -Milliseconds 250 }; "
              f"$ErrorActionPreference='Stop'; try {{ {action} }} "
              f"catch {{ Add-Content -LiteralPath '{update_log}' -Value $_.Exception.Message }}")
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
             | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    subprocess.Popen([str(powershell), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                      "-EncodedCommand", encoded], cwd=str(path.parent),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, creationflags=flags)


def _start_update_download() -> dict:
    job_id = secrets.token_urlsafe(12)
    job = {"id": job_id, "status": "preparing", "percent": 0, "downloaded": 0,
           "total": 0, "filename": "Обновление " + APP_NAME, "error": "", "opened": False}
    with _UPDATE_DOWNLOAD_JOBS_LOCK:
        _UPDATE_DOWNLOAD_JOBS[job_id] = job
        for stale_id in list(_UPDATE_DOWNLOAD_JOBS)[:-12]:
            if _UPDATE_DOWNLOAD_JOBS[stale_id]["status"] in ("finished", "failed"):
                _UPDATE_DOWNLOAD_JOBS.pop(stale_id, None)

    def worker():
        temporary = None
        try:
            update = github_update_status(force=True)
            if not update.get("available"):
                raise RuntimeError("Нового обновления больше нет.")
            url = str(update.get("download_url") or "")
            if not url.startswith("https://github.com/"):
                raise RuntimeError("Ссылка на установщик недоступна.")
            parsed = urlparse(url)
            asset_name = Path(unquote(parsed.path)).name
            asset_key = _update_asset_key(asset_name)
            if asset_key not in (_update_asset_key(APP_EXE_NAME), _update_asset_key(APP_SETUP_NAME), "lastfrequencysetup", "lastfrequency"):
                raise RuntimeError("В релизе не найден поддерживаемый EXE плеера " + APP_NAME + ".")
            version = re.sub(r"[^A-Za-z0-9._-]", "", str(update.get("latest") or ""))[:40]
            filename = f"{APP_NAME}-Setup-{version}.exe" if _is_setup_update(Path(asset_name)) else f"{APP_NAME}-{version}.exe"
            destination_dir = PLAYER_DIR / "updates"
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / filename
            temporary = destination.with_name(destination.name + f".{job_id}.part")
            request = urllib.request.Request(url, headers={
                "User-Agent": f"LastFrequency/{VERSION}",
                "Accept": "application/octet-stream",
            })
            with urllib.request.urlopen(request, timeout=30) as response, temporary.open("wb") as output:
                total = int(response.headers.get("Content-Length") or 0)
                if total > 512 * 1024 * 1024:
                    raise RuntimeError("Размер установщика превышает допустимый лимит.")
                with _UPDATE_DOWNLOAD_JOBS_LOCK:
                    job.update(status="downloading", total=total, filename=filename)
                downloaded = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > 512 * 1024 * 1024:
                        raise RuntimeError("Размер установщика превышает допустимый лимит.")
                    output.write(chunk)
                    with _UPDATE_DOWNLOAD_JOBS_LOCK:
                        job["downloaded"] = downloaded
                        job["percent"] = min(99, round(downloaded * 100 / total)) if total else 0
                output.flush()
                os.fsync(output.fileno())
            if downloaded < 1024 * 1024:
                raise RuntimeError("Скачанный установщик слишком мал и выглядит неполным.")
            if total and downloaded != total:
                raise RuntimeError("Обновление загружено не полностью. Попробуй ещё раз.")
            with temporary.open("rb") as downloaded_file:
                if downloaded_file.read(2) != b"MZ":
                    raise RuntimeError("Вместо EXE получен неподдерживаемый файл обновления.")
            temporary.replace(destination)
            opened = False
            close_player = False
            open_error = ""
            if os.name == "nt":
                try:
                    _launch_update_asset_after_exit(destination, os.getpid())
                    opened = True
                    close_player = True
                except OSError as error:
                    open_error = str(error)[:300]
                    logger.exception("Не удалось подготовить запуск скачанного обновления после закрытия плеера")
            with _UPDATE_DOWNLOAD_JOBS_LOCK:
                job.update(status="finished", percent=100, downloaded=downloaded,
                           total=total or downloaded, path=str(destination), opened=opened, error=open_error)
            if close_player:
                def close_before_install():
                    shutdown_server()
                close_timer = threading.Timer(1.5, close_before_install)
                close_timer.daemon = True
                close_timer.start()
        except Exception as error:
            if temporary is not None:
                try: temporary.unlink(missing_ok=True)
                except OSError: pass
            with _UPDATE_DOWNLOAD_JOBS_LOCK:
                job.update(status="failed", error=str(error)[:300])
            logger.warning("Не удалось скачать обновление %s: %s", APP_NAME, error)

    threading.Thread(target=worker, name=f"update-download-{job_id[:6]}", daemon=True).start()
    return _update_download_snapshot(job_id)


def _bool(value, default):
    return value if isinstance(value, bool) else default


def _choice(value, options, default):
    return value if value in options else default


_PLAY_HISTORY_LOCK = threading.Lock()
_CUSTOM_PLAYLISTS_LOCK = threading.Lock()
_REMOTE_FAVORITES_LOCK = threading.Lock()


def _sanitize_remote_favorites(items) -> list:
    if not isinstance(items, list):
        return []
    result, seen = [], set()
    allowed_providers = {"youtube", "youtube_music", "soundcloud", "spotify", "apple_music", "yandex", "apple", "deezer"}
    for item in items[:500]:
        if not isinstance(item, dict):
            continue
        track_id = str(item.get("id") or "").strip()[:200]
        title = str(item.get("title") or "Без названия").strip()[:180]
        if not track_id or track_id in seen or not title:
            continue
        seen.add(track_id)
        provider = str(item.get("provider") or "").strip()[:40]
        if provider not in allowed_providers:
            provider = "youtube"
        url = str(item.get("url") or "")[:2000]
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        valid_url = parsed.scheme == "https" and any(
            host == domain or host.endswith("." + domain)
            for domain in ("youtube.com", "youtu.be", "soundcloud.com"))
        thumbnail = str(item.get("thumbnail") or "")[:2000]
        if not (thumbnail.startswith("https://") or thumbnail.startswith("/")):
            thumbnail = ""
        artist_thumbnail = str(item.get("artist_thumbnail") or "")[:2000]
        if not (artist_thumbnail.startswith("https://") or artist_thumbnail.startswith("/")):
            artist_thumbnail = ""
        try:
            duration = max(0, min(int(float(item.get("duration") or item.get("dur") or 0)), 86400))
        except (TypeError, ValueError, OverflowError):
            duration = 0
        imported_from = str(item.get("imported_from") or "")[:40]
        result.append({"id": track_id, "title": title,
            "artist": str(item.get("artist") or "")[:180],
            "album": str(item.get("album") or "")[:180],
            "provider": provider, "source": str(item.get("source") or "Онлайн")[:80],
            "url": url if valid_url else "", "thumbnail": thumbnail,
            "artist_thumbnail": artist_thumbnail, "duration": duration, "dur": duration,
            "preview_only": bool(item.get("preview_only")), "remote": True,
            "imported_from": imported_from, "stream_url": ""})
    return result


def _read_remote_favorites() -> list:
    try:
        payload = json.loads(REMOTE_FAVORITES_FILE.read_text(encoding="utf-8"))
        return _sanitize_remote_favorites(payload.get("items", []) if isinstance(payload, dict) else payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []


def _write_remote_favorites(items) -> bool:
    temporary = REMOTE_FAVORITES_FILE.with_name(REMOTE_FAVORITES_FILE.name + ".tmp")
    try:
        PLAYER_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "items": _sanitize_remote_favorites(items)}
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, REMOTE_FAVORITES_FILE)
        return True
    except OSError:
        logger.exception("Не удалось сохранить избранные онлайн-треки")
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _sanitize_play_history(items) -> list:
    if not isinstance(items, list):
        return []
    result = {}
    for item in items[:2000]:
        if not isinstance(item, dict):
            continue
        track_id = str(item.get("id") or "").strip()[:200]
        if not track_id:
            continue
        remote = bool(item.get("remote"))
        try:
            played_at = max(0, min(int(item.get("at") or 0), int(time.time() * 1000)))
        except (TypeError, ValueError, OverflowError):
            played_at = 0
        try:
            duration = max(0, min(int(float(item.get("dur") or 0)), 86400))
        except (TypeError, ValueError, OverflowError):
            duration = 0
        try:
            count = max(1, min(int(item.get("count") or 1), 1000000000))
        except (TypeError, ValueError, OverflowError):
            count = 1
        url = str(item.get("url") or "")[:2000]
        if url and not url.startswith("https://"):
            url = ""
        thumbnail = str(item.get("thumbnail") or "")[:2000]
        if thumbnail and not thumbnail.startswith(("https://", "/")):
            thumbnail = ""
        artist_thumbnail = str(item.get("artist_thumbnail") or "")[:2000]
        if artist_thumbnail and not artist_thumbnail.startswith(("https://", "/")):
            artist_thumbnail = ""
        clean = {
            "id": track_id,
            "title": str(item.get("title") or "Без названия")[:180],
            "artist": str(item.get("artist") or "")[:180],
            "remote": remote,
            "provider": str(item.get("provider") or "")[:40],
            "source": str(item.get("source") or "")[:80],
            "url": url,
            "thumbnail": thumbnail,
            "artist_thumbnail": artist_thumbnail,
            "stream_url": "",  # temporary audio URLs expire; re-resolve from the original track page
            "preview_only": bool(item.get("preview_only")),
            "dur": duration,
            "cover": bool(item.get("cover")),
            "at": played_at,
            "count": count,
        }
        key = (track_id, remote)
        previous = result.get(key)
        if previous:
            clean["count"] = max(clean["count"], previous["count"])
        if previous is None or clean["at"] >= previous["at"]:
            result[key] = clean
        else:
            result[key]["count"] = max(result[key]["count"], clean["count"])
    return sorted(result.values(), key=lambda entry: entry["at"], reverse=True)[:2000]


def _read_play_history() -> list:
    try:
        payload = json.loads(PLAY_HISTORY_FILE.read_text(encoding="utf-8"))
        return _sanitize_play_history(payload.get("items", []) if isinstance(payload, dict) else payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []


def _merge_play_history(items) -> list:
    merged = {}
    for item in _read_play_history() + _sanitize_play_history(items):
        key = (item["id"], bool(item["remote"]))
        previous = merged.get(key)
        if previous:
            item = {**(previous if previous["at"] > item["at"] else item),
                    "count": max(previous["count"], item["count"])}
        merged[key] = item
    return sorted(merged.values(), key=lambda entry: entry["at"], reverse=True)[:2000]


def _write_play_history(items) -> bool:
    payload = {"version": 1, "items": _sanitize_play_history(items)}
    temporary = PLAY_HISTORY_FILE.with_name(PLAY_HISTORY_FILE.name + ".tmp")
    try:
        PLAYER_DIR.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, PLAY_HISTORY_FILE)
        return True
    except OSError:
        logger.exception("Не удалось сохранить историю прослушивания")
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _sanitize_custom_playlists(items) -> list:
    if not isinstance(items, list):
        return []
    result = []
    for playlist in items[:200]:
        if not isinstance(playlist, dict):
            continue
        playlist_id = str(playlist.get("id") or "").strip()[:128]
        name = str(playlist.get("name") or "").strip()[:80]
        raw_ids = playlist.get("ids")
        if not playlist_id or not name or not isinstance(raw_ids, list):
            continue
        ids = list(dict.fromkeys(str(track_id)[:200] for track_id in raw_ids[:5000]
                                 if isinstance(track_id, str) and track_id))
        raw_remote = playlist.get("remoteTracks")
        remote_tracks = {}
        if isinstance(raw_remote, dict):
            for track_id, track in list(raw_remote.items())[:len(ids)]:
                track_id = str(track_id)[:200]
                if track_id not in ids or not isinstance(track, dict):
                    continue
                try:
                    duration = max(0, min(int(float(track.get("duration") or track.get("dur") or 0)), 86400))
                except (TypeError, ValueError, OverflowError):
                    duration = 0
                url = str(track.get("url") or "")[:2000]
                thumbnail = str(track.get("thumbnail") or "")[:2000]
                artist_thumbnail = str(track.get("artist_thumbnail") or "")[:2000]
                remote_tracks[track_id] = {
                    "id": track_id,
                    "title": str(track.get("title") or "Без названия")[:180],
                    "artist": str(track.get("artist") or "")[:180],
                    "album": str(track.get("album") or "")[:180],
                    "provider": str(track.get("provider") or "")[:40],
                    "source": str(track.get("source") or "")[:80],
                    "url": url if url.startswith("https://") else "",
                    "thumbnail": thumbnail if thumbnail.startswith(("https://", "/")) else "",
                    "artist_thumbnail": artist_thumbnail if artist_thumbnail.startswith(("https://", "/")) else "",
                    "duration": duration,
                    "dur": duration,
                    "preview_only": bool(track.get("preview_only")),
                    "remote": True,
                    "stream_url": "",
                }
        try:
            updated_at = max(0, min(int(playlist.get("updated_at") or 0), int(time.time() * 1000)))
        except (TypeError, ValueError, OverflowError):
            updated_at = 0
        result.append({"id": playlist_id, "name": name, "ids": ids,
                       "remoteTracks": remote_tracks, "updated_at": updated_at})
    return result


def _read_custom_playlists() -> dict:
    try:
        payload = json.loads(CUSTOM_PLAYLISTS_FILE.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            raw_updated_at = payload.get("updated_at", 0)
            items = payload.get("items", [])
        else:
            raw_updated_at, items = 0, payload
        updated_at = max(0, min(int(raw_updated_at or 0), int(time.time() * 1000)))
        return {"updated_at": updated_at, "items": _sanitize_custom_playlists(items)}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return {"updated_at": 0, "items": []}


def _write_custom_playlists(updated_at, items) -> bool:
    try:
        updated_at = max(0, min(int(updated_at), int(time.time() * 1000)))
    except (TypeError, ValueError, OverflowError):
        updated_at = int(time.time() * 1000)
    payload = {"version": 1, "updated_at": updated_at,
               "items": _sanitize_custom_playlists(items)}
    temporary = CUSTOM_PLAYLISTS_FILE.with_name(CUSTOM_PLAYLISTS_FILE.name + ".tmp")
    try:
        PLAYER_DIR.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, CUSTOM_PLAYLISTS_FILE)
        return True
    except OSError:
        logger.exception("Не удалось сохранить пользовательские плейлисты")
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def source_id_for(path: str) -> str:
    return hashlib.sha1(os.path.normcase(path).encode("utf-8")).hexdigest()[:8]


def sanitize_settings(data: dict, base: dict) -> dict:
    """Приводит настройки к допустимому виду; всё лишнее и кривое выбрасывает."""
    out = {}
    requested_theme = data.get("theme")
    fallback_theme = base.get("theme") if base.get("theme") in _ALLOWED_THEMES else "midnight"
    out["theme"] = requested_theme if isinstance(requested_theme, str) and requested_theme in _ALLOWED_THEMES else fallback_theme
    accent = data.get("accent", base["accent"])
    out["accent"] = accent if accent == "" or (isinstance(accent, str) and _HEX_RE.match(accent)) else base["accent"]
    out["maretu_bg_opacity"] = 20

    bg_in = data.get("background") if isinstance(data.get("background"), dict) else {}
    bg_base = base["background"]
    image = bg_in.get("image", bg_base["image"])
    if not (image == "" or (isinstance(image, str) and _FILE_RE.match(image) and (BG_DIR / image).is_file())):
        image = ""
    color = bg_in.get("color", bg_base["color"])
    out["background"] = {
        "type": _choice(bg_in.get("type"), ("theme", "gradient", "color", "image", "cover"), bg_base["type"]),
        "image": image,
        "color": color if isinstance(color, str) and _HEX_RE.match(color) else bg_base["color"],
        "color2": bg_in.get("color2") if isinstance(bg_in.get("color2"), str) and _HEX_RE.match(bg_in.get("color2")) else bg_base.get("color2", "#1a1d3a"),
        "gradient_style": _choice(bg_in.get("gradient_style"), ("linear", "radial", "conic"), bg_base.get("gradient_style", "linear")),
        "gradient_angle": int(_num(bg_in.get("gradient_angle"), 0, 720, bg_base.get("gradient_angle", 135))),
        "blur": int(_num(bg_in.get("blur"), 0, 120, bg_base["blur"])),
        "dim": int(_num(bg_in.get("dim"), 0, 90, bg_base["dim"])),
        "animate": _bool(bg_in.get("animate"), bg_base["animate"]),
        "crop_x": int(_num(bg_in.get("crop_x"), 0, 100, bg_base.get("crop_x", 50))),
        "crop_y": int(_num(bg_in.get("crop_y"), 0, 100, bg_base.get("crop_y", 50))),
        "zoom": int(_num(bg_in.get("zoom"), 100, 300, bg_base.get("zoom", 100))),
    }
    out["glass"] = int(_num(data.get("glass"), 0, 95, base["glass"]))
    out["panel_blur"] = int(_num(data.get("panel_blur"), 0, 80, base.get("panel_blur", 18)))
    out["ui_scale"] = int(_num(data.get("ui_scale"), 75, 250, base.get("ui_scale", 100)))
    out["dvd_enabled"] = _bool(data.get("dvd_enabled"), base.get("dvd_enabled", False))
    dvd_image = data.get("dvd_image", base.get("dvd_image", ""))
    out["dvd_image"] = dvd_image if dvd_image == "" or (isinstance(dvd_image, str) and _FILE_RE.match(dvd_image) and (BG_DIR / dvd_image).is_file()) else ""
    dvd_images = data.get("dvd_images", base.get("dvd_images", []))
    if not isinstance(dvd_images, list):
        dvd_images = []
    valid_dvd_images = [name for name in dvd_images[:40] if isinstance(name, str) and _FILE_RE.match(name) and (BG_DIR / name).is_file()]
    if not valid_dvd_images and out["dvd_image"]:
        valid_dvd_images = [out["dvd_image"]]
    out["dvd_images"] = valid_dvd_images
    out["dvd_size"] = int(_num(data.get("dvd_size"), 64, 840, base.get("dvd_size", 112)))
    out["dvd_rotate"] = _bool(data.get("dvd_rotate"), base.get("dvd_rotate", False))
    out["dvd_rotation_speed"] = int(_num(data.get("dvd_rotation_speed"), 1, 40, base.get("dvd_rotation_speed", 1)))
    out["radius"] = int(_num(data.get("radius"), 0, 64, base["radius"]))
    out["font"] = _choice(data.get("font"), ("system", "serif", "mono", "rounded", "quicksand", "pacifico", "bold", "roboto", "opensans", "montserrat", "lato", "inter"), base["font"])
    out["search_provider"] = _choice(data.get("search_provider"), ("all", "youtube_music", "youtube", "soundcloud"), base.get("search_provider", "all"))
    out["radio_provider"] = _choice(data.get("radio_provider"), ("all", "youtube_music", "youtube", "soundcloud"), base.get("radio_provider", "all"))
    out["radio_exclude_remixes"] = _bool(data.get("radio_exclude_remixes"), base.get("radio_exclude_remixes", True))
    out["radio_exclude_ai_slop"] = _bool(data.get("radio_exclude_ai_slop"), base.get("radio_exclude_ai_slop", True))
    out["radio_exclude_instrumentals"] = _bool(data.get("radio_exclude_instrumentals"), base.get("radio_exclude_instrumentals", True))
    out["radio_recommendation_count"] = int(_num(data.get("radio_recommendation_count"), 1, 100, base.get("radio_recommendation_count", 30)))
    out["radio_seed_count"] = int(_num(data.get("radio_seed_count"), 1, 20, base.get("radio_seed_count", 5)))
    out["radio_history_days"] = _num(data.get("radio_history_days"), .5, 365, base.get("radio_history_days", 10.5))
    blocked_tracks = data.get("radio_blocked_tracks", base.get("radio_blocked_tracks", []))
    out["radio_blocked_tracks"] = [{key: track[key] for key in ("id", "title", "artist", "remote", "url", "thumbnail")}
                                   for track in _sanitize_play_history(blocked_tracks)]
    def track_ids(value):
        return list(dict.fromkeys(item for item in value[:10000] if isinstance(item, str) and _ID_RE.fullmatch(item))) if isinstance(value, list) else []
    out["hidden_tracks"] = track_ids(data.get("hidden_tracks", base.get("hidden_tracks", [])))
    hidden_playlists = data.get("hidden_playlist_tracks", base.get("hidden_playlist_tracks", {}))
    out["hidden_playlist_tracks"] = {key[:300]: track_ids(ids) for key, ids in list(hidden_playlists.items())[:200]
                                      if isinstance(key, str) and isinstance(ids, list)} if isinstance(hidden_playlists, dict) else {}
    out["chat_enabled"] = _bool(data.get("chat_enabled"), base.get("chat_enabled", False))
    out["chat_host_enabled"] = _bool(data.get("chat_host_enabled"), base.get("chat_host_enabled", False))
    out["chat_host_port"] = int(_num(data.get("chat_host_port"), 1024, 65535, base.get("chat_host_port", 8786)))
    out["chat_nickname"] = str(data.get("chat_nickname", base.get("chat_nickname", "")) or "").strip()[:32]
    chat_url = str(data.get("chat_server_url", base.get("chat_server_url", "")) or "").strip()[:2000].rstrip("/")
    parsed_chat_url = urlparse(chat_url)
    out["chat_server_url"] = chat_url if (parsed_chat_url.scheme in ("http", "https") and parsed_chat_url.hostname and not parsed_chat_url.username and not parsed_chat_url.password and not parsed_chat_url.query and not parsed_chat_url.fragment) else ""
    out["chat_token"] = str(data.get("chat_token", base.get("chat_token", "")) or "")[:500]
    raw_track_orders = data.get("playlist_track_order", base.get("playlist_track_order", {}))
    clean_track_orders = {}
    if isinstance(raw_track_orders, dict):
        for key, ids in list(raw_track_orders.items())[:200]:
            if not isinstance(key, str) or not isinstance(ids, list):
                continue
            clean_track_orders[key[:300]] = [track_id for track_id in ids[:10000]
                                              if isinstance(track_id, str) and _ID_RE.fullmatch(track_id)]
    out["playlist_track_order"] = clean_track_orders
    out["density"] = _choice(data.get("density"), ("comfortable", "compact"), base["density"])
    out["visualizer"] = _choice(data.get("visualizer"), ("off", "bars", "wave", "heart"), "bars" if base.get("visualizer") == "fire" else base["visualizer"])
    out["visualizer_position"] = _choice(data.get("visualizer_position"), ("background", "bar"), base.get("visualizer_position", "background"))
    fps = data.get("visualizer_fps", base.get("visualizer_fps", 20))
    out["visualizer_fps"] = int(fps) if isinstance(fps, (int, float)) and not isinstance(fps, bool) and int(fps) in VISUALIZER_FPS_CHOICES else base.get("visualizer_fps", 20)
    out["visualizer_color_mode"] = _choice(data.get("visualizer_color_mode"), ("theme", "custom"), base.get("visualizer_color_mode", "theme"))
    viz_color = data.get("visualizer_color", base.get("visualizer_color", "#8b95ff"))
    out["visualizer_color"] = viz_color if isinstance(viz_color, str) and _HEX_RE.fullmatch(viz_color) else base.get("visualizer_color", "#8b95ff")
    out["visualizer_opacity_mode"] = _choice(data.get("visualizer_opacity_mode"), ("normal", "custom"), base.get("visualizer_opacity_mode", "normal"))
    out["visualizer_opacity"] = int(_num(data.get("visualizer_opacity"), 0, 100, base.get("visualizer_opacity", 46)))
    out["visualizer_glow"] = int(_num(data.get("visualizer_glow"), 0, 100, base.get("visualizer_glow", 60)))
    out["track_scale"] = int(_num(data.get("track_scale"), 50, 200, base.get("track_scale", 100)))
    out["row_scale"] = int(_num(data.get("row_scale"), 75, 400, base.get("row_scale", 100)))
    raw_mode = data.get("performance_mode", base.get("performance_mode", "eco_offline"))
    # Convert values used by older versions to the new independent online/economy model.
    raw_mode = {"economy": "eco_offline", "eco_dl": "eco_online", "standard": "online"}.get(raw_mode, raw_mode)
    out["performance_mode"] = _choice(raw_mode, PERFORMANCE_MODES, base.get("performance_mode", "eco_offline"))
    out["clips_enabled"] = _bool(data.get("clips_enabled"), base.get("clips_enabled", True))
    out["clips_online_enabled"] = _bool(data.get("clips_online_enabled"), base.get("clips_online_enabled", False))
    out["clip_video_quality"] = int(_choice(str(data.get("clip_video_quality", base.get("clip_video_quality", 1080))), ("360", "480", "720", "1080"), str(base.get("clip_video_quality", 1080))))
    out["mp3_downloader_enabled"] = _bool(data.get("mp3_downloader_enabled"), base.get("mp3_downloader_enabled", True))
    out["download_last_completed_at"] = _num(data.get("download_last_completed_at"), 0, 4102444800, base.get("download_last_completed_at", 0))
    effects = data.get("music_effects") if isinstance(data.get("music_effects"), dict) else {}
    out["music_effects"] = {
        "hearts": _bool(effects.get("hearts"), base.get("music_effects", {}).get("hearts", False)),
        "pulse": _bool(effects.get("pulse"), base.get("music_effects", {}).get("pulse", True)),
        "style": _choice(effects.get("style"), ("default", "flash", "neon", "wave"), base.get("music_effects", {}).get("style", "default")),
        "mode": _choice(effects.get("mode"), ("volume", "beat", "combined"), base.get("music_effects", {}).get("mode", "volume")),
        "color_mode": _choice(effects.get("color_mode"), ("theme", "custom"), base.get("music_effects", {}).get("color_mode", "theme")),
        "color": effects.get("color") if isinstance(effects.get("color"), str) and _HEX_RE.fullmatch(effects.get("color")) else base.get("music_effects", {}).get("color", "#8b95ff"),
    }
    out["playlist_icon_mode"] = _choice(data.get("playlist_icon_mode"), ("source", "cover"), base.get("playlist_icon_mode", "source"))
    out["setup_complete"] = _bool(data.get("setup_complete"), base.get("setup_complete", False))
    download_path = data.get("download_path", base.get("download_path", ""))
    out["download_path"] = download_path.strip()[:1000] if isinstance(download_path, str) else ""
    custom_theme = data.get("custom_theme") if isinstance(data.get("custom_theme"), dict) else {}
    custom_base = base.get("custom_theme", DEFAULT_SETTINGS["custom_theme"])
    out["custom_theme"] = {key: (custom_theme.get(key) if isinstance(custom_theme.get(key), str) and _HEX_RE.match(custom_theme.get(key)) else custom_base.get(key, DEFAULT_SETTINGS["custom_theme"][key]))
                            for key in DEFAULT_SETTINGS["custom_theme"]}

    # Источники треков: встроенные всегда на месте (путь задаёт сервер), свои — любые папки.
    incoming = data.get("sources", base["sources"])
    if not isinstance(incoming, list):
        incoming = base["sources"]
    sources, seen = [], set()
    for item in incoming[:40]:
        if not isinstance(item, dict):
            continue
        enabled = _bool(item.get("enabled"), True)
        sid = item.get("id")
        if sid in BUILTIN_LABELS and sid not in seen:
            sources.append({"id": sid, "enabled": enabled})
            seen.add(sid)
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path.strip() or len(path) > 500:
            continue
        path = os.path.normpath(path.strip())
        sid = source_id_for(path)
        if sid in seen or any(b and os.path.normcase(path) == os.path.normcase(b) for b in builtin_paths(base).values()):
            continue
        sources.append({"id": sid, "path": path, "enabled": enabled})
        seen.add(sid)
    for builtin in BUILTIN_LABELS:
        if builtin not in seen:
            sources.append({"id": builtin, "enabled": True})
    out["sources"] = sources

    out["volume"] = round(_num(data.get("volume"), 0, 1, base["volume"]), 3)
    out["volume_boost"] = round(_num(data.get("volume_boost"), 1, MAX_VOLUME_BOOST, base.get("volume_boost", 1.0)), 2)  # усилитель громкости: 1×…25×
    out["shuffle"] = _bool(data.get("shuffle"), base["shuffle"])
    out["repeat"] = _choice(data.get("repeat"), ("off", "all", "one"), base["repeat"])
    last_in = data.get("last") if isinstance(data.get("last"), dict) else {}
    last_id = last_in.get("id", base["last"]["id"])
    out["last"] = {
        "id": last_id if last_id == "" or (isinstance(last_id, str) and _ID_RE.match(last_id)) else "",
        "pos": _num(last_in.get("pos"), 0, 86400, base["last"]["pos"]),
    }
    favorites = data.get("favorites", base["favorites"])
    if not isinstance(favorites, list):
        favorites = base["favorites"]
    out["favorites"] = [f for f in dict.fromkeys(favorites) if isinstance(f, str) and _ID_RE.match(f)][:5000]

    dc_in = data.get("discord") if isinstance(data.get("discord"), dict) else {}
    dc_base = base["discord"]
    out["discord"] = {
        "enabled": _bool(dc_in.get("enabled"), dc_base["enabled"]),
        "client_id": DISCORD_CLIENT_ID,
        "show_elapsed": _bool(dc_in.get("show_elapsed"), dc_base["show_elapsed"]),
        "source_mode": "player",  # Discord displays this player's playback only.
        "large_image": "",
        "show_cover": _bool(dc_in.get("show_cover"), dc_base.get("show_cover", True)),
        "show_artist_image": _bool(dc_in.get("show_artist_image"), dc_base.get("show_artist_image", True)),
        "show_paused": _bool(dc_in.get("show_paused"), dc_base.get("show_paused", True)),
        "show_link": _bool(dc_in.get("show_link"), dc_base.get("show_link", True)),
        "status_format": "track_artist",
    }
    multi_in = data.get("multi") if isinstance(data.get("multi"), dict) else {}
    multi_base = base["multi"]
    layers, seen_layers = [], set()
    raw_layers = multi_in.get("layers", multi_base["layers"])
    for item in (raw_layers if isinstance(raw_layers, list) else [])[:200]:
        if len(layers) >= MAX_LAYERS:
            break
        if not isinstance(item, dict):
            continue
        lid = item.get("id")
        if not (isinstance(lid, str) and _ID_RE.match(lid)) or lid in seen_layers:
            continue
        seen_layers.add(lid)
        layers.append({"id": lid, "loop": _bool(item.get("loop"), True),
                       "vol": round(_num(item.get("vol"), 0, 1, 1.0), 3)})
    out["multi"] = {
        "enabled": _bool(multi_in.get("enabled"), multi_base["enabled"]) and out["performance_mode"] not in ECO_MODES,
        "loop_default": _bool(multi_in.get("loop_default"), multi_base["loop_default"]),
        "layers": layers,
    }
    out["autoclose"] = _bool(data.get("autoclose"), base["autoclose"])
    mini_in = data.get("mini_player") if isinstance(data.get("mini_player"), dict) else {}
    mini_base = base.get("mini_player", DEFAULT_SETTINGS["mini_player"])
    out["mini_player"] = {
        "enabled": _bool(mini_in.get("enabled"), mini_base["enabled"]),
        "style": _choice(mini_in.get("style"), ("standard", "compact"), mini_base.get("style", "standard")),
        "opacity": int(_num(mini_in.get("opacity"), 35, 100, mini_base["opacity"])),
        "scale": int(_num(mini_in.get("scale"), 50, 500, mini_base["scale"])),
        "position": _choice(mini_in.get("position"), ("top-left", "top-right", "bottom-left", "bottom-right", "custom"), mini_base["position"]),
        "x": int(_num(mini_in.get("x"), 0, 10000, mini_base["x"])),
        "y": int(_num(mini_in.get("y"), 0, 10000, mini_base["y"])),
        "lyrics_enabled": _bool(mini_in.get("lyrics_enabled"), mini_base.get("lyrics_enabled", True)),
        "lyrics_lines": int(_num(mini_in.get("lyrics_lines"), 1, 5, mini_base.get("lyrics_lines", 3))),
        "lyrics_x": int(_num(mini_in.get("lyrics_x"), 0, 10000, mini_base.get("lyrics_x", 0))),
        "lyrics_y": int(_num(mini_in.get("lyrics_y"), 0, 10000, mini_base.get("lyrics_y", 0))),
    }
    hotkeys_in = data.get("hotkeys") if isinstance(data.get("hotkeys"), dict) else {}
    hotkeys_base = base.get("hotkeys", DEFAULT_SETTINGS["hotkeys"])
    valid_hotkey = re.compile(r"^(?:(?:ctrl|alt|shift|win)\+){0,3}(?:[a-z0-9]|num[0-9]|numadd|numsubtract|nummultiply|numdivide|numdecimal|space|enter|tab|backspace|delete|insert|home|end|pageup|pagedown|arrowup|arrowdown|arrowleft|arrowright|f(?:[1-9]|1[0-9]|2[0-4]))$")
    out["hotkeys"] = {
        action: (value.lower() if isinstance(value, str) and valid_hotkey.fullmatch(value.lower()) else hotkeys_base[action])
        for action in ("show", "hide", "quit")
        for value in [hotkeys_in.get(action, hotkeys_base[action])]
    }
    out["port"] = int(_num(data.get("port"), 1024, 65535, base["port"]))
    return out


def merge_patch(current: dict, patch: dict) -> dict:
    merged = dict(current)
    for key, value in patch.items():
        if key in ("background", "discord", "last", "multi", "mini_player") and isinstance(value, dict):
            merged[key] = {**current.get(key, {}), **value}
        else:
            merged[key] = value
    return merged


class SettingsStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = sanitize_settings({}, json.loads(json.dumps(DEFAULT_SETTINGS)))
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                self.data = sanitize_settings(saved, self.data)
                # Existing installs keep their current layout; show the onboarding only to new profiles.
                if "setup_complete" not in saved:
                    self.data["setup_complete"] = True
        except (OSError, json.JSONDecodeError):
            pass

    def get(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.data))

    def update(self, patch: dict) -> dict:
        with self.lock:
            merged = merge_patch(self.data, patch)
            if "hidden_playlist_tracks" in patch:
                merged["hidden_playlist_tracks"] = patch["hidden_playlist_tracks"]
            self.data = sanitize_settings(merged, self.data)
            self._save()
            return json.loads(json.dumps(self.data))

    def _save(self):
        temporary = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".tmp")
        try:
            temporary.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, SETTINGS_FILE)
        except OSError as error:
            logger.warning("Не смог сохранить настройки: %s", error)


def resolve_sources(settings: dict) -> list:
    """[{id, label, path, enabled, builtin}] — с учётом путей встроенных папок."""
    result = []
    paths = builtin_paths(settings)
    for item in settings["sources"]:
        if item["id"] in BUILTIN_LABELS:
            result.append({"id": item["id"], "label": BUILTIN_LABELS[item["id"]], "path": paths[item["id"]],
                           "enabled": item["enabled"], "builtin": True})
        else:
            result.append({"id": item["id"], "label": Path(item["path"]).name or item["path"],
                           "path": item["path"], "enabled": item["enabled"], "builtin": False})
    return result


# ======================================================================================
#  Поиск и загрузка MP3
# ======================================================================================
def _internet_available(timeout: float = 1.5) -> bool:
    """Quick connectivity check used only when an online mode is requested at startup."""
    request = urllib.request.Request(
        "https://connectivitycheck.gstatic.com/generate_204",
        headers={"User-Agent": f"LAST-FREQUENCY/{VERSION}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= int(getattr(response, "status", response.getcode()) or 0) < 300
    except Exception:
        return False


def _require_online_search_enabled() -> None:
    if _ACTIVE_PERFORMANCE_MODE not in ONLINE_MODES:
        raise RuntimeError("Онлайн-поиск доступен в режимах «Обычный режим» и «Больше визуала». Переключи режим и перезапусти плеер.")


def _yt_dlp_module(allow_online_search: bool = False):
    # Global online search and in-player playback must not depend on the MP3 downloader toggle.
    if allow_online_search:
        _require_online_search_enabled()
    else:
        require_downloads_enabled()
    try:
        import yt_dlp
        return yt_dlp
    except ImportError as error:
        raise RuntimeError("Модуль yt-dlp не найден в текущем Python. Установи requirements.txt тем же интерпретатором, которым запускается player.py.") from error

def _provider_query(provider: str, query: str) -> str:
    if provider == "youtube_music": return "https://music.youtube.com/search?q=" + quote(query, safe="") + "#songs"
    if provider == "youtube": return "ytsearch20:" + query + " music"
    if provider == "soundcloud": return "scsearch20:" + query
    raise ValueError("Неизвестный источник")


_SOUNDCLOUD_CLIENT_ID = ""
_SOUNDCLOUD_CLIENT_ID_LOCK = threading.Lock()


def _soundcloud_client_id(*, refresh: bool = False) -> str:
    """Get the public client id SoundCloud exposes to its own web player."""
    global _SOUNDCLOUD_CLIENT_ID
    with _SOUNDCLOUD_CLIENT_ID_LOCK:
        if _SOUNDCLOUD_CLIENT_ID and not refresh:
            return _SOUNDCLOUD_CLIENT_ID
        try:
            status, page_bytes = _platform_request(
                "https://soundcloud.com/", ("soundcloud.com", "sndcdn.com"),
                headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"},
                timeout=12.0, max_bytes=2_000_000)
            if status == 200:
                page = page_bytes.decode("utf-8", "replace")
                scripts = re.findall(r'<script[^>]+src=["\'](https://(?:a-v2\.)?sndcdn\.com/assets/[^"\']+\.js)["\']', page, re.I)
                for script_url in reversed(scripts[-8:]):
                    try:
                        js_status, js_bytes = _platform_request(script_url, ("sndcdn.com",),
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=10.0, max_bytes=5_000_000)
                    except Exception:
                        continue
                    if js_status != 200:
                        continue
                    match = re.search(rb'client_id[:=]\s*["\']([a-zA-Z0-9]{32})["\']', js_bytes)
                    if match:
                        _SOUNDCLOUD_CLIENT_ID = match.group(1).decode("ascii")
                        return _SOUNDCLOUD_CLIENT_ID
        except Exception as error:
            logger.info("SoundCloud web client id refresh failed: %s", type(error).__name__)
        return _SOUNDCLOUD_CLIENT_ID


def _search_soundcloud_api(query: str, limit: int = 20) -> list:
    """Search public SoundCloud catalog data like Riff's web-player fallback."""
    client_id = _soundcloud_client_id()
    if not client_id:
        return []
    url = "https://api-v2.soundcloud.com/search/tracks?" + urlencode({
        "q": query, "limit": min(50, max(1, int(limit))), "offset": 0,
        "linked_partitioning": 1, "client_id": client_id})
    status, body = _platform_request(url, ("soundcloud.com",), headers={
        "User-Agent": "Mozilla/5.0", "Accept": "application/json"}, timeout=15.0,
        max_bytes=5_000_000)
    if status in (401, 403):
        client_id = _soundcloud_client_id(refresh=True)
        if not client_id:
            return []
        retry_url = "https://api-v2.soundcloud.com/search/tracks?" + urlencode({
            "q": query, "limit": min(50, max(1, int(limit))), "offset": 0,
            "linked_partitioning": 1, "client_id": client_id})
        status, body = _platform_request(retry_url, ("soundcloud.com",), headers={
            "User-Agent": "Mozilla/5.0", "Accept": "application/json"}, timeout=15.0,
            max_bytes=5_000_000)
    if status != 200:
        return []
    data = json.loads(body.decode("utf-8", "replace"))
    collection = data.get("collection") if isinstance(data, dict) else None
    results = []
    for item in collection if isinstance(collection, list) else []:
        if not isinstance(item, dict):
            continue
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        publisher = item.get("publisher_metadata") if isinstance(item.get("publisher_metadata"), dict) else {}
        artwork = str(item.get("artwork_url") or "")
        if artwork:
            artwork = re.sub(r"-large\.(jpg|png|jpeg)$", r"-t500x500.\1", artwork, flags=re.I)
        permalink = str(item.get("permalink_url") or "")
        if not permalink or not permalink.startswith("https://"):
            continue
        results.append({"id": str(item.get("id") or ""),
            "title": str(item.get("title") or "Без названия"),
            "artist": str(publisher.get("artist") or user.get("username") or "Неизвестный автор"),
            "duration": _playlist_duration(item.get("duration")), "thumbnail": artwork,
            "url": permalink, "source": "SoundCloud", "provider": "soundcloud",
            "preview_only": False})
        if len(results) >= limit:
            break
    return results

def _artist_metadata(value) -> str:
    if isinstance(value, dict):
        for key in ("name", "artist", "title", "text"):
            candidate = _artist_metadata(value.get(key))
            if candidate:
                return candidate
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(candidate for candidate in (_artist_metadata(item) for item in value) if candidate)
    if not isinstance(value, str):
        return ""
    return value.strip()


def _valid_music_artist(value) -> str:
    value = _artist_metadata(value)
    value = re.sub(r"\s+-\s+Topic$", "", value, flags=re.I).strip()
    if not value or value.casefold() in {
        "unknown", "unknown artist", "unknown uploader", "неизвестный автор",
        "неизвестный исполнитель", "youtube", "youtube music", "topic",
        "various artists", "auto-generated by youtube",
    }:
        return ""
    if re.fullmatch(r"(?:UC|UCh)[A-Za-z0-9_-]{15,}", value):
        return ""
    return value


def _public_result(info: dict) -> dict:
    thumbnails = info.get("thumbnails") if isinstance(info.get("thumbnails"), list) else []
    thumb = info.get("thumbnail") or next((t.get("url") for t in reversed(thumbnails) if isinstance(t, dict) and t.get("url")), "")
    square = [t for t in thumbnails if isinstance(t, dict) and t.get("url")
              and t.get("width") and t.get("width") == t.get("height")]
    if square:
        thumb = max(square, key=lambda t: int(t.get("width") or 0))["url"]
    artist = next((_valid_music_artist(info.get(key)) for key in
                   ("artist", "artists", "artist_name", "album_artist", "creator", "channel", "channel_title", "uploader")
                   if _valid_music_artist(info.get(key))), "")
    if not artist:
        fallback_artist = _valid_music_artist(info.get("uploader_id") or info.get("channel_id"))
        artist = fallback_artist or "Неизвестный автор"
    return {"id": str(info.get("id") or ""), "title": str(info.get("title") or "Без названия"),
            "artist": str(artist),
            "duration": info.get("duration"), "thumbnail": thumb,
            "artist_thumbnail": str(info.get("uploader_thumbnail") or info.get("channel_thumbnail") or ""),
            "url": info.get("webpage_url") or info.get("permalink_url") or info.get("original_url") or (info.get("url") if str(info.get("url") or "").startswith("http") else ("https://www.youtube.com/watch?v=" + str(info.get("id")) if str(info.get("extractor_key") or info.get("ie_key") or info.get("extractor") or "").lower() in ("youtube", "youtubeie", "youtube music", "youtubemusic") else "")),
            "source": info.get("extractor_key") or info.get("ie_key") or info.get("extractor") or ""}

def search_remote(provider: str, query: str, *, require_downloader: bool = True) -> list:
    if require_downloader:
        require_downloads_enabled()
        ytdlp = _yt_dlp_module()
    else:
        _require_online_search_enabled()
        ytdlp = _yt_dlp_module(allow_online_search=True)
    query = str(query or "").strip()[:200]
    if not query: return []
    if provider == "soundcloud":
        try:
            direct_results = _search_soundcloud_api(query, 20)
            if direct_results:
                return direct_results
        except Exception as error:
            logger.info("SoundCloud public API search failed; falling back to yt-dlp: %s", type(error).__name__)
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": "in_playlist",
            "playlistend": 20, "socket_timeout": 12, "noplaylist": True}
    with ytdlp.YoutubeDL(opts) as ydl:
        data = ydl.extract_info(_provider_query(provider, query), download=False) or {}
    entries = data.get("entries") if isinstance(data, dict) else None
    results = []
    non_music_words = re.compile(r"\b(reaction|reacts|review|tutorial|how to|gameplay|walkthrough|podcast|news|interview|vlog|shorts?|trailer|full movie|episode|documentary|обзор|реакци\w*|урок\w*|обучени\w*|прохождени\w*|подкаст\w*|новост\w*|интервью|влог\w*|трейлер|фильм|серия|документал\w*)\b", re.I)
    for item in (entries or []):
        if not isinstance(item, dict) or not (item.get("url") or item.get("webpage_url")):
            continue
        if provider == "youtube":
            title_text = str(item.get("title") or "")
            try:
                too_long = float(item.get("duration") or 0) > 900
            except (TypeError, ValueError):
                too_long = False
            if too_long or non_music_words.search(title_text):
                continue
        result = _public_result(item)
        if provider in ("youtube", "youtube_music"):
            result["source"] = "YouTube Music" if provider == "youtube_music" else "YouTube"
            if provider == "youtube_music" and result["artist"] == "Неизвестный автор":
                # Flat Music entries frequently lack uploader metadata. A title
                # formatted as "Artist - Track" still provides a useful artist.
                split = re.match(r"^(.{2,100}?)\s+[\-–—]\s+(.+)$", result["title"])
                if split:
                    result["artist"] = split.group(1).strip()
            if not result.get("url") and result.get("id"):
                result["url"] = "https://www.youtube.com/watch?v=" + str(result["id"])
            if not result.get("thumbnail") and re.fullmatch(r"[A-Za-z0-9_-]{11}", str(result.get("id") or "")):
                result["thumbnail"] = "https://i.ytimg.com/vi/" + str(result["id"]) + "/hqdefault.jpg"
        result["provider"] = provider
        result["preview_only"] = False
        results.append(result)
        if len(results) >= 20:
            break
    return results


def _http_json(url: str, timeout: float = 10.0) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": COVER_USER_AGENT if "COVER_USER_AGENT" in globals() else f"Mozilla/5.0 LAST FREQUENCY/{VERSION}", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(3_000_000)
    data = json.loads(raw.decode("utf-8", "replace"))
    return data if isinstance(data, dict) else {}


def _allowed_online_stream_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or ""))
        host = (parsed.hostname or "").lower().rstrip(".")
        return parsed.scheme == "https" and bool(host) and any(
            host == domain or host.endswith("." + domain) for domain in _ONLINE_STREAM_HOSTS
        )
    except Exception:
        return False


def _probe_online_video_stream(url: str, headers: dict | None = None) -> bool:
    if not _allowed_online_stream_url(url):
        return False
    request_headers = {str(key): str(value) for key, value in (headers or {}).items()
                       if str(key).lower() in {"user-agent", "accept", "accept-language", "referer"}
                       and "\r" not in str(value) and "\n" not in str(value)}
    request_headers["Range"] = "bytes=0-0"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=request_headers), timeout=12) as response:
            final_url = str(getattr(response, "url", "") or url)
            status = int(getattr(response, "status", 200) or 200)
            return _allowed_online_stream_url(final_url) and status in (200, 206) and bool(response.read(1))
    except urllib.error.HTTPError as error:
        logger.info("Online video source probe failed: HTTP %s", error.code)
        return False
    except Exception as error:
        logger.info("Online video source probe failed: %s", type(error).__name__)
        return False


def _youtube_clip_source(track_id: str, source_url: str = "") -> str:
    parsed = urlparse(str(source_url or ""))
    host = (parsed.hostname or "").lower().rstrip(".")
    video_id = ""
    if parsed.scheme == "https" and (host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be"):
        video_id = (parse_qs(parsed.query).get("v") or [""])[0]
        if host == "youtu.be":
            video_id = parsed.path.strip("/").split("/")[0]
    if not video_id:
        candidate = str(track_id).rsplit(":", 1)[-1]
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
            video_id = candidate
    if not video_id:
        state = globals().get("STATE")
        if state is not None:
            with state.library.lock:
                track = state.library.tracks.get(track_id) or {}
                match = re.search(r"\[([A-Za-z0-9_-]{11})\]$", Path(track.get("path") or "").stem)
                if match:
                    video_id = match.group(1)
    return "https://www.youtube.com/watch?v=" + video_id if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) else ""


def _is_audio_only_upload(info: dict) -> bool:
    video_type = str(info.get("music_video_type") or info.get("musicVideoType") or "")
    if video_type == "MUSIC_VIDEO_TYPE_ATV":
        return True
    channel = str(info.get("channel") or info.get("uploader") or "")
    description = str(info.get("description") or "")
    title = str(info.get("title") or "")
    return bool(re.search(r"\s-\sTopic$", channel, re.I)
                or re.search(r"auto-generated by youtube|provided to youtube by", description, re.I)
                or re.search(r"\b(?:official audio|audio only|full album)\b|официальное аудио", title, re.I))


def _find_accessible_youtube_video(query: str, max_height: int = 480, *, background: bool = False,
                                   source_url: str = "", artist: str = "") -> tuple[str, str, str, dict]:
    """Use only the track's original video; static artwork never triggers a search."""
    canonical_source = _youtube_clip_source("", source_url)
    if not canonical_source:
        return "", "", "", {}
    ytdlp = _yt_dlp_module(allow_online_search=True)
    video_format = (
        f"bestvideo[ext=mp4][height<={max_height}][vcodec^=avc1]/"
        f"bestvideo[ext=mp4][height<={max_height}]/best[ext=mp4][height<={max_height}]/"
        f"bestvideo[height<={max_height}]/best[height<={max_height}]"
    )
    if background:
        video_format = f"bestvideo[ext=mp4][vcodec^=avc1][height<={max_height}][fps<=30][protocol=https]/best[ext=mp4][vcodec^=avc1][height<={max_height}][fps<=30][protocol=https]"

    def resolve(video_url):
        for client in (None, "android", "web_safari"):
            options = {"quiet": True, "no_warnings": True, "skip_download": True,
                       "ignore_no_formats_error": True, "noplaylist": True, "format": video_format,
                       "socket_timeout": 12, "retries": 1}
            if client:
                options["extractor_args"] = {"youtube": {"player_client": [client]}}
            try:
                with ytdlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(video_url, download=False) or {}
                if _is_audio_only_upload(info):
                    logger.info("Клип: исходный трек содержит обложку; отдельный клип не ищется")
                    return "", "", "", {}
                stream = str(info.get("url") or "")
                headers = info.get("http_headers") or {}
                if stream and _probe_online_video_stream(stream, headers):
                    return video_url, str(info.get("format_id") or ""), stream, headers
            except Exception as error:
                logger.info("Клип: клиент %s не предоставил доступное видео (%s)", client or "default", type(error).__name__)
        return "", "", "", {}

    result = resolve(canonical_source)
    if result[0]:
        logger.info("Клип: использую видео исходного трека")
    return result


def _register_online_stream(url: str, headers: dict | None = None, *, is_hls: bool = False) -> str:
    """Register a trusted provider stream and return a short-lived localhost proxy URL."""
    if not _allowed_online_stream_url(url):
        return ""
    token = secrets.token_urlsafe(24)
    safe_headers = {}
    for key, value in (headers or {}).items():
        key, value = str(key), str(value)
        if key.lower() in {"user-agent", "referer", "origin", "accept", "accept-language"} and "\r" not in value and "\n" not in value:
            safe_headers[key] = value[:1000]
    safe_headers.setdefault("User-Agent", f"Mozilla/5.0 LAST FREQUENCY/{VERSION}")
    with ONLINE_STREAMS_LOCK:
        now = time.time()
        expired = [key for key, item in ONLINE_STREAMS.items() if item.get("expires", 0) <= now]
        for key in expired:
            ONLINE_STREAMS.pop(key, None)
        # Bound memory if searches are spammed.
        while len(ONLINE_STREAMS) >= 256:
            oldest = min(ONLINE_STREAMS, key=lambda key: ONLINE_STREAMS[key].get("created", 0))
            ONLINE_STREAMS.pop(oldest, None)
        ONLINE_STREAMS[token] = {"url": str(url), "headers": safe_headers, "created": now,
                                 "expires": now + ONLINE_STREAM_TTL, "is_hls": bool(is_hls),
                                 "resources": {}, "resource_ids": {}}
    return f"/api/online/stream/{token}"


def _search_deezer(query: str) -> list:
    url = "https://api.deezer.com/search?" + urlencode({"q": query, "limit": 20})
    data = _http_json(url)
    results = []
    for item in data.get("data", []) if isinstance(data.get("data"), list) else []:
        if not isinstance(item, dict) or not item.get("link"):
            continue
        artist = item.get("artist") if isinstance(item.get("artist"), dict) else {}
        album = item.get("album") if isinstance(item.get("album"), dict) else {}
        preview = str(item.get("preview") or "")
        url = str(item.get("link") or "")
        rid = hashlib.sha1(("deezer|" + url).encode("utf-8", "replace")).hexdigest()[:16]
        results.append({"id": rid, "title": str(item.get("title") or "Без названия"),
                        "artist": str(artist.get("name") or "Неизвестный автор"),
                        "duration": item.get("duration"), "thumbnail": str(album.get("cover_medium") or album.get("cover") or ""),
                        "url": url, "source": "Deezer", "provider": "deezer",
                        "stream_url": _register_online_stream(preview) if preview else "",
                        "preview_only": True})
    return results


def _search_apple_music(query: str) -> list:
    url = "https://itunes.apple.com/search?" + urlencode({"term": query, "entity": "song", "limit": 20, "media": "music"})
    data = _http_json(url)
    results = []
    for item in data.get("results", []) if isinstance(data.get("results"), list) else []:
        if not isinstance(item, dict) or not item.get("trackViewUrl"):
            continue
        link = str(item.get("trackViewUrl") or "")
        preview = str(item.get("previewUrl") or "")
        rid = hashlib.sha1(("apple|" + link).encode("utf-8", "replace")).hexdigest()[:16]
        results.append({"id": rid, "title": str(item.get("trackName") or "Без названия"),
                        "artist": str(item.get("artistName") or "Неизвестный автор"),
                        "duration": round(float(item.get("trackTimeMillis") or 0) / 1000) if item.get("trackTimeMillis") else None,
                        "thumbnail": str(item.get("artworkUrl100") or ""), "url": link,
                        "source": "Apple Music", "provider": "apple",
                        "stream_url": _register_online_stream(preview) if preview else "",
                        "preview_only": True})
    return results


_ONLINE_PAGE_HOSTS = ("youtube.com", "youtu.be", "soundcloud.com")


def _remote_page_provider(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        return ""
    if any(host == domain or host.endswith("." + domain) for domain in _ONLINE_PAGE_HOSTS):
        return "youtube" if host.endswith(("youtube.com", "youtu.be")) else "soundcloud"
    return ""


def _global_playlist_result(info: dict, provider: str, fallback_url: str = "") -> dict:
    playlist_id = str(info.get("id") or "")
    url = str(info.get("webpage_url") or info.get("permalink_url") or info.get("original_url") or "")
    if not url and str(info.get("url") or "").startswith("https://"):
        url = str(info.get("url"))
    if not url and provider in ("youtube", "youtube_music") and playlist_id:
        url = "https://www.youtube.com/playlist?list=" + quote(playlist_id, safe="")
    url = url or fallback_url
    if not url:
        return {}
    page_provider = _remote_page_provider(url)
    if page_provider != ("soundcloud" if provider == "soundcloud" else "youtube"):
        return {}
    thumbnail = str(_public_result(info).get("thumbnail") or info.get("artwork_url") or "")
    entries = info.get("entries")
    if entries is None:
        entries = []
    if not thumbnail:
        try:
            first_entry = next(iter(entries), None)
        except (TypeError, RuntimeError):
            first_entry = None
        if isinstance(first_entry, dict):
            thumbnail = str(_public_result(first_entry).get("thumbnail") or "")
            first_id = str(first_entry.get("id") or "")
            if not thumbnail and provider in ("youtube", "youtube_music") and re.fullmatch(r"[A-Za-z0-9_-]{11}", first_id):
                thumbnail = "https://i.ytimg.com/vi/" + first_id + "/hqdefault.jpg"
    raw_count = info.get("playlist_count") or info.get("n_entries")
    if raw_count is None:
        try:
            raw_count = len(entries)
        except (TypeError, AttributeError):
            raw_count = 0
    try:
        track_count = int(raw_count)
    except (TypeError, ValueError):
        track_count = 0
    return {"id": playlist_id or hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()[:16],
            "title": str(info.get("title") or "Плейлист")[:180],
            "artist": str(info.get("uploader") or info.get("creator") or info.get("channel") or "")[:180],
            "thumbnail": thumbnail, "url": url,
            "source": "YouTube" if provider in ("youtube", "youtube_music") else "SoundCloud",
            "provider": provider, "kind": "playlist", "is_playlist": True,
            "track_count": max(0, track_count)}


def _global_url_result(url: str) -> dict:
    if _external_playlist_provider(url):
        return _load_external_playlist(url)
    provider = _remote_page_provider(url)
    if not provider:
        raise ValueError("Вставь ссылку YouTube, SoundCloud или публичный плейлист Spotify, Apple Music или Яндекс Музыки")
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    video_id = params.get("v", [""])[0]
    if parsed.hostname and parsed.hostname.lower().endswith("youtu.be") and parsed.path.strip("/"):
        video_id = parsed.path.strip("/").split("/")[0]
    # A video URL wins over a playlist parameter (for example a watch URL with
    # an autoplay list), matching the download and playback behavior.
    normalized = "https://www.youtube.com/watch?v=" + quote(video_id, safe="") if provider == "youtube" and video_id else url
    ytdlp = _yt_dlp_module(allow_online_search=True)
    options = {"quiet": True, "no_warnings": True, "skip_download": True,
               "extract_flat": "in_playlist", "playlistend": 100,
               "socket_timeout": 18, "noplaylist": bool(video_id)}
    with ytdlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(normalized, download=False) or {}
    if not isinstance(info, dict):
        raise RuntimeError("Не удалось разобрать ссылку")
    if info.get("entries") is not None and info.get("_type") in ("playlist", "multi_video"):
        result = _global_playlist_result(info, provider, normalized)
        if result:
            return result
    result = _public_result(info)
    result["provider"] = provider
    result["source"] = "YouTube" if provider == "youtube" else "SoundCloud"
    result["preview_only"] = False
    if provider == "youtube" and result.get("id") and not result.get("thumbnail"):
        result["thumbnail"] = "https://i.ytimg.com/vi/" + str(result["id"]) + "/hqdefault.jpg"
    if not result.get("url"):
        result["url"] = normalized
    return result


def _search_global_playlists(query: str, provider: str) -> list:
    ytdlp = _yt_dlp_module(allow_online_search=True)
    search = quote(query, safe="")
    if provider == "soundcloud":
        target = "https://soundcloud.com/search/sets?q=" + search
    else:
        # YouTube's playlist filter (EgIQAw) asks the extractor for playlist
        # results instead of ordinary videos.
        target = "https://www.youtube.com/results?search_query=" + search + "&sp=EgIQAw%3D%3D"
    options = {"quiet": True, "no_warnings": True, "skip_download": True,
               "extract_flat": "in_playlist", "playlistend": 20,
               "socket_timeout": 15, "noplaylist": False}
    with ytdlp.YoutubeDL(options) as ydl:
        data = ydl.extract_info(target, download=False) or {}
    entries = data.get("entries") if isinstance(data, dict) else None
    if entries is None:
        entries = []
    results = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        extractor = str(item.get("ie_key") or item.get("extractor_key") or item.get("extractor") or "").lower()
        item_id = str(item.get("id") or "")
        is_playlist = (item.get("_type") in ("playlist", "multi_video") or
                       any(token in extractor for token in ("playlist", "set", "tab")) or
                       (provider in ("youtube", "youtube_music") and item_id.startswith(("PL", "UU", "LL", "FL", "RD", "OLAK5uy"))))
        if not is_playlist:
            continue
        playlist = _global_playlist_result(item, provider)
        if playlist:
            results.append(playlist)
        if len(results) >= 20:
            break
    return results


_PLAYLIST_PROVIDER_NAMES = {
    "spotify": "Spotify", "apple_music": "Apple Music", "yandex": "Яндекс Музыка",
    "youtube": "YouTube", "youtube_music": "YouTube Music", "soundcloud": "SoundCloud",
}


def _external_playlist_provider(url: str) -> str:
    """Identify supported public playlist links without accepting arbitrary hosts."""
    value = str(url or "").strip()
    if re.fullmatch(r"spotify:playlist:[A-Za-z0-9]{22}", value, re.I):
        return "spotify"
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        return ""
    if host in {"open.spotify.com", "www.open.spotify.com"} and re.search(r"/(?:intl-[a-z]{2}/)?(?:embed/)?playlist/[A-Za-z0-9]{22}(?:/|$)", parsed.path, re.I):
        return "spotify"
    if host == "music.apple.com" and re.search(r"/(?:[a-z]{2}/)?(?:playlist|album)/", parsed.path, re.I):
        return "apple_music"
    if host.startswith("music.yandex.") and re.search(r"/(?:users/[^/]+/playlists/[^/]+|playlists/[^/]+|playlist/[^/]+(?:/[^/]+)?)(?:/|$)", parsed.path, re.I):
        return "yandex"
    return ""


def _domain_allowed(host: str, domains: tuple[str, ...]) -> bool:
    host = str(host or "").lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


class _PlatformRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_domains: tuple[str, ...]):
        super().__init__()
        self.allowed_domains = allowed_domains

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.scheme != "https" or not _domain_allowed(parsed.hostname or "", self.allowed_domains):
            raise ValueError("Площадка перенаправила запрос на неподдерживаемый адрес")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _platform_request(url: str, allowed_domains: tuple[str, ...], *, data: bytes | None = None,
                      headers: dict | None = None, timeout: float = 18.0,
                      max_bytes: int = 8_000_000) -> tuple[int, bytes]:
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or not _domain_allowed(parsed.hostname or "", allowed_domains):
        raise ValueError("Недопустимый адрес сервиса плейлистов")
    request = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    opener = urllib.request.build_opener(_PlatformRedirectHandler(allowed_domains))
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        final = urlparse(response.geturl())
        if final.scheme != "https" or not _domain_allowed(final.hostname or "", allowed_domains):
            raise ValueError("Площадка перенаправила запрос на неподдерживаемый адрес")
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError("Ответ сервиса плейлистов слишком большой")
        return int(response.status), body


def lookup_lyrics(title: str, artist: str = "", album: str = "", duration=0) -> dict:
    """Find public plain or line-synced lyrics; cache misses briefly to avoid repeated lookups."""
    _require_online_search_enabled()
    title, artist, album = (str(value or "").strip()[:180] for value in (title, artist, album))
    if artist.casefold() in {"youtube music", "youtube", "неизвестный автор", "неизвестный исполнитель"}:
        artist = ""
    lyrics_title = title
    if artist:
        lyrics_title = re.sub(r"^" + re.escape(artist) + r"\s*[-–—:]\s*", "", title, flags=re.IGNORECASE).strip() or title
    if not title:
        return {"found": False, "message": "Нужно название трека."}
    try:
        seconds = max(0, min(3600, int(float(duration or 0))))
    except (TypeError, ValueError, OverflowError):
        seconds = 0
    key = (title.casefold(), artist.casefold(), album.casefold(), seconds)
    now = time.monotonic()
    with _LYRICS_CACHE_LOCK:
        cached = _LYRICS_CACHE.get(key)
        if cached and cached[0] > now:
            return dict(cached[1])
    def lyrics_request(url, allowed_domains):
        last_status, last_body = 503, b""
        for attempt in range(3):
            try:
                last_status, last_body = _platform_request(url, allowed_domains,
                    headers={"Accept": "application/json", "User-Agent": f"LAST-FREQUENCY/{VERSION}"},
                    timeout=12, max_bytes=1_000_000)
            except (OSError, TimeoutError, urllib.error.URLError) as error:
                logger.info("Временная ошибка сервиса текста, попытка %s/3: %s", attempt + 1, type(error).__name__)
                last_status, last_body = 503, b""
            if last_status != 429 and last_status < 500:
                break
            if attempt < 2:
                time.sleep(0.35 * (attempt + 1))
        return last_status, last_body
    def lookup_lrclib():
        source_domains = ("lrclib.net",)
        params = {"track_name": lyrics_title}
        if artist:
            params["artist_name"] = artist
        if album:
            params["album_name"] = album
        if seconds:
            params["duration"] = seconds
        status, raw = (404, b"")
        if artist:
            status, raw = lyrics_request("https://lrclib.net/api/get?" + urlencode(params), source_domains)
        candidate = None
        if status == 200:
            try:
                value = json.loads(raw.decode("utf-8"))
                candidate = value if isinstance(value, dict) else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                candidate = None
        if not candidate or not (candidate.get("plainLyrics") or candidate.get("syncedLyrics")):
            query = urlencode({"q": f"{artist} {lyrics_title}".strip()})
            search_status, search_raw = lyrics_request("https://lrclib.net/api/search?" + query, source_domains)
            if search_status == 200:
                try:
                    results = json.loads(search_raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    results = []
                if isinstance(results, list):
                    target_title, target_artist = _music_match_text(lyrics_title), _music_match_text(artist)
                    scored = []
                    for item in results[:30]:
                        if not isinstance(item, dict):
                            continue
                        title_score = difflib.SequenceMatcher(None, target_title, _music_match_text(item.get("trackName"))).ratio()
                        artist_score = (difflib.SequenceMatcher(None, target_artist, _music_match_text(item.get("artistName"))).ratio()
                                        if target_artist else .78)
                        duration_diff = abs(float(item.get("duration") or 0) - seconds) if seconds else 0
                        score = title_score * (.72 if target_artist else .9) + artist_score * (.28 if target_artist else .1) - (min(duration_diff, 120) / 120 * .12)
                        if item.get("plainLyrics") or item.get("syncedLyrics"):
                            scored.append((score, item))
                    if scored and max(scored, key=lambda pair: pair[0])[0] >= (.72 if target_artist else .78):
                        candidate = max(scored, key=lambda pair: pair[0])[1]
            if search_status == 429 or search_status >= 500:
                return None, "unavailable"
        if candidate and (candidate.get("plainLyrics") or candidate.get("syncedLyrics")):
            return candidate, "found"
        return None, "not_found"

    def lookup_lyrics_ovh():
        if not artist:
            return None, "not_found"
        lyrics_url = "https://api.lyrics.ovh/v1/{}/{}".format(
            quote(artist, safe=""), quote(lyrics_title, safe=""))
        status, raw = lyrics_request(lyrics_url, ("lyrics.ovh",))
        if status == 429 or status >= 500:
            return None, "unavailable"
        if status != 200:
            return None, "not_found"
        try:
            value = json.loads(raw.decode("utf-8"))
            lyrics = str(value.get("lyrics") or "").strip() if isinstance(value, dict) else ""
        except (UnicodeDecodeError, json.JSONDecodeError):
            lyrics = ""
        if not lyrics:
            return None, "not_found"
        return {"trackName": title, "artistName": artist, "albumName": album,
                "plainLyrics": lyrics, "syncedLyrics": "", "instrumental": False}, "found"

    # Query both providers at once, then choose LRCLIB whenever it has lyrics.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="lyrics") as executor:
        lrclib_future = executor.submit(lookup_lrclib)
        lyrics_ovh_future = executor.submit(lookup_lyrics_ovh)
        lrclib_candidate, lrclib_status = lrclib_future.result()
        lyrics_ovh_candidate, lyrics_ovh_status = lyrics_ovh_future.result()
    sources = {"lrclib": lrclib_status, "lyrics_ovh": lyrics_ovh_status}
    candidate = lrclib_candidate or lyrics_ovh_candidate
    unavailable = "unavailable" in sources.values()
    if not candidate:
        result = {"found": False, "message": "Сервис текста временно недоступен." if unavailable else "Текст для этого трека не найден.", "sources": sources}
    else:
        result = {"found": True, "title": str(candidate.get("trackName") or title)[:180],
                  "artist": str(candidate.get("artistName") or artist)[:180],
                  "album": str(candidate.get("albumName") or album)[:180],
                  "plain": str(candidate.get("plainLyrics") or "")[:30000],
                  "synced": str(candidate.get("syncedLyrics") or "")[:30000],
                  "instrumental": bool(candidate.get("instrumental")), "sources": sources}
        if not result["plain"] and result["synced"]:
            result["plain"] = re.sub(r"(?m)^\s*\[[^]]+\]\s*", "", result["synced"])
    with _LYRICS_CACHE_LOCK:
        if len(_LYRICS_CACHE) >= 500:
            oldest = min(_LYRICS_CACHE, key=lambda item: _LYRICS_CACHE[item][0])
            _LYRICS_CACHE.pop(oldest, None)
        if not unavailable:
            _LYRICS_CACHE[key] = (now + (6 * 3600 if result.get("found") else 10 * 60), result)
    return dict(result)


def _playlist_duration(value) -> int:
    if isinstance(value, dict):
        value = value.get("totalMilliseconds") or value.get("durationInMillis") or value.get("duration_ms") or value.get("duration")
    if isinstance(value, str) and value.upper().startswith("PT"):
        match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value.upper())
        if match:
            hours, minutes, seconds = (int(part or 0) for part in match.groups())
            return hours * 3600 + minutes * 60 + seconds
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, round(number / 1000 if number > 10000 else number))


def _dedupe_imported_tracks(tracks: list, limit: int = 1000) -> list:
    results, seen = [], set()
    for track in tracks:
        title = str(track.get("title") or "").strip()[:180]
        artist = str(track.get("artist") or "").strip()[:180]
        if not title:
            continue
        key = (title.casefold(), artist.casefold())
        if key in seen:
            continue
        seen.add(key)
        track.update(title=title, artist=artist, duration=_playlist_duration(track.get("duration")))
        results.append(track)
        if len(results) >= limit:
            break
    return results


def _spotify_pathfinder_tracks(playlist_id: str, access_token: str) -> tuple[list, int]:
    """Read public playlist pages with the short-lived token supplied by Spotify's public embed."""
    endpoint = "https://api-partner.spotify.com/pathfinder/v1/query"
    query_hash = "8964e8eafb21aa992a7d951d256d83285c04be2105d209262901de70cb97584a"
    uri = "spotify:playlist:" + playlist_id
    tracks, total, offset = [], 0, 0
    for _ in range(20):
        payload = json.dumps({"variables": {"uri": uri, "offset": offset, "limit": 100,
                                             "enableWatchFeedEntrypoint": True},
                              "operationName": "fetchPlaylist",
                              "extensions": {"persistedQuery": {"version": 1, "sha256Hash": query_hash}}}).encode("utf-8")
        status, body = _platform_request(endpoint, ("api-partner.spotify.com",), data=payload,
            headers={"Authorization": "Bearer " + access_token, "Content-Type": "application/json",
                     "Accept": "application/json", "Referer": "https://open.spotify.com/",
                     "User-Agent": "Mozilla/5.0 LAST FREQUENCY/1.23"}, max_bytes=2_500_000)
        if status != 200:
            break
        response = json.loads(body.decode("utf-8", "replace"))
        content = (((response.get("data") or {}).get("playlistV2") or {}).get("content") or {})
        items = content.get("items")
        if not isinstance(items, list) or not items:
            break
        total = int(content.get("totalCount") or total or len(items))
        def image_area(source):
            try:
                return int(source.get("width") or 0) * int(source.get("height") or 0)
            except (TypeError, ValueError, OverflowError):
                return 0
        for item in items:
            track = (item.get("itemV2") or {}).get("data") or (item.get("item") or {}).get("data") or {}
            album = track.get("albumOfTrack") if isinstance(track.get("albumOfTrack"), dict) else {}
            artists = (((album.get("artists") or {}).get("items") or []))
            artist = ", ".join(str(((artist_item.get("profile") or {}).get("name")) or "") for artist_item in artists).strip(", ")
            cover = album.get("coverArt") if isinstance(album.get("coverArt"), dict) else {}
            cover_sources = cover.get("sources") if isinstance(cover.get("sources"), list) else []
            image = max((source for source in cover_sources if isinstance(source, dict) and source.get("url")),
                        key=image_area, default={})
            tracks.append({"id": str(track.get("uri") or ""), "title": str(track.get("name") or ""),
                           "artist": artist, "duration": _playlist_duration(track.get("trackDuration") or track.get("duration")),
                           "thumbnail": str(image.get("url") or "")})
        offset += len(items)
        if offset >= total or len(tracks) >= 2000:
            break
    return _dedupe_imported_tracks(tracks, 2000), total


def _load_spotify_playlist(url: str) -> dict:
    playlist_id = re.search(r"(?:spotify:playlist:|/playlist/)([A-Za-z0-9]{22})", url, re.I)
    if not playlist_id:
        raise ValueError("Вставь ссылку на плейлист Spotify")
    playlist_id = playlist_id.group(1)
    embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    status, body_bytes = _platform_request(embed_url, ("open.spotify.com",), headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9", "Accept": "text/html,application/json"})
    if status != 200:
        raise ValueError(f"Spotify не открыл публичный плейлист (HTTP {status})")
    page = body_bytes.decode("utf-8", "replace")
    next_data = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>([\s\S]*?)</script>', page, re.I)
    if not next_data:
        raise ValueError("Spotify не отдал данные плейлиста. Проверь, что он публичный.")
    try:
        data = json.loads(html.unescape(next_data.group(1)))
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError("Spotify вернул некорректные данные плейлиста") from error
    page_props = ((data.get("props") or {}).get("pageProps") or {})
    state = page_props.get("state") or {}
    entity = (((state.get("data") or {}).get("entity")) or {})
    title = str(entity.get("name") or page_props.get("title") or "Spotify Playlist")
    cover = ""
    for candidate in (entity.get("images"), (entity.get("coverArt") or {}).get("sources")):
        if isinstance(candidate, list) and candidate and isinstance(candidate[0], dict):
            cover = str(candidate[0].get("url") or "")
            if cover:
                break
    if not cover:
        og = re.search(r'<meta\s+(?:property|name)=["\'](?:og:image|twitter:image)["\']\s+content=["\']([^"\']+)', page, re.I)
        if og:
            cover = html.unescape(og.group(1))
    track_list = entity.get("trackList") if isinstance(entity.get("trackList"), list) else []
    tracks = []
    for item in track_list:
        if not isinstance(item, dict):
            continue
        tracks.append({"id": str(item.get("uri") or ""), "title": str(item.get("title") or item.get("name") or ""),
                       "artist": str(item.get("subtitle") or item.get("artist") or ""),
                       "duration": _playlist_duration(item.get("duration") or item.get("duration_ms")),
                       "thumbnail": str(item.get("image") or "")})
    total = int(entity.get("totalCount") or len(tracks))
    access_token = str((((state.get("settings") or {}).get("session") or {}).get("accessToken") or ""))
    if access_token:
        try:
            full_tracks, full_total = _spotify_pathfinder_tracks(playlist_id, access_token)
            if full_tracks:
                tracks, total = full_tracks, full_total or total
        except Exception as error:
            logger.info("Spotify public playlist paging failed: %s", type(error).__name__)
    tracks = _dedupe_imported_tracks(tracks)
    if not tracks:
        raise ValueError("Плейлист Spotify пустой, закрытый или недоступен")
    for track in tracks:
        if not track.get("thumbnail"):
            track["thumbnail"] = cover
    return {"id": playlist_id, "title": title[:180], "artist": "Spotify", "thumbnail": cover,
            "url": f"https://open.spotify.com/playlist/{playlist_id}", "provider": "spotify",
            "source": "Spotify", "kind": "playlist", "is_playlist": True,
            "tracks": tracks, "track_count": total or len(tracks), "has_more": total > len(tracks)}


def _load_apple_playlist(url: str) -> dict:
    parsed = urlparse(url)
    album = re.search(r"/(?:[a-z]{2}/)?album/(?:[^/]+/)?(\d+)(?:/|$)", parsed.path, re.I)
    tracks, title, cover, total = [], "", "", 0
    if album:
        lookup = "https://itunes.apple.com/lookup?" + urlencode({"id": album.group(1), "entity": "song", "limit": 1000})
        status, body = _platform_request(lookup, ("itunes.apple.com",), headers={"Accept": "application/json"}, max_bytes=5_000_000)
        if status != 200:
            raise ValueError(f"Apple Music не открыл альбом (HTTP {status})")
        data = json.loads(body.decode("utf-8", "replace"))
        items = data.get("results") if isinstance(data.get("results"), list) else []
        collection = next((item for item in items if item.get("wrapperType") == "collection"), {})
        title = str(collection.get("collectionName") or "Apple Music Album")
        cover = str(collection.get("artworkUrl100") or "").replace("100x100", "600x600")
        tracks = [{"id": str(item.get("trackId") or ""), "title": item.get("trackName", ""),
                   "artist": item.get("artistName") or collection.get("artistName", ""),
                   "duration": _playlist_duration(item.get("trackTimeMillis")), "thumbnail": cover}
                  for item in items if item.get("wrapperType") == "track"]
        total = len(tracks)
    else:
        if not re.search(r"/playlist/.*?/pl\.(?:u-)?[A-Za-z0-9_-]+", parsed.path, re.I):
            raise ValueError("Вставь публичную ссылку Apple Music на плейлист или альбом")
        status, body = _platform_request(url, ("music.apple.com",), headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9"})
        if status != 200:
            raise ValueError(f"Apple Music не открыл публичный плейлист (HTTP {status})")
        page = body.decode("utf-8", "replace")
        serialized = re.search(r'<script\b(?=[^>]*\bid=["\']serialized-server-data["\'])(?=[^>]*\btype=["\']application/json["\'])[^>]*>([\s\S]*?)</script>', page, re.I)
        if not serialized:
            serialized = re.search(r'<script[^>]+id=["\']schema:music-playlist["\'][^>]*>([\s\S]*?)</script>', page, re.I)
        data = None
        if serialized:
            try:
                data = json.loads(html.unescape(serialized.group(1)))
            except (json.JSONDecodeError, ValueError):
                data = None
        if not data:
            raise ValueError("Apple Music не отдал список треков. Проверь, что плейлист публичный.")
        seen = set()
        def walk(node):
            nonlocal title, cover, total
            if isinstance(node, list):
                for child in node:
                    walk(child)
                return
            if not isinstance(node, dict):
                return
            descriptor = node.get("contentDescriptor") or {}
            attrs = node.get("attributes") if isinstance(node.get("attributes"), dict) else node
            node_type = str(node.get("type") or "").lower()
            if not title and (descriptor.get("kind") == "playlist" or node_type == "playlists" or node.get("@type") == "MusicPlaylist"):
                title = str(attrs.get("name") or node.get("title") or node.get("name") or "")
            artwork = node.get("artwork") or attrs.get("artwork") or {}
            if not cover and isinstance(artwork, dict):
                cover = str(artwork.get("url") or (artwork.get("dictionary") or {}).get("url") or "")
            if not total and isinstance(node.get("trackCount"), int):
                total = node["trackCount"]
            track_title = attrs.get("name") or node.get("title")
            artist = attrs.get("artistName") or node.get("artistName")
            if not artist:
                artist_node = node.get("artist")
                artist = artist_node.get("name") if isinstance(artist_node, dict) else artist_node
            is_track = (descriptor.get("kind") in {"song", "music-video"} or node_type in {"songs", "song", "library-songs"}
                        or node.get("@type") == "MusicRecording" or bool(node.get("audioTraits")))
            if isinstance(track_title, str) and track_title.strip() and isinstance(artist, str) and artist.strip() and is_track:
                key = (track_title.strip().casefold(), artist.strip().casefold())
                if key not in seen:
                    seen.add(key)
                    tracks.append({"title": track_title, "artist": artist,
                                   "duration": _playlist_duration(attrs.get("durationInMillis") or attrs.get("duration") or node.get("duration")),
                                   "thumbnail": cover})
                return
            for child in node.values():
                walk(child)
        walk(data)
        if not title:
            og = re.search(r'<meta\s+(?:property|name)=["\']og:title["\']\s+content=["\']([^"\']*)', page, re.I)
            title = html.unescape(og.group(1)).replace(" - Apple Music", "").strip() if og else "Apple Music Playlist"
        cover = cover.replace("{w}x{h}bb.{f}", "600x600bb.jpg")
    for track in tracks:
        thumbnail = str(track.get("thumbnail") or cover)
        thumbnail = thumbnail.replace("{w}x{h}bb.{f}", "600x600bb.jpg")
        if thumbnail and not thumbnail.startswith("https://"):
            thumbnail = cover
        track["thumbnail"] = thumbnail
    tracks = _dedupe_imported_tracks(tracks)
    if not tracks:
        raise ValueError("В публичных данных Apple Music не найдено ни одного трека")
    return {"id": hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()[:16], "title": title[:180],
            "artist": "Apple Music", "thumbnail": cover, "url": url, "provider": "apple_music",
            "source": "Apple Music", "kind": "playlist", "is_playlist": True,
            "tracks": tracks, "track_count": total or len(tracks), "has_more": total > len(tracks)}


def _load_yandex_playlist(url: str) -> dict:
    parsed = urlparse(url)
    path = unquote(parsed.path)
    user_match = re.search(r"/users/([^/]+)/playlists/([^/]+)", path, re.I)
    id_match = re.search(r"/playlists/([^/]+)", path, re.I)
    direct_match = re.search(r"/playlist/([^/]+)(?:/([^/]+))?", path, re.I)
    if user_match:
        api_path = "/users/{}/playlists/{}".format(quote(user_match.group(1), safe=""), quote(user_match.group(2), safe=""))
    elif direct_match and direct_match.group(2):
        api_path = "/users/{}/playlists/{}".format(quote(direct_match.group(1), safe=""), quote(direct_match.group(2), safe=""))
    elif direct_match:
        api_path = "/playlist/" + quote(direct_match.group(1), safe="")
    elif id_match:
        api_path = "/playlist/" + quote(id_match.group(1), safe="")
    else:
        raise ValueError("Не удалось разобрать ссылку на плейлист Яндекс Музыки")
    api_url = "https://api.music.yandex.net" + api_path
    status, body = _platform_request(api_url, ("api.music.yandex.net",), headers={
        "User-Agent": "Yandex-Music-API", "X-Yandex-Music-Client": "YandexMusicAndroid/24023231",
        "Accept": "application/json"}, max_bytes=8_000_000)
    if status != 200:
        raise ValueError(f"Яндекс Музыка не отдала публичный плейлист (HTTP {status})")
    data = json.loads(body.decode("utf-8", "replace"))
    result = data.get("result") if isinstance(data.get("result"), dict) else data
    raw_tracks = result.get("tracks") if isinstance(result.get("tracks"), list) else []
    cover_info = result.get("cover") or {}
    cover_uri = str(result.get("ogImage") or (cover_info.get("uri") if isinstance(cover_info, dict) else cover_info if isinstance(cover_info, str) else ""))
    cover = cover_uri if cover_uri.startswith("https://") else ("https://" + cover_uri.replace("%%", "400x400") if cover_uri else "")
    tracks = []
    for item in raw_tracks:
        track = item.get("track") if isinstance(item, dict) and isinstance(item.get("track"), dict) else item
        if not isinstance(track, dict):
            continue
        artists = track.get("artists") if isinstance(track.get("artists"), list) else []
        artist = ", ".join(str(entry.get("name") or "") for entry in artists if isinstance(entry, dict)).strip(", ")
        track_cover = str(track.get("coverUri") or "")
        thumbnail = ("https://" + track_cover.replace("%%", "400x400")) if track_cover and not track_cover.startswith("https://") else track_cover
        tracks.append({"id": str(track.get("id") or ""), "title": track.get("title") or "",
                       "artist": artist, "duration": _playlist_duration(track.get("durationMs") or track.get("duration")),
                       "thumbnail": thumbnail or cover})
    tracks = _dedupe_imported_tracks(tracks)
    if not tracks:
        raise ValueError("Плейлист Яндекс Музыки пустой, закрытый или недоступен")
    return {"id": str(result.get("uid") or result.get("kind") or hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()[:16]),
            "title": str(result.get("title") or result.get("name") or "Яндекс Музыка")[:180], "artist": "Яндекс Музыка",
            "thumbnail": cover, "url": url, "provider": "yandex", "source": "Яндекс Музыка",
            "kind": "playlist", "is_playlist": True, "tracks": tracks,
            "track_count": int(result.get("trackCount") or len(tracks)),
            "has_more": int(result.get("trackCount") or len(tracks)) > len(tracks)}


def _load_external_playlist(url: str) -> dict:
    provider = _external_playlist_provider(url)
    if provider == "spotify":
        return _load_spotify_playlist(url)
    if provider == "apple_music":
        return _load_apple_playlist(url)
    if provider == "yandex":
        return _load_yandex_playlist(url)
    raise ValueError("Ссылка должна вести на публичный плейлист Spotify, Apple Music или Яндекс Музыки")


def load_global_playlist(url: str) -> dict:
    """Load a bounded, flat list of tracks from a public supported playlist."""
    _require_online_search_enabled()
    url = str(url or "").strip()[:2000]
    if _external_playlist_provider(url):
        return _load_external_playlist(url)
    provider = _remote_page_provider(url)
    if not provider:
        raise ValueError("Вставь публичную ссылку на плейлист поддерживаемой площадки")
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    video_id = params.get("v", [""])[0]
    if parsed.hostname and parsed.hostname.lower().endswith("youtu.be") and parsed.path.strip("/"):
        video_id = parsed.path.strip("/").split("/")[0]
    if video_id:
        raise ValueError("Это ссылка на один трек. Нужна ссылка на плейлист")
    ytdlp = _yt_dlp_module(allow_online_search=True)
    options = {"quiet": True, "no_warnings": True, "skip_download": True,
               "extract_flat": "in_playlist", "playlistend": 100,
               "socket_timeout": 20, "noplaylist": False}
    with ytdlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    entries = info.get("entries") if isinstance(info, dict) else None
    if entries is None:
        raise ValueError("По этой ссылке не найден плейлист")
    try:
        entries = list(entries)[:100]
    except Exception as error:
        raise RuntimeError("Не удалось прочитать список треков плейлиста") from error
    tracks = []
    playlist_thumbnail = str(_public_result(info).get("thumbnail") or info.get("artwork_url") or "")
    if not playlist_thumbnail and entries:
        first_entry = entries[0] if isinstance(entries, list) else None
        if isinstance(first_entry, dict):
            playlist_thumbnail = str(_public_result(first_entry).get("thumbnail") or "")
    for item in entries[:100]:
        if not isinstance(item, dict):
            continue
        track = _public_result(item)
        track["provider"] = provider
        track["source"] = "YouTube" if provider == "youtube" else "SoundCloud"
        track["preview_only"] = False
        if not track.get("thumbnail"):
            track["thumbnail"] = str(item.get("thumbnail") or playlist_thumbnail)
        if provider == "youtube" and track.get("id") and not track.get("thumbnail"):
            track["thumbnail"] = "https://i.ytimg.com/vi/" + str(track["id"]) + "/hqdefault.jpg"
        if not track.get("url") and item.get("url"):
            candidate = str(item.get("url"))
            if candidate.startswith("https://"):
                track["url"] = candidate
        if track.get("url"):
            tracks.append(track)
    if not playlist_thumbnail:
        playlist_thumbnail = next((str(track.get("thumbnail") or "") for track in tracks if track.get("thumbnail")), "")
    return {"title": str(info.get("title") or "Плейлист")[:180], "provider": provider,
            "thumbnail": playlist_thumbnail, "url": url, "tracks": tracks, "track_count": len(tracks)}


_RADIO_REMIX_RE = re.compile(
    r"\b(remix(?:ed)?|bootleg|rework|mash[ -]?up|nightcore|speed[ -]?up|slowed|club mix|extended mix)\b|"
    r"ремикс\w*|мэш[ -]?ап\w*|найткор\w*|ускоренн\w+ версия|замедленн\w+ версия", re.I)
_RADIO_METADATA_CACHE = {}
_RADIO_METADATA_LOCK = threading.Lock()


def _prepare_radio_metadata(tracks: list) -> list:
    """Resolve missing authors without requesting or preloading audio/video streams."""
    def prepare(track):
        if _valid_music_artist(track.get("artist")):
            return track
        url = str(track.get("url") or "")
        if _remote_page_provider(url) not in ("youtube", "youtube_music"):
            return track
        with _RADIO_METADATA_LOCK:
            cached = _RADIO_METADATA_CACHE.get(url)
        if cached and cached[0] > time.monotonic():
            metadata = cached[1]
        else:
            try:
                metadata = _http_json("https://www.youtube.com/oembed?" + urlencode({"url": url, "format": "json"}), timeout=5)
            except Exception:
                metadata = {}
            with _RADIO_METADATA_LOCK:
                _RADIO_METADATA_CACHE[url] = (time.monotonic() + (3600 if metadata else 60), metadata)
                while len(_RADIO_METADATA_CACHE) > 1000:
                    _RADIO_METADATA_CACHE.pop(next(iter(_RADIO_METADATA_CACHE)))
        author = _valid_music_artist(metadata.get("author_name"))
        if author:
            track["artist"] = author
        return track
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="radio-metadata") as pool:
        return list(pool.map(prepare, tracks))
_RADIO_AI_SLOP_RE = re.compile(
    r"\b(ai[ -]?(?:generated|music|cover|song|voice)|artificial intelligence|suno|udio|riffusion|"
    r"stable audio|voice clone|ai slop|generated by ai)\b|"
    r"ии[ -]?(?:музык\w*|кавер\w*|песн\w*|сгенерирован\w*)|нейросет\w*|создан\w+ нейросет\w*", re.I)
_RADIO_INSTRUMENTAL_RE = re.compile(
    r"\b(?:instrumental(?:s)?|karaoke|backing[ -]track|minus[ -]one)\b|"
    r"инструментал\w*|минусовк\w*|без\s+вокала", re.I)


def _radio_track_identity(title: str, artist: str = "") -> tuple[str, str]:
    title_key = _music_match_text(title)
    artist_key = _music_match_text(artist)
    if artist_key and title_key.startswith(artist_key + " "):
        title_key = title_key[len(artist_key):].strip()
    return title_key, artist_key


def _radio_identity_is_duplicate(identity: tuple[str, str], previous: list[tuple[str, str]]) -> bool:
    title_key, artist_key = identity
    if not title_key:
        return False
    for old_title, old_artist in previous:
        if not old_title:
            continue
        title_score = difflib.SequenceMatcher(None, title_key, old_title).ratio()
        if title_key != old_title and (min(len(title_key), len(old_title)) < 8 or title_score < 0.94):
            continue
        if not artist_key or not old_artist or artist_key == old_artist:
            return True
        if difflib.SequenceMatcher(None, artist_key, old_artist).ratio() >= 0.84:
            return True
    return False


def _radio_track_filtered(title: str, artist: str, *, exclude_remixes: bool, exclude_ai_slop: bool,
                          exclude_instrumentals: bool = True, blocked_tracks=()) -> bool:
    text = f"{title} {artist}"
    return ((exclude_remixes and bool(_RADIO_REMIX_RE.search(text)))
            or (exclude_ai_slop and bool(_RADIO_AI_SLOP_RE.search(text)))
            or (exclude_instrumentals and bool(_RADIO_INSTRUMENTAL_RE.search(text)))
            or _radio_identity_is_duplicate(_radio_track_identity(title, artist), blocked_tracks))


def load_radio_queue(seed_url: str, title: str = "", artist: str = "", limit: int = 30,
                     provider: str = "all", exclude_remixes: bool | None = None,
                     exclude_ai_slop: bool | None = None, exclude_instrumentals: bool | None = None) -> dict:
    """Resolve a YouTube-generated mix, falling back to a bounded music search."""
    _require_online_search_enabled()
    limit = max(1, min(100, int(limit or 30)))
    provider = str(provider or "all").strip().lower()
    if provider not in {"all", "youtube_music", "youtube", "soundcloud"}:
        provider = "all"
    state = globals().get("STATE")
    settings = state.settings.get() if state is not None else DEFAULT_SETTINGS
    blocked_tracks = [_radio_track_identity(track.get("title", ""), track.get("artist", ""))
                      for track in settings.get("radio_blocked_tracks", [])]
    exclude_remixes = settings.get("radio_exclude_remixes", True) if exclude_remixes is None else bool(exclude_remixes)
    exclude_ai_slop = settings.get("radio_exclude_ai_slop", True) if exclude_ai_slop is None else bool(exclude_ai_slop)
    exclude_instrumentals = settings.get("radio_exclude_instrumentals", True) if exclude_instrumentals is None else bool(exclude_instrumentals)
    seed_url = str(seed_url or "").strip()[:2000]
    title, artist = str(title or "").strip()[:180], str(artist or "").strip()[:180]
    video_id = ""
    if seed_url:
        parsed = urlparse(seed_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme == "https" and (host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be"):
            video_id = (parse_qs(parsed.query).get("v") or [""])[0]
            if host == "youtu.be" and parsed.path.strip("/"):
                video_id = parsed.path.strip("/").split("/")[0]
            if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                video_id = ""

    ytdlp = _yt_dlp_module(allow_online_search=True)
    options = {"quiet": True, "no_warnings": True, "skip_download": True,
               "extract_flat": "in_playlist", "playlistend": 60,
               "socket_timeout": 18, "noplaylist": False}
    entries = []
    if video_id and provider in ("all", "youtube"):
        mix_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
        try:
            with ytdlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(mix_url, download=False) or {}
            entries = [item for item in (info.get("entries") or []) if isinstance(item, dict)]
        except Exception as error:
            logger.info("YouTube Radio Mix недоступен, использую музыкальный поиск: %s", type(error).__name__)

    base_query = " ".join(part for part in (artist, title) if part).strip()
    queries = []
    if artist and title:
        queries.extend((f"{artist} {title} similar songs", f"{artist} related music", f"{artist} songs"))
    elif base_query:
        queries.extend((f"{base_query} similar songs", f"{base_query} music"))

    non_music = re.compile(r"\b(reaction|reacts|review|tutorial|gameplay|walkthrough|podcast|news|interview|vlog|shorts?|trailer|full movie|episode|documentary|обзор|реакци\w*|урок\w*|обучени\w*|прохождени\w*|подкаст\w*|новост\w*|интервью|влог\w*|трейлер|фильм|серия|документал\w*)\b", re.I)
    if provider in {"youtube_music", "soundcloud"}:
        results, seen, song_keys = [], set(), []
        seed_title = _music_match_text(title)
        seed_artist = _music_match_text(artist)
        for query in queries:
            if len(results) >= limit:
                break
            try:
                found = search_remote(provider, query, require_downloader=False)
            except Exception as error:
                logger.info("Поиск радио в %s не удался: %s", provider, type(error).__name__)
                continue
            for item in found:
                if not isinstance(item, dict):
                    continue
                track = dict(item)
                track_id = str(track.get("id") or track.get("url") or "").strip()
                track_title = str(track.get("title") or "").strip()
                track_artist = str(track.get("artist") or "").strip()
                if (not track_id or not track_title or non_music.search(track_title)
                        or _radio_track_filtered(track_title, track_artist, exclude_remixes=exclude_remixes,
                                                 exclude_ai_slop=exclude_ai_slop, exclude_instrumentals=exclude_instrumentals, blocked_tracks=blocked_tracks)):
                    continue
                try:
                    duration = float(track.get("duration") or track.get("dur") or 0)
                except (TypeError, ValueError):
                    duration = 0
                if duration > 900:
                    continue
                identity = _radio_track_identity(track_title, track_artist)
                if _radio_identity_is_duplicate(identity, song_keys):
                    continue
                if _radio_identity_is_duplicate(identity, [_radio_track_identity(seed_title, seed_artist)]):
                    continue
                key = (provider, track_id.casefold())
                if key in seen:
                    continue
                seen.add(key)
                track.update({"id": track_id, "provider": provider,
                              "source": "YouTube Music" if provider == "youtube_music" else "SoundCloud",
                              "preview_only": False})
                results.append(track)
                song_keys.append(identity)
                if len(results) >= limit:
                    break
        results = _prepare_radio_metadata(results)
        results = [track for track in results if not _radio_track_filtered(track.get("title", ""), track.get("artist", ""), exclude_remixes=exclude_remixes, exclude_ai_slop=exclude_ai_slop, exclude_instrumentals=exclude_instrumentals, blocked_tracks=blocked_tracks)]
        return {"results": results, "seed": video_id, "requested": limit, "provider": provider}

    for query in queries:
        if len(entries) >= limit * 3:
            break
        try:
            with ytdlp.YoutubeDL({**options, "noplaylist": True}) as ydl:
                found = ydl.extract_info(f"ytsearch{min(60, limit * 2)}:{query}", download=False) or {}
            entries.extend(item for item in (found.get("entries") or []) if isinstance(item, dict))
        except Exception as error:
            logger.info("Поиск похожих треков для радио не удался: %s", type(error).__name__)

    results, deferred, seen, artist_counts, song_keys = [], [], {video_id} if video_id else set(), {}, []
    source_limit = max(1, (limit + 2) // 3) if provider == "all" else limit
    for item in entries:
        raw_id = str(item.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", raw_id):
            raw_id = _public_result(item).get("id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(raw_id)) or raw_id in seen:
            continue
        text = str(item.get("title") or "")
        track = _public_result(item)
        try:
            duration = float(item.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        if (duration > 900 or non_music.search(text)
                or _radio_track_filtered(text, str(track.get("artist") or ""), exclude_remixes=exclude_remixes,
                                         exclude_ai_slop=exclude_ai_slop, exclude_instrumentals=exclude_instrumentals, blocked_tracks=blocked_tracks)):
            continue
        track.update({"id": raw_id, "url": f"https://www.youtube.com/watch?v={raw_id}",
                      "provider": "youtube", "source": "YouTube", "preview_only": False})
        if not track.get("thumbnail"):
            track["thumbnail"] = f"https://i.ytimg.com/vi/{raw_id}/hqdefault.jpg"
        if not track.get("artist") or track["artist"] in ("Неизвестный автор", "YouTube", "YouTube Music"):
            track["artist"] = artist or "YouTube Music"
        identity = _radio_track_identity(track.get("title") or "", track.get("artist") or "")
        if _radio_identity_is_duplicate(identity, song_keys):
            continue
        if _radio_identity_is_duplicate(identity, [_radio_track_identity(title, artist)]):
            continue
        seen.add(raw_id)
        song_keys.append(identity)
        artist_key = _music_match_text(track.get("artist") or "")
        if artist_key and artist_key not in {"youtube", "youtube music", "неизвестный автор"} and artist_counts.get(artist_key, 0) >= 4:
            deferred.append(track)
            continue
        results.append(track)
        if artist_key:
            artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
        if len(results) >= source_limit:
            break
    if len(results) < source_limit:
        results.extend(deferred[:source_limit-len(results)])

    if provider == "all" and queries and len(results) < limit:
        seed_title, seed_artist = _music_match_text(title), _music_match_text(artist)
        source_ids = {(str(item.get("provider") or "youtube"), str(item.get("id") or "").casefold())
                      for item in results}
        for source in ("youtube_music", "soundcloud"):
            source_count = 0
            try:
                found = search_remote(source, queries[0], require_downloader=False)
            except Exception as error:
                logger.info("Дополнительный поиск радио в %s не удался: %s", source, type(error).__name__)
                continue
            for item in found:
                if len(results) >= limit or source_count >= source_limit or not isinstance(item, dict):
                    break
                track = dict(item)
                track_id = str(track.get("id") or track.get("url") or "").strip()
                track_title = str(track.get("title") or "").strip()
                track_artist = str(track.get("artist") or "").strip()
                if (not track_id or not track_title or not track.get("url") or non_music.search(track_title)
                        or _radio_track_filtered(track_title, track_artist, exclude_remixes=exclude_remixes,
                                                 exclude_ai_slop=exclude_ai_slop, exclude_instrumentals=exclude_instrumentals, blocked_tracks=blocked_tracks)):
                    continue
                try:
                    duration = float(track.get("duration") or track.get("dur") or 0)
                except (TypeError, ValueError):
                    duration = 0
                if duration > 900:
                    continue
                identity = _radio_track_identity(track_title, track_artist)
                if _radio_identity_is_duplicate(identity, [_radio_track_identity(seed_title, seed_artist)]):
                    continue
                if _radio_identity_is_duplicate(identity, song_keys):
                    continue
                key = (source, track_id.casefold())
                if key in source_ids:
                    continue
                source_ids.add(key)
                song_keys.append(identity)
                track.update({"id": track_id, "provider": source,
                              "source": "YouTube Music" if source == "youtube_music" else "SoundCloud",
                              "preview_only": False})
                results.append(track)
                source_count += 1
    results = _prepare_radio_metadata(results)
    results = [track for track in results if not _radio_track_filtered(track.get("title", ""), track.get("artist", ""), exclude_remixes=exclude_remixes, exclude_ai_slop=exclude_ai_slop, exclude_instrumentals=exclude_instrumentals, blocked_tracks=blocked_tracks)]
    return {"results": results, "seed": video_id, "requested": limit, "provider": provider}


def search_global(query: str, provider: str = "all", kind: str = "track") -> dict:
    """Search all supported catalogs or one selected provider, without requiring the MP3 downloader."""
    _require_online_search_enabled()
    query = str(query or "").strip()
    query = query[:2000] if query.lower().startswith(("https://", "http://")) else query[:200]
    provider = str(provider or "all").strip().lower()
    selected_provider = provider
    kind = "playlist" if str(kind or "track").strip().lower() == "playlist" else "track"
    if provider not in {"all", "youtube_music", "youtube", "soundcloud"}:
        raise ValueError("Выбери доступный источник: все площадки, YouTube Music, YouTube или SoundCloud")
    if not query:
        return {"results": [], "errors": [], "query": "", "provider": provider, "kind": kind}
    if query.lower().startswith(("https://", "http://", "spotify:playlist:")):
        result = _global_url_result(query)
        return {"results": [result], "errors": [], "query": query, "provider": result.get("provider", provider), "kind": result.get("kind", "track")}
    if kind == "playlist":
        # yt-dlp's SoundCloud search extractor indexes tracks only. Treating
        # /search/sets as a user URL makes it fail with a noisy 404; SoundCloud's
        # official playlist-search API requires OAuth credentials this app does
        # not have. Public SoundCloud playlist links can still be opened directly.
        soundcloud_playlist_note = "Поиск плейлистов SoundCloud недоступен без авторизации. Вставь прямую ссылку на плейлист."
        selected = ["youtube"] if provider in ("all", "youtube_music", "youtube") else []
        results, errors = [], ([{"provider": "SoundCloud", "message": soundcloud_playlist_note}]
                               if provider in ("all", "soundcloud") else [])
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="lf-playlist-search") as pool:
            futures = {pool.submit(_search_global_playlists, query, source): source for source in selected}
            completed, pending = wait(futures, timeout=20)
            for future in completed:
                source = futures[future]
                try:
                    results.extend(future.result())
                except Exception as error:
                    logger.info("Global playlist search provider %s failed: %s", source, error)
                    errors.append({"provider": source, "message": str(error)[:160]})
            for future in pending:
                errors.append({"provider": futures[future], "message": "Источник отвечает слишком долго"})
                future.cancel()
        deduped, seen = [], set()
        for item in results:
            key = (item.get("provider"), str(item.get("id") or item.get("url") or "").casefold())
            if key not in seen:
                seen.add(key)
                deduped.append(item)
        return {"results": deduped[:20], "errors": errors, "query": query, "provider": selected_provider, "kind": kind}
    all_jobs = {
        "youtube_music": lambda: search_remote("youtube_music", query, require_downloader=False),
        "youtube": lambda: search_remote("youtube", query, require_downloader=False),
        "soundcloud": lambda: search_remote("soundcloud", query, require_downloader=False),
    }
    jobs = all_jobs if provider == "all" else {provider: all_jobs[provider]}
    results, errors = [], []
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="lf-global-search") as pool:
        futures = {pool.submit(fn): provider for provider, fn in jobs.items()}
        completed, pending = wait(futures, timeout=20)
        for future in completed:
            source_provider = futures[future]
            try:
                found = future.result()
                for item in found[:20]:
                    item["provider"] = item.get("provider") or source_provider
                    results.append(item)
            except Exception as error:
                logger.info("Global search provider %s failed: %s", source_provider, error)
                errors.append({"provider": source_provider, "message": str(error)[:160]})
        for future in pending:
            errors.append({"provider": futures[future], "message": "Источник отвечает слишком долго"})
            future.cancel()
    # Keep provider ranking and extractor order: yt-dlp already returns relevance order.
    order = {"youtube_music": 0, "youtube": 1, "soundcloud": 2}
    results.sort(key=lambda item: order.get(item.get("provider"), 9))
    deduped, seen = [], set()
    for item in results:
        provider_id = str(item.get("provider") or "")
        family = "youtube" if provider_id in ("youtube", "youtube_music") else provider_id
        key = (family, str(item.get("id") or item.get("url") or "").casefold())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return {"results": deduped[:20], "errors": errors, "query": query, "provider": selected_provider, "kind": kind}


def _music_match_text(value: str) -> str:
    value = str(value or "").casefold()
    value = re.sub(r"\b(official|audio|video|lyrics?|visuali[sz]er|remaster(?:ed)?|topic|hd|4k)\b", " ", value)
    return re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).strip()


def _resolve_imported_playlist_track(title: str, artist: str = "", duration=0) -> dict:
    """Match imported Spotify/Apple/Yandex metadata to a playable YouTube audio page."""
    _require_online_search_enabled()
    title = str(title or "").strip()[:180]
    artist = str(artist or "").strip()[:180]
    if not title:
        raise ValueError("У трека нет названия для поиска совпадения")
    query = " ".join(part for part in (artist, title) if part)[:200]
    target_title, target_artist = _music_match_text(title), _music_match_text(artist)
    try:
        target_duration = float(duration or 0)
    except (TypeError, ValueError):
        target_duration = 0
    best = None
    best_score = 0.0
    for provider in ("youtube_music", "youtube"):
        try:
            candidates = search_remote(provider, query, require_downloader=False)
        except Exception as error:
            logger.info("Imported track matching provider %s failed: %s", provider, type(error).__name__)
            continue
        for candidate in candidates:
            if not candidate.get("url"):
                continue
            candidate_title = _music_match_text(candidate.get("title") or "")
            candidate_artist = _music_match_text(candidate.get("artist") or "")
            title_score = difflib.SequenceMatcher(None, target_title, candidate_title).ratio()
            if target_title and candidate_title and (target_title in candidate_title or candidate_title in target_title):
                title_score = max(title_score, 0.86)
            if target_artist and candidate_artist:
                artist_score = difflib.SequenceMatcher(None, target_artist, candidate_artist).ratio()
                if target_artist in candidate_artist or candidate_artist in target_artist:
                    artist_score = max(artist_score, 0.92)
            else:
                artist_score = 0.5
            try:
                candidate_duration = float(candidate.get("duration") or 0)
            except (TypeError, ValueError):
                candidate_duration = 0
            duration_score = 0.5 if not target_duration or not candidate_duration else max(0.0, 1.0 - abs(target_duration - candidate_duration) / max(45.0, target_duration * 0.6))
            score = title_score * 0.70 + artist_score * 0.24 + duration_score * 0.06
            if score > best_score:
                best, best_score = candidate, score
        if best and best_score >= 0.68:
            break
    if not best or best_score < 0.52:
        raise ValueError("Не нашёл подходящую версию на YouTube Music или YouTube")
    return {"id": str(best.get("id") or ""), "title": str(best.get("title") or title),
            "artist": str(best.get("artist") or artist), "duration": _playlist_duration(best.get("duration")),
            "thumbnail": str(best.get("thumbnail") or ""), "url": str(best.get("url") or ""),
            "source": str(best.get("source") or _PLAYLIST_PROVIDER_NAMES.get(best.get("provider"), "YouTube Music")),
            "provider": str(best.get("provider") or "youtube_music"), "score": round(best_score, 3)}


class _OnlineYDLLogger:
    """Keep extractor diagnostics out of stderr; the HTTP handler reports one clean error."""
    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        logger.debug("yt-dlp online: %s", str(message)[:300])


def _resolve_online_stream_uncached(url: str, provider: str = "") -> dict:
    """Resolve a YouTube/SoundCloud page URL to a directly playable audio stream without downloading it."""
    _require_online_search_enabled()
    parsed = urlparse(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("https",) or not host or not any(host == item or host.endswith("." + item) for item in ("youtube.com", "youtu.be", "soundcloud.com")):
        raise ValueError("Встроенное воспроизведение доступно для ссылок YouTube и SoundCloud")
    ytdlp = _yt_dlp_module(allow_online_search=True)
    options = {"quiet": True, "no_warnings": True, "logger": _OnlineYDLLogger(),
               "skip_download": True, "noplaylist": True, "socket_timeout": 15,
               "format": "bestaudio/best/18"}
    is_youtube = (provider in ("youtube", "youtube_music")
                  or host == "youtu.be" or host.endswith((".youtube.com", "youtube.com")))
    info = {}
    if is_youtube:
        # YouTube may return default-client URLs that immediately answer 403.
        # Riff works around this by trying extractor clients in order; Android
        # currently tends to provide a directly playable HTTPS audio stream.
        clients = ("android", "tv", "web_safari", "ios", None)
        last_error = None
        for client in clients:
            attempt_options = dict(options)
            if client:
                attempt_options["extractor_args"] = {"youtube": {"player_client": [client]}}
            try:
                with ytdlp.YoutubeDL(attempt_options) as ydl:
                    candidate = ydl.extract_info(str(url), download=False) or {}
                candidate_formats = candidate.get("formats") if isinstance(candidate.get("formats"), list) else []
                if any(
                    isinstance(fmt, dict) and fmt.get("url")
                    and str(fmt.get("protocol") or "").startswith(("http", "m3u8"))
                    and (fmt.get("acodec") not in (None, "none")
                         or (str(fmt.get("protocol") or "").startswith("m3u8")
                             and fmt.get("vcodec") == "none"
                             and str(fmt.get("ext") or "").lower() in {"m4a", "mp3", "opus", "aac"}))
                    for fmt in candidate_formats
                ):
                    info = candidate
                    break
                info = candidate or info
            except Exception as error:  # one client may fail while another still yields audio
                last_error = error
                logger.info("YouTube stream resolver client failed: %s (%s)", client or "default", type(error).__name__)
        if not info and last_error:
            raise last_error
    else:
        with ytdlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(str(url), download=False) or {}
    formats = info.get("formats") if isinstance(info.get("formats"), list) else []
    audio_formats = []
    rejected_stream_hosts = []
    for fmt in formats:
        if not isinstance(fmt, dict) or not fmt.get("url"):
            continue
        protocol = str(fmt.get("protocol") or "").lower()
        format_id = str(fmt.get("format_id") or "").lower()
        acodec = fmt.get("acodec")
        audio_only_hls = (protocol.startswith("m3u8") and fmt.get("vcodec") == "none"
                          and str(fmt.get("ext") or "").lower() in {"m4a", "mp3", "opus", "aac"})
        # SoundCloud can omit acodec for audio-only HLS formats; the audio preset
        # and vcodec='none' still identify them. Never pick explicitly encrypted HLS.
        if acodec == "none" or (acodec is None and not audio_only_hls):
            continue
        if not protocol.startswith(("http", "m3u8")):
            continue
        if "hls-aes" in format_id or "encrypted" in format_id:
            continue
        if not _allowed_online_stream_url(fmt.get("url")):
            stream_parts = urlparse(str(fmt.get("url") or ""))
            # Log only scheme and hostname; signed URL query strings are credentials.
            rejected_stream_hosts.append(f"{stream_parts.scheme or '?'}://{(stream_parts.hostname or '?').lower()}")
            continue
        audio_formats.append(fmt)
    # Prefer progressive HTTP, but allow clear HLS when the source has no progressive stream.
    audio_formats.sort(key=lambda f: (
        "m3u8" not in str(f.get("protocol") or "").lower(),
        f.get("vcodec") == "none",
        int(f.get("abr") or 0), int(f.get("tbr") or 0)), reverse=True)
    selected = audio_formats[0] if audio_formats else {}
    source_url = selected.get("url")
    if not source_url:
        protocols = sorted({str(item.get("protocol") or "?") for item in formats if isinstance(item, dict)})
        details = ";".join("{}/{}/a={}/v={}/{}".format(
            str(item.get("protocol") or "?"), str(item.get("ext") or "?"),
            str(item.get("acodec") or "?"), str(item.get("vcodec") or "?"),
            str(item.get("format_id") or "?")[:48])
            for item in formats if isinstance(item, dict))[:500]
        logger.info("Online resolver found no compatible audio format: provider=%s formats=%d protocols=%s details=%s",
                    provider or "unknown", len(formats), ",".join(protocols[:12]) or "none", details or "none")
        if rejected_stream_hosts:
            logger.info("Online resolver rejected stream URL hosts: %s", ",".join(sorted(set(rejected_stream_hosts))[:8]))
        if provider == "soundcloud":
            raise RuntimeError("SoundCloud не предоставил совместимый HTTPS-аудиопоток. Это само по себе не означает, что трек защищён.")
        raise RuntimeError("Источник не предоставил защищённый HTTPS-аудиопоток")
    is_hls = "m3u8" in str(selected.get("protocol") or "").lower()
    stream_url = _register_online_stream(str(source_url), selected.get("http_headers") or info.get("http_headers") or {}, is_hls=is_hls)
    if not stream_url:
        raise RuntimeError("Не удалось безопасно открыть аудиопоток этого источника. Открой трек на площадке.")
    public = _public_result(info)
    artist = public.get("artist") or "Неизвестный автор"
    if artist == "Неизвестный автор" and is_youtube:
        artist = "YouTube Music" if provider == "youtube_music" else "YouTube"
    return {"stream_url": stream_url, "stream_type": "hls" if is_hls else "progressive", "duration": info.get("duration"),
            "title": str(info.get("title") or "Без названия"), "artist": str(artist),
            "thumbnail": str(public.get("thumbnail") or "")}


def resolve_online_stream(url: str, provider: str = "") -> dict:
    """Resolve once per source URL, classifying DRM only on explicit extractor reports."""
    key = str(url or "").strip()
    now = time.monotonic()
    with ONLINE_RESOLVE_GUARD:
        cached = ONLINE_RESOLVE_CACHE.get(key)
        if cached and cached[0] > now:
            if cached[1] in ("error", "drm"):
                if cached[1] == "drm":
                    raise _OnlineStreamDrmError(cached[2])
                raise RuntimeError(cached[2])
            return dict(cached[2])
        lock = ONLINE_RESOLVE_LOCKS.setdefault(key, threading.Lock())
    with lock:
        now = time.monotonic()
        with ONLINE_RESOLVE_GUARD:
            cached = ONLINE_RESOLVE_CACHE.get(key)
            if cached and cached[0] > now:
                if cached[1] in ("error", "drm"):
                    if cached[1] == "drm":
                        raise _OnlineStreamDrmError(cached[2])
                    raise RuntimeError(cached[2])
                return dict(cached[2])
        try:
            result = _resolve_online_stream_uncached(key, provider)
        except Exception as error:
            message = str(error)
            is_drm = "drm protected" in message.casefold()
            if is_drm:
                message = "SoundCloud пометил этот трек как DRM-защищённый. Встроенное воспроизведение для него недоступно."
            with ONLINE_RESOLVE_GUARD:
                ONLINE_RESOLVE_CACHE[key] = (time.monotonic() + (300 if is_drm else 20), "drm" if is_drm else "error", message)
                if len(ONLINE_RESOLVE_CACHE) > 256:
                    oldest = next(iter(ONLINE_RESOLVE_CACHE))
                    if oldest != key:
                        ONLINE_RESOLVE_CACHE.pop(oldest, None)
            if is_drm:
                raise _OnlineStreamDrmError(message) from None
            raise RuntimeError(message) from None
        with ONLINE_RESOLVE_GUARD:
            ONLINE_RESOLVE_CACHE[key] = (time.monotonic() + 45 * 60, "ok", dict(result))
            if len(ONLINE_RESOLVE_CACHE) > 256:
                oldest = next(iter(ONLINE_RESOLVE_CACHE))
                if oldest != key:
                    ONLINE_RESOLVE_CACHE.pop(oldest, None)
        return result


def _log_online_failure_once(url: str, ttl: float = 120.0) -> bool:
    """Avoid flooding the log when the UI retries one unavailable online track."""
    key, now = str(url or ""), time.monotonic()
    with ONLINE_RESOLVE_GUARD:
        for expired in [item for item, deadline in ONLINE_FAILURE_LOGGED.items() if deadline <= now]:
            ONLINE_FAILURE_LOGGED.pop(expired, None)
        if ONLINE_FAILURE_LOGGED.get(key, 0) > now:
            return False
        ONLINE_FAILURE_LOGGED[key] = now + ttl
        if len(ONLINE_FAILURE_LOGGED) > 256:
            ONLINE_FAILURE_LOGGED.pop(next(iter(ONLINE_FAILURE_LOGGED)))
        return True


DOWNLOAD_JOBS = {}
DOWNLOAD_JOBS_LOCK = threading.Lock()
CLIP_JOBS = {}
CLIP_JOBS_LOCK = threading.Lock()
ONLINE_CLIP_CACHE = {}
ONLINE_CLIP_CACHE_LOCK = threading.Lock()
DOWNLOADS_LOCK = threading.Lock()
DOWNLOADS_ENABLED = True
LAST_DOWNLOAD_COMPLETED = 0.0
DOWNLOAD_IDLE_STOP = threading.Event()
DOWNLOAD_IDLE_THREAD = None


def downloads_enabled() -> bool:
    with DOWNLOADS_LOCK:
        enabled = DOWNLOADS_ENABLED
    try:
        economy = _ACTIVE_PERFORMANCE_MODE not in DOWNLOAD_MODES
    except (NameError, AttributeError):
        economy = True
    return enabled and not economy


def set_downloads_enabled(enabled: bool) -> None:
    global DOWNLOADS_ENABLED, LAST_DOWNLOAD_COMPLETED
    with DOWNLOADS_LOCK:
        if enabled and not DOWNLOADS_ENABLED:
            LAST_DOWNLOAD_COMPLETED = 0.0
        DOWNLOADS_ENABLED = bool(enabled)


def download_block_reason() -> str:
    try:
        if _ACTIVE_PERFORMANCE_MODE not in DOWNLOAD_MODES:
            return "Онлайн-поиск и загрузка недоступны в оффлайн-режиме. Выбери «Обычный режим» или «Больше визуала», чтобы включить интернет-источники."
    except (NameError, AttributeError):
        pass
    return "Загрузчик MP3 полностью отключён в настройках"


def require_downloads_enabled() -> None:
    if not downloads_enabled():
        raise RuntimeError(download_block_reason())


def download_idle_status() -> dict:
    active = False
    with DOWNLOAD_JOBS_LOCK:
        active = any(job.get("status") not in ("finished", "failed", "cancelled") for job in DOWNLOAD_JOBS.values())
    with DOWNLOADS_LOCK:
        last_completed = LAST_DOWNLOAD_COMPLETED
    timeout = DOWNLOAD_IDLE_SHUTDOWN_SECONDS
    remaining = max(0, int(timeout - (time.time() - last_completed))) if last_completed else 0
    return {"enabled": downloads_enabled(), "active": active, "timeout": timeout,
            "seconds_left": remaining, "counting": bool(last_completed and downloads_enabled())}


def record_download_completed() -> None:
    global LAST_DOWNLOAD_COMPLETED
    completed_at = time.time()
    with DOWNLOADS_LOCK:
        LAST_DOWNLOAD_COMPLETED = completed_at
    try:
        STATE.settings.update({"download_last_completed_at": completed_at})
    except NameError:
        pass


def manage_download_idle_watchdog(enabled: bool) -> None:
    global DOWNLOAD_IDLE_THREAD
    if enabled:
        if DOWNLOAD_IDLE_THREAD is None or not DOWNLOAD_IDLE_THREAD.is_alive():
            DOWNLOAD_IDLE_STOP.clear()
            DOWNLOAD_IDLE_THREAD = threading.Thread(target=download_idle_watchdog,
                                                    name="download-idle-watchdog", daemon=True)
            DOWNLOAD_IDLE_THREAD.start()
    else:
        DOWNLOAD_IDLE_STOP.set()


def download_idle_watchdog() -> None:
    while True:
        if DOWNLOAD_IDLE_STOP.wait(3):
            if downloads_enabled():
                DOWNLOAD_IDLE_STOP.clear()
                continue
            return
        if not downloads_enabled():
            continue
        status = download_idle_status()
        if status["counting"] and status["seconds_left"] <= 0 and not status["active"]:
            STATE.settings.update({"mp3_downloader_enabled": False})
            set_downloads_enabled(False)
            manage_download_idle_watchdog(False)
            logger.info("Загрузчик MP3 отключён после %s секунд без новых загрузок", status["timeout"])

# One yt-dlp/FFmpeg pipeline at a time keeps peak memory predictable.
DOWNLOAD_SEMAPHORE = threading.Semaphore(1)
PLAYLIST_METADATA_FILE = PLAYER_DIR / "playlist_sources.json"
PLAYLIST_METADATA_LOCK = threading.Lock()


def _playlist_provider_map() -> dict:
    try:
        data = json.loads(PLAYLIST_METADATA_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(path): provider for path, provider in data.items()
                    if provider in ("youtube", "soundcloud")}
    except (OSError, ValueError):
        pass
    return {}


def _remember_playlist_provider(folder: Path, provider: str) -> None:
    if provider not in ("youtube", "soundcloud"):
        return
    key = os.path.normcase(os.path.abspath(folder))
    with PLAYLIST_METADATA_LOCK:
        data = _playlist_provider_map()
        data[key] = provider
        try:
            PLAYLIST_METADATA_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError as error:
            logger.warning("Не удалось сохранить источник плейлиста: %s", error)


class _DownloadLogger:
    """Collect extractor errors quietly; DRM errors are summarized after the job."""
    def __init__(self):
        self.drm_errors = 0
        self.errors = []

    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        message = str(message or "")
        if "drm protected" in message.lower():
            self.drm_errors += 1
        elif message:
            self.errors.append(message[:300])



def _download_job_snapshot(job_id: str) -> dict:
    with DOWNLOAD_JOBS_LOCK:
        job = DOWNLOAD_JOBS.get(job_id)
        if not job:
            raise ValueError("Загрузка не найдена")
        return {key: value for key, value in job.items() if key != "cancel_event"}


def _start_download_job(url: str, playlist: str, is_playlist: bool, destination_path: str = "",
                        provider: str = "", playlist_title: str = "", include_clip: bool = False) -> dict:
    require_downloads_enabled()
    job_id = secrets.token_urlsafe(12)
    job = {"id": job_id, "status": "preparing", "percent": 0, "downloaded": 0, "total": 0,
           "speed": 0, "eta": None, "filename": "Подготовка загрузки…", "error": "",
           "include_clip": bool(include_clip),
           "cancel_event": threading.Event()}
    with DOWNLOAD_JOBS_LOCK:
        DOWNLOAD_JOBS[job_id] = job
        for stale_id in list(DOWNLOAD_JOBS)[:-30]:
            if DOWNLOAD_JOBS[stale_id]["status"] in ("finished", "failed", "cancelled"):
                DOWNLOAD_JOBS.pop(stale_id, None)

    def progress(payload):
        if job["cancel_event"].is_set():
            ytdlp = _yt_dlp_module()
            raise ytdlp.utils.DownloadError("Загрузка отменена пользователем")
        status = payload.get("status")
        total = payload.get("total_bytes") or payload.get("total_bytes_estimate") or 0
        downloaded = payload.get("downloaded_bytes") or 0
        with DOWNLOAD_JOBS_LOCK:
            job["status"] = "downloading" if status == "downloading" else "processing"
            job["filename"] = Path(str(payload.get("filename") or "Загрузка MP3")).name[:180]
            job["downloaded"] = int(downloaded)
            job["total"] = int(total)
            job["percent"] = min(100, round(downloaded * 100 / total, 1)) if total else 0
            job["speed"] = int(payload.get("speed") or 0)
            job["eta"] = payload.get("eta")

    def worker():
        global LAST_DOWNLOAD_COMPLETED
        acquired = False
        completed_successfully = False
        try:
            require_downloads_enabled()
            with DOWNLOAD_JOBS_LOCK:
                job["status"] = "preparing"
                job["filename"] = "В очереди загрузки…"
            while not acquired:
                if job["cancel_event"].is_set():
                    raise RuntimeError("Загрузка отменена")
                acquired = DOWNLOAD_SEMAPHORE.acquire(timeout=0.5)
            with DOWNLOAD_JOBS_LOCK:
                job["status"] = "downloading"
                job["filename"] = "Проверяю ссылку…"
            result = download_remote(url, playlist, is_playlist, destination_path,
                                    provider=provider, playlist_title=playlist_title,
                                    cancel_event=job["cancel_event"], progress_callback=progress)
            if include_clip and _ACTIVE_CLIPS_ENABLED:
                clip_failures = 0
                tracks = result.get("tracks") if isinstance(result, dict) else []
                clip_errors = []
                if not tracks:
                    clip_failures = 1
                    clip_errors.append("Не найден MP3, к которому нужно привязать клип.")
                for track in tracks or []:
                    if job["cancel_event"].is_set():
                        break
                    title = str(track.get("title") or "Трек")
                    with DOWNLOAD_JOBS_LOCK:
                        job["status"] = "processing"
                        job["filename"] = ("Докачиваю клип · " + title)[:180]
                        job["percent"] = 100
                    clip_job = _start_clip_download(str(track.get("id") or ""), title,
                                                    str(track.get("artist") or ""),
                                                    cancel_event=job["cancel_event"], source_url=url)
                    while clip_job.get("id") and clip_job.get("status") not in ("finished", "failed", "cancelled"):
                        if job["cancel_event"].wait(0.5):
                            break
                        clip_job = _clip_job_snapshot(clip_job["id"])
                    if clip_job.get("status") != "finished":
                        clip_failures += 1
                        clip_errors.append(str(clip_job.get("error") or "Источник не отдал клип.")[:300])
                if job["cancel_event"].is_set():
                    raise RuntimeError("Загрузка отменена")
                result["clips_failed"] = clip_failures
                result["clip_errors"] = clip_errors
            completed_successfully = True
            with DOWNLOAD_JOBS_LOCK:
                job["status"] = "finished"
                job["percent"] = 100
                job["result"] = result
                if not job.get("filename") or job["filename"] in ("Проверяю ссылку…", "Подготовка загрузки…"):
                    job["filename"] = result.get("playlist") or "Загрузка завершена"
        except Exception as error:
            cancelled = job["cancel_event"].is_set()
            with DOWNLOAD_JOBS_LOCK:
                job["status"] = "cancelled" if cancelled else "failed"
                job["error"] = "Загрузка отменена" if cancelled else str(error)[:300]
                job["filename"] = job["error"]
            if not cancelled:
                logger.warning("Ошибка загрузки MP3: %s", error)
        finally:
            if acquired:
                DOWNLOAD_SEMAPHORE.release()
            if completed_successfully:
                record_download_completed()

    threading.Thread(target=worker, name=f"mp3-download-{job_id[:6]}", daemon=True).start()
    return _download_job_snapshot(job_id)


def _cancel_download_job(job_id: str) -> dict:
    with DOWNLOAD_JOBS_LOCK:
        job = DOWNLOAD_JOBS.get(job_id)
        if not job:
            raise ValueError("Загрузка не найдена")
        if job["status"] not in ("finished", "failed", "cancelled"):
            job["cancel_event"].set()
            job["status"] = "cancelling"
    return _download_job_snapshot(job_id)


def _clip_key(track_id: str) -> str:
    return hashlib.sha256(str(track_id or "").encode("utf-8", errors="replace")).hexdigest()


def _clip_path(track_id: str) -> Path:
    return CLIPS_DIR / f"{_clip_key(track_id)}.mp4"


def resolve_background_clip(track_id: str, title: str, artist: str, source_url: str = "") -> dict:
    """Use a saved clip first, otherwise return a short-lived online video stream."""
    track_id = str(track_id or "")[:500]
    title = str(title or "").strip()[:240]
    artist = str(artist or "").strip()[:240]
    if not track_id or not title or not _ACTIVE_CLIPS_ENABLED:
        return {"found": False, "url": ""}
    local_clip = _clip_path(track_id)
    if local_clip.is_file() and local_clip.stat().st_size:
        return {"found": True, "local": True, "url": f"/clip/{quote(track_id, safe='')}"}
    if not STATE.settings.get().get("clips_online_enabled", False):
        return {"found": False, "url": "", "online_disabled": True}
    if _ACTIVE_PERFORMANCE_MODE not in ONLINE_MODES:
        return {"found": False, "url": "", "offline": True}

    source_url = _youtube_clip_source(track_id, source_url)
    quality = int(STATE.settings.get().get("clip_video_quality", 1080))
    cache_key = (track_id, title.casefold(), artist.casefold(), source_url, quality)
    now = time.monotonic()
    with ONLINE_CLIP_CACHE_LOCK:
        cached = ONLINE_CLIP_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return dict(cached[1])
        if cached:
            ONLINE_CLIP_CACHE.pop(cache_key, None)

    query = " ".join(part for part in (artist, title, "official music video") if part)
    result = {"found": False, "url": ""}
    ttl = 120.0
    try:
        video_url, format_id, stream, headers = _find_accessible_youtube_video(query, max_height=quality, background=True, source_url=source_url, artist=artist)
        if video_url and format_id:
            proxy_url = _register_online_stream(stream, headers)
            if proxy_url:
                result = {"found": True, "local": False, "url": proxy_url}
                ttl = 12 * 60
    except Exception as error:
        logger.info("Не удалось подобрать онлайн-клип для %s: %s", _clip_key(track_id)[:12], type(error).__name__)

    with ONLINE_CLIP_CACHE_LOCK:
        ONLINE_CLIP_CACHE[cache_key] = (time.monotonic() + ttl, dict(result))
        while len(ONLINE_CLIP_CACHE) > 300:
            ONLINE_CLIP_CACHE.pop(next(iter(ONLINE_CLIP_CACHE)))
    return result


def _clip_job_snapshot(job_id: str) -> dict:
    with CLIP_JOBS_LOCK:
        job = CLIP_JOBS.get(job_id)
        if not job:
            raise ValueError("Загрузка клипа не найдена")
        return {key: value for key, value in job.items() if key != "track_id"}


def _start_clip_download(track_id: str, title: str, artist: str, cancel_event=None, source_url: str = "") -> dict:
    _require_online_search_enabled()
    if not _ACTIVE_CLIPS_ENABLED:
        raise RuntimeError("Загрузка клипов выключена в настройках «Режимы».")
    track_id = str(track_id or "")[:500]
    title = str(title or "").strip()[:240]
    artist = str(artist or "").strip()[:240]
    if not track_id or not title:
        raise ValueError("У трека нет названия для поиска клипа")
    existing = _clip_path(track_id)
    if existing.is_file() and existing.stat().st_size:
        return {"id": "", "status": "finished", "title": title, "already_downloaded": True}

    job_id = secrets.token_urlsafe(12)
    job = {"id": job_id, "track_id": track_id, "title": title, "status": "preparing", "error": ""}
    with CLIP_JOBS_LOCK:
        CLIP_JOBS[job_id] = job
        for stale_id in list(CLIP_JOBS)[:-40]:
            if CLIP_JOBS[stale_id]["status"] in ("finished", "failed", "cancelled"):
                CLIP_JOBS.pop(stale_id, None)

    def worker():
        target = _clip_path(track_id)
        try:
            _require_online_search_enabled()
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("Загрузка отменена")
            CLIPS_DIR.mkdir(parents=True, exist_ok=True)
            ytdlp = _yt_dlp_module(allow_online_search=True)
            logger_for_ytdlp = _DownloadLogger()
            query = " ".join(part for part in (artist, title, "official music video") if part)
            quality = int(STATE.settings.get().get("clip_video_quality", 1080))
            video_url, format_id, _stream_url, _stream_headers = _find_accessible_youtube_video(query, max_height=quality, source_url=_youtube_clip_source(track_id, source_url), artist=artist)
            if not video_url or not format_id:
                raise RuntimeError("В исходном треке нет доступного видео. Для треков с обложкой поиск отдельного клипа отключён.")
            options = {
                # Download the exact signed stream that passed the probe. Re-extracting
                # the watch page can select a different client and return a 403 URL.
                "format": "best",
                "http_headers": _stream_headers,
                "merge_output_format": "mp4",
                "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
                "outtmpl": str(CLIPS_DIR / f"{_clip_key(track_id)}.%(ext)s"),
                "quiet": True, "no_warnings": True, "logger": logger_for_ytdlp,
                "noplaylist": True, "socket_timeout": 30, "retries": 2,
                "fragment_retries": 2, "concurrent_fragment_downloads": 2,
            }
            options["ffmpeg_location"] = str(Path(_find_ffmpeg_binary()).parent)

            def progress(payload):
                if cancel_event and cancel_event.is_set():
                    raise ytdlp.utils.DownloadError("Загрузка отменена")
                status = payload.get("status")
                if status in ("downloading", "finished"):
                    with CLIP_JOBS_LOCK:
                        if job_id in CLIP_JOBS:
                            CLIP_JOBS[job_id]["status"] = "downloading" if status == "downloading" else "processing"
                            total = payload.get("total_bytes") or payload.get("total_bytes_estimate") or 0
                            downloaded = payload.get("downloaded_bytes") or 0
                            CLIP_JOBS[job_id]["percent"] = min(100, round(downloaded * 100 / total)) if total else 0

            options["progress_hooks"] = [progress]
            with ytdlp.YoutubeDL(options) as ydl:
                result_code = ydl.download([_stream_url])
            if not target.is_file() or not target.stat().st_size:
                # yt-dlp may add a source ID to an output name for some extractors;
                # normalize the first completed MP4 to our opaque track key.
                candidates = sorted(CLIPS_DIR.glob(f"{_clip_key(track_id)}*.mp4"), key=lambda item: item.stat().st_mtime, reverse=True)
                if candidates:
                    candidates[0].replace(target)
            if result_code or not target.is_file() or not target.stat().st_size:
                detail = logger_for_ytdlp.errors[-1] if logger_for_ytdlp.errors else "Видео не найдено или источник его не отдал."
                raise RuntimeError(detail[:300])
            with CLIP_JOBS_LOCK:
                if job_id in CLIP_JOBS:
                    CLIP_JOBS[job_id].update(status="finished", percent=100)
        except Exception as error:
            cancelled = bool(cancel_event and cancel_event.is_set())
            with CLIP_JOBS_LOCK:
                if job_id in CLIP_JOBS:
                    CLIP_JOBS[job_id].update(status="cancelled" if cancelled else "failed",
                                             error="Загрузка отменена" if cancelled else str(error)[:300])
            if not cancelled:
                logger.info("Не удалось загрузить клип для трека %s: %s", _clip_key(track_id)[:12], error)

    threading.Thread(target=worker, name=f"clip-download-{job_id[:6]}", daemon=True).start()
    return _clip_job_snapshot(job_id)

def inspect_remote_url(url: str) -> dict:
    require_downloads_enabled()
    url = str(url or "").strip()[:2000]
    external_provider = _external_playlist_provider(url)
    if external_provider:
        info = _load_external_playlist(url)
        return {"is_playlist": True, "title": str(info.get("title") or "Плейлист"),
                "provider": external_provider, "track_count": int(info.get("track_count") or len(info.get("tracks") or [])),
                "tracks": [], "track": None, "url": url}
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or not any(parsed.hostname == h or parsed.hostname.endswith("." + h) for h in ("youtube.com", "youtu.be", "soundcloud.com")):
        raise ValueError("Вставь ссылку YouTube или SoundCloud")
    # A YouTube watch URL can carry a radio/autoplay list. The video id means one track;
    # only /playlist?list=... without a video id means a playlist download.
    is_youtube = parsed.hostname == "youtu.be" or parsed.hostname.endswith("youtube.com")
    params = parse_qs(parsed.query)
    video_id = params.get("v", [""])[0]
    if parsed.hostname == "youtu.be" and parsed.path.strip("/"):
        video_id = parsed.path.strip("/").split("/")[0]
    explicit_playlist = is_youtube and not video_id and parsed.path.rstrip("/").endswith("playlist") and bool(params.get("list"))
    if is_youtube and video_id:
        url = "https://www.youtube.com/watch?v=" + video_id
    ytdlp = _yt_dlp_module()
    with ytdlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": "in_playlist", "playlistend": 1, "socket_timeout": 20, "noplaylist": is_youtube and not explicit_playlist}) as ydl:
        data = ydl.extract_info(url, download=False) or {}
    entries = data.get("entries") if (explicit_playlist or not is_youtube) and data.get("_type") in ("playlist", "multi_video") else None
    is_playlist = entries is not None
    extractor = str(data.get("extractor_key") or data.get("extractor") or "").lower()
    provider = "youtube" if is_youtube or "youtube" in extractor else "soundcloud" if "soundcloud" in parsed.hostname.lower() or "soundcloud" in extractor else ""
    return {"is_playlist": is_playlist, "title": str(data.get("title") or "Загрузка"), "provider": provider,
            "track_count": int(data.get("playlist_count") or data.get("n_entries") or len(entries or [])) if is_playlist else 0,
            "tracks": [], "track": _public_result(data) if not is_playlist else None, "url": url}


def _download_external_playlist(url: str, destination_path: str = "", *,
                                cancel_event=None, progress_callback=None) -> dict:
    """Download a public Spotify/Apple/Yandex list by matching its tracks to playable audio."""
    require_downloads_enabled()
    playlist = _load_external_playlist(url)
    tracks = playlist.get("tracks") if isinstance(playlist.get("tracks"), list) else []
    if not tracks:
        raise ValueError("В плейлисте нет песен для загрузки")
    settings = STATE.settings.get()
    root = Path(destination_path or settings.get("download_path") or MUSIC_DIR).expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise ValueError("Указанный путь существует, но это не папка")
    folder_name = re.sub(r'[\\/:*?"<>|]+', "_", str(playlist.get("title") or "Плейлист")).strip(" .")[:80] or "Плейлист"
    destination = root / folder_name
    destination.mkdir(parents=True, exist_ok=True)
    total = len(tracks)
    downloaded, skipped, failures = 0, 0, []
    for index, item in enumerate(tracks):
        require_downloads_enabled()
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("Загрузка отменена")
        title = str(item.get("title") or "Без названия")
        if progress_callback:
            progress_callback({"status": "downloading", "filename": f"{index + 1}/{total} · {title}",
                               "downloaded_bytes": index * 10000, "total_bytes": total * 10000,
                               "speed": 0, "eta": total - index})
        try:
            matched = _resolve_imported_playlist_track(title, str(item.get("artist") or ""), item.get("duration"))
            def report_track_progress(payload, item_index=index, item_title=title):
                if not progress_callback:
                    return
                try:
                    size = max(0, int(payload.get("total_bytes") or payload.get("total_bytes_estimate") or 0))
                    done = max(0, int(payload.get("downloaded_bytes") or 0))
                    fraction = min(1.0, done / size) if size else 0.0
                except (TypeError, ValueError):
                    fraction = 0.0
                progress_callback({"status": "downloading", "filename": f"{item_index + 1}/{total} · {item_title}",
                    "downloaded_bytes": int((item_index + fraction) * 10000), "total_bytes": total * 10000,
                    "speed": int(payload.get("speed") or 0), "eta": payload.get("eta")})
            result = download_remote(str(matched.get("url") or ""), folder_name, False,
                provider=str(matched.get("provider") or "youtube_music"),
                cancel_event=cancel_event, progress_callback=report_track_progress,
                target_directory=destination)
            completed = int(result.get("downloaded") or 0)
            if completed:
                downloaded += completed
            elif any(f"[{str(matched.get('id') or '')}]" in path.name for path in destination.glob("*.mp3")):
                downloaded += 1
            else:
                skipped += 1
        except Exception as error:
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("Загрузка отменена") from error
            skipped += 1
            if len(failures) < 3:
                failures.append(f"{title}: {str(error)[:100]}")
        if progress_callback:
            progress_callback({"status": "downloading", "filename": f"{index + 1}/{total} · {title}",
                "downloaded_bytes": (index + 1) * 10000, "total_bytes": total * 10000,
                "speed": 0, "eta": total - index - 1})
    if not downloaded:
        detail = " · " + "; ".join(failures) if failures else ""
        raise RuntimeError("Не удалось скачать ни одну песню из плейлиста." + detail)
    current = STATE.settings.get()
    covered = False
    for source in resolve_sources(current):
        if not source.get("enabled") or not source.get("path"):
            continue
        try:
            root.relative_to(Path(source["path"]).expanduser().resolve())
            covered = True
            break
        except (ValueError, OSError):
            continue
    if not covered:
        STATE.settings.update({"sources": current["sources"] + [{"path": str(root), "enabled": True}]})
    STATE.library.scan_async()
    return {"ok": True, "playlist": folder_name, "folder": str(destination),
            "downloaded": downloaded, "skipped": skipped, "skipped_drm": 0}

def download_remote(url: str, playlist: str, is_playlist: bool = False, destination_path: str = "",
                    provider: str = "", playlist_title: str = "",
                    cancel_event=None, progress_callback=None, target_directory: Path | None = None) -> dict:
    require_downloads_enabled()
    url = str(url or "").strip()[:2000]
    if is_playlist and _external_playlist_provider(url):
        return _download_external_playlist(url, destination_path, cancel_event=cancel_event,
                                           progress_callback=progress_callback)
    # Re-check and classify on the server: UI data is untrusted, and this prevents
    # a watch URL carrying a radio list from being treated as a playlist.
    info = inspect_remote_url(url)
    require_downloads_enabled()
    if cancel_event and cancel_event.is_set():
        raise RuntimeError("Загрузка отменена")
    is_playlist = info["is_playlist"]
    url = info["url"]
    clean = re.sub(r'[\\/:*?"<>|]+', "_", str(playlist or "Загрузки")).strip(" .")[:80] or "Загрузки"
    custom_destination = Path(destination_path).expanduser().resolve() if destination_path else None
    if target_directory is not None:
        custom_destination = None
    if custom_destination is not None and custom_destination.exists() and not custom_destination.is_dir():
        raise ValueError("Указанный путь существует, но это не папка")
    download_root = custom_destination or Path(STATE.settings.get().get("download_path") or MUSIC_DIR).expanduser().resolve()
    destination = download_root / clean if is_playlist else (Path(target_directory).expanduser().resolve() if target_directory is not None else custom_destination or (MUSIC_DIR / clean))
    if is_playlist:
        clean = re.sub(r'[\\/:*?"<>|]+', "_", info["title"]).strip(" .")[:80] or "Плейлист"
        destination = download_root / clean
        custom_destination = download_root if download_root != MUSIC_DIR else None
    destination.mkdir(parents=True, exist_ok=True)
    if is_playlist:
        _remember_playlist_provider(destination, info.get("provider", ""))
    ytdlp = _yt_dlp_module()
    ydl_logger = _DownloadLogger()
    drm_source = "SoundCloud" if info.get("provider") == "soundcloud" else "источник"
    drm_message = f"{drm_source} пометил этот трек как DRM-защищённый. Плеер не может скачать его."
    options = {"format": "bestaudio/best", "outtmpl": str(destination / "%(title).180B [%(id)s].%(ext)s"),
               "quiet": True, "no_warnings": True, "logger": ydl_logger,
               "noplaylist": not is_playlist, "ignoreerrors": True,
               "concurrent_fragment_downloads": 1, "buffersize": 4096,
               "socket_timeout": 30, "writethumbnail": True, "keepthumbnail": True, "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"},
               {"key": "FFmpegMetadata", "add_metadata": True}, {"key": "EmbedThumbnail"}],
               "postprocessor_args": {"EmbedThumbnail": ["-id3v2_version", "3"]}}
    if progress_callback:
        options["progress_hooks"] = [progress_callback]
    # PyInstaller build bundles a matching ffmpeg.exe + ffprobe.exe pair here.
    # imageio-ffmpeg only ships ffmpeg, so it cannot satisfy yt-dlp's MP3 postprocessors alone.
    ffmpeg_dirs = [RESOURCE_DIR / "ffmpeg" / "bin", RESOURCE_DIR / "ffmpeg"]
    if not FROZEN:
        # The rewritten folder sits beside the existing install; reuse its FFmpeg pair
        # when present instead of copying a second 200+ MB runtime into this folder.
        ffmpeg_dirs.append(Path(__file__).resolve().parent.parent / "ffmpeg" / "bin")
    if FROZEN:
        ffmpeg_dirs += [Path(sys.executable).resolve().parent / "ffmpeg" / "bin"]
    ffmpeg_pair = next((folder for folder in ffmpeg_dirs
                        if (folder / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")).is_file()
                        and (folder / ("ffprobe.exe" if os.name == "nt" else "ffprobe")).is_file()), None)
    if ffmpeg_pair:
        options["ffmpeg_location"] = str(ffmpeg_pair)
    else:
        try:
            import imageio_ffmpeg
            bundled_ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe())
            if bundled_ffmpeg.is_file():
                # Works for ffmpeg, but provide a useful warning because ffprobe may be absent.
                options["ffmpeg_location"] = str(bundled_ffmpeg)
        except (ImportError, OSError, RuntimeError):
            pass
    before = {path.name for path in destination.glob("*.mp3")}
    require_downloads_enabled()
    if cancel_event and cancel_event.is_set():
        raise RuntimeError("Загрузка отменена")
    try:
        from yt_dlp.postprocessor.common import PostProcessor

        class HighestResolutionThumbnailPP(PostProcessor):
            def run(self, info):
                thumbnails = info.get("thumbnails")
                if isinstance(thumbnails, list):
                    def resolution(item):
                        if not isinstance(item, dict):
                            return 0, 0, 0
                        try:
                            width, height = int(item.get("width") or 0), int(item.get("height") or 0)
                        except (TypeError, ValueError, OverflowError):
                            width = height = 0
                        return width * height, width, height
                    info["thumbnails"] = sorted(thumbnails, key=resolution)
                return [], info

        with ytdlp.YoutubeDL(options) as ydl:
            ydl.add_post_processor(HighestResolutionThumbnailPP(ydl), when="pre_process")
            code = ydl.download([url])
    except Exception as error:
        if ydl_logger.drm_errors or "drm protected" in str(error).lower():
            raise RuntimeError(drm_message) from error
        raise
    created = [path for path in destination.glob("*.mp3") if path.name not in before]
    completed_files = list(created)
    if not is_playlist and not completed_files:
        source_id = str((info.get("track") or {}).get("id") or "")
        if source_id:
            completed_files = [path for path in destination.glob("*.mp3")
                               if path.stem.endswith(f"[{source_id}]")]
    downloaded_count = len(created)
    if not created and ydl_logger.drm_errors:
        raise RuntimeError(drm_message)
    if code and not created:
        detail = ydl_logger.errors[-1] if ydl_logger.errors else "Проверь ссылку и доступность источника."
        raise RuntimeError("Не удалось скачать аудио. " + detail)
    if ydl_logger.drm_errors:
        logger.warning("%s: пропущено DRM-защищённых треков: %s", drm_source, ydl_logger.drm_errors)
    if custom_destination is not None and created:
        current = STATE.settings.get()
        destination_abs = os.path.normcase(os.path.abspath(custom_destination))
        covered_by_source = False
        for source in current["sources"]:
            if not source.get("enabled", True) or not source.get("path"):
                continue
            source_abs = os.path.normcase(os.path.abspath(source["path"]))
            try:
                if os.path.commonpath((destination_abs, source_abs)) == source_abs:
                    covered_by_source = True
                    break
            except ValueError:
                continue
        if not covered_by_source:
            STATE.settings.update({"sources": current["sources"] + [{"path": str(custom_destination), "enabled": True}]})
    if is_playlist and created:
        STATE.library.scan_async()
    elif not is_playlist and created:
        STATE.library.add_downloaded_files(created, destination, info.get("provider", ""))
    tracks = []
    if not is_playlist:
        for path in completed_files:
            tag_title, tag_artist, _album, _duration, _embedded = read_tags(path)
            _number, filename_artist, filename_title = parse_filename(path.stem)
            tracks.append({"id": track_id_for(str(path)),
                           "title": tag_title or filename_title or path.stem,
                           "artist": tag_artist or filename_artist or ""})
    return {"ok": True, "playlist": clean, "folder": str(destination), "downloaded": downloaded_count,
            "skipped_drm": ydl_logger.drm_errors, "tracks": tracks}


# ======================================================================================
#  Библиотека треков
# ======================================================================================
_NUM_PREFIX_RE = re.compile(r"^\s*(\d{1,4}(?:_\d{1,4})*)\s*[-._)]*\s*(?=\S)")


def parse_filename(stem: str):
    """«01 - Исполнитель - Название» → (номер, исполнитель, название)."""
    number = None
    match = _NUM_PREFIX_RE.match(stem)
    if match:
        first_number = re.match(r"\d+", match.group(1))
        number = int(first_number.group(0)) if first_number else None
        stem = stem[match.end():]
    stem = stem.strip()
    for separator in (" - ", " – ", " — "):
        if separator in stem:
            artist, title = stem.split(separator, 1)
            if artist.strip() and title.strip():
                return number, artist.strip(), title.strip()
    return number, "", stem or "Без названия"


def _tag_text(tags, *keys):
    for key in keys:
        try:
            value = tags.get(key)
        except Exception:
            continue
        if value is None:
            continue
        if hasattr(value, "text"):
            value = value.text
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if value:
            return str(value).strip()
    return ""


def read_tags(path: Path):
    """(title, artist, album, duration, есть_обложка) — пусто, если mutagen не установлен."""
    try:
        import mutagen
    except ImportError:
        return "", "", "", None, False
    try:
        audio = mutagen.File(str(path))
    except Exception:
        return "", "", "", None, False
    if audio is None:
        return "", "", "", None, False
    duration = getattr(getattr(audio, "info", None), "length", None)
    tags = getattr(audio, "tags", None)
    title = artist = album = ""
    has_cover = bool(getattr(audio, "pictures", None))
    if tags is not None:
        title = _tag_text(tags, "TIT2", "\xa9nam", "title", "TITLE")
        artist = _tag_text(tags, "TPE1", "\xa9ART", "artist", "ARTIST")
        album = _tag_text(tags, "TALB", "\xa9alb", "album", "ALBUM")
        try:
            keys = list(tags.keys())
        except Exception:
            keys = []
        has_cover = has_cover or any(str(k).startswith("APIC") for k in keys) or "covr" in keys
    return title, artist, album, (float(duration) if duration else None), has_cover


def read_embedded_cover(path: Path):
    try:
        import mutagen
        audio = mutagen.File(str(path))
    except Exception:
        return None
    if audio is None:
        return None
    pictures = getattr(audio, "pictures", None)
    if pictures:
        return pictures[0].data, pictures[0].mime or "image/jpeg"
    tags = getattr(audio, "tags", None)
    if tags is None:
        return None
    try:
        for key in list(tags.keys()):
            if str(key).startswith("APIC"):
                frame = tags[key]
                return frame.data, frame.mime or "image/jpeg"
        if "covr" in tags and tags["covr"]:
            cover = tags["covr"][0]
            return bytes(cover), ("image/png" if getattr(cover, "imageformat", None) == 14 else "image/jpeg")
    except Exception:
        return None
    return None


def track_id_for(path: str) -> str:
    return hashlib.sha1(os.path.normcase(path).encode("utf-8")).hexdigest()[:16]


class Library:
    def __init__(self, settings: SettingsStore):
        self.settings = settings
        self.lock = threading.RLock()
        self.tracks: dict[str, dict] = {}
        self.playlists: list[dict] = []
        self.source_counts: dict[str, int] = {}
        self.version = 0
        self.scanning = False
        self._cache = self._load_cache()
        self._scan_requested = threading.Event()
        self._watch_stop = threading.Event()
        self._watch_thread = None

    @staticmethod
    def _load_cache():
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def save_cache(self):
        with self.lock:
            temporary = CACHE_FILE.with_name(CACHE_FILE.name + ".tmp")
            try:
                temporary.write_text(json.dumps(self._cache, ensure_ascii=False), encoding="utf-8")
                os.replace(temporary, CACHE_FILE)
            except OSError as error:
                logger.warning("Не смог сохранить кэш библиотеки: %s", error)

    def load_cached_snapshot(self):
        """Build the visible library from metadata cache without touching music files."""
        settings = self.settings.get()
        sources = []
        for item in settings.get("sources", []):
            sid = item.get("id")
            if not item.get("enabled", True):
                continue
            if sid == "music":
                sources.append((sid, "music", os.path.normcase(os.path.abspath(str(MUSIC_DIR)))))
            elif sid == "bot":
                folder = str(settings.get("bot_folder") or "").strip()
                if folder:
                    candidate = Path(folder)
                    if candidate.name.lower() == "soundcloud":
                        root = candidate
                    elif (candidate / "downloads" / "SoundCloud").as_posix().lower().endswith("/downloads/soundcloud"):
                        root = candidate / "downloads" / "SoundCloud"
                    else:
                        root = candidate / "downloads" / "SoundCloud"
                    sources.append((sid, "bot", os.path.normcase(os.path.abspath(str(root)))))
            elif item.get("path"):
                path = os.path.normcase(os.path.abspath(item["path"]))
                sources.append((sid, Path(item["path"]).name or path, path))

        tracks = {}
        counts = {sid: 0 for sid, _, _ in sources}
        for full, cached in self._cache.items():
            if not isinstance(cached, list) or len(cached) < 7:
                continue
            norm = os.path.normcase(os.path.abspath(full))
            match = next(((sid, label, root) for sid, label, root in sources
                          if norm.startswith(root.rstrip(os.sep) + os.sep)), None)
            if match is None:
                # Auto-discovered bot folders are deliberately not probed in economy mode.
                lowered = norm.replace("\\", "/").lower()
                if "/downloads/soundcloud/" in lowered:
                    match = ("bot", "bot", norm[:lowered.index("/downloads/soundcloud/")] + os.sep + "downloads" + os.sep + "SoundCloud")
            if match is None:
                continue
            sid, label, root = match
            try:
                relative = os.path.relpath(os.path.dirname(full), root)
            except ValueError:
                continue
            relative_path = Path(relative)
            playlist_name = " / ".join(relative_path.parts) if relative_path.parts else label
            playlist_key = f"{sid}:{relative_path.as_posix()}"
            _, _, tag_title, tag_artist, album, duration, embedded = cached[:7]
            filename = Path(full).name
            number, name_artist, name_title = parse_filename(Path(filename).stem)
            if tag_artist.strip().isdigit() and name_artist:
                tag_artist = ""
            tid = track_id_for(full)
            tracks[tid] = {"id": tid, "title": tag_title or name_title, "artist": tag_artist or name_artist,
                           "album": album, "pl": playlist_key, "src": sid, "n": number, "dur": duration,
                           "mt": int(cached[0]), "cover": bool(embedded), "path": full, "playlist": playlist_name}
            counts[sid] = counts.get(sid, 0) + 1
        grouped = {}
        for track in tracks.values():
            item = grouped.setdefault(track["pl"], {"key": track["pl"], "name": track["playlist"],
                                                      "src": track["src"], "provider": "", "count": 0})
            item["count"] += 1
        with self.lock:
            self.tracks = tracks
            self.playlists = sorted(grouped.values(), key=lambda item: item["name"].lower())
            self.source_counts = counts
            self.version += 1
        logger.info("Экономный режим: библиотека показана из кэша (%s треков, без проверки файлов)", len(tracks))

    def scan_async(self, force=False):
        if not force and _ACTIVE_PERFORMANCE_MODE in ECO_MODES:
            logger.info("Экономный режим: автоматическое сканирование пропущено")
            return
        with self.lock:
            if self.scanning:
                self._scan_requested.set()  # после текущего скана сразу запустим ещё один
                return
            self.scanning = True
        threading.Thread(target=self._scan_loop, name="library-scan", daemon=True).start()

    def stop_watching(self):
        """Stop the library watcher when the player exits, including from the tray."""
        self._watch_stop.set()
        thread = self._watch_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.5)

    def start_watching(self):
        """Starts one watcher manager; it does no filesystem work in economy mode."""
        if self._watch_thread is not None and self._watch_thread.is_alive():
            return
        self._watch_stop.clear()
        self._watch_thread = threading.Thread(target=self._watch_loop, name="library-watch", daemon=True)
        self._watch_thread.start()

    def _watch_loop(self):
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            logger.warning("watchdog не установлен; включён резервный поиск новых треков раз в 30 секунд")
            self._poll_watch_loop()
            return

        changed = threading.Event()

        class TrackChangeHandler(FileSystemEventHandler):
            def on_any_event(self, event):
                if event.event_type not in {"created", "modified", "deleted", "moved"}:
                    return
                paths = [getattr(event, "src_path", ""), getattr(event, "dest_path", "")]
                relevant_file = any(
                    path and Path(path).suffix.lower() in AUDIO_EXTENSIONS | IMAGE_EXTENSIONS
                    for path in paths
                )
                relevant_directory = bool(event.is_directory) and event.event_type in {"created", "deleted", "moved"}
                if relevant_file or relevant_directory:
                    changed.set()

        observer = None
        watched = None
        try:
            while not self._watch_stop.is_set():
                if _ACTIVE_PERFORMANCE_MODE not in ("online", "offline"):
                    if observer is not None:
                        observer.stop()
                        observer.join(timeout=2)
                        observer = None
                    watched = None
                    self._watch_stop.wait(3)
                    continue
                paths = tuple(sorted({
                    os.path.normcase(os.path.abspath(source["path"]))
                    for source in resolve_sources(self.settings.get())
                    if source["enabled"] and source.get("path") and Path(source["path"]).is_dir()
                }))
                if paths != watched:
                    if observer is not None:
                        observer.stop()
                        observer.join(timeout=2)
                    observer = Observer()
                    handler = TrackChangeHandler()
                    for path in paths:
                        observer.schedule(handler, path, recursive=True)
                    if paths:
                        observer.start()
                        logger.info("Автообновление библиотеки включено: слежу за %s папками", len(paths))
                    watched = paths
                if changed.wait(5.0):
                    # Загрузчики создают MP3, метаданные и обложку серией событий.
                    # Подождать завершения пачки, чтобы не сканировать временный файл.
                    while not self._watch_stop.is_set():
                        changed.clear()
                        if self._watch_stop.wait(0.8) or not changed.is_set():
                            break
                    if not self._watch_stop.is_set():
                        self.scan_async()
        except Exception:
            logger.exception("Наблюдатель библиотеки завершился с ошибкой")
            self._poll_watch_loop()
        finally:
            if observer is not None:
                observer.stop()
                observer.join(timeout=2)

    def _poll_watch_loop(self):
        """Резервное слежение для среды без watchdog."""
        previous = None
        # Only used in standard mode when filesystem notifications are unavailable.
        while not self._watch_stop.wait(30):
            if _ACTIVE_PERFORMANCE_MODE not in ("online", "offline"):
                continue
            fingerprint = []
            sources = [s for s in resolve_sources(self.settings.get()) if s["enabled"]]
            for source in sources:
                root = Path(source["path"]) if source.get("path") else None
                if not root or not root.is_dir():
                    continue
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames.sort()
                    for name in sorted(filenames, key=str.lower):
                        path = Path(dirpath) / name
                        if path.suffix.lower() not in AUDIO_EXTENSIONS | IMAGE_EXTENSIONS:
                            continue
                        try:
                            stat = path.stat()
                            fingerprint.append((os.path.normcase(str(path)), stat.st_mtime_ns, stat.st_size))
                        except OSError:
                            continue
            current = tuple(sorted(fingerprint))
            if previous is not None and current != previous:
                self.scan_async()
            previous = current

    def _scan_loop(self):
        try:
            while True:
                self._scan_requested.clear()
                self._scan()
                if not self._scan_requested.is_set():
                    break
        except Exception:
            logger.exception("Ошибка сканирования библиотеки")
        finally:
            with self.lock:
                self.scanning = False

    def _scan(self):
        started = time.monotonic()
        with self.lock:
            previous_track_ids = set(self.tracks)
        sources = [s for s in resolve_sources(self.settings.get()) if s["enabled"]]
        tracks: dict[str, dict] = {}
        counts: dict[str, int] = {}
        new_cache = {}
        for source in sources:
            counts[source["id"]] = 0
            if not source["path"]:
                continue
            root = Path(source["path"])
            if not root.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames.sort()
                folder_has_cover = any(
                    Path(name).stem.lower() in COVER_NAMES and Path(name).suffix.lower() in IMAGE_EXTENSIONS
                    for name in filenames
                )
                relative = Path(dirpath).relative_to(root)
                playlist_name = " / ".join(relative.parts) if relative.parts else source["label"]
                playlist_key = f"{source['id']}:{relative.as_posix()}"
                for name in sorted(filenames, key=str.lower):
                    extension = Path(name).suffix.lower()
                    if extension not in AUDIO_EXTENSIONS:
                        continue
                    full = os.path.join(dirpath, name)
                    try:
                        stat = os.stat(full)
                    except OSError:
                        continue
                    cached = self._cache.get(full)
                    if cached and cached[0] == int(stat.st_mtime) and cached[1] == stat.st_size:
                        tag_title, tag_artist, album, duration, embedded = cached[2:7]
                    else:
                        tag_title, tag_artist, album, duration, embedded = read_tags(Path(full))
                    new_cache[full] = [int(stat.st_mtime), stat.st_size, tag_title, tag_artist, album, duration, embedded]
                    number, name_artist, name_title = parse_filename(Path(name).stem)
                    if tag_artist.strip().isdigit() and name_artist:
                        tag_artist = ""
                    tid = track_id_for(full)
                    tracks[tid] = {
                        "id": tid,
                        "title": tag_title or name_title,
                        "artist": tag_artist or name_artist,
                        "album": album,
                        "pl": playlist_key,
                        "src": source["id"],
                        "n": number,
                        "dur": duration,
                        "mt": int(stat.st_mtime),
                        "cover": bool(embedded or folder_has_cover),
                        "path": full,
                        "playlist": playlist_name,
                    }
                    counts[source["id"]] += 1

        grouped: dict[str, dict] = {}
        provider_map = _playlist_provider_map()
        inferred_votes: dict[str, dict[str, int]] = {}
        for track in tracks.values():
            folder = os.path.normcase(os.path.abspath(os.path.dirname(track["path"])))
            provider = provider_map.get(folder, "")
            if not provider:
                match = re.search(r"\[([A-Za-z0-9_-]{6,16})\](?=\.[^.]+$)", Path(track["path"]).name)
                if match:
                    track_id = match.group(1)
                    guessed = "youtube" if len(track_id) == 11 else "soundcloud" if track_id.isdigit() else ""
                    if guessed:
                        votes = inferred_votes.setdefault(track["pl"], {"youtube": 0, "soundcloud": 0})
                        votes[guessed] += 1
            entry = grouped.setdefault(track["pl"], {"key": track["pl"], "name": track["playlist"],
                                                      "src": track["src"], "provider": provider, "count": 0})
            entry["count"] += 1
            if not entry.get("provider") and provider:
                entry["provider"] = provider
        for key, entry in grouped.items():
            if not entry.get("provider"):
                votes = inferred_votes.get(key, {})
                guess, count = max(votes.items(), key=lambda item: item[1], default=("", 0))
                if count >= 1 and count / max(1, entry["count"]) >= .75:
                    entry["provider"] = guess
        playlists = sorted(grouped.values(), key=lambda p: p["name"].lower())
        now_epoch = time.time()
        newly_added_downloads = any(track_id not in previous_track_ids and track.get("mt", 0) >= now_epoch - 300
                                    for track_id, track in tracks.items())
        with self.lock:
            self.tracks = tracks
            self.playlists = playlists
            self.source_counts = counts
            self.version += 1
        if new_cache != self._cache:
            self._cache = new_cache
            self.save_cache()
        if newly_added_downloads:
            record_download_completed()
        logger.info("Библиотека: %s треков, %.1f с", len(tracks), time.monotonic() - started)

    def add_downloaded_files(self, files: list[Path], folder: Path, provider: str = "") -> None:
        """Adds completed single-track downloads without a full library walk."""
        folder = Path(folder)
        sources = [source for source in resolve_sources(self.settings.get())
                   if source["enabled"] and source.get("path")]
        source = next((item for item in sources
                       if os.path.normcase(os.path.abspath(item["path"])) ==
                       os.path.normcase(os.path.abspath(folder))), None)
        if source is None:
            # A new user-selected folder may need to be registered in the library.
            self.scan_async()
            return
        added = []
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            tag_title, tag_artist, album, duration, embedded = read_tags(path)
            number, name_artist, name_title = parse_filename(path.stem)
            tid = track_id_for(str(path))
            relative = path.parent.relative_to(Path(source["path"]))
            playlist_name = " / ".join(relative.parts) if relative.parts else source["label"]
            playlist_key = f"{source['id']}:{relative.as_posix()}"
            track = {"id": tid, "title": tag_title or name_title, "artist": tag_artist or name_artist,
                     "album": album, "pl": playlist_key, "src": source["id"], "n": number,
                     "dur": duration, "mt": int(stat.st_mtime), "cover": bool(embedded),
                     "path": str(path), "playlist": playlist_name}
            added.append((track, stat, tag_title, tag_artist, album, duration, embedded))
        if not added:
            return
        with self.lock:
            for track, _, _, _, _, _, _ in added:
                self.tracks[track["id"]] = track
            grouped = {}
            provider_map = _playlist_provider_map()
            if provider in ("youtube", "soundcloud"):
                provider_map[os.path.normcase(os.path.abspath(folder))] = provider
                try:
                    PLAYLIST_METADATA_FILE.write_text(json.dumps(provider_map, ensure_ascii=False), encoding="utf-8")
                except OSError as error:
                    logger.warning("Не удалось сохранить источник плейлиста: %s", error)
            for track in self.tracks.values():
                track_folder = os.path.normcase(os.path.abspath(os.path.dirname(track["path"])))
                item = grouped.setdefault(track["pl"], {"key": track["pl"], "name": track["playlist"],
                                                          "src": track["src"], "provider": provider_map.get(track_folder, ""), "count": 0})
                item["count"] += 1
            self.playlists = sorted(grouped.values(), key=lambda item: item["name"].lower())
            self.source_counts[source["id"]] = sum(track["src"] == source["id"] for track in self.tracks.values())
            self.version += 1
        for track, stat, title, artist, album, duration, embedded in added:
            self._cache[track["path"]] = [int(stat.st_mtime), stat.st_size, title, artist, album, duration, embedded]
        self.save_cache()
        record_download_completed()

    def snapshot(self) -> dict:
        with self.lock:
            public = [{k: v for k, v in t.items() if k not in ("path", "playlist")} for t in self.tracks.values()]
            return {
                "scanning": self.scanning,
                "version": self.version,
                "tracks": public,
                "playlists": list(self.playlists),
                "counts": dict(self.source_counts),
            }

    def path_of(self, track_id: str):
        with self.lock:
            track = self.tracks.get(track_id)
            return Path(track["path"]) if track else None

    def cover_of(self, track_id: str):
        """(байты, mime) обложки трека или None."""
        path = self.path_of(track_id)
        if path is None or not path.is_file():
            return None
        embedded = read_embedded_cover(path)
        if embedded:
            return embedded
        try:
            for suffix in (".jpg", ".jpeg", ".png", ".webp"):
                sidecar = path.with_suffix(suffix)
                if sidecar.is_file():
                    return sidecar.read_bytes(), CONTENT_TYPES[sidecar.suffix.lower()]
            for candidate in sorted(path.parent.iterdir()):
                if candidate.stem.lower() in COVER_NAMES and candidate.suffix.lower() in IMAGE_EXTENSIONS:
                    return candidate.read_bytes(), CONTENT_TYPES[candidate.suffix.lower()]
        except OSError:
            pass
        return None


# ======================================================================================
#  Обложка трека для Discord
# ======================================================================================
# Discord не может взять картинку с твоего ПК: ему нужна публичная https-ссылка. Поэтому обложка текущего
# трека (уменьшенная до 512 px) временно выкладывается на бесплатный файлообменник, а ссылка отдаётся
# в Rich Presence. Одинаковые обложки (например, один альбом) грузятся один раз.
# Хосты пробуются по очереди: если один отвечает ошибкой 5xx/412, лежит или тормозит, плеер сразу
# переходит к следующему, а упавший хост на пару минут пропускается. Раньше хост был один (Litterbox),
# и любой его сбой оставлял статус без обложки.
COVER_USER_AGENT = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) LastFrequency/{VERSION}"
COVER_REQUEST_TIMEOUT = 20        # секунд на один запрос
COVER_TOTAL_BUDGET = 70           # секунд на всю цепочку хостов для одной обложки
COVER_HOST_COOLDOWN = 120         # на сколько секунд пропускать хост после сбоя
COVER_CACHE_LIMIT = 300           # сколько ссылок хранить в discord_covers.json
COVER_LEGACY_TTL = 60 * 3600      # срок жизни записей старого формата (Litterbox, 72 ч с запасом)
COVER_URL_SCHEMES = ("https://",)  # Discord принимает для картинок только https
NO_COVER = ""                     # resolve(): у трека обложки нет вообще (повторять бесполезно)
DISCORD_COVER_RETRIES = 4         # сколько раз повторять загрузку обложки, пока трек играет
_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>\\]+")


def _multipart(fields: dict, file_field: str, filename: str, data: bytes, mime: str):
    boundary = "----LastFrequency" + secrets.token_hex(12)
    parts = []
    for key, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                  f'Content-Type: {mime}\r\n\r\n').encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _first_url(text: str):
    """Первая ссылка из ответа хоста: обычный текст или JSON."""
    try:
        node = json.loads(text)
    except ValueError:
        node = None
    stack = [node] if node is not None else []
    while stack:
        item = stack.pop(0)
        if isinstance(item, str) and item.startswith(("http://", "https://")):
            return item.strip()
        if isinstance(item, dict):
            stack = [item[k] for k in ("url", "link") if k in item] + list(item.values()) + stack
        elif isinstance(item, list):
            stack = item + stack
    match = _URL_IN_TEXT.search(text or "")
    return match.group(0).rstrip(".,;") if match else None


def _tmpfiles_direct(url: str) -> str:
    # tmpfiles.org отдаёт ссылку на страницу; сама картинка лежит по адресу /dl/…
    url = url.replace("http://", "https://", 1)
    return url if "/dl/" in url else url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1)


# (имя, адрес, поле файла, поля формы, сколько часов живёт файл, исправление ссылки)
COVER_PROVIDERS = (
    ("litterbox", "https://litterbox.catbox.moe/resources/internals/api.php", "fileToUpload",
     {"reqtype": "fileupload", "time": "72h"}, 72, None),
    ("uguu", "https://uguu.se/upload?output=text", "files[]", {}, 3, None),
    ("0x0", "https://0x0.st", "file", {"expires": "72"}, 72, None),
    ("tmpfiles", "https://tmpfiles.org/api/v1/upload", "file", {}, 1, _tmpfiles_direct),
    ("catbox", "https://catbox.moe/user/api.php", "fileToUpload", {"reqtype": "fileupload"}, 24 * 30, None),
)


class _UploadError(Exception):
    def __init__(self, message: str, transient: bool):
        super().__init__(message)
        self.transient = transient


class CoverHost:
    def __init__(self):
        self._lock = threading.Lock()
        self._cache: dict[str, dict] = {}
        self._down_until: dict[str, float] = {}
        try:
            raw = json.loads(COVER_URL_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._cache = {k: v for k, v in raw.items() if isinstance(v, dict) and self._alive(v)}
        except (OSError, ValueError, TypeError):
            pass

    @staticmethod
    def _alive(entry: dict) -> bool:
        try:
            ttl = float(entry.get("ttl", COVER_LEGACY_TTL))
            return bool(entry.get("url")) and time.time() - float(entry.get("ts", 0)) < ttl
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _prepare(data: bytes, mime: str, *, youtube_thumbnail: bool = False):
        """Prepare a square cover; YouTube's 4:3 previews include 16:9 letterboxing."""
        try:
            from PIL import Image, ImageOps
        except ImportError:
            Image = None
        if Image is not None:
            try:
                image = Image.open(io.BytesIO(data))
                image.load()
                image = ImageOps.exif_transpose(image)
                if image.mode != "RGB":
                    image = image.convert("RGB")
                width, height = image.size
                if youtube_thumbnail and 1.3 < width / max(1, height) < 1.4:
                    content_height = min(height, round(width * 9 / 16))
                    top = (height - content_height) // 2
                    image = image.crop((0, top, width, top + content_height))
                width, height = image.size
                side = min(width, height)
                left, top = (width - side) // 2, (height - side) // 2
                image = image.crop((left, top, left + side, top + side))
                image.thumbnail((512, 512))
                buffer = io.BytesIO()
                image.save(buffer, "JPEG", quality=88)
                return buffer.getvalue(), "image/jpeg"
            except Exception:
                logger.info("Обложка для Discord: не удалось прочитать картинку", exc_info=True)
                return None, None
        if len(data) <= 1_500_000 and mime in ("image/jpeg", "image/png"):
            return data, mime
        logger.info("Обложка для Discord пропущена: она большая, а Pillow не установлен (python -m pip install pillow)")
        return None, None

    @staticmethod
    def _post(url: str, body: bytes, content_type: str, timeout: float) -> str:
        request = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": content_type, "User-Agent": COVER_USER_AGENT, "Accept": "*/*", "Connection": "close"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(65536).decode("utf-8", "replace").strip()
        except urllib.error.HTTPError as error:
            raise _UploadError(f"HTTP {error.code}", transient=error.code >= 500 or error.code in (408, 425, 429)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise _UploadError("таймаут" if isinstance(reason, TimeoutError) or "timed out" in str(reason) else str(reason)[:80],
                               transient=True) from None

    @staticmethod
    def _looks_like_image(url: str, timeout: float) -> bool:
        """Быстрая проверка, что по ссылке действительно лежит картинка. Сомневаясь, считаем, что да."""
        request = urllib.request.Request(url, headers={"User-Agent": COVER_USER_AGENT, "Range": "bytes=0-1023"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                kind = (response.headers.get("Content-Type") or "").lower()
                return not kind or kind.startswith(("image/", "application/octet-stream", "binary/"))
        except urllib.error.HTTPError as error:
            return error.code not in (404, 410)
        except (urllib.error.URLError, TimeoutError, OSError):
            return True

    def _upload_to(self, provider, data: bytes, mime: str, deadline: float) -> str:
        _, endpoint, file_field, fields, _, fix = provider
        extension = ".png" if mime == "image/png" else ".jpg"
        body, content_type = _multipart(fields, file_field, "cover" + extension, data, mime)
        last = _UploadError("нет ответа", transient=True)
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining < 3:
                raise _UploadError("не хватило времени", transient=True)
            try:
                text = self._post(endpoint, body, content_type, min(COVER_REQUEST_TIMEOUT, remaining))
            except _UploadError as error:
                last = error
                if error.transient and attempt == 0:
                    time.sleep(1.0)
                    continue
                raise
            url = _first_url(text)
            if url and fix:
                url = fix(url)
            if not url or not url.startswith(COVER_URL_SCHEMES) or len(url) > 220:
                raise _UploadError("непонятный ответ: " + text[:60].replace("\n", " "), transient=False)
            if not self._looks_like_image(url, min(8.0, max(2.0, deadline - time.monotonic()))):
                raise _UploadError("по ссылке не картинка", transient=False)
            return url
        raise last

    def _upload(self, data: bytes, mime: str):
        """Пробует хосты по очереди. Возвращает (ссылка, имя хоста, срок жизни в секундах) или None."""
        deadline = time.monotonic() + COVER_TOTAL_BUDGET
        now = time.monotonic()
        with self._lock:
            order = [p for p in COVER_PROVIDERS if self._down_until.get(p[0], 0) <= now] or list(COVER_PROVIDERS)
        problems = []
        for provider in order:
            if deadline - time.monotonic() < 3:
                break
            try:
                url = self._upload_to(provider, data, mime, deadline)
            except _UploadError as error:
                problems.append(f"{provider[0]}: {error}")
                with self._lock:
                    self._down_until[provider[0]] = time.monotonic() + COVER_HOST_COOLDOWN
                continue
            except Exception as error:  # noqa: BLE001 — сбой одного хоста не должен ронять остальные
                problems.append(f"{provider[0]}: {type(error).__name__}")
                continue
            with self._lock:
                self._down_until.pop(provider[0], None)
            if problems:
                logger.info("Обложка для Discord: не ответили %s", "; ".join(problems))
            return url, provider[0], provider[4] * 3600
        logger.warning("Обложка для Discord не загружена: %s", "; ".join(problems) or "нет доступных хостов")
        return None

    def _save_cache(self):
        if len(self._cache) > COVER_CACHE_LIMIT:
            newest = sorted(self._cache.items(), key=lambda item: float(item[1].get("ts", 0)), reverse=True)
            self._cache = dict(newest[:COVER_CACHE_LIMIT])
        temporary = COVER_URL_FILE.with_name(COVER_URL_FILE.name + ".tmp")
        try:
            temporary.write_text(json.dumps(self._cache), encoding="utf-8")
            os.replace(temporary, COVER_URL_FILE)
        except OSError:
            pass

    def resolve(self, library, track_id: str):
        """Блокирующая операция (диск + сеть): вызывать только из фонового потока.

        Возвращает ссылку, NO_COVER (у трека нет обложки) или None (не удалось загрузить — стоит повторить)."""
        found = library.cover_of(track_id)
        if not found:
            return NO_COVER
        data, mime = self._prepare(*found)
        if not data:
            return NO_COVER
        key = hashlib.sha1(data).hexdigest()
        with self._lock:
            hit = self._cache.get(key)
            if hit and self._alive(hit):
                return hit["url"]
        uploaded = self._upload(data, mime)
        if not uploaded:
            return None
        url, host, lifetime = uploaded
        with self._lock:
            # С запасом: Discord подтягивает картинку не мгновенно, а ссылка к тому моменту не должна протухнуть.
            self._cache[key] = {"url": url, "ts": time.time(), "ttl": lifetime * 0.85, "host": host}
            self._save_cache()
        logger.info("Обложка для Discord загружена (%s): %s", host, url)
        return url

    def resolve_remote(self, image_url: str):
        """Fetch a public provider thumbnail and share it through the same expiring cover cache."""
        parsed = urlparse(str(image_url or ""))
        host = (parsed.hostname or "").lower().rstrip(".")
        allowed = ("ytimg.com", "youtube.com", "sndcdn.com", "soundcloud.com", "googleusercontent.com", "ggpht.com")
        if parsed.scheme != "https" or not host or not any(host == item or host.endswith("." + item) for item in allowed):
            return NO_COVER
        request = urllib.request.Request(image_url, headers={"User-Agent": COVER_USER_AGENT, "Accept": "image/*"})
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                final = urlparse(response.geturl())
                final_host = (final.hostname or "").lower().rstrip(".")
                mime = (response.headers.get("Content-Type") or "image/jpeg").split(";", 1)[0].lower()
                if final.scheme != "https" or not any(final_host == item or final_host.endswith("." + item) for item in allowed):
                    return NO_COVER
                if not mime.startswith("image/"):
                    return NO_COVER
                data = response.read(5_000_001)
        except (urllib.error.URLError, TimeoutError, OSError):
            return None
        if not data or len(data) > 5_000_000:
            return NO_COVER
        youtube_thumbnail = (host == "ytimg.com" or host.endswith(".ytimg.com")) and parsed.path.startswith(("/vi/", "/vi_webp/"))
        data, mime = self._prepare(data, mime, youtube_thumbnail=youtube_thumbnail)
        if not data:
            return NO_COVER
        key = hashlib.sha1(data).hexdigest()
        with self._lock:
            hit = self._cache.get(key)
            if hit and self._alive(hit):
                return hit["url"]
        uploaded = self._upload(data, mime)
        if not uploaded:
            return None
        url, provider_name, lifetime = uploaded
        with self._lock:
            self._cache[key] = {"url": url, "ts": time.time(), "ttl": lifetime * 0.85, "host": provider_name}
            self._save_cache()
        logger.info("Обложка онлайн-трека для Discord загружена (%s): %s", provider_name, url)
        return url


COVER_HOST = CoverHost()


# ======================================================================================
#  Discord Rich Presence
# ======================================================================================
class DiscordPresence:
    """Показывает в Discord, какой трек играет. Работает в своём потоке, не блокирует плеер."""

    MIN_GAP = 4.0  # Discord ограничивает частоту обновлений статуса
    CLEAR = object()

    def __init__(self):
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._enabled = False
        self._client_id = ""
        self._show_elapsed = True
        self._show_paused = True
        self._show_link = True
        self._status_format = "track_artist"
        self._large_image = ""
        self._show_cover = True
        self._show_artist_image = True
        self.library = None            # выставляется в AppState: нужен, чтобы достать обложку
        self._covers: dict[str, str] = {}      # id трека -> ссылка на обложку
        self._cover_busy: set[str] = set()
        self._cover_fail: dict[str, tuple[float, int]] = {}   # id трека -> (когда не вышло, сколько раз подряд)
        self._no_cover: set[str] = set()                      # у этих треков обложки нет — не повторяем
        self._no_artist_image: set[str] = set()
        self._now: dict | None = None  # последние данные о треке от плеера
        self._pending = None
        self._dirty = False
        self._rpc = None
        self._connected_id = ""
        self._last_send = 0.0
        self._next_try = 0.0
        self._status = {"state": "off", "message": "Выключено"}
        threading.Thread(target=self._run, name="discord-presence", daemon=True).start()

    def configure(self, cfg: dict):
        with self._lock:
            show_cover = cfg.get("show_cover", True)
            show_artist_image = cfg.get("show_artist_image", True)
            show_paused = cfg.get("show_paused", True)
            show_link = cfg.get("show_link", True)
            status_format = cfg.get("status_format", "track_artist")
            changed = (cfg["enabled"], cfg["client_id"], cfg["show_elapsed"], cfg["large_image"], show_cover,
                       show_artist_image,
                       show_paused, show_link, status_format) != (
                self._enabled, self._client_id, self._show_elapsed, self._large_image, self._show_cover,
                self._show_artist_image,
                self._show_paused, self._show_link, self._status_format)
            cover_setting_changed = show_cover != self._show_cover or (show_artist_image and not self._show_artist_image)
            self._enabled = cfg["enabled"]
            self._client_id = cfg["client_id"]
            self._show_elapsed = cfg["show_elapsed"]
            self._large_image = cfg["large_image"]
            self._show_cover = show_cover
            self._show_artist_image = show_artist_image
            self._show_paused = show_paused
            self._show_link = show_link
            self._status_format = status_format
            if cover_setting_changed and (show_cover or show_artist_image):
                self._no_cover.clear()
            if changed:
                self._next_try = 0.0
                self._pending = self._activity() if self._now else self.CLEAR
                self._dirty = True
                if not self._enabled or not self._client_id:
                    self._status = {"state": "off" if not self._enabled else "no_client_id",
                                    "message": "Выключено" if not self._enabled else "Укажи Client ID приложения Discord"}
            now_playing = self._now
        self._want_cover(now_playing)
        self._wake.set()

    def reconnect(self):
        with self._lock:
            self._next_try = 0.0
            self._pending = self._activity() if self._now else self.CLEAR
            self._dirty = True
            self._drop()
        self._wake.set()

    def update(self, now_playing: dict | None):
        # Only this player's playback is sent to Discord. Other apps are deliberately ignored.
        with self._lock:
            if now_playing and not now_playing.get("playing") and not self._show_paused:
                now_playing = None
            self._now = now_playing
            self._pending = self._activity() if now_playing else self.CLEAR
            self._dirty = True
        self._want_cover(now_playing)
        self._wake.set()

    # --- обложка текущего трека: готовим в фоне, статус при этом не тормозит ---
    def _want_cover(self, now_playing):
        track_id = str((now_playing or {}).get("id") or "")
        remote = bool((now_playing or {}).get("remote"))
        if remote:
            has_cover_source = bool((self._show_cover and (now_playing or {}).get("thumbnail")) or
                                    (self._show_artist_image and (now_playing or {}).get("artist_thumbnail")))
        else:
            has_cover_source = bool(self.library is not None and _ID_RE.match(track_id))
        if not ((self._show_cover or self._show_artist_image) and self._enabled and track_id and has_cover_source):
            return
        with self._lock:
            large_ready = not self._show_cover or track_id in self._covers
            artist_ready = not self._show_artist_image or track_id in getattr(self, "_artist_covers", {}) or track_id in self._no_artist_image
            if (large_ready and artist_ready) or track_id in self._cover_busy or track_id in self._no_cover:
                return
            # Одна загрузка за раз, даже если треки переключают быстро.
            if self._cover_busy:
                return
            failed = self._cover_fail.get(track_id)
            if failed and time.monotonic() - failed[0] < self._retry_delay(failed[1]):
                return
            self._cover_busy.add(track_id)
        threading.Thread(target=self._fetch_cover, args=(track_id,), name="discord-cover", daemon=True).start()

    @staticmethod
    def _retry_delay(failures: int) -> float:
        return min(300.0, 20.0 * 2 ** max(0, failures - 1))   # 20 с, 40 с, 80 с, 160 с, 300 с

    def _fetch_cover(self, track_id: str):
        url = None
        artist_url = NO_COVER
        try:
            with self._lock:
                current = dict(self._now or {})
            if str(current.get("id") or "") == track_id and current.get("remote"):
                url = COVER_HOST.resolve_remote(str(current.get("thumbnail") or "")) if self._show_cover else NO_COVER
                artist_url = COVER_HOST.resolve_remote(str(current.get("artist_thumbnail") or "")) if self._show_artist_image else NO_COVER
            else:
                url = COVER_HOST.resolve(self.library, track_id) if self._show_cover else NO_COVER
        except Exception:  # noqa: BLE001 — обложка не должна ломать статус
            logger.warning("Обложка для Discord: ошибка", exc_info=True)
        retry_in = None
        with self._lock:
            self._cover_busy.discard(track_id)
            if url:
                if len(self._covers) > 400:
                    self._covers.clear()
                self._covers[track_id] = url
                self._cover_fail.pop(track_id, None)
                if str((self._now or {}).get("id") or "") == track_id:
                    self._pending = self._activity()
                    self._dirty = True
            elif url == NO_COVER:
                if len(self._no_cover) > 2000:
                    self._no_cover.clear()
                self._no_cover.add(track_id)
            else:
                failures = self._cover_fail.get(track_id, (0.0, 0))[1] + 1
                self._cover_fail[track_id] = (time.monotonic(), failures)
                if failures <= DISCORD_COVER_RETRIES:
                    retry_in = self._retry_delay(failures)
            current = self._now
            if artist_url and artist_url != NO_COVER:
                if not hasattr(self, "_artist_covers"):
                    self._artist_covers = {}
                self._artist_covers[track_id] = artist_url
                if str((current or {}).get("id") or "") == track_id:
                    self._pending = self._activity()
                    self._dirty = True
            elif artist_url == NO_COVER:
                self._no_artist_image.add(track_id)
        self._wake.set()
        if current and str(current.get("id") or "") != track_id:
            self._want_cover(current)
        if retry_in is not None:
            # Хост мог просто лечь на минуту: пока трек играет, пробуем снова, а не ждём его следующего включения.
            timer = threading.Timer(retry_in + 0.5, self._retry_cover, args=(track_id,))
            timer.daemon = True
            timer.start()

    def _retry_cover(self, track_id: str):
        with self._lock:
            current = self._now
        if current and str(current.get("id") or "") == track_id:
            self._want_cover(current)

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def shutdown(self):
        self._stop.set()
        self._wake.set()
        with self._lock:
            self._drop(clear=True)

    # --- внутреннее ---
    def _activity(self):
        np = self._now or {}
        title = str(np.get("title") or "Без названия")[:120]
        artist = str(np.get("artist") or "Неизвестный исполнитель")[:120]
        source = str(np.get("source") or "").strip()[:36]
        playing = bool(np.get("playing"))
        # The provider is shown separately in the album/source field.
        details = title[:120]
        state_text = artist
        if not playing:
            state_text = ("⏸ " + state_text)[:120]
        activity = {"details": details + "\u200b" if len(details) < 2 else details,
                    "state": state_text + "\u200b" if len(state_text) < 2 else state_text}
        if playing and self._show_elapsed:
            try:
                position = float(np.get("position") or 0)
                duration = float(np.get("duration") or 0)
            except (TypeError, ValueError):
                position, duration = 0.0, 0.0
            start = int(time.time() - position)
            activity["start"] = start
            if duration > 0:
                activity["end"] = int(start + duration)
        cover = self._covers.get(str(np.get("id") or "")) if self._show_cover else None
        if cover:
            activity["large_image"] = cover
            large_text = (str(np.get("album") or "") or source or APP_NAME)[:120]
            activity["large_text"] = large_text if len(large_text) >= 2 else APP_NAME
        elif self._large_image:
            activity["large_image"] = self._large_image
            activity["large_text"] = APP_NAME
        artist_image = getattr(self, "_artist_covers", {}).get(str(np.get("id") or "")) if self._show_artist_image else None
        if artist_image:
            activity["small_image"] = artist_image
            activity["small_text"] = artist[:120] if len(artist) >= 2 else "Исполнитель"
        source_url = str(np.get("url") or "")
        if self._show_link and source_url.startswith("https://"):
            activity["buttons"] = [{"label": "Открыть трек", "url": source_url[:512]}]
        return activity

    def _drop(self, clear=False):
        rpc, self._rpc = self._rpc, None
        self._connected_id = ""
        if rpc is not None:
            try:
                if clear:
                    rpc.clear()
                rpc.close()
            except Exception:
                pass

    def _set_status(self, state, message):
        with self._lock:
            self._status = {"state": state, "message": message}

    def _run(self):
        while not self._stop.is_set():
            self._wake.wait(timeout=5 if (self._enabled and self._client_id) else 60)
            self._wake.clear()
            if self._stop.is_set():
                break
            with self._lock:
                enabled, client_id = self._enabled, self._client_id
                dirty, pending = self._dirty, self._pending
            if not enabled or not client_id:
                with self._lock:
                    self._drop(clear=True)
                continue
            if not dirty and self._rpc is not None:
                continue
            now = time.monotonic()
            if now < self._next_try:
                continue
            wait = self._last_send + self.MIN_GAP - now
            if wait > 0:
                time.sleep(wait)
            try:
                self._send(client_id, pending)
            except Exception as error:  # noqa: BLE001 — любые сбои RPC не должны ронять плеер
                self._handle_error(error)

    def _send(self, client_id, pending):
        try:
            import pypresence
        except ImportError:
            self._set_status("no_library", "Не установлен pypresence: python -m pip install pypresence")
            self._next_try = time.monotonic() + 30
            return
        with self._lock:
            if self._rpc is not None and self._connected_id != client_id:
                self._drop()
        if self._rpc is None:
            self._set_status("connecting", "Подключаюсь к Discord…")
            rpc = pypresence.Presence(client_id)
            rpc.connect()
            with self._lock:
                self._rpc, self._connected_id = rpc, client_id
        rpc = self._rpc
        if pending is self.CLEAR or pending is None:
            rpc.clear()
        else:
            payload = dict(pending)
            try:
                rpc.update(activity_type=pypresence.ActivityType.LISTENING, **payload)
            except (TypeError, AttributeError):
                payload.pop("buttons", None)  # older Discord RPC builds reject activity buttons
                try:
                    rpc.update(activity_type=pypresence.ActivityType.LISTENING, **payload)
                except (TypeError, AttributeError):
                    rpc.update(**payload)  # старая версия pypresence без «Слушает»
        self._last_send = time.monotonic()
        with self._lock:
            if self._pending is pending:
                self._dirty = False
        self._set_status("connected", "Подключено к Discord")

    def _handle_error(self, error):
        name = type(error).__name__
        text = str(error)
        if name == "DiscordNotFound":
            message = "Discord не запущен (открой приложение Discord на ПК)"
        elif name == "InvalidID":
            message = "Неверный Client ID приложения Discord"
        else:
            message = f"{name}: {text}"[:200]
        logger.warning("Discord RPC: %s", message)
        with self._lock:
            self._drop()
            self._status = {"state": "error", "message": message}
            self._next_try = time.monotonic() + 10


def _find_ffmpeg_binary() -> str:
    """Find FFmpeg in the app bundle, PATH, or imageio-ffmpeg package."""
    import shutil
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    candidates = [
        shutil.which("ffmpeg"),
        str(RESOURCE_DIR / "ffmpeg" / "bin" / exe),
        str(RESOURCE_DIR / "ffmpeg" / exe),
        str(Path(__file__).resolve().parent.parent / "ffmpeg" / "bin" / exe),
        str(Path(sys.executable).resolve().parent / "ffmpeg" / "bin" / exe),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    try:
        import imageio_ffmpeg
        candidate = imageio_ffmpeg.get_ffmpeg_exe()
        if candidate and Path(candidate).is_file():
            return candidate
    except Exception:
        pass
    raise RuntimeError("FFmpeg не найден. Положи ffmpeg.exe в папку ffmpeg\bin или установи imageio-ffmpeg.")


def _hide_library_track(track_id: str, playlist_key: str = "") -> dict:
    with STATE.library.lock:
        track = STATE.library.tracks.get(track_id)
        if not track:
            raise ValueError("Трек не найден в библиотеке")
        if playlist_key and track.get("pl") != playlist_key:
            raise ValueError("Трек не принадлежит этому плейлисту")
    settings = STATE.settings.get()
    if playlist_key:
        hidden = settings.get("hidden_playlist_tracks", {})
        hidden[playlist_key] = list(dict.fromkeys(hidden.get(playlist_key, []) + [track_id]))
        settings = STATE.settings.update({"hidden_playlist_tracks": hidden})
    else:
        settings = STATE.settings.update({"hidden_tracks": list(dict.fromkeys(settings.get("hidden_tracks", []) + [track_id]))})
    return {"ok": True, "settings": settings}


def _delete_library_track(track_id: str) -> dict:
    """Delete only an indexed audio file under an explicitly configured source."""
    if not _ID_RE.fullmatch(track_id):
        raise ValueError("Некорректный идентификатор трека")
    path = STATE.library.path_of(track_id)
    if path is None:
        raise ValueError("Трек не найден в библиотеке")
    resolved = path.resolve()
    roots = [Path(source["path"]).expanduser().resolve() for source in resolve_sources(STATE.settings.get()) if source.get("path")]
    if path.is_symlink() or resolved.suffix.lower() not in AUDIO_EXTENSIONS or not any(resolved.is_relative_to(root) for root in roots):
        raise ValueError("Удаление доступно только для аудиофайла в папке библиотеки")
    resolved.unlink(missing_ok=True)
    with STATE.library.lock:
        track = STATE.library.tracks.pop(track_id, None)
        STATE.library._cache.pop(str(path), None)
        STATE.library.save_cache()
        if track:
            source_id = track.get("src")
            STATE.library.source_counts[source_id] = max(0, STATE.library.source_counts.get(source_id, 0) - 1)
            for playlist in STATE.library.playlists:
                if playlist.get("key") == track.get("pl"):
                    playlist["count"] = max(0, playlist.get("count", 0) - 1)
            STATE.library.playlists = [item for item in STATE.library.playlists if item.get("count", 0)]
        STATE.library.version += 1
    current = STATE.settings.get()
    STATE.settings.update({"favorites": [item for item in current["favorites"] if item != track_id],
                           "hidden_tracks": [item for item in current.get("hidden_tracks", []) if item != track_id],
                           "hidden_playlist_tracks": {key: [item for item in ids if item != track_id]
                                                      for key, ids in current.get("hidden_playlist_tracks", {}).items()}})
    with _CUSTOM_PLAYLISTS_LOCK:
        playlists = _read_custom_playlists()
        for playlist in playlists["items"]:
            playlist["ids"] = [item for item in playlist["ids"] if item != track_id]
        _write_custom_playlists(int(time.time() * 1000), playlists["items"])
    try:
        _clip_path(track_id).unlink(missing_ok=True)
    except OSError:
        logger.info("Не удалось удалить сохранённый клип удалённого трека")
    STATE.library.scan_async(force=True)
    return {"ok": True, "id": track_id}


def _trim_local_track(track_id: str, start: float, end: float) -> dict:
    """Render a selected time range into a new MP3 without modifying the source."""
    if not isinstance(track_id, str) or not _ID_RE.fullmatch(track_id):
        raise ValueError("Некорректный идентификатор трека")
    path = STATE.library.path_of(track_id)
    if path is None or not path.is_file():
        raise ValueError("Исходный трек не найден")
    try:
        start, end = float(start), float(end)
    except (TypeError, ValueError):
        raise ValueError("Начало и конец фрагмента должны быть числами")
    _title, _artist, _album, duration, _cover = read_tags(path)
    if not (0 <= start < end and end - start >= 0.1):
        raise ValueError("Конец фрагмента должен быть позже начала")
    if duration and end > duration + 0.5:
        raise ValueError("Конец фрагмента выходит за длительность исходного трека")
    ffmpeg = _find_ffmpeg_binary()
    output_dir = MUSIC_DIR / "Edited"
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^0-9A-Za-zА-Яа-я _.-]+", "_", path.stem).strip(" ._")[:90] or "track"
    output = output_dir / f"{stem} - cut {int(time.time() * 1000)}.mp3"
    # Use the same cover resolution as the library/UI (embedded, sidecar image, or folder cover).
    embedded_cover = STATE.library.cover_of(track_id) or read_embedded_cover(path)
    cover_path = None
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-i", str(path)]
    if embedded_cover and embedded_cover[0]:
        try:
            import tempfile
            mime = str(embedded_cover[1] or "image/jpeg").lower()
            suffix = ".png" if "png" in mime else ".jpg"
            with tempfile.NamedTemporaryFile(prefix="lf-cover-", suffix=suffix, dir=str(output_dir), delete=False) as cover_file:
                cover_file.write(embedded_cover[0])
                cover_path = Path(cover_file.name)
            command.extend(["-i", str(cover_path)])
        except OSError:
            cover_path = None
    command.extend(["-t", f"{end-start:.3f}", "-map", "0:a:0"])
    if cover_path is not None:
        command.extend(["-map", "1:v:0", "-c:v", "copy", "-disposition:v:0", "attached_pic",
                        "-metadata:s:v:0", "title=Album cover", "-metadata:s:v:0", "comment=Cover (front)",
                        "-id3v2_version", "3"])
    else:
        command.append("-vn")
    command.extend(["-map_metadata", "0", "-metadata", "LAST_FREQUENCY_SOURCE_ID=" + track_id,
                    "-codec:a", "libmp3lame", "-q:a", "2", str(output)])
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=600,
                                creationflags=creationflags, check=False)
    except subprocess.TimeoutExpired:
        output.unlink(missing_ok=True)
        raise RuntimeError("Обрезка занимает слишком много времени и была остановлена")
    finally:
        if cover_path is not None:
            try: cover_path.unlink(missing_ok=True)
            except OSError: pass
    if result.returncode != 0 or not output.is_file() or output.stat().st_size < 256:
        output.unlink(missing_ok=True)
        message = (result.stderr or "FFmpeg не смог обработать файл").strip().splitlines()[-1:]
        raise RuntimeError((message[0] if message else "FFmpeg не смог обработать файл")[:300])
    STATE.library.scan_async(force=True)
    return {"ok": True, "filename": output.name, "path": str(output), "duration": end-start}


def _edited_source_track_id(path: Path) -> str:
    """New cuts retain provenance even if their title or filename is edited later."""
    try:
        from mutagen.id3 import ID3
        frame = ID3(path).get("TXXX:LAST_FREQUENCY_SOURCE_ID")
        return str(frame.text[0]) if frame and frame.text else ""
    except Exception:
        return ""


def _select_edited_replacement(source_track: dict, candidates: list, original_id: str = "") -> dict:
    """Resolve one original; a duplicate name must never replace an arbitrary file."""
    import unicodedata

    def name(value):
        value = unicodedata.normalize("NFKC", str(value or ""))
        value = re.sub(r"\s+-\s+cut\s+\d{10,}$", "", value, flags=re.I)
        value = re.sub(r"\s+[—-]\s+фрагмент(?:\s+[0-9a-f]{8})?$", "", value, flags=re.I)
        return " ".join(value.casefold().split())

    def artist(track):
        value = name(track.get("artist"))
        return "" if value in ("неизвестный автор", "неизвестный исполнитель", "unknown artist", "unknown") else value

    originals = [track for track in candidates if original_id and track.get("id") == original_id]
    if len(originals) == 1:
        return originals[0]
    title = name(source_track.get("title"))
    matches = [track for track in candidates if title and name(track.get("title")) == title]
    source_name = name(Path(source_track.get("path") or "").stem)
    if not matches and (not title or title == source_name):
        # Untagged cuts use a shortened, filename-safe version of the source name.
        matches = [track for track in candidates if source_name and name(
            re.sub(r"[^0-9A-Za-zА-Яа-я _.-]+", "_", Path(track.get("path") or "").stem).strip(" ._")[:90]
        ) == source_name]
    source_artist = artist(source_track)
    if source_artist:
        exact = [track for track in matches if artist(track) == source_artist]
        matches = exact or [track for track in matches if not artist(track)]
    if not matches:
        raise ValueError("В выбранном плейлисте нет исходного трека с таким названием. Файлы не изменены.")
    if len(matches) != 1:
        raise ValueError("В плейлисте несколько треков с таким названием и автором. Уточни их названия и повтори замену; файлы не изменены.")
    return matches[0]


def _distribute_edited_track(track_id: str, target_playlist_key: str, mode: str = "move",
                             replace_track_id: str = "") -> dict:
    """Move an Edited track into a local folder playlist, optionally replacing one track."""
    with STATE.library.lock:
        source_track = STATE.library.tracks.get(str(track_id or ""))
        target_track = STATE.library.tracks.get(str(replace_track_id or "")) if replace_track_id else None
        playlist_track = next((track for track in STATE.library.tracks.values()
                               if track.get("pl") == target_playlist_key), None)
        source_path = Path(source_track["path"]).resolve() if source_track else None
        playlist_path = Path(playlist_track["path"]).resolve().parent if playlist_track else None
        target_name = str(playlist_track.get("playlist") or "") if playlist_track else ""
        candidates = [dict(track) for track in STATE.library.tracks.values()
                      if track.get("pl") == target_playlist_key and track.get("id") != track_id]

    if source_path is None or not source_path.is_file():
        raise ValueError("Трек из Edited не найден. Обнови библиотеку и попробуй снова.")
    edited_root = (MUSIC_DIR / "Edited").resolve()
    try:
        source_path.relative_to(edited_root)
    except ValueError:
        raise ValueError("Можно распределять только треки из папки music/Edited.")
    if playlist_path is None:
        raise ValueError("Локальный плейлист не найден. Обнови библиотеку и попробуй снова.")
    try:
        playlist_path.relative_to(edited_root)
    except ValueError:
        pass
    else:
        raise ValueError("Выбери локальный плейлист вне папки Edited.")
    if not target_playlist_key or target_playlist_key == source_track.get("pl"):
        raise ValueError("Выбери другой локальный плейлист.")
    if not playlist_path.is_dir():
        raise ValueError("Папка выбранного плейлиста недоступна.")

    def unique_path(folder: Path, filename: str) -> Path:
        candidate = folder / filename
        if not candidate.exists():
            return candidate
        stem, suffix = candidate.stem, candidate.suffix
        index = 2
        while True:
            candidate = folder / f"{stem} ({index}){suffix}"
            if not candidate.exists():
                return candidate
            index += 1

    if mode not in ("move", "copy", "replace"):
        raise ValueError("Неизвестное действие с треком.")
    if mode in ("move", "copy"):
        destination = unique_path(playlist_path, source_path.name)
        if mode == "copy":
            shutil.copy2(source_path, destination)
        else:
            source_path.rename(destination)
        STATE.library.scan_async(force=True)
        return {"ok": True, "action": mode, "filename": destination.name, "playlist": target_name}

    if target_track is None and not replace_track_id:
        target_track = _select_edited_replacement(source_track, candidates, _edited_source_track_id(source_path))
    target_path = Path(target_track["path"]).resolve() if target_track else None
    if target_track is None or target_track.get("pl") != target_playlist_key or target_path is None or not target_path.is_file():
        raise ValueError("Трек для замены не найден в выбранном плейлисте.")
    if target_path == source_path:
        raise ValueError("Нельзя заменить трек самим собой.")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = unique_path(edited_root, f"{target_path.stem} - original {stamp}{target_path.suffix}")
    destination = target_path.with_suffix(source_path.suffix)
    if destination.exists() and destination != target_path:
        destination = unique_path(playlist_path, f"{target_path.stem} - Edited{source_path.suffix}")
    target_path.rename(backup)
    try:
        source_path.rename(destination)
    except OSError:
        try:
            backup.rename(target_path)
        except OSError:
            logger.exception("Не удалось восстановить оригинальный трек после ошибки замены")
        raise
    STATE.library.scan_async(force=True)
    return {"ok": True, "action": "replace", "filename": destination.name,
            "backup_filename": backup.name, "playlist": target_name}


# ======================================================================================
#  HTTP-сервер
# ======================================================================================
class AppState:
    def __init__(self):
        self.settings = SettingsStore()
        self.library = Library(self.settings)
        self.presence = DiscordPresence()
        self.presence.library = self.library
        self.token = secrets.token_urlsafe(24)
        self.port = DEFAULT_PORT
        self.last_seen = time.monotonic()
        self.server = None
        self.server_thread_started = False
        self.dialog_lock = threading.Lock()
        self.last_now: dict | None = None
        self.last_now_at = time.monotonic()
        self.last_now_lock = threading.Lock()
        self.mini_commands = []
        self.mini_commands_lock = threading.Lock()
        self.mini_playlists = []
        self.mini_catalog_version = 0
        self.mini_catalog_lock = threading.Lock()


STATE: AppState
_TRAY_ICON = None
_TRAY_LOCK = threading.Lock()
_MINI_PLAYER = None
_MINI_BACKGROUND_REQUESTED = False


def list_backgrounds():
    try:
        files = [p for p in BG_DIR.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS]
    except OSError:
        return []
    return [p.name for p in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)]


def pick_folder(initial: str = "") -> str:
    """Системное окно выбора папки (tkinter входит в Python для Windows)."""
    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except tkinter.TclError:
        pass
    try:
        return filedialog.askdirectory(
            initialdir=initial if initial and os.path.isdir(initial) else str(Path.home()),
            title="Выбери папку с музыкой",
            parent=root,
        ) or ""
    finally:
        root.destroy()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"LastFrequency/{VERSION}"

    def log_message(self, fmt, *args):
        logger.debug("%s - %s", self.address_string(), fmt % args)

    # --- вспомогательное ---
    def _send(self, status, body: bytes, content_type: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status=200):
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _fail(self, status, message):
        self._json({"error": message}, status)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{STATE.port}", f"localhost:{STATE.port}")

    def _token_ok(self, query) -> bool:
        token = self.headers.get("X-Token") or (query.get("t") or [""])[0]
        return secrets.compare_digest(token.encode(), STATE.token.encode())

    def _read_body(self, limit: int):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length < 0 or length > limit:
            return None
        return self.rfile.read(length) if length else b""

    def _serve_online_stream(self, stream_token: str, resource_id: str = ""):
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,80}", stream_token or ""):
            return self._fail(404, "Аудиопоток больше недоступен. Запусти поиск ещё раз.")
        with ONLINE_STREAMS_LOCK:
            entry = ONLINE_STREAMS.get(stream_token)
            if entry and entry.get("expires", 0) <= time.time():
                ONLINE_STREAMS.pop(stream_token, None)
                entry = None
        if not entry:
            return self._fail(404, "Аудиопоток устарел. Запусти поиск ещё раз.")
        if resource_id:
            if not re.fullmatch(r"[A-Za-z0-9_-]{12,32}", resource_id):
                return self._fail(404, "Сегмент потока больше недоступен.")
            with ONLINE_STREAMS_LOCK:
                target_url = (entry.get("resources") or {}).get(resource_id, "")
            if not target_url:
                return self._fail(404, "Сегмент потока больше недоступен.")
        else:
            target_url = entry["url"]
        if not _allowed_online_stream_url(target_url):
            return self._fail(502, "Площадка вернула неподдерживаемый адрес потока.")
        headers = dict(entry.get("headers") or {})
        range_value = self.headers.get("Range")
        if range_value and re.fullmatch(r"bytes=\d*-\d*", range_value.strip()):
            headers["Range"] = range_value.strip()
        if self.headers.get("If-Range"):
            headers["If-Range"] = self.headers.get("If-Range")
        headers.setdefault("Accept", "application/vnd.apple.mpegurl,video/*,audio/*,application/octet-stream;q=0.9,*/*;q=0.8")
        req = urllib.request.Request(target_url, headers=headers)
        try:
            upstream = urllib.request.urlopen(req, timeout=25)
        except Exception as error:
            logger.info("Online stream proxy failed: %s", type(error).__name__)
            return self._fail(502, "Площадка не отдала медиапоток. Попробуй другой результат или открой оригинал.")
        try:
            final_url = getattr(upstream, "url", "") or target_url
            if not _allowed_online_stream_url(final_url):
                return self._fail(502, "Площадка перенаправила аудиопоток на неподдерживаемый адрес.")
            content_type = upstream.headers.get("Content-Type") or "application/octet-stream"
            is_playlist = bool(entry.get("is_hls")) and (not resource_id or
                           "mpegurl" in content_type.lower() or urlparse(final_url).path.lower().endswith((".m3u8", ".m3u")))
            if is_playlist:
                raw = upstream.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    return self._fail(502, "Список сегментов источника слишком большой.")
                manifest = raw.decode("utf-8-sig", "replace")
                if "#EXTM3U" not in manifest[:128]:
                    return self._fail(502, "Источник вернул некорректный HLS-плейлист.")
                # Do not proxy encrypted or DRM-tagged HLS. Clear streams only.
                for key_line in re.findall(r"(?im)^#EXT-X-(?:SESSION-)?KEY:[^\r\n]*", manifest):
                    method = re.search(r"(?i)(?:^|,)\s*METHOD\s*=\s*\"?([^,\"]+)", key_line.split(":", 1)[-1])
                    if not method or method.group(1).strip().upper() != "NONE":
                        logger.info("Online HLS source uses encryption; refusing protected stream")
                        return self._fail(403, "Источник использует шифрование или DRM. Защищённые потоки встроенно недоступны.")

                def local_resource(remote_url):
                    absolute = urljoin(final_url, remote_url.strip())
                    if not _allowed_online_stream_url(absolute):
                        raise ValueError("unsupported HLS resource host")
                    with ONLINE_STREAMS_LOCK:
                        ids = entry.setdefault("resource_ids", {})
                        resources = entry.setdefault("resources", {})
                        rid = ids.get(absolute)
                        if not rid:
                            rid = secrets.token_urlsafe(12)
                            ids[absolute] = rid
                            resources[rid] = absolute
                    query_token = (parse_qs(urlparse(self.path).query).get("t") or [""])[0]
                    return f"/api/online/stream/{stream_token}/r/{rid}?t={quote(query_token)}"

                try:
                    rewritten = []
                    for line in manifest.splitlines():
                        if line.startswith("#"):
                            if "URI=" in line:
                                line = re.sub(r'URI="([^"]+)"', lambda match: 'URI="' + local_resource(match.group(1)) + '"', line)
                            rewritten.append(line)
                        elif line.strip():
                            rewritten.append(local_resource(line))
                        else:
                            rewritten.append("")
                    payload = ("\n".join(rewritten) + "\n").encode("utf-8")
                except ValueError:
                    return self._fail(502, "HLS-плейлист содержит неподдерживаемый адрес сегмента.")
                return self._send(200, payload, "application/vnd.apple.mpegurl; charset=utf-8",
                                  {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
            status = int(getattr(upstream, "status", 200) or 200)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            content_length = upstream.headers.get("Content-Length")
            if content_length and content_length.isdigit():
                self.send_header("Content-Length", content_length)
            else:
                self.send_header("Connection", "close")
                self.close_connection = True
            for name in ("Content-Range", "Accept-Ranges", "Last-Modified", "ETag"):
                value = upstream.headers.get(name)
                if value and "\r" not in value and "\n" not in value:
                    self.send_header(name, value)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                while True:
                    chunk = upstream.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception:
            logger.debug("Online stream relay interrupted", exc_info=True)
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    # --- GET ---
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = unquote(parsed.path)
        if not self._host_ok():
            return self._fail(403, "bad host")
        if path == "/":
            return self._serve_index()
        if path == "/api/ping":
            if self._token_ok(query):  # пинг от открытого окна: «я жив»
                STATE.last_seen = time.monotonic()
            return self._json({"app": APP_ID, "version": VERSION, "dir": str(PLAYER_DIR), "pid": os.getpid()})
        if path == "/app-icon.png":
            try:
                return self._send(200, (WEB_DIR / "app-icon.png").read_bytes(), "image/png", {"Cache-Control": "no-cache"})
            except OSError:
                return self._send(204, b"", "image/png")
        if path == "/favicon.ico":
            icon_path = RESOURCE_DIR / "icon.ico"
            try:
                return self._send(200, icon_path.read_bytes(), "image/x-icon", {"Cache-Control": "no-cache"})
            except OSError:
                return self._send(204, b"", "image/x-icon")
        if path == "/maretu-background.png":
            try:
                return self._send(200, (WEB_DIR / "maretu-background.png").read_bytes(), "image/png",
                                  {"Cache-Control": "public, max-age=86400"})
            except OSError:
                return self._fail(404, "background asset not found")
        if path == "/vendor/hls.min.js":
            try:
                return self._send(200, (WEB_DIR / "vendor" / "hls.min.js").read_bytes(),
                                  "text/javascript; charset=utf-8", {"Cache-Control": "public, max-age=31536000, immutable"})
            except OSError:
                return self._fail(404, "HLS support library not found")
        if path in ("/chat.js", "/chat.css"):
            try:
                mime = "text/javascript" if path.endswith(".js") else "text/css"
                return self._send(200, (WEB_DIR / path[1:]).read_bytes(), mime + "; charset=utf-8", {"Cache-Control": "no-cache"})
            except OSError:
                return self._fail(404, "Chat interface not found")
        if not self._token_ok(query):
            return self._fail(403, "bad token")
        if path.startswith("/api/chat/"):
            if _CHAT_SERVICE is None:
                return self._json({"enabled": False, "hosting": False, "error": "Чат выключен"},
                                  200 if path == "/api/chat/status" else 403)
            chat_query = urlencode({key: values[-1] for key, values in query.items() if key != "t"})
            status, payload = _CHAT_SERVICE.request("GET", path + ("?" + chat_query if chat_query else ""), None, session_token=self.headers.get("X-Chat-Session", ""))
            return self._json(payload, status)
        if path.startswith("/api/online/stream/"):
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:3] == ["api", "online", "stream"]:
                return self._serve_online_stream(parts[3])
            if len(parts) == 6 and parts[:3] == ["api", "online", "stream"] and parts[4] == "r":
                return self._serve_online_stream(parts[3], parts[5])
            return self._fail(404, "not found")
        if path == "/api/mini-command":
            with STATE.mini_commands_lock:
                commands, STATE.mini_commands = STATE.mini_commands, []
            return self._json({"commands": commands})
        STATE.last_seen = time.monotonic()
        try:
            if path == "/api/custom-playlists":
                with _CUSTOM_PLAYLISTS_LOCK:
                    return self._json(_read_custom_playlists())
            if path == "/api/play-history":
                with _PLAY_HISTORY_LOCK:
                    return self._json({"items": _read_play_history()})
            if path == "/api/remote-favorites":
                with _REMOTE_FAVORITES_LOCK:
                    return self._json({"items": _read_remote_favorites()})
            if path == "/api/sample-playlist":
                sample_dir = MUSIC_DIR / "Добро и позитив"
                marker = sample_dir / ".last_frequency_sample_playlist"
                return self._json({"available": bool(sample_dir.is_dir() and not sample_dir.is_symlink() and marker.is_file())})
            if path == "/api/update-check":
                force = str((query.get("force") or [""])[0]).lower() in ("1", "true", "yes")
                return self._json(github_update_status(force=force))
            if path == "/api/update/download/status":
                try:
                    return self._json(_update_download_snapshot(str((query.get("id") or [""])[0])))
                except ValueError as error:
                    return self._fail(404, str(error))
            if path == "/api/state":
                return self._json(self._state_payload())
            if path == "/api/settings":
                settings = STATE.settings.get()
                return self._json({"settings": settings, "sources": resolve_sources(settings),
                                   "backgrounds": list_backgrounds(), "discord": STATE.presence.status(),
                                   "active_performance_mode": _ACTIVE_PERFORMANCE_MODE,
                                   "active_clips_enabled": _ACTIVE_CLIPS_ENABLED,
                                   "automatic_offline": _AUTO_OFFLINE_FALLBACK})
            if path == "/api/download/idle":
                return self._json(download_idle_status())
            if path == "/api/download/status":
                try:
                    return self._json(_download_job_snapshot(str((query.get("id") or [""])[0])))
                except ValueError as error:
                    return self._fail(404, str(error))
            if path == "/api/clips/status":
                try:
                    return self._json(_clip_job_snapshot(str((query.get("id") or [""])[0])))
                except ValueError as error:
                    return self._fail(404, str(error))
            if path == "/api/library":
                return self._json(STATE.library.snapshot())
            if path.startswith("/audio/"):
                return self._serve_audio(path[len("/audio/"):])
            if path.startswith("/clip/"):
                return self._serve_clip(path[len("/clip/"):])
            if path.startswith("/cover/"):
                return self._serve_cover(path[len("/cover/"):])
            if path.startswith("/bg/"):
                return self._serve_background(path[len("/bg/"):])
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        self._fail(404, "not found")

    def _state_payload(self):
        settings = STATE.settings.get()
        return {
            "version": VERSION,
            "settings": settings,
            "active_performance_mode": _ACTIVE_PERFORMANCE_MODE,
            "active_clips_enabled": _ACTIVE_CLIPS_ENABLED,
            "automatic_offline": _AUTO_OFFLINE_FALLBACK,
            "sources": resolve_sources(settings),
            "backgrounds": list_backgrounds(),
            "discord": STATE.presence.status(),
        }

    def _serve_index(self):
        try:
            html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        except OSError:
            return self._fail(500, f"index.html не найден. Положи его сюда: {WEB_DIR / 'index.html'}")
        self._send(200, html.replace("__TOKEN__", STATE.token).encode("utf-8"), "text/html; charset=utf-8")

    def _serve_audio(self, track_id: str):
        path = STATE.library.path_of(track_id)
        if path is None or not path.is_file():
            return self._fail(404, "track not found")
        size = path.stat().st_size
        content_type = CONTENT_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start, end, status = 0, size - 1, 200
        range_header = self.headers.get("Range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if match and (match.group(1) or match.group(2)):
                if match.group(1):
                    start = int(match.group(1))
                    end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
                else:  # последние N байт
                    start = max(0, size - int(match.group(2)))
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _serve_clip(self, track_id: str):
        path = _clip_path(track_id)
        if not path.is_file():
            return self._fail(404, "clip not found")
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        range_header = self.headers.get("Range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if match and (match.group(1) or match.group(2)):
                if match.group(1):
                    start = int(match.group(1))
                    end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
                else:
                    start = max(0, size - int(match.group(2)))
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _serve_cover(self, track_id: str):
        cover = STATE.library.cover_of(track_id)
        if cover is None:
            return self._fail(404, "no cover")
        data, mime = cover
        self._send(200, data, mime, {"Cache-Control": "private, max-age=3600"})

    def _serve_background(self, name: str):
        if not _FILE_RE.match(name) or Path(name).suffix.lower() not in IMAGE_EXTENSIONS:
            return self._fail(404, "not found")
        file = BG_DIR / name
        if not file.is_file():
            return self._fail(404, "not found")
        self._send(200, file.read_bytes(), CONTENT_TYPES[file.suffix.lower()], {"Cache-Control": "private, max-age=86400"})

    # --- POST ---
    def do_POST(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = unquote(parsed.path)
        if not self._host_ok():
            return self._fail(403, "bad host")
        if not self._token_ok(query):
            return self._fail(403, "bad token")
        STATE.last_seen = time.monotonic()
        try:
            if path == "/api/background":
                return self._upload_background(query)
            body = self._read_body(MAX_JSON_BYTES)
            if body is None:
                return self._fail(413, "body too large")
            try:
                data = json.loads(body.decode("utf-8")) if body else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._fail(400, "bad json")
            if not isinstance(data, dict):
                return self._fail(400, "bad json")
            return self._route_post(path, data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def _route_post(self, path: str, data: dict):
        if path in ("/api/chat/invite/parse", "/api/chat/invite/join", "/api/chat/invite/create"):
            from chat_service import decode_invite, encode_invite
            try:
                if path == "/api/chat/invite/create":
                    if _CHAT_SERVICE is None or not _CHAT_SERVICE.status()["hosting"]:
                        return self._json({"error": "Сначала создай и включи чат на этом компьютере"}, 409)
                    settings = STATE.settings.get()
                    code = encode_invite(data.get("url"), settings.get("chat_token"))
                    return self._json({"invite_code": code, "url": decode_invite(code)["server_url"]})
                invite = decode_invite(data.get("invite_code"))
                if path == "/api/chat/invite/parse":
                    return self._json(invite)
                from chat_server import clean_text
                nickname = clean_text(data.get("nickname"), 32, required=True)
                if len(nickname) < 2:
                    raise ValueError("Ник должен содержать от 2 до 32 символов")
                settings = STATE.settings.update({"chat_enabled": True, "chat_host_enabled": False,
                                                  "chat_nickname": nickname,
                                                  "chat_server_url": invite["server_url"],
                                                  "chat_token": invite["access_code"]})
                configure_chat(settings)
                return self._json({"settings": settings})
            except ValueError as error:
                return self._json({"error": str(error)}, 400)
        if path.startswith("/api/chat/"):
            if _CHAT_SERVICE is None:
                return self._json({"error": "Чат выключен"}, 403)
            status, payload = _CHAT_SERVICE.request("POST", path, data, session_token=self.headers.get("X-Chat-Session", ""))
            return self._json(payload, status)
        if path == "/api/update/download":
            try:
                return self._json(_start_update_download())
            except Exception as error:
                logger.warning("Не удалось начать загрузку обновления: %s", error)
                return self._fail(500, str(error)[:300])
        if path == "/api/settings/export":
            try:
                destination = Path.home() / "Downloads" / (APP_NAME + "-settings.json")
                destination.parent.mkdir(parents=True, exist_ok=True)
                payload = {"app": APP_NAME, "format": 1,
                           "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                           "settings": STATE.settings.get()}
                destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                return self._json({"ok": True, "path": str(destination)})
            except OSError as error:
                logger.warning("Не удалось сохранить настройки в Downloads: %s", error)
                return self._fail(500, "Не удалось сохранить файл в папку Downloads")
        if path == "/api/sample-playlist":
            if data.get("action") != "delete":
                return self._fail(400, "Неизвестное действие")
            sample_dir = MUSIC_DIR / "Добро и позитив"
            marker = sample_dir / ".last_frequency_sample_playlist"
            try:
                if sample_dir.is_symlink() or not marker.is_file():
                    return self._fail(409, "Эта папка не помечена как тестовый плейлист; удаление отменено.")
                if sample_dir.resolve().parent != MUSIC_DIR.resolve():
                    return self._fail(400, "Недопустимый путь тестового плейлиста")
                shutil.rmtree(sample_dir)
                STATE.library.scan_async(force=True)
                return self._json({"ok": True})
            except OSError as error:
                logger.warning("Не удалось удалить тестовый плейлист: %s", error)
                return self._fail(500, "Не удалось удалить тестовые треки")
        if path == "/api/custom-playlists":
            items = data.get("items")
            if not isinstance(items, list) or len(items) > 200:
                return self._fail(400, "Некорректный список плейлистов")
            try:
                updated_at = int(data.get("updated_at") or 0)
            except (TypeError, ValueError, OverflowError):
                return self._fail(400, "Некорректная версия списка плейлистов")
            with _CUSTOM_PLAYLISTS_LOCK:
                current = _read_custom_playlists()
                if updated_at < current["updated_at"]:
                    return self._json(current)
                if not _write_custom_playlists(updated_at, items):
                    return self._fail(500, "Не удалось сохранить плейлисты")
                return self._json(_read_custom_playlists())
        if path == "/api/play-history":
            items = data.get("items")
            if not isinstance(items, list) or len(items) > 2000:
                return self._fail(400, "Некорректная история прослушивания")
            with _PLAY_HISTORY_LOCK:
                merged = _merge_play_history(items)
                if not _write_play_history(merged):
                    return self._fail(500, "Не удалось сохранить историю прослушивания")
            return self._json({"items": merged})
        if path == "/api/remote-favorites":
            items = data.get("items")
            if not isinstance(items, list) or len(items) > 500:
                return self._fail(400, "Некорректный список избранных онлайн-треков")
            with _REMOTE_FAVORITES_LOCK:
                clean = _sanitize_remote_favorites(items)
                if not _write_remote_favorites(clean):
                    return self._fail(500, "Не удалось сохранить избранное")
                with STATE.last_now_lock:
                    if STATE.last_now and STATE.last_now.get("remote"):
                        STATE.last_now["favorite"] = any(item["id"] == STATE.last_now.get("id") for item in clean)
            return self._json({"items": clean})
        if path == "/api/window-action":
            action = str(data.get("action") or "")
            if action == "quit":
                quit_player()
                return self._json({"ok": True})
            if action == "position":
                return self._json(_native_window_position())
            if action == "move":
                return self._json({"ok": _move_native_window(data.get("x"), data.get("y"))})
            return self._json({"ok": _perform_window_action(action)})
        if path == "/api/global-search":
            try:
                return self._json(search_global(str(data.get("query") or ""),
                                                str(data.get("provider") or "all"),
                                                str(data.get("kind") or "track")))
            except Exception as error:
                logger.warning("Глобальный поиск завершился с ошибкой: %s", error)
                return self._fail(400, str(error)[:300])
        if path == "/api/radio":
            try:
                return self._json(load_radio_queue(str(data.get("url") or ""),
                                                   str(data.get("title") or ""),
                                                   str(data.get("artist") or ""), int(STATE.settings.get().get("radio_recommendation_count", 30)),
                                                   str(data.get("provider") or "all"),
                                                   exclude_remixes=STATE.settings.get().get("radio_exclude_remixes", True),
                                                   exclude_ai_slop=STATE.settings.get().get("radio_exclude_ai_slop", True)))
            except Exception as error:
                logger.info("Не удалось собрать радио-очередь: %s", type(error).__name__)
                return self._fail(400, str(error)[:300])
        if path == "/api/online/match-imported-track":
            try:
                return self._json(_resolve_imported_playlist_track(
                    str(data.get("title") or ""), str(data.get("artist") or ""), data.get("duration")))
            except Exception as error:
                logger.info("Не удалось подобрать источник для импортированного трека: %s", type(error).__name__)
                return self._fail(404, str(error)[:300])
        if path == "/api/lyrics":
            try:
                return self._json(lookup_lyrics(str(data.get("title") or ""),
                                                str(data.get("artist") or ""),
                                                str(data.get("album") or ""), data.get("duration")))
            except Exception as error:
                logger.info("Не удалось получить текст песни: %s", type(error).__name__)
                return self._json({"found": False, "message": "Сервис текста временно недоступен."})
        if path == "/api/global-playlist":
            try:
                return self._json(load_global_playlist(str(data.get("url") or "")))
            except Exception as error:
                logger.warning("Не удалось открыть онлайн-плейлист: %s", error)
                return self._fail(400, str(error)[:300])
        if path == "/api/online/resolve":
            try:
                return self._json(resolve_online_stream(str(data.get("url") or ""), str(data.get("provider") or "")))
            except Exception as error:
                if _log_online_failure_once(data.get("url")):
                    if isinstance(error, _OnlineStreamDrmError):
                        logger.info("SoundCloud не разрешил поток защищённого трека")
                    else:
                        logger.warning("Не удалось открыть онлайн-трек: %s", error)
                return self._fail(400, str(error)[:300])
        if path == "/api/clips/resolve":
            try:
                return self._json(resolve_background_clip(str(data.get("id") or ""),
                                                          str(data.get("title") or ""),
                                                          str(data.get("artist") or ""),
                                                          source_url=str(data.get("url") or "")))
            except Exception as error:
                logger.info("Не удалось получить клип: %s", type(error).__name__)
                return self._json({"found": False, "url": ""})
        if path == "/api/clips/download":
            try:
                return self._json(_start_clip_download(str(data.get("id") or ""),
                                                       str(data.get("title") or ""),
                                                       str(data.get("artist") or ""),
                                                       source_url=str(data.get("url") or "")))
            except Exception as error:
                return self._fail(400, str(error)[:300])
        if path in ("/api/download/search", "/api/download/inspect", "/api/download/start") and not downloads_enabled():
            return self._fail(403, download_block_reason())
        if path == "/api/download/search":
            try:
                return self._json({"results": search_remote(str(data.get("provider") or ""), str(data.get("query") or ""))})
            except Exception as error:
                logger.warning("Ошибка поиска загрузки: %s", error)
                return self._fail(400, str(error)[:300])
        if path == "/api/download/inspect":
            try:
                return self._json(inspect_remote_url(str(data.get("url") or "")))
            except Exception as error:
                return self._fail(400, str(error)[:300])
        if path == "/api/download/start":
            try:
                include_clip = bool(data.get("include_clip"))
                if include_clip and not _ACTIVE_CLIPS_ENABLED:
                    return self._fail(403, "Докачка клипа выключена в настройках «Режимы».")
                destination_path = str(data.get("destination_path") or "")[:1000]
                destination_playlist_key = str(data.get("destination_playlist_key") or "")[:500]
                if destination_playlist_key:
                    with STATE.library.lock:
                        playlist_track = next((track for track in STATE.library.tracks.values()
                                               if track.get("pl") == destination_playlist_key), None)
                    if not playlist_track:
                        return self._fail(404, "Локальный плейлист не найден. Обнови библиотеку и попробуй снова.")
                    destination_path = str(Path(playlist_track["path"]).parent)
                return self._json(_start_download_job(
                    str(data.get("url") or ""), str(data.get("playlist") or "Загрузки"),
                    bool(data.get("is_playlist")), destination_path,
                    str(data.get("provider") or ""), str(data.get("playlist_title") or "")[:200],
                    include_clip=include_clip and not bool(data.get("is_playlist")),
                ))
            except Exception as error:
                logger.warning("Ошибка загрузки: %s", error)
                return self._fail(400, str(error)[:300])
        if path == "/api/download/cancel":
            try:
                return self._json(_cancel_download_job(str(data.get("id") or "")))
            except ValueError as error:
                return self._fail(404, str(error))
        if path == "/api/playlist/export-zip":
            name = str(data.get("name") or "Last Frequency playlist").strip()[:80] or "Last Frequency playlist"
            ids = data.get("ids")
            if not isinstance(ids, list) or len(ids) > 10000:
                return self._fail(400, "Некорректный список треков")
            safe_name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip(" .") or "playlist"
            buffer = io.BytesIO()
            manifest_tracks = []
            used_names = set()
            with STATE.library.lock:
                track_map = dict(STATE.library.tracks)
            try:
                with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=4) as archive:
                    for index, track_id in enumerate(ids, 1):
                        track = track_map.get(str(track_id))
                        if not track:
                            continue
                        source = Path(track.get("path", ""))
                        if not source.is_file() or source.suffix.lower() not in AUDIO_EXTENSIONS:
                            continue
                        filename = re.sub(r'[\\/:*?"<>|]+', "_", source.name).strip(" .") or f"track{index}{source.suffix}"
                        archive_name = f"Tracks/{filename}"
                        if archive_name.casefold() in used_names:
                            file_path = Path(filename)
                            duplicate = 2
                            while True:
                                candidate = f"Tracks/{file_path.stem} ({duplicate}){file_path.suffix}"
                                if candidate.casefold() not in used_names:
                                    archive_name = candidate
                                    break
                                duplicate += 1
                        used_names.add(archive_name.casefold())
                        archive.write(source, archive_name)
                        manifest_tracks.append({"id": str(track_id), "title": track.get("title", ""),
                                                "artist": track.get("artist", ""), "file": archive_name,
                                                "source_playlist": track.get("playlist", "")})
                    manifest = {"format": "LastFrequencyPlaylist", "version": 1, "name": name,
                                "exportedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tracks": manifest_tracks,
                                "note": "Audio files are included in Tracks/. Re-importing this ZIP is not yet supported; import playlist.json after extracting it."}
                    archive.writestr("playlist.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
                    archive.writestr("README.txt", (f"{APP_NAME} playlist: {name}\n\n"
                        f"Audio tracks included: {len(manifest_tracks)}\n"
                        "playlist.json contains the playlist manifest.\n").encode("utf-8"))
            except (OSError, zipfile.BadZipFile) as error:
                logger.exception("Не удалось собрать ZIP плейлиста")
                return self._fail(500, f"Не удалось собрать ZIP: {error}")
            data_bytes = buffer.getvalue()
            encoded_filename = quote(f"{safe_name}.zip", safe="")
            ascii_filename = re.sub(r"[^A-Za-z0-9._-]+", "_", safe_name).strip("._") or "playlist"
            disposition = f"attachment; filename=\"{ascii_filename}.zip\"; filename*=UTF-8''{encoded_filename}"
            return self._send(200, data_bytes, "application/zip", {
                "Content-Disposition": disposition,
                "Content-Length": str(len(data_bytes)),
            })
        if path in ("/api/track/hide", "/api/track/delete"):
            try:
                track_id = str(data.get("id") or "")
                return self._json(_hide_library_track(track_id, str(data.get("playlist_key") or ""))
                                  if path.endswith("/hide") else _delete_library_track(track_id))
            except ValueError as error:
                return self._fail(400, str(error))
            except OSError:
                return self._fail(409, "Файл занят или недоступен. Останови трек и попробуй снова.")
        if path == "/api/track/trim":
            try:
                return self._json(_trim_local_track(str(data.get("track_id") or ""), data.get("start"), data.get("end")))
            except (ValueError, RuntimeError) as error:
                return self._fail(400, str(error)[:300])
            except Exception as error:
                logger.exception("Не удалось обрезать трек")
                return self._fail(500, str(error)[:300])
        if path == "/api/track/distribute":
            try:
                return self._json(_distribute_edited_track(
                    str(data.get("track_id") or ""),
                    str(data.get("target_playlist_key") or "")[:500],
                    str(data.get("mode") or "move")[:20],
                    str(data.get("replace_track_id") or "")[:100],
                ))
            except ValueError as error:
                return self._fail(400, str(error)[:300])
            except OSError as error:
                logger.warning("Не удалось переместить трек из Edited: %s", error)
                return self._fail(409, "Не удалось изменить файл. Закрой проигрываемый трек или проверь доступ к папке.")
            except Exception as error:
                logger.exception("Не удалось распределить трек из Edited")
                return self._fail(500, str(error)[:300])
        if path == "/api/settings":
            if "chat_token" in data:
                from chat_service import validate_access_code
                try:
                    validate_access_code(data["chat_token"], required=False)
                except ValueError as error:
                    return self._json({"error": str(error)}, 400)
            before = STATE.settings.get()
            settings = STATE.settings.update(data)
            if any(key.startswith("chat_") for key in data):
                configure_chat(settings)
            if _MINI_PLAYER is not None:
                _MINI_PLAYER.configure(settings)
            mode = settings.get("performance_mode", "eco_offline")
            was_enabled = before.get("mp3_downloader_enabled", False) and _ACTIVE_PERFORMANCE_MODE in DOWNLOAD_MODES
            can_download = _ACTIVE_PERFORMANCE_MODE in DOWNLOAD_MODES and settings.get("mp3_downloader_enabled", False)
            set_downloads_enabled(can_download)
            manage_download_idle_watchdog(can_download)
            if can_download and not was_enabled:
                settings = STATE.settings.update({"download_last_completed_at": 0})
            if was_enabled and not can_download:
                with DOWNLOAD_JOBS_LOCK:
                    for job in DOWNLOAD_JOBS.values():
                        if job.get("status") not in ("finished", "failed", "cancelled"):
                            job["cancel_event"].set()
            STATE.presence.configure(settings["discord"])
            mode_changed = mode != before.get("performance_mode")
            # Режим влияет на инициализацию визуальных компонентов и служб.
            # Сохраняем выбор, но переключаем их только после перезапуска.
            if mode_changed:
                logger.info("Режим работы изменён на %s; применение после перезапуска", mode)
            if settings.get("clips_enabled") != before.get("clips_enabled"):
                logger.info("Настройка клипов изменена; применение после перезапуска")
            if resolve_sources(settings) != resolve_sources(before):
                STATE.library.scan_async()
            return self._json({"settings": settings, "sources": resolve_sources(settings),
                               "backgrounds": list_backgrounds(), "discord": STATE.presence.status(),
                               "active_performance_mode": _ACTIVE_PERFORMANCE_MODE,
                               "active_clips_enabled": _ACTIVE_CLIPS_ENABLED,
                               "automatic_offline": _AUTO_OFFLINE_FALLBACK})
        if path == "/api/window-theme":
            theme = str(data.get("theme") or "midnight")
            accent = str(data.get("accent") or "")
            ok = set_windows_window_theme(theme, accent)
            return self._json({"ok": ok})
        if path == "/api/rescan":
            STATE.library.scan_async(force=True)
            return self._json({"ok": True})
        if path == "/api/mini-control":
            action, value = data.get("action"), data.get("value")
            if action == "favorite" and isinstance(value, str) and _ID_RE.fullmatch(value):
                settings = STATE.settings.get()
                favorites = list(settings.get("favorites") or [])
                if value in favorites:
                    favorites.remove(value)
                    added = False
                else:
                    favorites.append(value)
                    added = True
                STATE.settings.update({"favorites": favorites})
                with STATE.mini_commands_lock:
                    STATE.mini_commands.append({"action": "favorite", "value": value, "favorite": added})
                    STATE.mini_commands = STATE.mini_commands[-8:]
                return self._json({"ok": True, "favorite": added})
            if action == "favorite" and isinstance(value, str) and len(value) <= 200:
                with STATE.last_now_lock:
                    current = dict(STATE.last_now) if STATE.last_now and STATE.last_now.get("id") == value else None
                if not current or not current.get("remote"):
                    return self._json({"ok": False, "error": "Трек не найден"}, 404)
                with _REMOTE_FAVORITES_LOCK:
                    favorites = _read_remote_favorites()
                    existing = next((item for item in favorites if item["id"] == value), None)
                    added = existing is None
                    if added:
                        clean = _sanitize_remote_favorites([current])
                        if clean:
                            favorites = _sanitize_remote_favorites([*favorites, clean[0]])
                    else:
                        favorites = [item for item in favorites if item["id"] != value]
                    if not _write_remote_favorites(favorites):
                        return self._fail(500, "Не удалось сохранить избранное")
                with STATE.last_now_lock:
                    if STATE.last_now and STATE.last_now.get("id") == value:
                        STATE.last_now["favorite"] = added
                with STATE.mini_commands_lock:
                    STATE.mini_commands.append({"action": "favorite", "value": value, "favorite": added})
                    STATE.mini_commands = STATE.mini_commands[-8:]
                return self._json({"ok": True, "favorite": added})
            if action in ("toggle", "next", "previous", "shuffle", "repeat"):
                command = {"action": action, "value": 0}
            elif action == "seek":
                command = {"action": action, "value": _num(value, 0, 86400, 0)}
            elif action == "volume":
                command = {"action": action, "value": _num(value, 0, 1, 0.8)}
            elif action == "playlist" and isinstance(value, str) and len(value) <= 512:
                command = {"action": action, "value": value}
            elif action == "track" and isinstance(value, str) and _ID_RE.fullmatch(value):
                command = {"action": action, "value": value}
            else:
                return self._json({"ok": False, "error": "unknown mini-player command"}, 400)
            with STATE.mini_commands_lock:
                STATE.mini_commands.append(command)
                STATE.mini_commands = STATE.mini_commands[-8:]
            return self._json({"ok": True})
        if path == "/api/mini-library":
            raw = data.get("playlists")
            if not isinstance(raw, list):
                return self._fail(400, "bad playlist list")
            clean = []
            for item in raw[:300]:
                if not isinstance(item, dict):
                    continue
                key, name, kind = item.get("key"), item.get("name"), item.get("type")
                if not isinstance(key, str) or not key or len(key) > 512 or not isinstance(name, str) or kind not in ("pl", "custompl"):
                    continue
                ids = item.get("ids", [])
                if not isinstance(ids, list):
                    ids = []
                clean.append({"key": key, "name": name[:120], "type": kind,
                              "src": str(item.get("src") or "")[:40],
                              "ids": [track_id for track_id in ids[:5000] if isinstance(track_id, str) and _ID_RE.fullmatch(track_id)]})
            with STATE.mini_catalog_lock:
                if clean != STATE.mini_playlists:
                    STATE.mini_playlists = clean
                    STATE.mini_catalog_version += 1
            return self._json({"ok": True})
        if path == "/api/nowplaying":
            now = data.get("track") if isinstance(data.get("track"), dict) else None
            with STATE.last_now_lock:
                STATE.last_now = now
                STATE.last_now_at = time.monotonic()
            STATE.presence.update(now)
            return self._json({"ok": True, "discord": STATE.presence.status()})
        if path == "/api/discord/reconnect":
            STATE.presence.configure(STATE.settings.get()["discord"])
            STATE.presence.reconnect()
            return self._json({"ok": True})
        if path == "/api/browse":
            initial = str(data.get("initial") or MUSIC_DIR)
            with STATE.dialog_lock:
                try:
                    folder = pick_folder(initial)
                except Exception as error:  # tkinter может отсутствовать
                    logger.warning("Диалог выбора папки недоступен: %s", error)
                    return self._fail(501, "Окно выбора папки недоступно — впиши путь вручную.")
            return self._json({"path": os.path.normpath(folder) if folder else ""})
        if path == "/api/background/delete":
            name = data.get("name")
            if isinstance(name, str) and _FILE_RE.match(name) and (BG_DIR / name).is_file():
                try:
                    (BG_DIR / name).unlink()
                except OSError as error:
                    return self._fail(500, str(error))
                current = STATE.settings.get()
                patch = {}
                if current["background"]["image"] == name:
                    patch["background"] = {"image": "", "type": "theme"}
                remaining_dvd = [image for image in current.get("dvd_images", []) if image != name]
                if len(remaining_dvd) != len(current.get("dvd_images", [])):
                    patch.update({"dvd_images": remaining_dvd, "dvd_image": remaining_dvd[0] if remaining_dvd else "", "dvd_enabled": bool(remaining_dvd)})
                if patch:
                    STATE.settings.update(patch)
            return self._json({"backgrounds": list_backgrounds(), "settings": STATE.settings.get()})
        if path == "/api/shutdown":
            self._json({"ok": True})
            threading.Thread(target=shutdown_server, daemon=True).start()
            return
        if path == "/api/restart":
            self._json({"ok": True})
            threading.Thread(target=restart_player, name="player-restart", daemon=True).start()
            return
        self._fail(404, "not found")

    def _upload_background(self, query):
        name = (query.get("name") or ["background"])[0]
        extension = Path(name).suffix.lower()
        if extension not in IMAGE_EXTENSIONS:
            return self._fail(400, "Нужна картинка: jpg, png, webp или gif")
        body = self._read_body(MAX_BACKGROUND_BYTES)
        if body is None or not body:
            return self._fail(413, "Файл слишком большой (максимум 25 МБ)")
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(name).stem)[:40].strip("_") or "bg"
        file_name = f"{int(time.time())}_{stem}{extension}"
        BG_DIR.mkdir(exist_ok=True)
        (BG_DIR / file_name).write_bytes(body)
        self._json({"name": file_name, "backgrounds": list_backgrounds()})


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        # Стандартный http.server вызывает socket.getfqdn(), которому нужен кодек idna;
        # в собранном .exe он может отсутствовать. Сервер локальный, имя хоста не нужно.
        import socketserver
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port

    def handle_error(self, request, client_address):
        error_type = sys.exc_info()[0]
        if error_type and issubclass(error_type, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            logger.debug("Локальное окно закрыло HTTP-соединение")
            return
        super().handle_error(request, client_address)


# ======================================================================================
#  Запуск, выход и перезапуск
# ======================================================================================
_SHUTDOWN_LOCK = threading.Lock()
_SHUTDOWN_STARTED = False


def _arm_force_exit(seconds: float = FORCE_EXIT_SECONDS) -> None:
    """Страховка от «зависшего» выхода: если процесс не закрылся сам, он завершается принудительно."""
    def force():
        logger.warning("Выход затянулся — завершаю процесс принудительно")
        _terminate_child_processes()
        logging.shutdown()
        os._exit(0)

    timer = threading.Timer(seconds, force)
    timer.daemon = True
    timer.start()


# Только эти процессы плеер завершает при выходе. Всё остальное, в том числе заново запущенная копия плеера
# при перезапуске (она тоже дочерняя) и окно браузера пользователя, остаётся нетронутым.
_OWN_CHILD_PROCESSES = frozenset({"msedgewebview2.exe", "ffmpeg.exe", "ffprobe.exe"})


def _select_own_children(children: dict, me: int, accept=None) -> list:
    """Выбирает, какие процессы завершить при выходе.

    children: родитель -> [(pid, имя файла в нижнем регистре)]. Идём вниз по дереву только через процессы
    из _OWN_CHILD_PROCESSES, так что до потомков заново запущенной копии плеера дело не доходит.
    accept(pid) — дополнительная проверка (процесс существует и не старше нас).
    """
    victims, queue, seen = [], list(children.get(me, [])), {me}
    while queue:
        pid, name = queue.pop()
        if pid in seen or name not in _OWN_CHILD_PROCESSES:
            continue
        seen.add(pid)
        if accept is not None and not accept(pid):
            continue
        victims.append(pid)
        queue.extend(children.get(pid, []))
    return victims


def _terminate_child_processes() -> None:
    """Завершает WebView2 и ffmpeg, которые запустил плеер: после выхода их быть не должно (Windows).

    Идём вниз по дереву только через процессы из _OWN_CHILD_PROCESSES, поэтому до потомков другой копии плеера дело
    не доходит. Дочерним считается процесс с нашим PID в роли родителя и временем создания не раньше нашего: это
    защищает от повторно использованных PID.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        class ProcessEntry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", wintypes.WCHAR * 260)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                                             ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME))
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE

        def created_at(handle):
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                            ctypes.byref(kernel), ctypes.byref(user)):
                return None
            return (created.dwHighDateTime << 32) | created.dwLowDateTime

        snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            return
        children: dict[int, list[tuple[int, str]]] = {}   # родитель -> [(pid, имя файла)]
        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(ProcessEntry)
            more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                children.setdefault(int(entry.th32ParentProcessID), []).append((int(entry.th32ProcessID), entry.szExeFile.lower()))
                more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)

        my_start = created_at(kernel32.GetCurrentProcess())
        handles = {}

        def accept(pid):
            handle = kernel32.OpenProcess(0x0001 | 0x1000, False, pid)  # TERMINATE | QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            started = created_at(handle)
            if my_start is not None and started is not None and started < my_start:
                kernel32.CloseHandle(handle)   # PID совпал случайно: этот процесс старше нас
                return False
            handles[pid] = handle
            return True

        victims = _select_own_children(children, os.getpid(), accept)
        for pid in victims:
            kernel32.TerminateProcess(handles[pid], 0)
        for handle in handles.values():
            kernel32.CloseHandle(handle)
        if victims:
            logger.info("Завершено дочерних процессов: %s", len(victims))
    except Exception:
        logger.debug("Не удалось завершить дочерние процессы", exc_info=True)


def shutdown_server(close_window=True):
    """Останавливает все службы плеера. Безопасно вызывать повторно: работа выполняется один раз."""
    global _EXIT_REQUESTED, _TRAY_ICON, _SHUTDOWN_STARTED
    _EXIT_REQUESTED = True
    with _SHUTDOWN_LOCK:
        first_call, _SHUTDOWN_STARTED = not _SHUTDOWN_STARTED, True
    if first_call:
        logger.info("Плеер полностью закрывается")
        _arm_force_exit()
        configure_chat({"chat_enabled": False})
        stop_global_hotkeys()
        try:
            STATE.library.stop_watching()
        except Exception:
            logger.debug("Не удалось остановить наблюдатель библиотеки", exc_info=True)
        try:
            set_downloads_enabled(False)
            manage_download_idle_watchdog(False)
        except Exception:
            logger.debug("Не удалось остановить фоновые задачи загрузчика", exc_info=True)
        tray = None
        try:
            with _TRAY_LOCK:
                tray, _TRAY_ICON = _TRAY_ICON, None
            if tray is not None:
                tray.stop()
        except Exception:
            logger.debug("Не удалось остановить значок в области уведомлений", exc_info=True)
        if _MINI_PLAYER is not None:
            try:
                _MINI_PLAYER.close()
            except Exception:
                logger.debug("Не удалось закрыть мини-плеер", exc_info=True)
        try:
            STATE.presence.shutdown()
        except Exception:
            logger.debug("Не удалось остановить статус Discord", exc_info=True)
        if STATE.server is not None:
            try:
                STATE.server.shutdown()
            except Exception:
                logger.debug("Не удалось остановить HTTP-сервер", exc_info=True)
    window = _NATIVE_WINDOW
    if close_window and window is not None:
        try:
            threading.Thread(target=window.destroy, name="native-window-close", daemon=True).start()
        except Exception:
            logger.debug("Не удалось закрыть встроенное окно", exc_info=True)


def _player_url():
    return f"http://127.0.0.1:{STATE.port}/" if STATE else f"http://127.0.0.1:{DEFAULT_PORT}/"


def _wait_for_process_exit(pid: int, limit: float = RESTART_WAIT_LIMIT) -> bool:
    """Ждёт закрытия процесса pid. Если за limit секунд он не закрылся, завершает его принудительно.

    Возвращает True, если процесс закрылся сам. Новый экземпляр не должен стартовать, пока старый держит
    mutex, порт и WebView2, но и висеть вечно из-за зависшей копии он тоже не должен.
    """
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
            kernel32.TerminateProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = kernel32.OpenProcess(0x00100000 | 0x0001, False, int(pid))  # SYNCHRONIZE | PROCESS_TERMINATE
            if not handle:
                return True   # процесса уже нет
            try:
                if kernel32.WaitForSingleObject(handle, int(limit * 1000)) == 0:   # WAIT_OBJECT_0
                    return True
                logger.warning("Прежний процесс %s не закрылся за %s с — завершаю его принудительно", pid, limit)
                kernel32.TerminateProcess(handle, 0)
                kernel32.WaitForSingleObject(handle, 5000)
                return False
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            logger.debug("Не удалось дождаться закрытия прежнего процесса", exc_info=True)
            time.sleep(min(limit, 3))
            return False
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        time.sleep(.1)
    return False


def _child_environment() -> dict:
    """Окружение для нового экземпляра .exe без служебных переменных PyInstaller.

    Если их оставить, onefile-загрузчик новой копии решит, что он дочерний процесс старой: возьмёт её
    временную папку _MEI… вместо собственной распаковки, а старая при выходе попытается эту папку удалить.
    Отсюда окно «Failed to remove temporary directory» и сломанный запуск.
    """
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("_PYI_") or key in ("_MEIPASS2", "_MEIPASS"):
            env.pop(key, None)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def restart_player():
    """Запускает новую копию плеера и закрывает эту. Новая копия ждёт выхода старой (--restart-after)."""
    command = [sys.executable] if FROZEN else [sys.executable, str(Path(__file__).resolve())]
    command += ["--restart-after", str(os.getpid())]
    # Рабочая папка нового процесса не должна лежать внутри временной папки _MEI… этого процесса:
    # открытая рабочая папка не даёт загрузчику PyInstaller удалить её при выходе.
    options = {"cwd": str(PLAYER_DIR), "env": _child_environment(), "stdin": subprocess.DEVNULL,
               "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        options["start_new_session"] = True
    try:
        if _NATIVE_WINDOW is not None:
            _NATIVE_WINDOW.hide()  # не оставляем на экране недозакрытое окно
    except Exception:
        pass
    try:
        subprocess.Popen(command, **options)
    except OSError:
        logger.exception("Не удалось запустить плеер повторно")
        return
    shutdown_server()


def show_player_window():
    """Показывает скрытое встроенное окно или открывает окно плеера."""
    global _MINI_BACKGROUND_REQUESTED
    _MINI_BACKGROUND_REQUESTED = False
    if _MINI_PLAYER is not None:
        _MINI_PLAYER.hide()
    window = _NATIVE_WINDOW
    if window is not None:
        try:
            window.show()
            window.restore()
            return
        except Exception:
            logger.debug("Не удалось показать встроенное окно", exc_info=True)
    hwnd = find_player_hwnd(include_hidden=True) if os.name == "nt" else None
    if hwnd:
        try:
            import ctypes
            user32 = ctypes.windll.user32
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return
        except Exception:
            logger.debug("Не удалось восстановить окно", exc_info=True)
    open_window(_player_url())


def _mini_get_track():
    if not STATE:
        return None
    with STATE.last_now_lock:
        track = dict(STATE.last_now) if STATE.last_now else None
        updated = STATE.last_now_at
    if not track:
        return None
    settings = STATE.settings.get()
    track["volume"] = float(settings.get("volume", 0.8))
    track["shuffle"] = bool(settings.get("shuffle", False))
    track["repeat"] = settings.get("repeat", "off")
    if track.get("remote"):
        track["favorite"] = bool(track.get("favorite"))
    else:
        track["favorite"] = bool(track.get("id") and track.get("id") in settings.get("favorites", []))
    if track.get("playing"):
        duration = float(track.get("duration") or 0)
        position = float(track.get("position") or 0) + max(0, time.monotonic() - updated)
        track["position"] = min(duration, position) if duration else position
    return track


def _mini_cover(track_id):
    cover = STATE.library.cover_of(track_id) if STATE and track_id else None
    return cover[0] if cover else None


def _mini_catalog():
    snapshot = STATE.library.snapshot()
    with STATE.mini_catalog_lock:
        playlists = list(STATE.mini_playlists)
        custom_version = STATE.mini_catalog_version
    if not playlists:
        playlists = [{**item, "type": "pl", "ids": []} for item in snapshot.get("playlists", [])]
    return {"version": f"{snapshot.get('version', 0)}:{custom_version}",
            "tracks": snapshot.get("tracks", []), "playlists": playlists}


def _mini_control(action, value):
    if STATE and STATE.server:
        window = _NATIVE_WINDOW
        if window is not None:
            try:
                command = json.dumps({"action": action, "value": value})
                result = window.evaluate_js(f"window.ecoMiniDispatch && window.ecoMiniDispatch({command})")
                if result is not False:
                    return
            except Exception:
                logger.debug("Мини-плеер: прямое управление недоступно, использую очередь", exc_info=True)
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{STATE.port}/api/mini-control?t={STATE.token}",
                data=json.dumps({"action": action, "value": value}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(request, timeout=1).close()
        except Exception:
            logger.debug("Мини-плеер: команда не доставлена", exc_info=True)


def _save_mini_position(position):
    if STATE:
        STATE.settings.update({"mini_player": position})


def _mini_visibility_watchdog():
    import ctypes
    while STATE and STATE.server:
        try:
            settings = STATE.settings.get()
            if not settings.get("mini_player", {}).get("enabled", True):
                _MINI_PLAYER.hide()
            else:
                hwnd = _NATIVE_HWND or find_player_hwnd(include_hidden=True)
                visible = bool(hwnd and ctypes.windll.user32.IsWindowVisible(hwnd))
                minimized = bool(hwnd and ctypes.windll.user32.IsIconic(hwnd))
                in_background = _MINI_BACKGROUND_REQUESTED and (not visible or minimized)
                if not in_background or not _mini_get_track():
                    _MINI_PLAYER.hide()
                elif not _MINI_PLAYER._visible:
                    _MINI_PLAYER.show()
        except Exception:
            logger.debug("Мини-плеер: проверка окна не удалась", exc_info=True)
        time.sleep(.6)


def _start_mini_player():
    global _MINI_PLAYER
    if os.name != "nt":
        return
    _MINI_PLAYER = FloatingMiniPlayer(lambda: STATE.settings.get(), _mini_get_track,
        _mini_cover, _mini_control, show_player_window, _save_mini_position, _mini_catalog)
    _MINI_PLAYER.configure(STATE.settings.get())
    threading.Thread(target=_mini_visibility_watchdog, name="mini-player-monitor", daemon=True).start()


def hide_player_window():
    global _MINI_BACKGROUND_REQUESTED
    _MINI_BACKGROUND_REQUESTED = False
    if _MINI_PLAYER is not None:
        _MINI_PLAYER.hide()
    window = _NATIVE_WINDOW
    if window is not None:
        try:
            window.hide()
            return
        except Exception:
            logger.debug("Не удалось скрыть встроенное окно", exc_info=True)
    hwnd = find_player_hwnd() if os.name == "nt" else None
    if hwnd:
        try:
            import ctypes
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
        except Exception:
            logger.debug("Не удалось скрыть окно", exc_info=True)


def quit_player():
    threading.Thread(target=shutdown_server, name="tray-quit", daemon=True).start()


def _native_window_position() -> dict:
    if os.name != "nt":
        return {"ok": False}
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = _NATIVE_HWND or find_player_hwnd(include_hidden=True)
        if not hwnd:
            return {"ok": False}
        # Resolve the visible top-level form when webview exposes an intermediate HWND.
        user32 = ctypes.windll.user32
        user32.IsWindow.argtypes = (wintypes.HWND,)
        user32.IsWindow.restype = wintypes.BOOL
        user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        user32.GetAncestor.restype = wintypes.HWND
        root_hwnd = user32.GetAncestor(hwnd, 2)  # GA_ROOT
        if root_hwnd and user32.IsWindow(root_hwnd):
            hwnd = root_hwnd
        rect = wintypes.RECT()
        get_rect = user32.GetWindowRect
        get_rect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        get_rect.restype = wintypes.BOOL
        if not get_rect(hwnd, ctypes.byref(rect)):
            return {"ok": False}
        is_zoomed = user32.IsZoomed
        is_zoomed.argtypes = (wintypes.HWND,)
        is_zoomed.restype = wintypes.BOOL
        return {"ok": True, "x": rect.left, "y": rect.top,
                "maximized": bool(is_zoomed(hwnd))}
    except Exception:
        logger.debug("Не удалось получить положение окна", exc_info=True)
        return {"ok": False}


def _move_native_window(x, y) -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        x, y = int(x), int(y)
        if not (-10000 <= x <= 10000 and -10000 <= y <= 10000):
            return False
        hwnd = _NATIVE_HWND or find_player_hwnd(include_hidden=True)
        if not hwnd:
            return False
        user32 = ctypes.windll.user32
        user32.IsWindow.argtypes = (wintypes.HWND,)
        user32.IsWindow.restype = wintypes.BOOL
        user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        user32.GetAncestor.restype = wintypes.HWND
        root_hwnd = user32.GetAncestor(hwnd, 2)  # GA_ROOT
        if root_hwnd and user32.IsWindow(root_hwnd):
            hwnd = root_hwnd
        is_zoomed = user32.IsZoomed
        is_zoomed.argtypes = (wintypes.HWND,)
        is_zoomed.restype = wintypes.BOOL
        if is_zoomed(hwnd):
            return False
        set_position = user32.SetWindowPos
        set_position.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, wintypes.UINT)
        set_position.restype = wintypes.BOOL
        # Explicit integer coordinates avoid pywebview.easy_drag's None-value bug.
        return bool(set_position(hwnd, 0, x, y, 0, 0, 0x0015))  # NOSIZE | NOZORDER | NOACTIVATE
    except Exception:
        logger.debug("Не удалось переместить окно", exc_info=True)
        return False

def _perform_window_action(action: str) -> bool:
    """Run custom title-bar actions, using Win32 for reliable native dragging."""
    global _NATIVE_IS_MAXIMIZED, _MINI_BACKGROUND_REQUESTED
    if action not in {"minimize", "maximize", "close", "drag"}:
        return False
    if action == "minimize":
        _MINI_BACKGROUND_REQUESTED = True
    window = _NATIVE_WINDOW
    if window is not None:
        try:
            if action == "minimize":
                window.minimize()
                return True
            if action == "maximize":
                if _NATIVE_IS_MAXIMIZED:
                    window.restore()
                else:
                    window.maximize()
                return True
            if action == "close":
                window.destroy()
                return True
            # Do not route drag through pywebview.easy_drag: its WinForms
            # backend passes None coordinates to SetWindowPos on this runtime.
            if action == "drag":
                raise RuntimeError("Use native Win32 caption dragging")
        except Exception:
            logger.debug("pywebview не выполнил действие заголовка окна", exc_info=True)
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.IsWindow.argtypes = (wintypes.HWND,)
        user32.IsWindow.restype = wintypes.BOOL
        user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        user32.GetAncestor.restype = wintypes.HWND
        user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.ShowWindow.restype = wintypes.BOOL
        user32.IsZoomed.argtypes = (wintypes.HWND,)
        user32.IsZoomed.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.PostMessageW.restype = wintypes.BOOL
        user32.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.SendMessageW.restype = wintypes.LRESULT
        user32.ReleaseCapture.restype = wintypes.BOOL
        hwnd = _NATIVE_HWND or find_player_hwnd(include_hidden=True)
        if not hwnd or not user32.IsWindow(hwnd):
            return False
        # The pywebview handle may be a child WebView/WinForms HWND. Native
        # caption dragging must target the visible top-level window.
        root_hwnd = user32.GetAncestor(hwnd, 2)  # GA_ROOT
        if root_hwnd and user32.IsWindow(root_hwnd):
            hwnd = root_hwnd
        if action == "minimize":
            user32.ShowWindow(hwnd, 6)
            return True
        if action == "maximize":
            user32.ShowWindow(hwnd, 9 if user32.IsZoomed(hwnd) else 3)
            return True
        if action == "close":
            return bool(user32.PostMessageW(hwnd, 0x0010, 0, 0))
        class POINT(ctypes.Structure):
            _fields_ = (("x", ctypes.c_long), ("y", ctypes.c_long))
        cursor = POINT()
        user32.GetCursorPos.argtypes = (ctypes.POINTER(POINT),)
        user32.GetCursorPos.restype = wintypes.BOOL
        if not user32.GetCursorPos(ctypes.byref(cursor)):
            return False
        # WM_NCLBUTTONDOWN requires the cursor's screen coordinates in lParam.
        # Passing zero makes the title-bar drag fail or jump to the screen origin.
        lparam = ((cursor.y & 0xFFFF) << 16) | (cursor.x & 0xFFFF)
        user32.ReleaseCapture()
        user32.SendMessageW(hwnd, 0x00A1, 2, lparam)  # WM_NCLBUTTONDOWN / HTCAPTION
        return True
    except Exception:
        logger.debug("Не удалось выполнить действие заголовка окна", exc_info=True)
        return False

def _apply_native_window_corners(hwnd=None, radius=8):
    """Use DWM's native rounding on Windows 11; use a rounded region on Windows 10."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.IsWindow.argtypes = (wintypes.HWND,)
        user32.IsWindow.restype = wintypes.BOOL
        user32.IsZoomed.argtypes = (wintypes.HWND,)
        user32.IsZoomed.restype = wintypes.BOOL
        hwnd = hwnd or _NATIVE_HWND
        if not hwnd or not user32.IsWindow(hwnd):
            return False
        user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        user32.GetAncestor.restype = wintypes.HWND
        root_hwnd = user32.GetAncestor(hwnd, 2)  # GA_ROOT
        if root_hwnd and user32.IsWindow(root_hwnd):
            hwnd = root_hwnd
        # Windows 11 cannot apply its native corners to a window with a custom region.
        # Liner's frameless Electron window relies on system/compositor rounding instead
        # of setting a region, so do the same here whenever the user has full opacity.
        build = sys.getwindowsversion().build
        if build >= 22000:
            dwm = ctypes.windll.dwmapi
            dwm.DwmSetWindowAttribute.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
            dwm.DwmSetWindowAttribute.restype = ctypes.c_long
            preference = wintypes.DWORD(2)  # DWMWCP_ROUND
            return dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(preference), ctypes.sizeof(preference)) == 0
        rect = wintypes.RECT()
        user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        user32.GetWindowRect.restype = wintypes.BOOL
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        width, height = rect.right - rect.left, rect.bottom - rect.top
        user32.SetWindowRgn.argtypes = (wintypes.HWND, wintypes.HRGN, wintypes.BOOL)
        user32.SetWindowRgn.restype = ctypes.c_int
        if user32.IsZoomed(hwnd):
            user32.SetWindowRgn(hwnd, None, True)
            return True
        # CreateRoundRectRgn is exported by GDI32, not USER32.
        gdi32 = ctypes.windll.gdi32
        create_region = gdi32.CreateRoundRectRgn
        create_region.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int)
        create_region.restype = wintypes.HRGN
        region = create_region(0, 0, width, height, radius * 2, radius * 2)
        applied = bool(region and user32.SetWindowRgn(hwnd, region, True))
        if not applied and region:
            gdi32.DeleteObject(region)
        if applied:
            user32.RedrawWindow(hwnd, None, None, 0x0485)
        return applied
    except Exception:
        logger.debug("Не удалось скруглить углы окна", exc_info=True)
        return False


def _set_windows_app_user_model_id():
    """Give the player and its WebView2 child processes one Windows app identity."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        setter = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        setter.argtypes = (ctypes.c_wchar_p,)
        setter.restype = ctypes.c_long
        return setter("LASTFREQUENCY.Player") == 0
    except Exception:
        logger.debug("Не удалось назначить Windows AppUserModelID", exc_info=True)
        return False


def run_native_window(url: str):
    """Start the player without the system titlebar; the page provides themed controls."""
    global _NATIVE_WINDOW, _NATIVE_HWND, _NATIVE_IS_MAXIMIZED
    _set_windows_app_user_model_id()
    import webview

    window = webview.create_window(
        APP_NAME, url, width=1240, height=800, min_size=(760, 520),
        frameless=True, easy_drag=False, confirm_close=False,
    )
    _NATIVE_WINDOW = window

    def on_maximized():
        global _NATIVE_IS_MAXIMIZED
        _NATIVE_IS_MAXIMIZED = True
        window.maximized = True
        _apply_native_window_corners(_NATIVE_HWND)

    def on_restored():
        global _NATIVE_IS_MAXIMIZED, _MINI_BACKGROUND_REQUESTED
        _NATIVE_IS_MAXIMIZED = False
        _MINI_BACKGROUND_REQUESTED = False
        if _MINI_PLAYER is not None:
            _MINI_PLAYER.hide()
        window.maximized = False
        _apply_native_window_corners(_NATIVE_HWND)

    def on_resized(*_args):
        _apply_native_window_corners(_NATIVE_HWND)

    def on_shown(*_args):
        # The first native bounds can be stale before WebView is laid out.
        threading.Timer(0.25, _apply_native_window_corners, args=(_NATIVE_HWND,)).start()
        threading.Timer(0.8, _apply_native_window_corners, args=(_NATIVE_HWND,)).start()

    window.events.maximized += on_maximized
    window.events.restored += on_restored
    window.events.resized += on_resized
    window.events.shown += on_shown

    def on_before_show():
        global _NATIVE_HWND
        try:
            handle = window.native.Handle
            _NATIVE_HWND = int(handle.ToInt64()) if hasattr(handle, "ToInt64") else int(handle)
            _apply_native_window_icon(_NATIVE_HWND)
            # Preserve native resize and maximize support while the titlebar is hidden.
            import ctypes
            user32 = ctypes.windll.user32
            from ctypes import wintypes
            get_style = user32.GetWindowLongW
            get_style.argtypes = (wintypes.HWND, ctypes.c_int)
            get_style.restype = wintypes.LONG
            set_style = user32.SetWindowLongW
            set_style.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.LONG)
            set_style.restype = wintypes.LONG
            set_position = user32.SetWindowPos
            set_position.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, wintypes.UINT)
            set_position.restype = wintypes.BOOL
            style = get_style(_NATIVE_HWND, -16)
            set_style(_NATIVE_HWND, -16, style | 0x00040000 | 0x00010000 | 0x00020000)
            set_position(_NATIVE_HWND, None, 0, 0, 0, 0, 0x0027)
            settings = STATE.settings.get()
            set_windows_window_theme(settings["theme"], settings.get("accent", ""), _NATIVE_HWND)
            rounded = _apply_native_window_corners(_NATIVE_HWND, 8)
            logger.info("Скругление главного окна: %s, HWND=%s, Windows build=%s",
                        "применено" if rounded else "не применено", _NATIVE_HWND,
                        getattr(sys.getwindowsversion(), "build", "unknown"))
        except Exception:
            logger.debug("Не удалось получить HWND встроенного окна", exc_info=True)

    def on_closing():
        global _EXIT_REQUESTED, _MINI_BACKGROUND_REQUESTED
        if _EXIT_REQUESTED or STATE.settings.get().get("autoclose", True):
            _EXIT_REQUESTED = True
            threading.Thread(target=shutdown_server, kwargs={"close_window": False},
                             name="window-close-shutdown", daemon=True).start()
            return True
        # Оставляем тот же WebView и его аудиопроигрыватель живыми; в трей уходит только окно.
        try:
            _MINI_BACKGROUND_REQUESTED = True
            window.hide()
            return False
        except Exception:
            logger.exception("Не удалось свернуть окно в фон")
            return True

    window.events.before_show += on_before_show
    window.events.closing += on_closing
    # WebView's default InPrivate profile deliberately hides MediaSession metadata
    # from Windows. Use this app's own persistent profile for system media controls.
    profile_dir = PLAYER_DIR / "webview-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    webview.start(debug=False, private_mode=False, storage_path=str(profile_dir))
    _NATIVE_WINDOW = None
    _NATIVE_HWND = None
    _NATIVE_IS_MAXIMIZED = False


def start_tray():
    """Иконка в области уведомлений; опциональная зависимость для сборки Windows."""
    global _TRAY_ICON
    try:
        import pystray
        from PIL import Image
        icon_path = RESOURCE_DIR / "icon.ico"
        image = Image.open(icon_path).convert("RGBA") if icon_path.is_file() else Image.new("RGBA", (64, 64), (110, 120, 255, 255))
        menu = pystray.Menu(
            pystray.MenuItem("Открыть плеер", lambda icon, item: show_player_window(), default=True),
            pystray.MenuItem("Скрыть окно", lambda icon, item: hide_player_window()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Полностью закрыть", lambda icon, item: quit_player()),
        )
        icon = pystray.Icon("LastFrequency", image, APP_NAME, menu)
        with _TRAY_LOCK:
            _TRAY_ICON = icon
        icon.run()
    except Exception as error:
        logger.warning("Иконка в области уведомлений недоступна: %s", error)
    finally:
        with _TRAY_LOCK:
            if _TRAY_ICON is locals().get("icon"):
                _TRAY_ICON = None


def _parse_hotkey(combo):
    """Преобразует ctrl+alt+o в Windows RegisterHotKey параметры."""
    import ctypes
    from ctypes import wintypes
    parts = combo.lower().split("+")
    key = parts[-1]
    modifiers = 0
    for part in parts[:-1]:
        modifiers |= {"alt": 0x0001, "ctrl": 0x0002, "shift": 0x0004, "win": 0x0008}.get(part, 0)
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif re.fullmatch(r"num[0-9]", key):
        vk = 0x60 + int(key[-1])
    elif re.fullmatch(r"f(?:[1-9]|1[0-9]|2[0-4])", key):
        vk = 0x70 + int(key[1:]) - 1
    else:
        vk = {"space": 0x20, "esc": 0x1B, "enter": 0x0D, "numadd": 0x6B,
              "numsubtract": 0x6D, "nummultiply": 0x6A, "numdivide": 0x6F,
              "numdecimal": 0x6E, "tab": 0x09, "backspace": 0x08,
              "delete": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
              "pageup": 0x21, "pagedown": 0x22, "arrowup": 0x26,
              "arrowdown": 0x28, "arrowleft": 0x25, "arrowright": 0x27}.get(key, 0)
    return modifiers, vk


_GLOBAL_HOTKEY_THREAD = None
_GLOBAL_HOTKEY_STOP = threading.Event()


def stop_global_hotkeys():
    _GLOBAL_HOTKEY_STOP.set()


def start_global_hotkeys():
    """Регистрирует изменяемые системные сочетания клавиш на Windows."""
    global _GLOBAL_HOTKEY_THREAD
    if os.name != "nt":
        return
    if _GLOBAL_HOTKEY_THREAD is not None and _GLOBAL_HOTKEY_THREAD.is_alive():
        _GLOBAL_HOTKEY_STOP.clear()
        return
    _GLOBAL_HOTKEY_STOP.clear()

    def worker():
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        actions = {1: "show", 2: "hide", 3: "quit"}
        registered = {}
        last_config = None
        message = wintypes.MSG()
        while not _GLOBAL_HOTKEY_STOP.is_set():
            settings = STATE.settings.get()
            if _ACTIVE_PERFORMANCE_MODE not in ("online", "offline"):
                for hotkey_id in list(registered):
                    user32.UnregisterHotKey(None, hotkey_id)
                registered.clear()
                last_config = None
                if _GLOBAL_HOTKEY_STOP.wait(1):
                    break
                continue
            config = tuple(settings.get("hotkeys", {}).get(name, "") for name in ("show", "hide", "quit"))
            if config != last_config:
                for hotkey_id in list(registered):
                    user32.UnregisterHotKey(None, hotkey_id)
                registered.clear()
                used = set()
                for hotkey_id, combo in zip(actions, config):
                    if not combo:
                        continue
                    try:
                        modifiers, vk = _parse_hotkey(combo)
                        pair = (modifiers, vk)
                        if vk and pair not in used and user32.RegisterHotKey(None, hotkey_id, modifiers, vk):
                            registered[hotkey_id] = pair
                            used.add(pair)
                        else:
                            logger.warning("Не удалось назначить глобальную клавишу %s", combo)
                    except Exception:
                        logger.warning("Некорректная глобальная клавиша: %s", combo)
                last_config = config
            while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                if message.message == 0x0312:
                    action = actions.get(int(message.wParam))
                    if action == "show": show_player_window()
                    elif action == "hide": hide_player_window()
                    elif action == "quit": quit_player()
            if _GLOBAL_HOTKEY_STOP.wait(0.3):
                break
        for hotkey_id in list(registered):
            user32.UnregisterHotKey(None, hotkey_id)

    _GLOBAL_HOTKEY_THREAD = threading.Thread(target=worker, name="global-hotkeys", daemon=True)
    _GLOBAL_HOTKEY_THREAD.start()


def open_window(url: str):
    """Open the player in a browser app window with the native Windows title bar."""
    state = globals().get("STATE")
    candidates = []
    for variable in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
            candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    for exe in candidates:
        if exe.exists():
            try:
                profile = PLAYER_DIR / ".window_profile"
                profile.mkdir(parents=True, exist_ok=True)
                subprocess.Popen([str(exe), f"--user-data-dir={profile}", "--no-first-run",
                                  "--no-default-browser-check", "--new-window", f"--app={url}",
                                  "--window-size=1240,800"])
                threading.Thread(target=apply_windows_theme_repeated,
                    args=((state.settings.get()["theme"] if state else "midnight"), (state.settings.get().get("accent", "") if state else "")),
                    daemon=True).start()
                return "browser"
            except OSError as error:
                logger.warning("Не смог открыть %s: %s", exe, error)
    webbrowser.open(url)
    return "browser"


def already_running(port: int) -> bool:
    """Only reuse a responding instance if it is this exact app version. Older copies may share the default port."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload.get("app") == APP_ID and payload.get("version") == VERSION
    except Exception:
        return False


def setup_logging():
    handlers = []
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    try:
        LOG_DIR.mkdir(exist_ok=True)
        handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
    except OSError:
        pass
    for handler in handlers:
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%d.%m %H:%M:%S"))
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)


def run_check():
    """python player\\player.py --check — печатает, что где лежит и что установлено."""
    print("=== " + APP_NAME + ": самопроверка ===")
    print("Python:", sys.version.split()[0], "|", sys.executable)
    print("player.py:", Path(__file__).resolve())
    print("Режим:", "собранный .exe" if FROZEN else "из исходников", "| данные:", PLAYER_DIR)
    print()
    places = [
        ("web/index.html", WEB_DIR / "index.html"),
        ("music", MUSIC_DIR),
        ("settings", SETTINGS_FILE),
    ]
    for name, path in places:
        print(("ЕСТЬ  " if path.exists() else "НЕТ   ") + f"{name}: {path}")
    bot_path = bot_tracks_path(SettingsStore().get())
    print(("ЕСТЬ  " if bot_path and Path(bot_path).is_dir() else "НЕТ   ") + f"папка треков бота: {bot_path or 'не найдена (укажи в настройках -> Треки)'}")
    print()
    for module in ("mutagen", "pypresence", "yt_dlp", "tkinter", "watchdog", "pystray", "webview"):
        try:
            imported = __import__(module)
            print("ЕСТЬ  ", module, getattr(imported, "__version__", ""))
        except Exception as error:  # noqa: BLE001
            print("НЕТ   ", module, "-", error)
    print()
    port = DEFAULT_PORT
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as response:
            print(f"Порт {port}: уже отвечает ->", response.read().decode("utf-8"))
            print("   (если здесь другой каталог, чем выше, это СТАРАЯ копия плеера: закрой её и запусти заново)")
    except Exception as error:  # noqa: BLE001
        print(f"Порт {port}: свободен или не отвечает ({error})")
    print()


def acquire_single_instance():
    """Keep a second launch from spawning another player/WebView process tree."""
    global _INSTANCE_MUTEX
    if os.name != "nt":
        return True
    try:
        import ctypes
        identity_path = Path(sys.executable if FROZEN else __file__).resolve().parent
        identity = hashlib.sha256(str(identity_path).casefold().encode("utf-8")).hexdigest()[:20]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, 1, f"Local\\LastFrequency-{identity}")
        if not handle:
            return True
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
            kernel32.CloseHandle(handle)
            return False
        _INSTANCE_MUTEX = handle
        return True
    except Exception:
        logger.debug("Не удалось проверить единственный экземпляр плеера", exc_info=True)
        return True


def _restart_settle():
    """Короткая пауза после выхода старого экземпляра и ожидание, пока освободится порт."""
    port = DEFAULT_PORT
    try:
        port = int(json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig")).get("port", port))
    except Exception:
        pass
    time.sleep(RESTART_SETTLE_SECONDS)
    import socket
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.3)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return
        time.sleep(0.3)


def main():
    global STATE, _ACTIVE_PERFORMANCE_MODE, _ACTIVE_CLIPS_ENABLED, _AUTO_OFFLINE_FALLBACK
    if "--check" in sys.argv:
        run_check()
        return
    setup_logging()
    if "--restart-after" in sys.argv:
        try:
            old_pid = int(sys.argv[sys.argv.index("--restart-after") + 1])
        except (ValueError, IndexError):
            old_pid = 0
        if old_pid and old_pid != os.getpid():
            logger.info("Перезапуск: жду закрытия прежнего процесса %s", old_pid)
            _wait_for_process_exit(old_pid)
            _restart_settle()   # старый экземпляр должен отпустить mutex, порт и WebView2
    if not acquire_single_instance():
        hwnd = find_player_hwnd(include_hidden=True) if os.name == "nt" else None
        if hwnd:
            try:
                import ctypes
                ctypes.windll.user32.ShowWindow(hwnd, 9)
                ctypes.windll.user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
        return
    for folder in (BG_DIR, MUSIC_DIR):
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            logger.warning("Не смог создать папку %s: %s", folder, error)

    STATE = AppState()
    settings = STATE.settings.get()
    configured_mode = settings.get("performance_mode", "eco_offline")
    _AUTO_OFFLINE_FALLBACK = configured_mode in ONLINE_MODES and not _internet_available()
    if _AUTO_OFFLINE_FALLBACK:
        _ACTIVE_PERFORMANCE_MODE = "eco_offline" if configured_mode == "eco_online" else "offline"
        logger.info("Интернет недоступен при запуске: временно включён оффлайн-режим %s", _ACTIVE_PERFORMANCE_MODE)
    else:
        _ACTIVE_PERFORMANCE_MODE = configured_mode
    _ACTIVE_CLIPS_ENABLED = bool(settings.get("clips_enabled", True))
    port = settings["port"]
    try:
        server = QuietThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        if already_running(port):
            logger.info("Плеер уже запущен — переключаюсь на существующее окно")
            hwnd = find_player_hwnd(include_hidden=True) if os.name == "nt" else None
            if hwnd:
                try:
                    import ctypes
                    ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
                except Exception:
                    logger.debug("Не удалось активировать существующее окно", exc_info=True)
            else:
                logger.info("Окно уже работает в фоне; второй экземпляр не создаю")
            return
        logger.warning("Порт %s занят старой версией плеера или другим процессом; запускаю %s %s на свободном порту", port, APP_NAME, VERSION)
        server = QuietThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
    server.daemon_threads = True
    STATE.server = server
    STATE.port = port
    configure_chat(settings)
    _start_mini_player()
    STATE.presence.configure(settings["discord"])
    global LAST_DOWNLOAD_COMPLETED
    with DOWNLOADS_LOCK:
        LAST_DOWNLOAD_COMPLETED = float(settings.get("download_last_completed_at", 0) or 0)
    can_download = _ACTIVE_PERFORMANCE_MODE in DOWNLOAD_MODES and settings.get("mp3_downloader_enabled", False)
    set_downloads_enabled(can_download)
    manage_download_idle_watchdog(can_download)
    logger.info("Загрузчик MP3: %s", "доступен (yt-dlp загрузится при первом использовании)" if can_download else "выключен, yt-dlp не загружается")
    threading.Thread(target=start_tray, name="system-tray", daemon=True).start()
    if _ACTIVE_PERFORMANCE_MODE in ("online", "offline"):
        start_global_hotkeys()
    if _ACTIVE_PERFORMANCE_MODE in ECO_MODES:
        STATE.library.load_cached_snapshot()
    else:
        STATE.library.scan_async(force=True)
    if _ACTIVE_PERFORMANCE_MODE in ("online", "offline"):
        STATE.library.start_watching()
    logger.info("═══ %s %s запущен: http://127.0.0.1:%s ═══", APP_NAME, VERSION, port)
    url = f"http://127.0.0.1:{port}/"
    server_thread = threading.Thread(target=server.serve_forever, name="http-server", daemon=True)
    server_thread.start()
    if os.name == "nt":
        try:
            run_native_window(url)
        except Exception:
            global _NATIVE_WINDOW, _NATIVE_HWND
            logger.exception("Встроенное окно недоступно; открываю окно браузера")
            _NATIVE_WINDOW = None
            _NATIVE_HWND = None
            open_window(url)
            server_thread.join()
    else:
        open_window(url)
        server_thread.join()
    if not _EXIT_REQUESTED:
        shutdown_server(close_window=False)
    try:
        server_thread.join(timeout=5)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            STATE.presence.shutdown()
        except Exception:
            pass
        server.server_close()
        _terminate_child_processes()


if __name__ == "__main__":
    main()
