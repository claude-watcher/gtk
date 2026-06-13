#!/usr/bin/env bash
set -euo pipefail

# Claude Code Watcher (GTK) — installer
#
# Remote (recommended):
#   curl -fsSL https://github.com/claude-watcher/gtk/releases/latest/download/install.sh | bash
#   curl -fsSL https://github.com/claude-watcher/gtk/releases/download/v1.5.1/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --version v1.5.1   # explicit pin
#
# From a local clone:
#   ./install.sh                                              # installs the checked-out script
#
# Env overrides (handy for piped / unattended installs):
#   CW_VERSION=v1.5.1   pin a version
#   CW_NO_AUTOSTART=1   skip the GNOME autostart entry
#   CW_NO_START=1       don't launch the widget at the end

readonly REPO="claude-watcher/gtk"
readonly SCRIPT_NAME="claude-watcher-gtk.py"
readonly LOGO_NAME="claude-logo.svg"
# Replaced by the release workflow with the published tag. "__VERSION__" means
# the script is running outside a release (from source / raw checkout).
readonly DEFAULT_VERSION="__VERSION__"

readonly CYAN="\033[36m"; readonly GREEN="\033[32m"; readonly YELLOW="\033[33m"; readonly RESET="\033[0m"

# ── Local vs remote mode ──────────────────────────────────────────────────────
# Running from a clone (the sibling script exists) → install locally, no network.
# Piped through curl → download release assets.
SELF="${BASH_SOURCE[0]:-}"
LOCAL_DIR=""
if [[ -n "$SELF" && -f "$SELF" ]]; then
    LOCAL_DIR="$(cd "$(dirname "$SELF")" && pwd)"
fi
local_mode() { [[ -n "$LOCAL_DIR" && -f "$LOCAL_DIR/$SCRIPT_NAME" ]]; }

# ── Resolve the version to install ────────────────────────────────────────────
VERSION="${CW_VERSION:-}"
while [[ $# -gt 0 ]]; do
    case "$1" in
        -v|--version) VERSION="${2:?--version needs a value}"; shift 2 ;;
        --version=*)  VERSION="${1#*=}"; shift ;;
        -h|--help)    sed -n '3,20p' "$0" 2>/dev/null; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

resolve_latest() {
    local tag
    tag=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
          | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n1)
    [[ -n "$tag" ]] || { echo "Could not resolve the latest release of ${REPO}." >&2; exit 1; }
    printf '%s' "$tag"
}

if ! local_mode; then
    if [[ -z "$VERSION" ]]; then
        if [[ "$DEFAULT_VERSION" != "__VERSION__" ]]; then
            VERSION="$DEFAULT_VERSION"
        else
            VERSION="$(resolve_latest)"
        fi
    fi
fi
readonly BASE_URL="https://github.com/${REPO}/releases/download/${VERSION}"

# fetch <remote-filename> <destination>
fetch() {
    local name="$1" dest="$2"
    if local_mode; then
        cp "$LOCAL_DIR/$name" "$dest"
    else
        curl -fsSL "$BASE_URL/$name" -o "$dest"
    fi
}

echo -e "${CYAN}"
echo "╔══════════════════════════════════════╗"
echo "║   Claude Code Watcher GTK — Install  ║"
echo "╚══════════════════════════════════════╝"
echo -e "${RESET}"
if local_mode; then
    echo "  source: local clone ($LOCAL_DIR)"
else
    echo "  source: release ${VERSION}"
fi

# ── 1. Dependencies ───────────────────────────────────────────────────────────
echo -e "${CYAN}[1/4] Dependencies...${RESET}"

# Use the system python3 (/usr/bin) — Linuxbrew python3 does not have python3-gi
readonly SYSPY=/usr/bin/python3
PKGS=()
pycheck() { "$SYSPY" -c "$1" 2>/dev/null; }

pycheck "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk" \
    || PKGS+=(python3-gi gir1.2-gtk-3.0)
