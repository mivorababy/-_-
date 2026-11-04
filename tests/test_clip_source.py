"""Verify original-video selection without importing or starting the player."""
import ast
import logging
import re
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse


class ClipSourceTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / "player.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        names = {"_youtube_clip_source", "_is_audio_only_upload", "_find_accessible_youtube_video"}
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        self.extract = Mock()
        self.probe = Mock(return_value=True)
        ydl = Mock()
        ydl.__enter__ = Mock(return_value=ydl)
        ydl.__exit__ = Mock(return_value=False)
        ydl.extract_info = self.extract
        self.loader = Mock(return_value=Mock(YoutubeDL=Mock(return_value=ydl)))
        self.ns = {"re": re, "Path": Path, "urlparse": urlparse, "parse_qs": parse_qs,
                   "logger": logging.getLogger("clip-test"), "STATE": None,
                   "_yt_dlp_module": self.loader, "_probe_online_video_stream": self.probe}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), self.ns)
        self.source = "https://www.youtube.com/watch?v=abcdefghijk"

    def resolve(self, source=None):
        return self.ns["_find_accessible_youtube_video"]("Song music video", source_url=source or "")

    def test_missing_source_does_not_load_or_search(self):
        self.assertFalse(self.resolve()[0])
        self.loader.assert_not_called()

    def test_cover_sources_stop_without_search_or_stream_probe(self):
        for info in ({"musicVideoType": "MUSIC_VIDEO_TYPE_ATV"}, {"channel": "Artist - Topic"},
                     {"description": "Provided to YouTube by Label"}, {"title": "Song (Official Audio)"}):
            self.extract.reset_mock()
            self.extract.return_value = info
            self.assertFalse(self.resolve(self.source)[0])
            self.extract.assert_called_once_with(self.source, download=False)
        self.probe.assert_not_called()

    def test_real_video_keeps_original_source_even_with_thumbnail(self):
        self.extract.return_value = {"title": "Official Video", "thumbnail": "cover.jpg",
                                     "url": "https://media.googlevideo.com/video", "format_id": "137"}
        self.assertEqual(self.resolve(self.source)[:3], (self.source, "137", "https://media.googlevideo.com/video"))
        self.extract.assert_called_once_with(self.source, download=False)

    def test_unavailable_source_only_retries_that_source(self):
        self.extract.side_effect = RuntimeError("not accessible")
        self.assertFalse(self.resolve(self.source)[0])
        self.assertEqual(self.extract.call_count, 3)
        for call in self.extract.call_args_list:
            self.assertEqual(call.args[0], self.source)


if __name__ == "__main__":
    unittest.main()
