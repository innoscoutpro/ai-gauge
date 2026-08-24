#!/usr/bin/env bash
# Build a standalone macOS .app or Linux folder using PyInstaller.
#
# Usage:
#   ./build.sh             # one-folder build (recommended)
#   ./build.sh --onefile   # single-file binary (slower first launch)
#
# Expects a virtualenv at .venv (created by `python -m venv .venv` and
# `.venv/bin/pip install -e .[dev]`).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$SCRIPT_DIR/.venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
    echo "Virtualenv not found at .venv. Run:" >&2
    echo "  python3 -m venv .venv" >&2
    echo "  .venv/bin/pip install -e .[dev]" >&2
    exit 1
fi

ONEFILE=0
for arg in "$@"; do
    case "$arg" in
        --onefile|--one-file|-OneFile) ONEFILE=1 ;;
        *) echo "Unknown argument: $arg" >&2; exit 2 ;;
    esac
done

"$VENV_PY" -m pip install --quiet pyinstaller

PYINSTALLER_ARGS=(
    -m PyInstaller
    --noconfirm
    --clean
    --windowed
    --noupx
    --name ai-gauge
    --paths src
    --collect-data aigauge
    --collect-all PyQt6.QtWebEngineWidgets
    --collect-all PyQt6.QtWebEngineCore
    pyinstaller_entry.py
)

if [ "$(uname -s)" = "Darwin" ]; then
    # Reverse-DNS bundle id; keeps Info.plist + LaunchServices happy.
    PYINSTALLER_ARGS+=(--osx-bundle-identifier org.aigauge.ai-gauge)
    PYINSTALLER_ARGS+=(--icon src/aigauge/assets/aigaugeicon.icns)
else
    PYINSTALLER_ARGS+=(--icon src/aigauge/assets/aigaugeicon.png)
fi

if [ "$ONEFILE" -eq 1 ]; then
    PYINSTALLER_ARGS+=(--onefile)
fi

"$VENV_PY" "${PYINSTALLER_ARGS[@]}"

"$VENV_PY" -m PyInstaller \
    --noconfirm \
    --clean \
    --console \
    --onefile \
    --noupx \
    --name ai-gauge-mcp \
    --paths src \
    pyinstaller_mcp_entry.py

# macOS ships the helper beside the .app because there is no folder bundle to
# put it in; every other layout nests it next to the GUI binary.
if [ "$(uname -s)" != "Darwin" ] && [ "$ONEFILE" -eq 0 ]; then
    mv dist/ai-gauge-mcp dist/ai-gauge/ai-gauge-mcp
    EXPECTED_MCP="dist/ai-gauge/ai-gauge-mcp"
else
    EXPECTED_MCP="dist/ai-gauge-mcp"
fi

# release.yml smoke-tests and packages the helper at a fixed path. Fail here,
# with the path named, rather than partway through a tag release.
if [ ! -f "$EXPECTED_MCP" ]; then
    echo "MCP helper missing at expected release path: $EXPECTED_MCP" >&2
    exit 1
fi

# On macOS, mark the bundle as a menu-bar-only agent so it doesn't show a
# Dock icon. Tradeoff: the floating-widget mode (off by default on Mac)
# also won't appear in Cmd-Tab while LSUIElement is set.
# PyInstaller mis-lays-out QtWebEngineCore.framework on macOS: it puts the
# Helpers directory and the WebEngine resource files at
# Versions/Resources/{Helpers,Resources} instead of Versions/A/{Helpers,Resources},
# so the top-level Helpers and Resources symlinks (which target
# Versions/Current/...) dangle and Qt can't find QtWebEngineProcess or
# icudtl.dat / *.pak.  Relocate them into the proper Versions/A layout.
if [ "$(uname -s)" = "Darwin" ] && [ "$ONEFILE" -eq 0 ]; then
    FWDIR="dist/ai-gauge.app/Contents/Frameworks/PyQt6/Qt6/lib/QtWebEngineCore.framework"
    STRAY="$FWDIR/Versions/Resources"
    if [ -d "$STRAY/Helpers" ] && [ ! -d "$FWDIR/Versions/A/Helpers" ]; then
        mv "$STRAY/Helpers" "$FWDIR/Versions/A/Helpers"
    fi
    if [ -d "$STRAY/Resources" ]; then
        cp -R "$STRAY/Resources/." "$FWDIR/Versions/A/Resources/"
    fi
    if [ -d "$STRAY" ]; then
        rm -rf "$STRAY"
    fi
fi

if [ "$(uname -s)" = "Darwin" ] && [ "$ONEFILE" -eq 0 ] \
        && [ -f "dist/ai-gauge.app/Contents/Info.plist" ]; then
    /usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" \
        dist/ai-gauge.app/Contents/Info.plist 2>/dev/null \
        || /usr/libexec/PlistBuddy -c "Set :LSUIElement true" \
            dist/ai-gauge.app/Contents/Info.plist
fi

# --collect-all on the WebEngine modules also drags in Chromium's debug
# resource packs, the DevTools front-end, and every Qt translation — ~140 MB
# the app never loads. Strip them before the folder is archived (issue #7).
# One-file builds are already packed by this point, so there is nothing to do.
if [ "$ONEFILE" -eq 0 ]; then
    if [ "$(uname -s)" = "Darwin" ]; then
        PRUNE_TARGET="dist/ai-gauge.app"
    else
        PRUNE_TARGET="dist/ai-gauge"
    fi
    echo
    echo "Pruning unused Qt/Chromium payload..."
    "$VENV_PY" "$SCRIPT_DIR/tools/prune_bundle.py" "$PRUNE_TARGET"
fi

echo
echo "Build complete."
case "$(uname -s)" in
    Darwin)
        if [ "$ONEFILE" -eq 1 ]; then
            echo "Binary: dist/ai-gauge"
        else
            echo "Bundle: dist/ai-gauge.app"
        fi
        echo "MCP helper: dist/ai-gauge-mcp"
        ;;
    *)
        if [ "$ONEFILE" -eq 1 ]; then
            echo "Binary: dist/ai-gauge"
        else
            echo "Folder: dist/ai-gauge/  (run ./ai-gauge inside)"
            echo "MCP helper: dist/ai-gauge/ai-gauge-mcp"
        fi
        ;;
esac
