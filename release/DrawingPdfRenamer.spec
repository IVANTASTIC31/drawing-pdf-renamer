# -*- mode: python ; coding: utf-8 -*-
from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all


project_root = Path(SPECPATH).parent
source_root = project_root / "src"
model_root = Path(
    os.environ.get(
        "DRAWING_RENAMER_MODEL_CACHE",
        str(Path.home() / ".paddlex" / "official_models"),
    )
)
required_models = ("PP-OCRv6_small_det", "PP-OCRv6_small_rec")
missing_models = [name for name in required_models if not (model_root / name).is_dir()]
if missing_models:
    raise SystemExit(
        "缺少离线 OCR 模型："
        + ", ".join(missing_models)
        + f"。期望目录：{model_root}"
    )

datas = []
for model_name in required_models:
    model_path = model_root / model_name
    for source_file in model_path.rglob("*"):
        if not source_file.is_file():
            continue
        relative_parent = source_file.parent.relative_to(model_path)
        destination = (
            Path("paddlex_cache")
            / "official_models"
            / model_name
            / relative_parent
        )
        datas.append((str(source_file), str(destination)))
datas.append((str(project_root / "assets" / "app-logo.png"), "assets"))
binaries = []
hiddenimports = []
for package in ("paddle", "paddleocr", "paddlex", "cv2", "bidi", "pypdfium2"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

a = Analysis(
    [str(project_root / "main.py")],
    pathex=[str(source_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "IPython",
        "jupyter",
        "notebook",
        "tkinter",
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DrawingPdfRenamer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(project_root / "assets" / "app-logo.ico"),
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="DrawingPdfRenamer",
)
