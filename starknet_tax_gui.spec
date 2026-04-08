# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for starknet-tax-gui.
# Build with:  pyinstaller starknet_tax_gui.spec
#

from PyInstaller.utils.hooks import collect_all, collect_data_files

# ── Collect reportlab (fonts, colour profiles, etc.) ────────────────────────
reportlab_datas, reportlab_binaries, reportlab_hiddenimports = collect_all("reportlab")

block_cipher = None

a = Analysis(
    ["starknet_tax/gui.py"],
    pathex=["."],
    binaries=reportlab_binaries,
    datas=reportlab_datas,
    hiddenimports=[
        # PyCryptodome / pycryptodomex
        "Crypto",
        "Crypto.Hash",
        "Crypto.Hash.keccak",
        "Crypto.Hash.SHA256",
        "Crypto.Hash.SHA3_256",
        "Crypto.Cipher",
        "Crypto.Signature",
        "Crypto.PublicKey",
        # python-bidi (RTL text support for PDF)
        "bidi",
        "bidi.algorithm",
        # reportlab extras surfaced by collect_all
        *reportlab_hiddenimports,
        # starknet_tax submodules (needed so imports inside threads resolve)
        "starknet_tax",
        "starknet_tax.classifier",
        "starknet_tax.config",
        "starknet_tax.fetcher",
        "starknet_tax.fifo",
        "starknet_tax.form1399",
        "starknet_tax.pricing",
        "starknet_tax.report",
        "starknet_tax.tax",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="starknet-tax-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,      # no terminal window; all output goes to the GUI log panel
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    onefile=True,
)
