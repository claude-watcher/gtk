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
# Uninstall:
#   ./install.sh --uninstall                                 # remove script + desktop entries (config kept)
#
# Env overrides (handy for piped / unattended installs):
#   CW_VERSION=v1.5.1   pin a version
#   CW_NO_AUTOSTART=1   skip the autostart entry (app-menu entry still installed)
#   CW_NO_START=1       don't launch the widget at the end

readonly REPO="claude-watcher/gtk"
readonly SCRIPT_NAME="claude-watcher-gtk.py"
readonly LOGO_NAME="claude-logo.svg"
# Replaced by the release workflow with the published tag. "__VERSION__" means
# the script is running outside a release (from source / raw checkout).
readonly DEFAULT_VERSION="__VERSION__"

readonly CYAN="\033[36m"; readonly GREEN="\033[32m"; readonly YELLOW="\033[33m"; readonly RESET="\033[0m"

# Install layout. The widget is a GUI app launched via its .desktop entry's
# absolute path, so it belongs in the XDG data dir — not on PATH. The logo
# stays in the config dir because the running widget reads it from there.
readonly INSTALL_DIR="$HOME/.local/share/claude-watcher"
readonly INSTALLED_BIN="$INSTALL_DIR/claude-watcher"
readonly CONFIG_DIR="$HOME/.config/claude-watcher"
readonly APP_DESKTOP="$HOME/.local/share/applications/claude-watcher.desktop"
readonly AUTOSTART_DESKTOP="$HOME/.config/autostart/claude-watcher.desktop"

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
UNINSTALL=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        -v|--version) VERSION="${2:?--version needs a value}"; shift 2 ;;
        --version=*)  VERSION="${1#*=}"; shift ;;
        --uninstall)  UNINSTALL=1; shift ;;
        -h|--help)    sed -n '3,20p' "$0" 2>/dev/null; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

# ── Process helpers ───────────────────────────────────────────────────────────
# arg_re builds an ERE that matches a path only as a *whole* argv token (bounded
# by whitespace or string ends), with regex metacharacters escaped. This is what
# keeps us from matching the prefix-sibling claude-watcher-tui, or misbehaving on
# a $HOME that contains regex-special characters.
arg_re() {
    local esc
    esc=$(printf '%s' "$1" | sed 's/[][\.*+?(){}|^$]/\\&/g')
    printf '(^|[[:space:]])%s([[:space:]]|$)' "$esc"
}

