#!/usr/bin/env bash
# Deploy the CurlyOS Hermes plugin from its single source (hermes_integration/)
# into the live Hermes runtime. The repo is canonical; ~/.hermes/plugins/curlyos
# is a build artifact — never edit it directly.
#
# Usage:  bash deploy/install-hermes-plugin.sh
# After:  restart Hermes so it reloads the plugin.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)/hermes_integration"
DST="$HOME/.hermes/plugins/curlyos"
STAMP=$(date +%Y%m%d_%H%M%S)

[ -f "$SRC/plugin.py" ] || { echo "ERROR: $SRC/plugin.py missing"; exit 1; }
mkdir -p "$DST"

# keep one rolling backup of what was live
if [ -f "$DST/__init__.py" ]; then
    cp -f "$DST/__init__.py" "$DST/__init__.py.bak.$STAMP"
fi

cp -f "$SRC/plugin.py"          "$DST/__init__.py"
cp -f "$SRC/_import_helper.py"  "$DST/_import_helper.py"
cp -f "$SRC/plugin.yaml"        "$DST/plugin.yaml"
rm -rf "$DST/__pycache__"

# prune old backups beyond the 3 most recent
ls -t "$DST"/__init__.py.bak.* 2>/dev/null | tail -n +4 | xargs -r rm -f

echo "Installed hermes_integration → $DST (backup: __init__.py.bak.$STAMP)"
echo "Restart Hermes to load it."
