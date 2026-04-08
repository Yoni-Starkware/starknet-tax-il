#!/bin/bash
set -e
cd "$(dirname "$0")"
python3.9 -m pip install --break-system-packages -q FreeSimpleGUI pyinstaller
python3.9 -m PyInstaller starknet_tax_gui.spec
echo ""
echo "Built: dist/starknet-tax-gui  (share this single file with friends)"