# stop_widget terminates any running widget launched from one of the given paths
# (SIGTERM, then SIGKILL after a ~1s grace period so a fresh instance does not
# race the old one on the tray socket / shared state). Returns 0 if it stopped
# something, 1 if nothing matched.
stop_widget() {
    local p pat _
    local -a pats=() pids
    for p in "$@"; do pats+=("$(arg_re "$p")"); done
    pat=$(IFS='|'; printf '%s' "${pats[*]}")
    mapfile -t pids < <(pgrep -f "$pat" 2>/dev/null || true)
    ((${#pids[@]})) || return 1
    kill "${pids[@]}" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
        sleep 0.2
        mapfile -t pids < <(pgrep -f "$pat" 2>/dev/null || true)
        ((${#pids[@]})) || return 0
    done
    kill -9 "${pids[@]}" 2>/dev/null || true
    return 0
}

# ── Uninstall ─────────────────────────────────────────────────────────────────
# Reverts every file the installer creates. User config (config.ini, logo) is
# deliberately preserved — purge it manually if desired.
uninstall() {
    echo -e "${CYAN}Uninstalling Claude Code Watcher (GTK)...${RESET}"
    # Stop the widget at the new path plus any legacy on-PATH locations.
    if stop_widget "$INSTALLED_BIN" "$HOME/bin/claude-watcher" "$HOME/.local/bin/claude-watcher"; then
        echo "  stopped running widget"
    fi
    local f
    # Includes legacy locations from installers that put the script on PATH.
    for f in "$AUTOSTART_DESKTOP" "$APP_DESKTOP" "$INSTALLED_BIN" \
             "$HOME/bin/claude-watcher" "$HOME/.local/bin/claude-watcher"; do
        if [[ -L "$f" || -e "$f" ]]; then
            rm -f "$f"
            echo "  removed $f"
        fi
    done
    rmdir "$INSTALL_DIR" 2>/dev/null && echo "  removed $INSTALL_DIR" || true
    echo -e "${GREEN}Done.${RESET} Config kept at ${CONFIG_DIR} (delete manually to purge)."
}

if [[ "$UNINSTALL" == 1 ]]; then
    uninstall
    exit 0
fi

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
# Migrate away from older locations that put the script on PATH.
rm -f "$HOME/bin/claude-watcher" "$HOME/.local/bin/claude-watcher"
mkdir -p "$INSTALL_DIR"
fetch "$SCRIPT_NAME" "$INSTALLED_BIN"
chmod +x "$INSTALLED_BIN"
echo "  $INSTALLED_BIN"

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

# ── 4. Desktop entry + autostart ──────────────────────────────────────────────
echo -e "${CYAN}[4/4] Desktop entry...${RESET}"
# App-menu launcher (always installed). Quote the script path — $HOME may
# contain spaces; $SYSPY (/usr/bin/python3) has none so it stays bare. Icon
# points at the logo the running widget already reads from the config dir.
mkdir -p "$(dirname "$APP_DESKTOP")"
cat > "$APP_DESKTOP" << DESK
[Desktop Entry]
Type=Application
Name=Claude Code Watcher
Comment=Monitor Claude Code sessions
Exec=$SYSPY "$INSTALLED_BIN"
Icon=$CONFIG_DIR/$LOGO_NAME
Hidden=false
NoDisplay=false
StartupNotify=false
Terminal=false
X-GNOME-Autostart-enabled=true
DESK
echo "  $APP_DESKTOP"
# Refresh the menu database so the launcher appears without a re-login (best
# effort — desktop environments also rescan on their own).
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$(dirname "$APP_DESKTOP")" >/dev/null 2>&1 || true
fi

# Autostart is a symlink to the app entry, so the two never drift apart.
if [[ -n "${CW_NO_AUTOSTART:-}" ]]; then
    echo "  autostart skipped (CW_NO_AUTOSTART)"
else
    mkdir -p "$(dirname "$AUTOSTART_DESKTOP")"
    ln -sf "$APP_DESKTOP" "$AUTOSTART_DESKTOP"
    echo "  $AUTOSTART_DESKTOP -> app entry"
fi

echo ""
if [[ -n "${CW_NO_START:-}" || -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
    echo -e "${GREEN}Done!${RESET} Launch from your app menu, or: $SYSPY \"$INSTALLED_BIN\" &"
else
    echo -e "${GREEN}Done! Starting...${RESET}"
    # Stop any prior instance first — the widget has no single-instance guard,
    # so an upgrade would otherwise leave two widgets running at once.
    stop_widget "$INSTALLED_BIN" "$HOME/bin/claude-watcher" "$HOME/.local/bin/claude-watcher" \
        && echo "  stopped previous instance" || true
    # Launch through the .desktop so the desktop environment tracks the app
    # (window↔launcher association, icon). Fall back to a bare background run.
    if command -v gio >/dev/null 2>&1; then
        gio launch "$APP_DESKTOP" >/dev/null 2>&1 || true
    elif command -v gtk-launch >/dev/null 2>&1; then
        gtk-launch claude-watcher >/dev/null 2>&1 || true
    else
        nohup "$SYSPY" "$INSTALLED_BIN" >/dev/null 2>&1 &
    fi
    # A desktop launcher can fail silently (no D-Bus, partial session), so
    # confirm the process is actually up rather than printing a false success.
    sleep 0.5
    cwpid=$(pgrep -f "$(arg_re "$INSTALLED_BIN")" | head -n1 || true)
    if [[ -n "$cwpid" ]]; then
        echo "  started (PID $cwpid)"
    else
        echo -e "${YELLOW}  Warning: widget did not start — launch manually: $SYSPY \"$INSTALLED_BIN\"${RESET}" >&2
    fi
fi
