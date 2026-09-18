# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 单文件打包配置（[T-50]）。

产物名（[T-50] 步骤1，不可改名，与 [12.2] sh 脚本引用一致）:
  Windows -> mcmodsync.exe        Linux -> mcmodsync-linux
显式排除 A/B 端第三方依赖（boto3/botocore/paramiko/cryptography 及其传递依赖），
保证 C 端产物为纯标准库；同时排除常见体积/误杀来源（tkinter、测试框架等）。

构建:
  pyinstaller --clean --noconfirm packaging/mcmodsync.spec
（推荐直接用 tools/package_client.py 一步完成构建 + 组装 + 校验和）
"""
import os
import sys

REPO = os.path.abspath(os.path.join(SPECPATH, os.pardir))
ENTRY = os.path.join(REPO, "packaging", "entry_client.py")
NAME = "mcmodsync" if sys.platform.startswith("win") else "mcmodsync-linux"

EXCLUDES = [
    # [T-50] 显式排除的第三方依赖
    "boto3", "botocore", "s3transfer", "jmespath", "paramiko", "cryptography",
    "cffi", "pycparser", "nacl", "bcrypt", "requests", "urllib3", "charset_normalizer",
    "idna", "certifi", "dateutil", "yaml",
    # 体积与误杀来源（C 端不使用）
    "tkinter", "pytest", "setuptools", "pip", "pkg_resources", "numpy", "PIL",
    "unittest", "pydoc", "doctest", "distutils",
]

a = Analysis(
    [ENTRY],
    pathex=[REPO],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                       # 不使用 UPX（压缩壳是常见杀软误报来源，见 B-02）
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