command -v wmctrl  >/dev/null 2>&1 || PKGS+=(wmctrl)
command -v xdotool >/dev/null 2>&1 || PKGS+=(xdotool)
pycheck "import gi; gi.require_version('Wnck','3.0')"          || PKGS+=(gir1.2-wnck-3.0)
pycheck "import gi; gi.require_version('AppIndicator3','0.1')" || PKGS+=(gir1.2-appindicator3-0.1)
pycheck "import gi; gi.require_version('Keybinder','3.0')"     || PKGS+=(gir1.2-keybinder-3.0)
if [[ -n "${WAYLAND_DISPLAY:-}" ]]; then
    pycheck "import gi; gi.require_version('GtkLayerShell','0.1')" || PKGS+=(libgtk-layer-shell-dev)
fi

if [[ ${#PKGS[@]} -gt 0 ]]; then
    echo "  Installing missing packages: ${PKGS[*]}"
    sudo apt install -y "${PKGS[@]}"
else
    echo "  All dependencies already installed"
fi

# ── 2. Script ─────────────────────────────────────────────────────────────────
echo -e "${CYAN}[2/4] Installing...${RESET}"
mkdir -p "$HOME/bin"
fetch "$SCRIPT_NAME" "$HOME/bin/claude-watcher"
chmod +x "$HOME/bin/claude-watcher"
echo "  ~/bin/claude-watcher"

# ── 3. Config ─────────────────────────────────────────────────────────────────
echo -e "${CYAN}[3/4] Config...${RESET}"
mkdir -p "$HOME/.config/claude-watcher"
fetch "$LOGO_NAME" "$HOME/.config/claude-watcher/$LOGO_NAME"
echo "  ~/.config/claude-watcher/$LOGO_NAME"
# Never clobber an existing config — a reinstall must preserve the user's
# screen/corner/hotkey choices (the widget rewrites this file on Apply).
if [[ -f "$HOME/.config/claude-watcher/config.ini" ]]; then
    echo "  ~/.config/claude-watcher/config.ini (existant, conservé)"
else
    cat > "$HOME/.config/claude-watcher/config.ini" << 'EOF'
[general]
# lang = fr   # fr | en — auto-détecté depuis la locale système si absent
# hotkey = <Ctrl><Alt>q

[display]
# screen     = 0             # monitor index (0=first, 1=second…)
# corner     = bottom-right  # bottom-right | bottom-left | top-right | top-left
# margin_x   = 20            # horizontal margin from corner in px
# margin_y   = 35            # vertical margin from corner in px
# width      = 320
# refresh_ms = 2000
# snooze_sec = 30            # snooze duration in seconds
# bg_alpha   = 88            # opacity in % (20-100)

[features]
# tray            = true
# shortcut_enable = true
EOF
    echo "  ~/.config/claude-watcher/config.ini"
fi

# ── 4. GNOME autostart ────────────────────────────────────────────────────────
echo -e "${CYAN}[4/4] Autostart...${RESET}"
if [[ -n "${CW_NO_AUTOSTART:-}" ]]; then
    echo "  skipped (CW_NO_AUTOSTART)"
else
    mkdir -p "$HOME/.config/autostart"
    cat > "$HOME/.config/autostart/claude-watcher.desktop" << DESK
[Desktop Entry]
Type=Application
Name=Claude Code Watcher
Exec=/usr/bin/python3 $HOME/bin/claude-watcher
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
Comment=Monitor Claude Code sessions
DESK
    echo "  ~/.config/autostart/claude-watcher.desktop"
fi

echo ""
if [[ -n "${CW_NO_START:-}" || -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
    echo -e "${GREEN}Done!${RESET} Launch with: /usr/bin/python3 ~/bin/claude-watcher &"
else
    echo -e "${GREEN}Done! Starting...${RESET}"
    /usr/bin/python3 "$HOME/bin/claude-watcher" &
    echo -e "  PID $!"
fi
