"""Exercise automatic cut replacement against real temporary playlist files."""
import ast
import logging
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
NAMES = {"_edited_source_track_id", "_select_edited_replacement", "_distribute_edited_track"}
tree = ast.parse((ROOT / "player.py").read_text(encoding="utf-8"))
functions = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in NAMES], type_ignores=[])
scope = {"Path": Path, "re": re, "shutil": shutil, "time": time, "logger": logging.getLogger(__name__)}
exec(compile(functions, str(ROOT / "player.py"), "exec"), scope)


class TrimReplacementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.music = Path(self.temp.name) / "music"
        self.edited = self.music / "Edited"
        self.playlist = self.music / "Playlist"
        self.edited.mkdir(parents=True)
        self.playlist.mkdir()
        self.tracks = {}
        self.scans = []
        scope.update(MUSIC_DIR=self.music, STATE=SimpleNamespace(library=SimpleNamespace(
            lock=threading.Lock(), tracks=self.tracks, scan_async=lambda **options: self.scans.append(options))))
        self.cut = self.track("cut", self.edited / "Song - cut 1730000000000.mp3", "edited", "Song", "Artist", b"cut audio")
        self.original = self.track("original", self.playlist / "Song.mp3", "target", "Song", "Artist", b"original audio")

    def tearDown(self):
        self.temp.cleanup()

    def track(self, track_id, path, playlist, title, artist, content=b"other audio"):
        path.write_bytes(content)
        track = {"id": track_id, "path": str(path), "pl": playlist, "playlist": "Playlist", "title": title, "artist": artist}
        self.tracks[track_id] = track
        return track

    def replace(self):
        return scope["_distribute_edited_track"]("cut", "target", "replace")

    def test_unique_title_replaces_and_keeps_original_backup(self):
        result = self.replace()
        self.assertEqual(result["action"], "replace")
        self.assertEqual((self.playlist / "Song.mp3").read_bytes(), b"cut audio")
        self.assertEqual((self.edited / result["backup_filename"]).read_bytes(), b"original audio")
        self.assertFalse(Path(self.cut["path"]).exists())
        self.assertEqual(len(self.scans), 1)

    def test_same_title_different_artist_selects_correct_original(self):
        other = self.track("other", self.playlist / "Other.mp3", "target", "Song", "Other artist")
        self.replace()
        self.assertEqual(Path(other["path"]).read_bytes(), b"other audio")
        self.assertEqual(Path(self.original["path"]).read_bytes(), b"cut audio")

    def test_duplicate_title_and_artist_changes_no_files(self):
        self.track("duplicate", self.playlist / "Duplicate.mp3", "target", "Song", "Artist")
        with self.assertRaisesRegex(ValueError, "несколько треков"):
            self.replace()
        self.assertEqual(Path(self.cut["path"]).read_bytes(), b"cut audio")
        self.assertEqual(Path(self.original["path"]).read_bytes(), b"original audio")
        self.assertFalse(self.scans)

    def test_no_matching_title_or_artist_changes_no_files(self):
        for field, value in (("title", "Different song"), ("artist", "Different artist")):
            original = self.cut[field]
            self.cut[field] = value
            with self.assertRaisesRegex(ValueError, "нет исходного трека"):
                self.replace()
            self.cut[field] = original
        self.assertEqual(Path(self.original["path"]).read_bytes(), b"original audio")
        self.assertEqual(Path(self.cut["path"]).read_bytes(), b"cut audio")

    def test_untagged_old_cut_matches_filename(self):
        self.cut["title"] = "Song - cut 1730000000000"
        self.cut["artist"] = "Неизвестный автор"
        self.original["title"] = "Song"
        self.replace()
        self.assertEqual(Path(self.original["path"]).read_bytes(), b"cut audio")

    def test_origin_id_disambiguates_duplicate_titles(self):
        duplicate = self.track("duplicate", self.playlist / "Duplicate.mp3", "target", "Song", "Artist")
        selected = scope["_select_edited_replacement"](self.cut, [self.original, duplicate], "original")
        self.assertIs(selected, self.original)

    def test_ffmpeg_provenance_is_readable_and_used_for_replacement(self):
        binary = ROOT / "ffmpeg" / "bin" / "ffmpeg.exe"
        if not binary.is_file():
            self.skipTest("Bundled FFmpeg unavailable")
        subprocess.run([str(binary), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", "anullsrc=r=44100:cl=mono", "-t", "0.2", "-metadata",
                        "LAST_FREQUENCY_SOURCE_ID=original", self.cut["path"]],
                       capture_output=True, check=True, timeout=20)
        self.assertEqual(scope["_edited_source_track_id"](Path(self.cut["path"])), "original")
        self.track("duplicate", self.playlist / "Duplicate.mp3", "target", "Song", "Artist")
        self.assertEqual(self.replace()["action"], "replace")


if __name__ == "__main__":
    unittest.main()
