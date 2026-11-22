# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for FastETS (works on macOS and Windows).

    pyinstaller --noconfirm --clean packaging/FastETS.spec

The UI assets and a static ffmpeg binary are bundled so the produced app runs
without a Python installation and without ffmpeg on the PATH.
"""

import os
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent.resolve()
sys.path.insert(0, str(ROOT))


def read_version() -> str:
    """The version the package carries.

    It is written down in one place, in ``fastets/__init__.py``, and read
    from there.  A version written twice is a version that is wrong in
    one of the two places, and the wrong one is what the person sees
    in the app's about window.
    """
    import re

    text = (ROOT / "fastets" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(
        r"""^__version__\s*(?::\s*str)?\s*=\s*["']([^"']+)["']""",
        text,
        re.M,
    )
    if not match:
        raise SystemExit("[FastETS] fastets/__init__.py 里找不到 __version__")
    return match.group(1)


VERSION = read_version()
print(f"[FastETS] version {VERSION}")

datas = [(str(ROOT / "fastets" / "ui" / "web"), "fastets/ui/web")]

# Application icon: macOS wants an .icns, Windows an .ico.
ICON_DIR = ROOT / "packaging" / "icon"
ICNS = ICON_DIR / "FastETS.icns"
ICO = ICON_DIR / "FastETS.ico"
APP_ICON = str(ICNS) if sys.platform == "darwin" and ICNS.exists() else None
EXE_ICON = str(ICO) if sys.platform == "win32" and ICO.exists() else None

# Bundle a static ffmpeg so the app never depends on a system installation.
try:
    import imageio_ffmpeg

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    if Path(ffmpeg_exe).exists():
        datas.append((ffmpeg_exe, "fastets/bin"))
        print(f"[FastETS] bundling ffmpeg: {ffmpeg_exe}")
except Exception as exc:  # pragma: no cover - build environment dependent
    print(f"[FastETS] ffmpeg not bundled: {exc}")

# Bundle the Speex helper (ffmpeg 6+ has no libspeex encoder any more).
for helper_name in ("speexenc_raw", "speexenc_raw.exe"):
    helper = ROOT / "fastets" / "bin" / helper_name
    if helper.exists():
        datas.append((str(helper), "fastets/bin"))
        print(f"[FastETS] bundling speex helper: {helper}")
        break
else:
    print("[FastETS] speex helper missing - run packaging/native/build_speex.sh")

hiddenimports = [
    "webview",
    "requests",
    "urllib3",
    "pyzipper",
    "Crypto",
    "Cryptodome",
    "edge_tts",
]

block_cipher = None

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy.testing",
        "pytest",
        "IPython",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FastETS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=EXE_ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="FastETS",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="FastETS.app",
        icon=APP_ICON,
        bundle_identifier="cc.fastets.app",
        version=VERSION,
        info_plist={
            "CFBundleName": "FastETS",
            "CFBundleDisplayName": "FastETS",
            "LSMinimumSystemVersion": "10.15",
            "NSHighResolutionCapable": True,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
        },
    )
