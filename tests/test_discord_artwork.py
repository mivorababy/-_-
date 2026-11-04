"""Discord payload and artwork rendering checks without starting the player or RPC."""
import ast
import hashlib
import io
import logging
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from urllib.parse import urlparse

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
tree = ast.parse((ROOT / "player.py").read_text(encoding="utf-8"))
cover_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "CoverHost")
presence_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DiscordPresence")
cover_class.body = [node for node in cover_class.body if isinstance(node, ast.FunctionDef) and node.name in ("_prepare", "resolve_remote")]
activity = next(node for node in presence_class.body if isinstance(node, ast.FunctionDef) and node.name == "_activity")
scope = {"io": io, "hashlib": hashlib, "time": time, "urlparse": urlparse, "urllib": urllib,
         "logger": logging.getLogger(__name__), "NO_COVER": "", "COVER_USER_AGENT": "LF-test"}
exec(compile(ast.Module(body=[cover_class, activity], type_ignores=[]), str(ROOT / "player.py"), "exec"), scope)


def encoded(image, format="PNG"):
    buffer = io.BytesIO()
    image.save(buffer, format)
    return buffer.getvalue()


class DiscordArtworkTests(unittest.TestCase):
    def test_artist_row_has_no_duplicate_provider(self):
        presence = SimpleNamespace(_now={"id": "song", "title": "Mind Brand", "artist": "MARETU",
                                         "source": "YouTube Music", "playing": True},
                                   _show_elapsed=False, _show_cover=True, _covers={"song": "https://example.com/art.jpg"},
                                   _large_image="", _show_artist_image=False, _show_link=False)
        activity = scope["_activity"](presence)
        self.assertEqual(activity["details"], "Mind Brand")
        self.assertEqual(activity["state"], "MARETU")
        self.assertEqual(activity["large_text"], "YouTube Music")
        presence._now["playing"] = False
        self.assertEqual(scope["_activity"](presence)["state"], "⏸ MARETU")

    def test_wide_and_tall_artwork_is_centered_square(self):
        for size in ((900, 600), (600, 900)):
            image = Image.new("RGB", size, "red")
            x, y = (size[0] - 600) // 2, (size[1] - 600) // 2
            ImageDraw.Draw(image).rectangle((x, y, x + 599, y + 599), fill="lime")
            data, mime = scope["CoverHost"]._prepare(encoded(image), "image/png")
            result = Image.open(io.BytesIO(data))
            self.assertEqual(mime, "image/jpeg")
            self.assertEqual(result.size, (512, 512))
            self.assertGreater(result.getpixel((2, 2))[1], 245)
            self.assertLess(result.getpixel((2, 2))[0], 10)

    def test_youtube_hq_preview_drops_embedded_letterbox(self):
        image = Image.new("RGB", (480, 360), "black")
        ImageDraw.Draw(image).rectangle((0, 45, 479, 314), fill="lime")
        data, _ = scope["CoverHost"]._prepare(encoded(image), "image/png", youtube_thumbnail=True)
        result = Image.open(io.BytesIO(data))
        self.assertEqual(result.size, (270, 270))
        for point in ((0, 0), (269, 0), (0, 269), (269, 269)):
            self.assertGreater(result.getpixel(point)[1], 245)

    def test_remote_youtube_uses_new_rendered_hash_and_not_old_uncropped_cache(self):
        image = Image.new("RGB", (480, 360), "black")
        ImageDraw.Draw(image).rectangle((0, 45, 479, 314), fill="lime")
        original = encoded(image)
        legacy = io.BytesIO()
        image.save(legacy, "JPEG", quality=88)
        old_key = hashlib.sha1(legacy.getvalue()).hexdigest()
        host = scope["CoverHost"].__new__(scope["CoverHost"])
        host._lock = threading.Lock()
        host._cache = {old_key: {"url": "https://example.com/old-wide.jpg"}}
        host._alive = lambda entry: True
        host._save_cache = lambda: None
        uploaded = []
        host._upload = lambda data, mime: (uploaded.append(data) or ("https://example.com/new-square.jpg", "test", 3600))

        class Response:
            headers = {"Content-Type": "image/png"}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def geturl(self): return "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg"
            def read(self, limit): return original[:limit]

        with patch("urllib.request.urlopen", return_value=Response()):
            result = host.resolve_remote("https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg")
        self.assertEqual(result, "https://example.com/new-square.jpg")
        self.assertEqual(len(uploaded), 1)
        self.assertEqual(Image.open(io.BytesIO(uploaded[0])).size, (270, 270))
        self.assertIn(hashlib.sha1(uploaded[0]).hexdigest(), host._cache)
        self.assertIn(old_key, host._cache)


if __name__ == "__main__":
    unittest.main()
