"""Brand migration: asset selection, downloaded filenames and detached updater scripts."""
import ast
import base64
import io
import logging
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
tree = ast.parse((ROOT / "player.py").read_text(encoding="utf-8"))
names = {"_update_asset_key", "_select_update_asset", "_is_setup_update", "_launch_update_asset_after_exit",
         "_start_update_download", "_update_download_snapshot"}
nodes = [node for node in tree.body if (isinstance(node, ast.FunctionDef) and node.name in names)
         or (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in
             ("APP_NAME", "APP_EXE_NAME", "APP_SETUP_NAME") for target in node.targets))]
scope = {"Path": Path, "re": re, "base64": base64, "shutil": shutil, "subprocess": subprocess,
         "secrets": secrets, "urllib": urllib, "unquote": unquote, "urlparse": urlparse,
         "VERSION": "1.32-last-frequency", "logger": logging.getLogger(__name__)}
exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / "player.py"), "exec"), scope)


class UpdaterBrandTests(unittest.TestCase):
    def assets(self, *names):
        return [{"name": name, "browser_download_url": "https://github.com/owner/repo/releases/download/v1.33/" + name} for name in names]

    def select(self, names, frozen=True, executable="NOT soundLARP 100%.exe"):
        selected = scope["_select_update_asset"](self.assets(*names), frozen, executable)
        return selected["name"] if selected else None

    def test_current_brand_prefers_new_raw_then_new_setup(self):
        names = ["LastFrequency.exe", "LastFrequency-Setup.exe", "NOT-soundLARP-100-Setup.exe", "NOT-soundLARP-100.exe"]
        self.assertEqual(self.select(names), "NOT-soundLARP-100.exe")
        self.assertEqual(self.select(names[:-1]), "NOT-soundLARP-100-Setup.exe")

    def test_legacy_process_requires_setup_to_migrate_name_and_shortcuts(self):
        names = ["LastFrequency-Setup.exe", "NOT-soundLARP-100.exe", "NOT-soundLARP-100-Setup.exe"]
        self.assertEqual(self.select(names, executable="LastFrequency.exe"), "NOT-soundLARP-100-Setup.exe")
        self.assertEqual(self.select(names[:2], executable="LastFrequency.exe"), "LastFrequency-Setup.exe")
        self.assertIsNone(self.select(["NOT-soundLARP-100.exe"], executable="LastFrequency.exe"))

    def test_source_mode_and_legacy_release_assets(self):
        self.assertEqual(self.select(["NOT-soundLARP-100.exe", "NOT-soundLARP-100-Setup.exe"], frozen=False), "NOT-soundLARP-100-Setup.exe")
        self.assertEqual(self.select(["LastFrequency.exe"]), "LastFrequency.exe")
        self.assertEqual(self.select(["LastFrequency.exe", "LastFrequency-Setup.exe"]), "LastFrequency-Setup.exe")

    def test_github_sanitized_and_full_brand_names_are_supported(self):
        self.assertEqual(self.select(["NOT.soundLARP.100.-Setup.exe"]), "NOT.soundLARP.100.-Setup.exe")
        self.assertEqual(self.select(["NOT soundLARP 100%.exe"]), "NOT soundLARP 100%.exe")
        self.assertIsNone(self.select(["OtherPlayer.exe", "NOT-soundLARP-100.exe.zip", "NOT-soundLARP-100-malware.exe"]))

    def update_script(self, filename, frozen=True):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "Player's 100% folder"
            scope.update(PLAYER_DIR=directory, FROZEN=frozen,
                         sys=SimpleNamespace(executable=str(directory / scope["APP_EXE_NAME"])),
                         os=SimpleNamespace(name="nt", environ={"WINDIR": folder}))
            with patch.object(Path, "is_file", return_value=True), patch.object(subprocess, "Popen") as start:
                scope["_launch_update_asset_after_exit"](directory / "updates" / filename, 1234)
            command = start.call_args.args[0]
            self.assertIn("Hidden", command)
            script = base64.b64decode(command[-1]).decode("utf-16le")
            return script, directory

    def test_setup_script_preserves_install_directory_and_percent_literal(self):
        for filename in ("NOT soundLARP 100%-Setup-v1.33.exe", "LastFrequency-Setup-v1.33.exe", "NOT-soundLARP-100-Setup.exe"):
            script, directory = self.update_script(filename)
            self.assertIn("Get-Process -Id 1234", script)
            self.assertIn('/DIR=' + str(directory).replace("'", "''"), script)
            self.assertIn("100%", script)
            self.assertNotIn("$lfTarget=", script)

    def test_raw_script_keeps_canonical_name_and_rollback(self):
        script, directory = self.update_script("NOT soundLARP 100%-v1.33.exe")
        self.assertIn("NOT soundLARP 100%.exe", script)
        self.assertIn("Player''s 100% folder", script)
        self.assertIn(".previous", script)
        self.assertIn(".update-new", script)
        self.assertNotIn("LastFrequency.exe", script)
        source_script, _ = self.update_script("NOT soundLARP 100%-v1.33.exe", frozen=False)
        self.assertIn("NOT soundLARP 100%.exe", source_script)

    def test_real_download_worker_uses_full_brand_filename_for_new_and_legacy_assets(self):
        data = b"MZ" + b"\0" * (1024 * 1024)

        class Response(io.BytesIO):
            headers = {"Content-Length": str(len(data))}

        class ImmediateThread:
            def __init__(self, target, **kwargs): self.target = target
            def start(self): self.target()

        for asset, expected in (("NOT-soundLARP-100.exe", "NOT soundLARP 100%-v1.33.exe"),
                                ("NOT-soundLARP-100-Setup.exe", "NOT soundLARP 100%-Setup-v1.33.exe"),
                                ("LastFrequency-Setup.exe", "NOT soundLARP 100%-Setup-v1.33.exe")):
            with tempfile.TemporaryDirectory() as folder:
                scope.update(PLAYER_DIR=Path(folder), _UPDATE_DOWNLOAD_JOBS={}, _UPDATE_DOWNLOAD_JOBS_LOCK=threading.Lock(),
                             threading=SimpleNamespace(Thread=ImmediateThread), os=SimpleNamespace(name="posix", fsync=lambda fd: None),
                             github_update_status=lambda **kwargs: {"available": True, "latest": "v1.33",
                                 "download_url": "https://github.com/owner/repo/releases/download/v1.33/" + asset})
                with patch("urllib.request.urlopen", return_value=Response(data)):
                    result = scope["_start_update_download"]()
                self.assertEqual(result["status"], "finished", result)
                self.assertEqual(result["filename"], expected)
                self.assertEqual((Path(folder) / "updates" / expected).read_bytes(), data)


if __name__ == "__main__":
    unittest.main()
