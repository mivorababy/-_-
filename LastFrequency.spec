# -*- mode: python ; coding: utf-8 -*-
# Build description used by build_exe.ps1 (PyInstaller LastFrequency.spec).
# Пользовательские данные сохраняются рядом с установленным .exe; тестовый плейлист идёт в комплекте.
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [('web', 'web'), ('icon.ico', '.'), ('assets/sample-playlist', 'sample-playlist')]
binaries = [('ffmpeg/bin', 'ffmpeg/bin')]
hiddenimports = ['encodings.idna', 'mini_player', 'chat_service', 'chat_server', 'tkinter', 'tkinter.ttk', 'tkinter.filedialog', 'clr']
hiddenimports += collect_submodules('mutagen')
hiddenimports += collect_submodules('pypresence')
for package in ('yt_dlp', 'watchdog', 'pystray', 'PIL', 'webview', 'clr_loader'):
    package_datas, package_binaries, package_imports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_imports


a = Analysis(
    ['player.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['imageio_ffmpeg'],  # ffmpeg + ffprobe already included from ffmpeg/bin.
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='LastFrequency',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
