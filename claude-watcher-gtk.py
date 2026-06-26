#!/usr/bin/env python3
"""
Claude Code Watcher — GTK3 desktop widget
Monitors running Claude Code sessions and lets you focus their terminal.

Config: ~/.config/claude-watcher/config.ini
Deps:   sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-wnck-3.0 gir1.2-appindicator3-0.1 wmctrl xdotool
Wayland: sudo apt install libgtk-layer-shell-dev
"""

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
try:
    gi.require_version('Wnck', '3.0')
    from gi.repository import Wnck
    HAS_WNCK = True
except ValueError:
    HAS_WNCK = False

try:
    gi.require_version('AppIndicator3', '0.1')
    from gi.repository import AppIndicator3
    HAS_APPINDICATOR = True
except ValueError:
    HAS_APPINDICATOR = False

try:
    gi.require_version('GtkLayerShell', '0.1')
    from gi.repository import GtkLayerShell
    HAS_LAYER_SHELL = True
except (ValueError, ImportError):
    HAS_LAYER_SHELL = False

try:
    gi.require_version('Keybinder', '3.0')
    from gi.repository import Keybinder
    HAS_KEYBINDER = True
except Exception:
    HAS_KEYBINDER = False

from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, Gio, Pango

import argparse
import cairo
import configparser
import math
import json
import os
import re
import subprocess
import signal
import sys
import threading
import time
import urllib.request
import warnings
from pathlib import Path

warnings.filterwarnings('ignore', category=DeprecationWarning, module='gi')

# ── Session type detection ────────────────────────────────────────────────────

IS_WAYLAND = (
    bool(os.environ.get("WAYLAND_DISPLAY"))
    and os.environ.get("GDK_BACKEND", "") != "x11"
)

# ── Config ────────────────────────────────────────────────────────────────────

def _detect_lang() -> str:
    import locale
    lang = os.environ.get('LANG') or os.environ.get('LANGUAGE') or locale.getlocale()[0] or ''
    return 'fr' if lang.lower().startswith('fr') else 'en'

CONFIG_DIR  = Path.home() / '.config' / 'claude-watcher'
CONFIG_PATH = CONFIG_DIR / 'config.ini'
POS_FILE    = CONFIG_DIR / 'position.json'

VERSION = "0.0.0"  # placeholder; release workflow stamps the git tag into this asset

# Update check — latest published release on GitHub
GITHUB_RELEASES_API = "https://api.github.com/repos/claude-watcher/gtk/releases/latest"
RELEASES_URL        = "https://github.com/claude-watcher/gtk/releases"

def _semver_tuple(s: str) -> tuple[int, ...]:
    """Loose semver → comparable int tuple. 'v1.2.3' → (1, 2, 3)."""
    parts = [int(n) for n in re.findall(r'\d+', s or '')][:3]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)

# Glyphe titre terminal émis par Claude Code (séquence OSC)
CLAUDE_IDLE_GLYPH = '✳'   # prompt visible, attend l'utilisateur

def _parse_bg_alpha(raw) -> int:
    # Clamp to the 20-100 range advertised by the settings UI; a non-numeric
    # manual edit falls back to the default instead of crashing at startup
    try:
        return max(20, min(100, int(raw)))
    except ValueError:
        return BG_ALPHA_DEFAULT


def load_config() -> dict:
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)

    d = cfg['display']  if 'display'  in cfg else {}
    g = cfg['general']  if 'general'  in cfg else {}
    f = cfg['features'] if 'features' in cfg else {}

    idle_fmt = d.get('idle_format', 'none').lower()

    return {
        'lang':       g.get('lang', _detect_lang()),
        'mode':       d.get('mode', 'corner'),
        'screen':     int(d.get('screen',     0)),
        'corner':     d.get('corner',     'bottom-right'),
        'margin_x':   int(d.get('margin_x',   20)),
        'margin_y':   int(d.get('margin_y',   35)),
        'width':      int(d.get('width',      320)),
        'auto_width': d.get('auto_width', 'false').lower() == 'true',
        # Multi-colonnes (1 par défaut) + plafond de hauteur (0/vide = pas de
        # limite propre, l'écran borne de toute façon). `int(x or N)` tolère la
        # clé absente (None) comme la valeur vide ('').
        'columns':    max(1, int(d.get('columns') or 1)),
        'max_height': max(0, int(d.get('max_height') or 0)),
        # Tri : 'default' (alpha) ou 'idle' (par ancienneté d'inactivité). Format
        # de la durée d'inactivité affichée : 'none' (off), 'loose' (~Xm), 'precise'.
        'sort_mode':  'idle' if d.get('sort_mode', 'default').lower() == 'idle' else 'default',
        'idle_format': idle_fmt if idle_fmt in ('none', 'loose', 'precise') else 'none',
        'show_topic': f.get('show_topic', 'true').lower() == 'true',
        'refresh_ms': int(d.get('refresh_ms', 2000)),
        'snooze_sec': int(d.get('snooze_sec', 30)),
        'bg_alpha':   _parse_bg_alpha(d.get('bg_alpha', BG_ALPHA_DEFAULT)),
        'tray':             f.get('tray',             'true').lower() == 'true',
        'shortcut_enable':  f.get('shortcut_enable',  'true').lower() == 'true',
        'hotkey':           g.get('hotkey', '<Ctrl><Alt>q').strip(),
    }


def parse_args(defaults: dict, argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Claude Code Watcher — widget GTK3 de suivi des sessions Claude.",
    )
    p.add_argument('--screen', type=int, default=defaults['screen'], metavar='N',
                   help=f"index du monitor (défaut {defaults['screen']}). Voir --list-screens.")
    p.add_argument('--corner', default=defaults['corner'],
                   choices=['bottom-right', 'bottom-left', 'top-right', 'top-left'],
                   help="coin d'ancrage (défaut bottom-right).")
    p.add_argument('--x', type=int, default=None, metavar='PX',
                   help='position X libre en px, relative au monitor (override --corner ; exige --y).')
    p.add_argument('--y', type=int, default=None, metavar='PX',
                   help='position Y libre en px, relative au monitor (exige --x).')
    p.add_argument('--margin-x', type=int, default=defaults['margin_x'], metavar='PX',
                   dest='margin_x', help=f"marge horizontale au coin (défaut {defaults['margin_x']}).")
    p.add_argument('--margin-y', type=int, default=defaults['margin_y'], metavar='PX',
                   dest='margin_y', help=f"marge verticale au coin (défaut {defaults['margin_y']}).")
    p.add_argument('--no-tray', dest='tray', action='store_false', default=defaults['tray'],
                   help="désactive l'icône systray.")
    p.add_argument('--list-screens', action='store_true',
                   help='liste les monitors détectés et quitte.')
    p.add_argument('--dump', action='store_true',
                   help="un tour de calcul d'état (registre vs JSONL vs final) en texte, puis quitte.")
    args = p.parse_args(argv)
    if (args.x is None) != (args.y is None):
        p.error('--x et --y doivent être fournis ensemble.')
    # Valeurs non overridables via CLI (viennent du config.ini uniquement)
    args.lang       = defaults['lang']
    args.mode       = defaults['mode']
    args.width      = defaults['width']
    args.auto_width = defaults['auto_width']
    args.columns    = defaults['columns']
    args.max_height = defaults['max_height']
    args.sort_mode   = defaults['sort_mode']
    args.idle_format = defaults['idle_format']
    args.show_topic = defaults['show_topic']
    args.refresh_ms = defaults['refresh_ms']
    args.snooze_sec        = defaults['snooze_sec']
    args.bg_alpha          = defaults['bg_alpha']
    args.hotkey            = defaults['hotkey']
    args.shortcut_enable   = defaults['shortcut_enable']
    return args


# Global config — peuplé dans main() après merge config.ini + CLI
CFG: argparse.Namespace = argparse.Namespace()

# ── i18n ──────────────────────────────────────────────────────────────────────

STRINGS = {
    'fr': {
        # widget principal
        'title':      'CLAUDE CODE',
        'waiting':    'attente',
        'working':    'travaille',
        'idle':       'inactif',
        'no_session': 'aucune session active',
        'attend':     'attend',
        'pid':        'pid',
        # systray
        'settings_menu': 'Paramètres…',
        'show':          'Afficher',
        'hide':          'Masquer',
        'snooze_wake':   'Réveiller',
        'snooze_hide':   'Masquer pendant',
        'about':         'À propos…',
        'quit':          'Quitter',
        # version / mise à jour
        'ver_uptodate':  'À jour',
        'ver_outdated':  'Mise à jour disponible',
        'ver_checking':  'Vérification de la version…',
        'ver_unknown':   'Version à jour inconnue (hors-ligne ?)',
        'ver_current':   'Version installée',
        'ver_click_hint':'Cliquer pour le détail',
        'ver_latest':    'Dernière version',
        'ver_status':    'Statut',
        'see_releases':  'Voir les releases',
        'update_cmd':    'Commande de mise à jour',
        'copy':          'Copier',
        'tab_about':     'À propos',
        'tab_version':   'Version',
        'tab_credits':   'Crédits',
        'authors':       'Auteurs',
        'close':         'Fermer',
        # dialogue paramètres
        'settings_title': 'Paramètres — Claude Code Watcher',
        'cancel':         'Annuler',
        'apply':          'Appliquer',
        'sec_lang':       'Langue',
        'sec_position':   'Position',
        'sec_display':    'Affichage',
        'sec_shortcut':   'Raccourci clavier',
        'fld_shortcut_enable': 'Activer le raccourci',
        'fld_hotkey':     'Raccourci',
        'hotkey_hint':    'ex. <Ctrl><Alt>q',
        'fld_lang':       'Langue',
        'fld_mode':       'Mode',
        'fld_screen':     'Écran',
        'fld_corner':     'Coin',
        'fld_margin_x':   'Marge X',
        'fld_margin_y':   'Marge Y',
        'fld_width':      'Largeur (max si auto)',
        'fld_auto_width': 'Largeur automatique',
        'fld_columns':    'Colonnes',
        'fld_max_height': 'Hauteur max',
        'help_max_height': ('0 = aucune limite : la hauteur du widget est de toute '
                            'façon toujours bornée par la taille de l’écran. '
                            'Une valeur > 0 plafonne en plus à ce nombre de pixels ; '
                            'au-delà, la liste des sessions défile.'),
        'fld_show_topic': 'Afficher le sujet de session',
        'fld_sort':        'Tri',
        'sort_default':    'Par défaut (projet)',
        'sort_idle':       'Par inactivité',
        'fld_idle_format': 'Durée d’inactivité',
        'idle_none':       'Masquée',
        'idle_loose':      'Approx. (~Xm)',
        'idle_precise':    'Précise (1d 02:24:23)',
        'fld_refresh':    'Rafraîch.',
        'fld_snooze':     'Veille',
        'fld_bg_alpha':   'Opacité',
        'btn_default':    'Défaut',
        'mode_corner':    'Ancrée au coin',
        'mode_free':      'Libre (drag)',
        'corner_br':      'Bas droite',
        'corner_bl':      'Bas gauche',
        'corner_tr':      'Haut droite',
        'corner_tl':      'Haut gauche',
        'lang_fr':        'Français',
        'lang_en':        'English',
        'monitor_idx':    'Moniteur',
        'monitor_primary':'principal',
    },
    'en': {
        # main widget
        'title':      'CLAUDE CODE',
        'waiting':    'waiting',
        'working':    'working',
        'idle':       'idle',
        'no_session': 'no active session',
        'attend':     'waiting',
        'pid':        'pid',
        # systray
        'settings_menu': 'Settings…',
        'show':          'Show',
        'hide':          'Hide',
        'snooze_wake':   'Wake up',
        'snooze_hide':   'Hide for',
        'about':         'About…',
        'quit':          'Quit',
        # version / update
        'ver_uptodate':  'Up to date',
        'ver_outdated':  'Update available',
        'ver_checking':  'Checking version…',
        'ver_unknown':   'Update status unknown (offline?)',
        'ver_current':   'Installed version',
        'ver_click_hint':'Click for details',
        'ver_latest':    'Latest version',
        'ver_status':    'Status',
        'see_releases':  'View releases',
        'update_cmd':    'Update command',
        'copy':          'Copy',
        'tab_about':     'About',
        'tab_version':   'Version',
        'tab_credits':   'Credits',
        'authors':       'Authors',
        'close':         'Close',
        # settings dialog
        'settings_title': 'Settings — Claude Code Watcher',
        'cancel':         'Cancel',
        'apply':          'Apply',
        'sec_lang':       'Language',
        'sec_position':   'Position',
        'sec_display':    'Display',
        'sec_shortcut':   'Keyboard shortcut',
        'fld_shortcut_enable': 'Enable shortcut',
        'fld_hotkey':     'Shortcut',
        'hotkey_hint':    'e.g. <Ctrl><Alt>q',
        'fld_lang':       'Language',
        'fld_mode':       'Mode',
        'fld_screen':     'Screen',
        'fld_corner':     'Corner',
        'fld_margin_x':   'Margin X',
        'fld_margin_y':   'Margin Y',
        'fld_width':      'Width (max if auto)',
        'fld_auto_width': 'Auto width',
        'fld_columns':    'Columns',
        'fld_max_height': 'Max height',
        'help_max_height': ('0 = no limit: the widget height is always capped by '
                            'the screen size anyway. A value > 0 additionally caps '
                            'it to that many pixels; beyond it, the session list scrolls.'),
        'fld_show_topic': 'Show session topic',
        'fld_sort':        'Sort',
        'sort_default':    'Default (project)',
        'sort_idle':       'By idle time',
        'fld_idle_format': 'Idle duration',
        'idle_none':       'Hidden',
        'idle_loose':      'Approx. (~Xm)',
        'idle_precise':    'Precise (1d 02:24:23)',
        'fld_refresh':    'Refresh',
        'fld_snooze':     'Snooze',
        'fld_bg_alpha':   'Opacity',
        'btn_default':    'Default',
        'mode_corner':    'Anchored to corner',
        'mode_free':      'Free (drag)',
        'corner_br':      'Bottom right',
        'corner_bl':      'Bottom left',
        'corner_tr':      'Top right',
        'corner_tl':      'Top left',
        'lang_fr':        'Français',
        'lang_en':        'English',
        'monitor_idx':    'Monitor',
        'monitor_primary':'primary',
    },
}

def tr(key: str) -> str:
    lang = getattr(CFG, 'lang', 'fr')
    return STRINGS.get(lang, STRINGS['fr']).get(key, key)

# ── Couleurs ──────────────────────────────────────────────────────────────────

BG_RGB           = (0.07, 0.07, 0.09)  # alpha comes from bg_alpha (config, %)
BG_ALPHA_DEFAULT = 88                  # default background opacity, in %
COL_SPACING      = 14                  # px gutter between columns (holds the vertical separator)
TEXT_PRIMARY  = "#e2e2e2"
TEXT_DIM      = "#55556a"
TEXT_DIM2     = "#888898"
COLOR_TITLE   = "#cc8a2e"
COLOR_WAITING = "#e86c3a"
COLOR_WORKING = "#d4a052"
COLOR_IDLE    = "#4caf7d"

# Alpha values for the waiting-dot pulse (6 ticks @ 600 ms ≈ 3.6 s cycle)
_PULSE_ALPHAS = [0.35, 0.6, 0.9, 1.0, 0.9, 0.6]
COLOR_SNOOZE  = "#5a7a9a"
COLOR_CLAUDE  = "#cc785c"   # Claude brand orange — marque les instances CLAUDE_CONFIG_DIR custom
COLOR_HOVER   = (1, 1, 1, 0.06)
COLOR_HOVER_W = (0.91, 0.42, 0.14, 0.10)
COLOR_KB_SEL  = (1, 1, 1, 0.14)
COLOR_VER_OK  = "#2e9e5b"   # dark green — installed version is the latest release
COLOR_VER_OLD = "#e0524f"   # red — a newer release is available

# ── Détection process ─────────────────────────────────────────────────────────

WAITING_WCHANS = {
    'ep_poll', 'poll_schedule_timeout', 'wait_woken',
    'n_tty_read', 'read_chan', 'do_select',
}

TERMINAL_NAMES = [
    'gnome-terminal', 'xterm', 'konsole', 'tilix',
    'terminator', 'alacritty', 'kitty', 'xfce4-terminal',
    'mate-terminal', 'lxterminal', 'st', 'urxvt',
    'ghostty', 'wezterm', 'foot', 'rio', 'hyper', 'tabby',
]

CLAUDE_PROJECTS_DIR = Path.home() / '.claude' / 'projects'

# Claude Code tient son propre registre de sessions (première partie), keyé par
# PID et mis à jour en temps réel : ~/.claude/sessions/<pid>.json. C'est la
# source d'état primaire. Le JSONL sert de fallback si le fichier est absent
# (session lancée par une version de Claude antérieure à ce mécanisme).
_SESSIONS_DIR = Path.home() / '.claude' / 'sessions'

# status (champ du registre) → état du widget. 'shell' (commande shell en cours)
# et 'compacting' (compaction du contexte) = la session travaille ; 'waiting' =
# bloquée sur une permission / notification ; 'idle' = en attente du prompt.
_STATUS_MAP = {
    'busy':       'working',
    'shell':      'working',
    'compacting': 'working',
    'waiting':    'waiting',
    'idle':       'idle',
}


_CLK_TCK = os.sysconf('SC_CLK_TCK')


def get_claude_processes() -> list[dict]:
    """Énumère les process 'claude' via /proc — pas de fork ps à chaque tick.

    elapsed = uptime − starttime, où starttime est le champ 22 de
    /proc/<pid>/stat (ticks d'horloge depuis le boot). Cohérent avec le reste
    du code, qui lit déjà cwd/status/environ/wchan dans /proc.
    """
    try:
        uptime = float(Path('/proc/uptime').read_text().split()[0])
    except Exception:
        return []
    procs = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # comm est tronqué à 15 car (TASK_COMM_LEN) — 'claude' y tient.
            if (entry / 'comm').read_text().strip() != 'claude':
                continue
            stat = (entry / 'stat').read_text()
            # Le champ 2 (comm) est entre parenthèses et peut contenir des
            # espaces ; parser après le dernier ')' réaligne les index.
            fields = stat[stat.rindex(')') + 2:].split()
            starttime = int(fields[19])  # champ 22 global = index 19 après comm
            elapsed = int(uptime - starttime / _CLK_TCK)
            start_unix = time.time() - elapsed
        except Exception:
            continue
        procs.append({'pid': int(entry.name), 'elapsed': elapsed,
                      'start_unix': start_unix, 'starttime': starttime})
    return procs


def get_wchan(pid: int) -> str:
    try:
        return Path(f'/proc/{pid}/wchan').read_text().strip()
    except Exception:
        return ''


def get_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f'/proc/{pid}/cwd')
    except Exception:
        return None


def get_parent_terminal(pid: int, window_pids: set[int] | None = None) -> dict | None:
    """Remonte l'arbre de process pour trouver le terminal parent.

    Deux chemins :
    1. Nom connu dans TERMINAL_NAMES → match rapide explicite.
    2. Premier ancêtre qui possède une fenêtre X11 (window_pids) → universel,
       fonctionne avec tout terminal sans avoir à le nommer.
    """
    current, visited = int(pid), set()
    while current > 1 and current not in visited:
        visited.add(current)
        try:
            with open(f'/proc/{current}/status') as f:
                content = f.read()
        except Exception:
            break
        name_m = re.search(r'Name:\s+(.+)', content)
        ppid_m = re.search(r'PPid:\s+(\d+)', content)
        name = name_m.group(1).strip() if name_m else ''
        for term_name in TERMINAL_NAMES:
            if term_name in name.lower():
                return {'pid': current, 'name': name}
        if window_pids and current in window_pids:
            return {'pid': current, 'name': name}
        current = int(ppid_m.group(1)) if ppid_m else 1
    return None


def get_env(pid: int) -> dict[str, str]:
    """Lit /proc/<pid>/environ → dict. Ne lève jamais d'exception."""
    try:
        return dict(
            kv.split('=', 1)
            for kv in Path(f'/proc/{pid}/environ').read_bytes().decode().split('\x00')
            if '=' in kv
        )
    except Exception:
        return {}


def _get_all_windows_wmctrl() -> list[dict]:
    """Fallback : liste les fenêtres via wmctrl (si Wnck indisponible)."""
    windows: list[dict] = []
    try:
        r = subprocess.run(['wmctrl', '-l', '-p'], capture_output=True, text=True, timeout=2)
    except Exception:
        return windows
    for line in r.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid = int(parts[2])
        except ValueError:
            continue
        windows.append({'wid': parts[0], 'pid': pid, 'title': parts[4]})
    return windows


def get_all_windows() -> list[dict]:
    """Retourne toutes les fenêtres : [{wid, pid, title}].

    Sur Wayland : retourne [] — la détection d'état passe par JSONL (source
    primaire) et wchan (fallback), sans enumération de fenêtres.
    Sur X11 : Wnck (source primaire) ou wmctrl (fallback).
    """
    if IS_WAYLAND:
        return []
    if not HAS_WNCK:
        return _get_all_windows_wmctrl()
    screen = Wnck.Screen.get_default()
    if screen is None:
        return _get_all_windows_wmctrl()
    screen.force_update()
    windows: list[dict] = []
    for w in screen.get_windows():
        windows.append({
            'wid':   hex(w.get_xid()),
            'pid':   w.get_pid(),
            'title': w.get_name() or '',
        })
    return windows


def find_best_window(term_pid: int | None, cwd: str | None,
                     all_windows: list[dict]) -> str | None:
    """Parmi les fenêtres du terminal PID, choisit celle qui héberge la session.

    Ordre de préférence :
    1. Fenêtre dont le titre porte un glyphe d'état Claude (braille / ✳)
    2. Fenêtre dont le titre contient le nom du répertoire du projet
    3. Première fenêtre du terminal (fallback)
    """
    if not term_pid:
        return None
    candidates = [w for w in all_windows if w['pid'] == term_pid]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]['wid']
    for w in candidates:
        if classify_state_from_title(w['title']):
            return w['wid']
    if cwd:
        proj = Path(cwd).name
        for w in candidates:
            if proj in w['title']:
                return w['wid']
    return candidates[0]['wid']


def classify_state_from_title(title: str | None) -> str | None:
    """Détecte l'état Claude depuis le 1er glyphe du titre terminal.

    Claude Code émet des séquences OSC pour mettre à jour le titre :
      - spinner braille (U+2800–U+28FF) en tête → travaille
      - '✳' (U+2733) en tête                   → prompt visible, attend l'utilisateur
    """
    s = (title or '').strip()
    if not s:
        return None
    if 0x2800 <= ord(s[0]) <= 0x28FF:
        return 'working'
    if s[0] == CLAUDE_IDLE_GLYPH:
        return 'waiting'
    return None


def cwd_to_project_dir(cwd: str | None, config_dir: str | None = None) -> Path | None:
    if not cwd:
        return None
    # Instance CLAUDE_CONFIG_DIR custom → ses JSONL vivent dans <config_dir>/projects,
    # pas dans ~/.claude/projects. Sinon état/contexte lus au mauvais endroit.
    base = Path(config_dir) / 'projects' if config_dir else CLAUDE_PROJECTS_DIR
    # Claude slugifie le cwd en remplaçant CHAQUE non-alphanumérique par '-'
    # (pas seulement '/'), donc 'geoffrey.laurent' → 'geoffrey-laurent'.
    slug = re.sub(r'[^a-zA-Z0-9]', '-', cwd)
    path = base / slug
    return path if path.exists() else None


DEFAULT_CONTEXT_WINDOW = 200_000


def context_window_for(model: str | None) -> int:
    """Fenêtre de contexte (tokens) déduite du nom du modèle.

    Le JSONL ne trace ni la taille de fenêtre ni le beta 1M d'Opus : on déduit
    donc depuis `message.model` (heuristique). Claude Code lance Opus/Sonnet 4.x
    et Fable/Mythos 5 avec la fenêtre 1M ; Haiku et les modèles inconnus
    retombent sur 200k.
    """
    m = (model or '').lower()
    if 'opus-4' in m or 'sonnet-4' in m or 'fable-5' in m or 'mythos-5' in m:
        return 1_000_000
    return DEFAULT_CONTEXT_WINDOW


# Cache {path: (mtime, résultat)} — évite de relire un JSONL inchangé d'un tick
# à l'autre. Taille du tail relu à chaud : l'état et le dernier usage assistant
# tiennent quasi toujours dans les derniers Ko (parse bottom-up + break précoce).
_JSONL_CACHE: dict[str, tuple[float, tuple[str | None, int | None, str | None]]] = {}
_JSONL_TAIL_BYTES = 65536


def _read_tail_lines(path: Path, max_bytes: int) -> tuple[list[str], bool]:
    """Derniers `max_bytes` du fichier, en lignes. Le bool indique si tout le
    fichier a été lu (tail complet → pas de fallback nécessaire)."""
    with path.open('rb') as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        start = max(0, size - max_bytes)
        f.seek(start)
        data = f.read()
    lines = data.decode(errors='ignore').split('\n')
    if start > 0 and len(lines) > 1:
        lines = lines[1:]  # 1re ligne potentiellement tronquée → jetée
    return lines, start == 0


# Topic de session : `ai-title` (aiTitle, généré par Claude) écrit une fois tôt
# dans le JSONL puis rarement régénéré ; `last-prompt` (lastPrompt) est appendé à
# chaque tour. Le tail-read de l'état ne les voit pas (titre hors des derniers Ko).
# Cache dédié {path: (offset_dernière_ligne_complète, title, lastPrompt)} : scan
# complet au 1er passage, puis relecture du seul delta appendé. L'offset mémorisé
# tombe toujours sur une frontière de ligne → pas de 1re ligne à jeter.
_TOPIC_CACHE: dict[str, tuple[int, str | None, str | None]] = {}


def _read_topic(path: Path) -> tuple[str | None, str | None]:
    """(aiTitle, lastPrompt) du JSONL, en ne relisant que les octets ajoutés."""
    try:
        size = path.stat().st_size
    except OSError:
        return None, None
    title = last_prompt = None
    start = 0
    cached = _TOPIC_CACHE.get(str(path))
    if cached:
        prev, title, last_prompt = cached
        if size == prev:
            return title, last_prompt
        if size > prev:
            start = prev          # delta uniquement (start = frontière de ligne)
        else:
            # size < prev → fichier tronqué/rotaté → rescan complet depuis 0 ;
            # on repart de zéro (titre potentiellement disparu → pas de valeur périmée).
            title = last_prompt = None
    try:
        with path.open('rb') as f:
            f.seek(start)
            data = f.read()
    except OSError:
        return title, last_prompt
    nl = data.rfind(b'\n')
    if nl == -1:                  # aucune ligne complète dans le delta
        return title, last_prompt
    for line in data[:nl + 1].decode(errors='ignore').split('\n'):
        if '"ai-title"' not in line and '"last-prompt"' not in line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get('type') == 'ai-title' and ev.get('aiTitle'):
            title = ev['aiTitle']
        elif ev.get('type') == 'last-prompt' and ev.get('lastPrompt'):
            last_prompt = ev['lastPrompt']
    if len(_TOPIC_CACHE) > 200:
        _TOPIC_CACHE.clear()
    _TOPIC_CACHE[str(path)] = (start + nl + 1, title, last_prompt)
    return title, last_prompt


def _parse_session_lines(lines: list[str]) -> tuple[str | None, int | None, str | None]:
    """Parse bottom-up : (state, context_pct, tool).

    `tool` = nom du dernier tool_use du message assistant LE PLUS RÉCENT (l'outil
    courant). On ne le récupère que sur le premier message assistant rencontré en
    remontant ; un tool_use plus ancien ne reflète pas ce qui tourne maintenant.
    `state` n'est utilisé qu'en fallback (registre absent) ; le % de contexte
    vient du dernier usage assistant disponible.
    """
    state = None
    context_pct = None
    tool = None
    seen_assistant = False
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get('isSidechain'):
            continue
        kind = ev.get('type', '')
        if state is None:
            if kind == 'assistant':
                # stop_reason discriminates "working" from "waiting": 'tool_use'
                # (a tool was dispatched, result pending) or a still-streaming
                # message (None) means Claude is busy; only a terminal end-of-turn
                # reason means it handed control back and is waiting on the user.
                sr = (ev.get('message') or {}).get('stop_reason')
                state = 'working' if sr in (None, 'tool_use', 'pause_turn') else 'waiting'
            elif kind == 'user':
                state = 'working'
            elif kind == 'system':
                state = 'idle'
        if kind == 'assistant':
            msg = ev.get('message', {})
            if not seen_assistant:
                seen_assistant = True
                content = msg.get('content')
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get('type') == 'tool_use':
                            tool = block.get('name')
                            break
            if context_pct is None:
                usage = msg.get('usage', {})
                if usage:
                    total = (usage.get('input_tokens', 0)
                             + usage.get('cache_creation_input_tokens', 0)
                             + usage.get('cache_read_input_tokens', 0))
                    if total > 0:
                        window = context_window_for(msg.get('model'))
                        context_pct = min(100, round(total * 100 / window))
        if state is not None and context_pct is not None:
            break
    return state, context_pct, tool


def get_session_info_from_jsonl(
    cwd: str | None,
    config_dir: str | None = None,
    session_id: str | None = None,
) -> tuple[str | None, int | None, str | None, str | None, float | None]:
    """État + % de contexte + outil courant + topic depuis le JSONL de la session.

    Retourne (state, context_pct, tool, topic, mtime) :
      state      : 'waiting' | 'working' | 'idle' | None (fallback registre absent)
      context_pct: 0-100 (% du contexte utilisé) | None si indisponible
      tool       : nom de l'outil courant | None
      topic      : titre IA de la session, sinon dernier prompt | None
      mtime      : mtime du JSONL (= dernière activité, proxy « inactif depuis »)
                   | None si le JSONL est introuvable

    Si `session_id` est fourni, cible directement <session_id>.jsonl (chemin
    exact donné par le registre, aucun devinage) ; sinon retombe sur le .jsonl
    le plus récent du projet. Court-circuit par mtime + lecture du seul tail
    (relecture complète si le tail tronqué n'a pas livré état + pct).
    """
    project_dir = cwd_to_project_dir(cwd, config_dir)
    if not project_dir:
        return None, None, None, None, None
    latest = None
    if session_id:
        cand = project_dir / f'{session_id}.jsonl'
        if cand.is_file():
            latest = cand
    if latest is None:
        jsonl_files = [f for f in project_dir.glob('*.jsonl') if f.is_file()]
        if not jsonl_files:
            return None, None, None, None, None
        try:
            latest, _ = max(
                ((f, f.stat().st_mtime) for f in jsonl_files),
                key=lambda x: x[1],
            )
        except (OSError, ValueError):
            return None, None, None, None, None
    try:
        mtime = latest.stat().st_mtime
    except OSError:
        return None, None, None, None, None
    key = str(latest)
    cached = _JSONL_CACHE.get(key)
    if cached and cached[0] == mtime:
        result = cached[1]
    else:
        result = (None, None, None)
        try:
            lines, complete = _read_tail_lines(latest, _JSONL_TAIL_BYTES)
            result = _parse_session_lines(lines)
            # Tail tronqué et incomplet (état ou pct manquant) → relecture complète.
            if not complete and (result[0] is None or result[1] is None):
                result = _parse_session_lines(latest.read_text(errors='ignore').split('\n'))
        except Exception:
            pass
        if len(_JSONL_CACHE) > 200:
            _JSONL_CACHE.clear()
        _JSONL_CACHE[key] = (mtime, result)
    # Topic désactivable (features.show_topic) : si off, on saute carrément la
    # lecture du JSONL pour le titre → aucun coût I/O quand la feature est éteinte.
    if getattr(CFG, 'show_topic', True):
        title, last_prompt = _read_topic(latest)
        topic = title or last_prompt
    else:
        topic = None
    return result[0], result[1], result[2], topic, mtime


def get_session_registry(pid: int, starttime: int,
                         config_dir: str | None = None) -> dict | None:
    """Registre de session première-partie écrit par Claude : <config>/sessions/<pid>.json.

    C'est la source d'état primaire — Claude y maintient en temps réel un champ
    `status` (busy/shell/compacting/waiting/idle) ainsi que `sessionId` et `cwd`.
    Indépendant du terminal (marche sous Wayland) et du système de hooks.

    Le registre vit sous le CLAUDE_CONFIG_DIR de l'instance : une session lancée
    avec un config dir custom écrit dans <config_dir>/sessions/, PAS dans
    ~/.claude/sessions/. Le chercher au mauvais endroit le rend introuvable et
    fait retomber (à tort) sur le fallback JSONL.

    Garde anti-recyclage de PID : `procStart` (ticks de démarrage du process,
    champ 22 de /proc/<pid>/stat) doit correspondre au `starttime` du process
    courant ; sinon le fichier provient d'une session précédente ayant porté le
    même PID → ignoré. Retourne le dict, ou None si absent/illisible/périmé.
    """
    sessions_dir = (Path(config_dir) / 'sessions') if config_dir else _SESSIONS_DIR
    try:
        data = json.loads((sessions_dir / f'{pid}.json').read_text())
    except (OSError, ValueError):
        return None
    ps = data.get('procStart')
    if ps is not None:
        try:
            if int(ps) != starttime:
                return None
        except (TypeError, ValueError):
            pass
    return data


def get_session_state(pid: int, cwd: str | None,
                      starttime: int = 0,
                      config_dir: str | None = None) -> tuple[str, int | None, str | None, str | None, float | None]:
    """État de la session. Retourne (state, context_pct, tool_name, topic, last_activity).

    Le registre ~/.claude/sessions/<pid>.json (champ `status`, temps réel) est
    prioritaire quand il existe ; selon la version de Claude Code il peut être
    absent, auquel cas l'état est déduit du JSONL. Le JSONL fournit dans tous
    les cas le % de contexte et l'outil courant (absents du registre).

    `sessionId` du registre, quand il existe, donne le chemin EXACT du JSONL ;
    sinon on devine par slug du cwd.
    """
    reg = get_session_registry(pid, starttime, config_dir)
    session_id = reg.get('sessionId') if reg else None
    if reg and not cwd:
        cwd = reg.get('cwd')
    jsonl_state, context_pct, tool, topic, last_activity = get_session_info_from_jsonl(
        cwd, config_dir, session_id)
    if reg:
        status = reg.get('status', '')
        state = _STATUS_MAP.get(status, 'idle')
        # 'shell' persiste tant qu'un shell de fond tourne (un `!cmd` interactif
        # ou un Bash run_in_background), MÊME après que Claude a rendu la main :
        # le statut reste figé sur 'shell' alors que la session attend en réalité
        # l'utilisateur. On recoupe avec le JSONL — s'il indique que le tour est
        # terminé (dernier assistant en stop_reason terminal → 'waiting'/'idle'),
        # le shell n'est qu'un résidu de fond et l'état réel est celui du JSONL,
        # pas 'working'. jsonl_state vaut None si le JSONL est introuvable : la
        # condition est alors fausse et on garde l'ancien comportement.
        if status == 'shell' and jsonl_state in ('waiting', 'idle'):
            state = jsonl_state
    else:
        state = jsonl_state or 'idle'
    return state, context_pct, tool, topic, last_activity


def format_elapsed(s) -> str:
    s = int(s)
    if s < 60:   return f"{s}s"
    if s < 3600: return f"{s//60}m{s%60:02d}s"
    return f"{s//3600}h{(s%3600)//60:02d}m"


def format_idle(secs, mode: str) -> str:
    """Durée d'inactivité formatée. mode='loose' (~Xm approx) ou 'precise' ([Nd ]HH:MM:SS)."""
    s = max(0, int(secs))
    if mode == 'precise':
        d, rem = divmod(s, 86400)
        h, rem = divmod(rem, 3600)
        m, sec = divmod(rem, 60)
        clock = f'{h:02d}:{m:02d}:{sec:02d}'
        return f'{d}d {clock}' if d else clock
    # loose : unité la plus significative, préfixée d'un ~ (approximatif)
    if s < 60:    return f'~{s}s'
    if s < 3600:  return f'~{s//60}m'
    if s < 86400: return f'~{s//3600}h'
    return f'~{s//86400}d'


def project_label(cwd: str | None) -> str:
    if not cwd:
        return '?'
    parts = Path(cwd).parts
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return parts[-1] if parts else '?'


def display_config_dir(path: str | None) -> str | None:
    """Nom d'instance depuis CLAUDE_CONFIG_DIR.

    Cas courant ~/.claude-<name> → juste <name>. Sinon chemin avec $HOME → ~.
    """
    if not path:
        return None
    home = str(Path.home())
    collapsed = '~' + path[len(home):] if path == home or path.startswith(home + '/') else path
    prefix = '~/.claude-'
    if collapsed.startswith(prefix) and len(collapsed) > len(prefix):
        return collapsed[len(prefix):]
    return collapsed


def _focus_terminal_wayland(terminal_pid: int | None) -> bool:
    """Focus un terminal sous Wayland — terminaux XWayland uniquement via wmctrl.

    GNOME 46 a supprimé Shell.Eval : il n'existe pas d'API externe pour forcer
    le focus sur un terminal natif Wayland. Le clic est silencieusement ignoré
    pour les terminaux Wayland natifs.
    """
    if not terminal_pid:
        return False
    try:
        r = subprocess.run(['wmctrl', '-l', '-p'], capture_output=True, text=True, timeout=2)
        for line in r.stdout.splitlines():
            parts = line.split(None, 4)
            if len(parts) >= 3 and parts[2] == str(terminal_pid):
                subprocess.run(['wmctrl', '-ia', parts[0]], timeout=2)
                return True
    except Exception:
        pass
    return False


def focus_terminal(window_id: str | None, terminal_pid: int | None,
                   kitty_socket: str | None = None,
                   kitty_window_id: str | None = None) -> bool:
    if IS_WAYLAND:
        return _focus_terminal_wayland(terminal_pid)

    focused = False

    # Bascule de workspace + activation de la fenêtre X11 (WINDOWID depuis l'env,
    # ou meilleure fenêtre par titre). `wmctrl -ia` change de bureau virtuel pour
    # atteindre la fenêtre — indispensable quand kitty est sur un autre workspace.
    # On le fait AVANT le focus-window kitty : la commande remote de kitty
    # sélectionne l'onglet à l'intérieur de kitty mais ne demande pas au WM de
    # changer de bureau, donc seule kitty laissait le focus sur un autre workspace.
    if window_id:
        try:
            subprocess.run(['wmctrl', '-ia', window_id], timeout=2)
            focused = True
        except Exception:
            pass

    # Kitty remote control : désambiguïse quand plusieurs onglets partagent un wid.
    if kitty_socket and kitty_window_id:
        try:
            r = subprocess.run(
                ['kitty', '@', '--to', kitty_socket,
                 'focus-window', '--match', f'id:{kitty_window_id}'],
                capture_output=True, timeout=2,
            )
            if r.returncode == 0:
                focused = True
        except Exception:
            pass

    if focused:
        return True
    # Fallback xdotool sur le PID du terminal (terminaux XWayland ou X11 natifs)
    if terminal_pid:
        try:
            r = subprocess.run(
                ['xdotool', 'search', '--pid', str(terminal_pid), 'windowfocus', '--sync'],
                capture_output=True, timeout=2,
            )
            if r.returncode == 0:
                return True
        except Exception:
            pass
    return False


def scan_sessions() -> list[dict]:
    all_windows = get_all_windows()
    window_pids = {w['pid'] for w in all_windows}

    procs = get_claude_processes()

    sessions = []
    for p in procs:
        pid      = p['pid']
        cwd      = get_cwd(pid)
        term     = get_parent_terminal(pid, window_pids)
        term_pid = term['pid'] if term else None
        env      = get_env(pid)

        # Résolution du window_id de l'onglet exact, par ordre de fiabilité :
        # 1. WINDOWID dans l'env du process claude → X11 window id de l'onglet
        # 2. Kitty remote control (KITTY_LISTEN_ON + KITTY_WINDOW_ID dans l'env)
        # 3. Meilleure fenêtre du terminal par titre / nom de projet
        kitty_socket    = env.get('KITTY_LISTEN_ON') or None
        kitty_window_id = env.get('KITTY_WINDOW_ID') or None
        raw_wid         = env.get('WINDOWID')
        if raw_wid:
            # WINDOWID est un entier décimal ; wmctrl -ia attend 0x...
            try:
                window_id = hex(int(raw_wid))
            except ValueError:
                window_id = raw_wid
        else:
            window_id = find_best_window(term_pid, cwd, all_windows)

        config_dir = env.get('CLAUDE_CONFIG_DIR') or None
        if config_dir:
            # CLAUDE_CONFIG_DIR hérité de l'env de la session : on résout `~`
            # (quoté → non-expansé par le shell) et on rejette tout chemin
            # relatif (sans cwd de la session, il pointerait sur le cwd du
            # watcher → registre/JSONL/watch au mauvais endroit). → défaut.
            config_dir = os.path.expanduser(config_dir)
            if not os.path.isabs(config_dir):
                config_dir = None
        state, context_pct, tool, topic, last_activity = get_session_state(
            pid, cwd, p['starttime'], config_dir)
        sessions.append({
            'pid':             pid,
            'project':         project_label(cwd),
            'topic':           topic,
            'cwd':             cwd or '?',
            'elapsed':         p['elapsed'],
            'waiting':         state == 'waiting',
            'working':         state == 'working',
            'context_pct':     context_pct,
            'tool':            tool,
            'terminal_pid':    term_pid,
            'window_id':       window_id,
            'kitty_socket':    kitty_socket,
            'kitty_window_id': kitty_window_id,
            'config_dir':      config_dir,
            'last_activity':   last_activity,
        })
    # Priorité d'état (attente > travaille > idle) dans tous les modes. En mode
    # 'idle', SEUL le groupe inactif est départagé par ancienneté d'inactivité
    # (plus récemment devenu inactif en tête) ; attente/travaille gardent le tri
    # alpha. Trier les sessions actives par mtime serait instable — leur JSONL
    # bouge en continu, l'ordre changerait à chaque scan, forçant un rebuild
    # complet des lignes (flicker + churn RSS). last_activity absent (JSONL
    # introuvable) → coule en bas du groupe inactif via +inf.
    if getattr(CFG, 'sort_mode', 'default') == 'idle':
        now = time.time()
        def _sort_key(s: dict) -> tuple:
            if s['waiting']:   bucket = 0
            elif s['working']: bucket = 1
            else:              bucket = 2
            la = s.get('last_activity')
            idle = ((now - la) if la is not None else float('inf')) if bucket == 2 else 0.0
            return (bucket, idle, s['project'].lower())
        sessions.sort(key=_sort_key)
    else:
        sessions.sort(key=lambda s: (not s['waiting'], not s['working'], s['project'].lower()))
    return sessions

# ── Session row ───────────────────────────────────────────────────────────────

class SessionRow(Gtk.EventBox):
    def __init__(self, session: dict):
        super().__init__()
        self.session  = session
        self._hovered     = False
        self._kb_selected = False

        # Survol : chemin de travail complet + sujet complet (les labels tronquent
        # — projet aux 2 derniers segments, sujet à la 1re ligne ellipsée).
        tip = session['cwd']
        topic = (session.get('topic') or '').strip()
        if topic:
            tip = f'{tip}\n\nTopic: {topic}'
        self.set_tooltip_text(tip)
        self.set_visible_window(True)
        self.connect('button-press-event', self._on_click)
        self.connect('enter-notify-event',  self._on_enter)
        self.connect('leave-notify-event',  self._on_leave)
        self.connect('draw', self._on_draw)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(7)
        box.set_margin_bottom(7)
        box.set_margin_start(12)
        box.set_margin_end(12)
        self.add(box)

        self.dot = Gtk.DrawingArea()
        self.dot.set_size_request(8, 8)
        self.dot.connect('draw', self._draw_dot)
        dot_wrap = Gtk.Box()
        dot_wrap.set_valign(Gtk.Align.CENTER)
        dot_wrap.add(self.dot)
        box.pack_start(dot_wrap, False, False, 0)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info.set_valign(Gtk.Align.CENTER)
        # halign FILL + hexpand + xalign 0 : le label occupe toute la largeur dispo
        # de la ligne et n'ellipse qu'au bord réel du widget (s'adapte à la largeur
        # configurée / auto). max_width_chars ne sert que de garde-fou en mode
        # auto_width (borne la largeur naturelle réclamée).
        self.lbl_project = Gtk.Label()
        self.lbl_project.set_halign(Gtk.Align.FILL)
        self.lbl_project.set_xalign(0.0)
        self.lbl_project.set_hexpand(True)
        self.lbl_project.set_ellipsize(Pango.EllipsizeMode.END)
        self.lbl_project.set_max_width_chars(40)
        # Topic IA : distingue plusieurs sessions partageant le même cwd.
        self.lbl_topic = Gtk.Label()
        self.lbl_topic.set_halign(Gtk.Align.FILL)
        self.lbl_topic.set_xalign(0.0)
        self.lbl_topic.set_hexpand(True)
        self.lbl_topic.set_ellipsize(Pango.EllipsizeMode.END)
        self.lbl_topic.set_max_width_chars(48)
        self.lbl_topic.set_no_show_all(True)  # masqué si pas de topic, sans gap
        # Ellipsize ici aussi : sans ça la ligne meta (non tronquable) impose la
        # largeur minimale de la fenêtre et empêche de descendre sous ~200 px.
        self.lbl_meta = Gtk.Label()
        self.lbl_meta.set_halign(Gtk.Align.FILL)
        self.lbl_meta.set_xalign(0.0)
        self.lbl_meta.set_ellipsize(Pango.EllipsizeMode.END)
        self.lbl_meta.set_max_width_chars(40)
        info.pack_start(self.lbl_project, False, False, 0)
        info.pack_start(self.lbl_topic,   False, False, 0)
        info.pack_start(self.lbl_meta,    False, False, 0)
        box.pack_start(info, True, True, 0)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        right.set_valign(Gtk.Align.CENTER)
        self.badge = Gtk.Label()
        self.badge.set_halign(Gtk.Align.END)
        self.lbl_ctx = Gtk.Label()
        self.lbl_ctx.set_halign(Gtk.Align.END)
        self.lbl_ctx.set_ellipsize(Pango.EllipsizeMode.END)
        self.lbl_ctx.set_max_width_chars(16)
        right.pack_start(self.badge,   False, False, 0)
        right.pack_start(self.lbl_ctx, False, False, 0)
        box.pack_end(right, False, False, 0)

        self._update_labels()
        self.show_all()

    def update_session(self, session: dict):
        """Met à jour la ligne EN PLACE (pas de recréation).

        Réécrit tooltip + labels + couleur du point depuis la nouvelle session,
        sans détruire le widget : l'état de survol (hover) et la sélection clavier
        sont préservés, et l'anim de pulse n'est pas réinitialisée. Appelé par
        _rebuild_sessions quand la structure (pids/colonnes) est inchangée.
        """
        self.session = session
        tip = session['cwd']
        topic = (session.get('topic') or '').strip()
        if topic:
            tip = f'{tip}\n\nTopic: {topic}'
        self.set_tooltip_text(tip)
        self._update_labels()
        self.dot.queue_draw()

    def _update_labels(self):
        s = self.session
        if s['waiting']:
            color, badge_txt = COLOR_WAITING, tr('attend')
        elif s['working']:
            color, badge_txt = COLOR_WORKING, tr('working')
        else:
            color, badge_txt = COLOR_IDLE, tr('idle')
        self._dot_color = color
        self.lbl_project.set_markup(
            f'<span foreground="{TEXT_PRIMARY}" font="Monospace 9" weight="500">'
            f'{GLib.markup_escape_text(s["project"])}</span>'
        )
        topic = (s.get('topic') or '').strip().split('\n', 1)[0]
        if topic:
            self.lbl_topic.set_markup(
                f'<span foreground="{TEXT_DIM2}" font="Monospace 8" style="italic">'
                f'{GLib.markup_escape_text(topic)}</span>'
            )
            self.lbl_topic.set_visible(True)
        else:
            self.lbl_topic.set_visible(False)
        ctx = s.get('context_pct')
        if ctx is not None:
            if ctx >= 80:   ctx_color = '#e86c3a'
            elif ctx >= 60: ctx_color = '#d4a052'
            else:           ctx_color = TEXT_DIM2
            ctx_markup = (
                f' <span foreground="{ctx_color}" font="Monospace 8">· ctx {ctx}%</span>'
            )
        else:
            ctx_markup = ''
        meta = (
            f'<span foreground="{TEXT_DIM2}" font="Monospace 8">'
            f'{tr("pid")} {s["pid"]} · {format_elapsed(s["elapsed"])}</span>'
            f'{ctx_markup}'
        )
        cfg = display_config_dir(s.get('config_dir'))
        if cfg:
            meta += (
                f' <span foreground="{COLOR_CLAUDE}" font="Monospace 8">'
                f'{CLAUDE_IDLE_GLYPH}{GLib.markup_escape_text(cfg)}</span>'
            )
        self.lbl_meta.set_markup(meta)
        tool = s.get('tool') if (s['working'] or s['waiting']) else None
        idle_fmt = getattr(CFG, 'idle_format', 'none')
        la = s.get('last_activity')
        if tool:
            self.lbl_ctx.set_markup(
                f'<span foreground="{TEXT_DIM2}" font="Monospace 8">'
                f'{GLib.markup_escape_text(tool)}</span>'
            )
        elif not s['working'] and not s['waiting'] and idle_fmt != 'none' and la is not None:
            # Session inactive : la colonne outil (vide en idle) sert la durée
            # d'inactivité = now − dernière activité (mtime du JSONL).
            self.lbl_ctx.set_markup(
                f'<span foreground="{TEXT_DIM2}" font="Monospace 8">'
                f'{GLib.markup_escape_text(format_idle(time.time() - la, idle_fmt))}</span>'
            )
        else:
            self.lbl_ctx.set_text('')
        self.badge.set_markup(
            f'<span foreground="{color}" font="Monospace 8">{badge_txt}</span>'
        )

    def _draw_dot(self, widget, cr):
        c = Gdk.RGBA()
        c.parse(getattr(self, '_dot_color', COLOR_IDLE))
        if self.session.get('waiting'):
            alpha = _PULSE_ALPHAS[getattr(self, '_anim_tick', 0) % len(_PULSE_ALPHAS)]
        else:
            alpha = 1.0
        cr.set_source_rgba(c.red, c.green, c.blue, alpha)
        cr.arc(4, 4, 3.5, 0, 2 * math.pi)
        cr.fill()

    def _on_draw(self, widget, cr):
        if self._kb_selected:
            cr.set_source_rgba(*COLOR_KB_SEL)
        elif self._hovered:
            cr.set_source_rgba(*(COLOR_HOVER_W if self.session['waiting'] else COLOR_HOVER))
        else:
            return
        cr.rectangle(0, 0, widget.get_allocated_width(), widget.get_allocated_height())
        cr.fill()

    def _on_enter(self, widget, event):
        # Les events INFERIOR/VIRTUAL sont synthétiques (fenêtre qui apparaît sous
        # le curseur) — on les ignore pour éviter le hover visuel au démarrage.
        if event.detail in (Gdk.NotifyType.INFERIOR, Gdk.NotifyType.VIRTUAL,
                            Gdk.NotifyType.NONLINEAR_VIRTUAL):
            return
        self._hovered = True
        self.get_window().set_cursor(Gdk.Cursor.new_from_name(self.get_display(), 'pointer'))
        self.queue_draw()

    def _on_leave(self, *_):
        self._hovered = False
        self.queue_draw()

    def set_kb_selected(self, selected: bool):
        if self._kb_selected != selected:
            self._kb_selected = selected
            self.queue_draw()

    def _do_focus(self):
        focus_terminal(
            self.session.get('window_id'),
            self.session['terminal_pid'],
            self.session.get('kitty_socket'),
            self.session.get('kitty_window_id'),
        )

    def _on_click(self, widget, event):
        if event.button == 1:
            self._do_focus()
            return True  # don't bubble up to the window background menu
        return False

# ── Settings dialog ──────────────────────────────────────────────────────────

class SettingsDialog(Gtk.Dialog):
    """Dialogue de configuration — accessible depuis le systray."""

    def __init__(self, parent: 'ClaudeWatcher'):
        super().__init__(title=tr('settings_title'), modal=True)
        self._parent = parent
        self._original_values = {
            'lang':       CFG.lang,
            'free':       parent._user_pos is not None,
            'screen':     CFG.screen,
            'corner':     CFG.corner,
            'margin_x':   CFG.margin_x,
            'margin_y':   CFG.margin_y,
            'width':      CFG.width,
            'auto_width': CFG.auto_width,
            'columns':    getattr(CFG, 'columns', 1),
            'max_height': getattr(CFG, 'max_height', 0),
            'sort_mode':  getattr(CFG, 'sort_mode', 'default'),
            'idle_format': getattr(CFG, 'idle_format', 'none'),
            'show_topic': getattr(CFG, 'show_topic', True),
            'refresh_ms': CFG.refresh_ms,
            'snooze_sec': CFG.snooze_sec,
            # Effective on-screen value, not CFG: shift+scroll moves it away
            # from the configured base — the dialog must show (and restore)
            # what's on screen, while CFG keeps the base until Apply.
            'bg_alpha':   round(parent._effective_alpha() * 100),
        }
        self.set_default_size(400, -1)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.add_button(tr('cancel'), Gtk.ResponseType.CANCEL)
        ok_btn = self.add_button(tr('apply'), Gtk.ResponseType.OK)
        ok_btn.get_style_context().add_class('suggested-action')

        content = self.get_content_area()
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_margin_start(18)
        outer.set_margin_end(18)
        outer.set_margin_top(14)
        outer.set_margin_bottom(8)
        content.add(outer)

        def section_label(title: str) -> Gtk.Label:
            lbl = Gtk.Label()
            lbl.set_markup(f'<b>{title}</b>')
            lbl.set_halign(Gtk.Align.START)
            lbl.set_margin_top(10)
            lbl.set_margin_bottom(6)
            return lbl

        def field_label(text: str) -> Gtk.Label:
            lbl = Gtk.Label(label=text)
            lbl.set_halign(Gtk.Align.END)
            lbl.set_valign(Gtk.Align.CENTER)
            return lbl

        def make_grid() -> Gtk.Grid:
            g = Gtk.Grid()
            g.set_row_spacing(8)
            g.set_column_spacing(10)
            g.set_margin_start(6)
            outer.pack_start(g, False, False, 0)
            return g

        # ── Langue ──────────────────────────────────────────────────────────
        outer.pack_start(section_label(tr('sec_lang')), False, False, 0)
        g0 = make_grid()
        g0.attach(field_label(tr('fld_lang')), 0, 0, 1, 1)
        self._lang_combo = Gtk.ComboBoxText()
        self._lang_combo.append('fr', tr('lang_fr'))
        self._lang_combo.append('en', tr('lang_en'))
        self._lang_combo.set_active_id(CFG.lang)
        g0.attach(self._lang_combo, 1, 0, 2, 1)

        # ── Position ─────────────────────────────────────────────────────────
        outer.pack_start(Gtk.Separator(), False, False, 0)
        outer.pack_start(section_label(tr('sec_position')), False, False, 0)
        g1 = make_grid()

        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        self._radio_corner = Gtk.RadioButton(label=tr('mode_corner'))
        self._radio_free   = Gtk.RadioButton.new_with_label_from_widget(
            self._radio_corner, tr('mode_free'))
        mode_box.pack_start(self._radio_corner, False, False, 0)
        mode_box.pack_start(self._radio_free,   False, False, 0)
        g1.attach(field_label(tr('fld_mode')), 0, 0, 1, 1)
        g1.attach(mode_box, 1, 0, 2, 1)

        g1.attach(field_label(tr('fld_screen')), 0, 1, 1, 1)
        self._screen_combo = Gtk.ComboBoxText()
        self._cfg_screen = CFG.screen  # valeur configurée, potentiellement hors-range
        display = Gdk.Display.get_default()
        for i in range(display.get_n_monitors()):
            m    = display.get_monitor(i)
            geom = m.get_geometry()
            text = f"{tr('monitor_idx')} {i}  ({geom.width}×{geom.height})"
            if m.is_primary():
                text += f"  [{tr('monitor_primary')}]"
            self._screen_combo.append(str(i), text)
        # set_active_id échoue silencieusement si l'écran configuré est absent.
        # Dans ce cas, sélectionner 0 comme fallback visuel AVANT de connecter
        # les signaux — ainsi le changed initial ne compte pas comme choix user.
        if not self._screen_combo.set_active_id(str(CFG.screen)):
            self._screen_combo.set_active(0)
            self._screen_user_changed = False
        else:
            self._screen_user_changed = True  # l'écran est présent, toute valeur est valide
        self._screen_combo.connect('changed', lambda _w: setattr(self, '_screen_user_changed', True))
        g1.attach(self._screen_combo, 1, 1, 2, 1)

        g1.attach(field_label(tr('fld_corner')), 0, 2, 1, 1)
        self._corner_combo = Gtk.ComboBoxText()
        for val, key in [
            ('bottom-right', 'corner_br'),
            ('bottom-left',  'corner_bl'),
            ('top-right',    'corner_tr'),
            ('top-left',     'corner_tl'),
        ]:
            self._corner_combo.append(val, tr(key))
        self._corner_combo.set_active_id(CFG.corner)
        g1.attach(self._corner_combo, 1, 2, 2, 1)

        g1.attach(field_label(tr('fld_margin_x')), 0, 3, 1, 1)
        self._spin_mx = Gtk.SpinButton.new_with_range(0, 500, 1)
        self._spin_mx.set_value(CFG.margin_x)
        g1.attach(self._spin_mx, 1, 3, 1, 1)
        g1.attach(Gtk.Label(label="px"), 2, 3, 1, 1)

        g1.attach(field_label(tr('fld_margin_y')), 0, 4, 1, 1)
        self._spin_my = Gtk.SpinButton.new_with_range(0, 500, 1)
        self._spin_my.set_value(CFG.margin_y)
        g1.attach(self._spin_my, 1, 4, 1, 1)
        g1.attach(Gtk.Label(label="px"), 2, 4, 1, 1)

        is_free = parent._user_pos is not None
        if is_free:
            self._radio_free.set_active(True)
        self._corner_widgets: list[Gtk.Widget] = [
            self._screen_combo, self._corner_combo,
            self._spin_mx, self._spin_my,
        ]
        for w in self._corner_widgets:
            w.set_sensitive(not is_free)
        self._radio_corner.connect('toggled', self._on_mode_toggled)

        # ── Affichage ────────────────────────────────────────────────────────
        outer.pack_start(Gtk.Separator(), False, False, 0)
        outer.pack_start(section_label(tr('sec_display')), False, False, 0)
        g2 = make_grid()

        self._chk_auto_width = Gtk.CheckButton(label=tr('fld_auto_width'))
        self._chk_auto_width.set_active(CFG.auto_width)
        g2.attach(self._chk_auto_width, 0, 0, 3, 1)

        self._chk_show_topic = Gtk.CheckButton(label=tr('fld_show_topic'))
        self._chk_show_topic.set_active(getattr(CFG, 'show_topic', True))
        g2.attach(self._chk_show_topic, 0, 5, 3, 1)

        self._lbl_width = field_label(tr('fld_width'))
        g2.attach(self._lbl_width, 0, 1, 1, 1)
        self._spin_width = Gtk.SpinButton.new_with_range(200, 800, 10)
        self._spin_width.set_value(CFG.width)
        g2.attach(self._spin_width, 1, 1, 1, 1)
        g2.attach(Gtk.Label(label="px"), 2, 1, 1, 1)

        g2.attach(field_label(tr('fld_refresh')), 0, 2, 1, 1)
        self._spin_refresh = Gtk.SpinButton.new_with_range(500, 10000, 500)
        self._spin_refresh.set_value(CFG.refresh_ms)
        g2.attach(self._spin_refresh, 1, 2, 1, 1)
        g2.attach(Gtk.Label(label="ms"), 2, 2, 1, 1)

        g2.attach(field_label(tr('fld_snooze')), 0, 3, 1, 1)
        self._spin_snooze = Gtk.SpinButton.new_with_range(10, 3600, 10)
        self._spin_snooze.set_value(CFG.snooze_sec)
        g2.attach(self._spin_snooze, 1, 3, 1, 1)
        g2.attach(Gtk.Label(label="s"), 2, 3, 1, 1)

        g2.attach(field_label(tr('fld_bg_alpha')), 0, 4, 1, 1)
        # 20 floor mirrors _set_effective_alpha — lower values would silently snap
        self._spin_bg_alpha = Gtk.SpinButton.new_with_range(20, 100, 1)
        self._spin_bg_alpha.set_value(round(parent._effective_alpha() * 100))
        g2.attach(self._spin_bg_alpha, 1, 4, 1, 1)
        g2.attach(Gtk.Label(label="%"), 2, 4, 1, 1)
        btn_bg_default = Gtk.Button(label=f"{tr('btn_default')} ({BG_ALPHA_DEFAULT})")
        # set_value fires value-changed → live preview updates immediately
        btn_bg_default.connect(
            'clicked', lambda _b: self._spin_bg_alpha.set_value(BG_ALPHA_DEFAULT))
        g2.attach(btn_bg_default, 3, 4, 1, 1)

        g2.attach(field_label(tr('fld_columns')), 0, 6, 1, 1)
        self._spin_columns = Gtk.SpinButton.new_with_range(1, 6, 1)
        self._spin_columns.set_value(getattr(CFG, 'columns', 1))
        g2.attach(self._spin_columns, 1, 6, 1, 1)

        # Label « Hauteur max » + icône info (tooltip explicatif au survol) plutôt
        # qu'un « (0 = écran) » accolé : plus lisible, l'explication complète tient
        # dans le tooltip.
        mh_lbl_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        mh_lbl_box.set_halign(Gtk.Align.END)
        mh_lbl_box.set_valign(Gtk.Align.CENTER)
        mh_lbl = Gtk.Label(label=tr('fld_max_height'))
        mh_info = Gtk.Image.new_from_icon_name('dialog-information-symbolic', Gtk.IconSize.MENU)
        mh_info.set_tooltip_text(tr('help_max_height'))
        mh_lbl_box.pack_start(mh_lbl,  False, False, 0)
        mh_lbl_box.pack_start(mh_info, False, False, 0)
        g2.attach(mh_lbl_box, 0, 7, 1, 1)
        # 0 = pas de limite propre (l'écran borne) ; pas-50 px ; plafond large.
        self._spin_max_height = Gtk.SpinButton.new_with_range(0, 4000, 50)
        self._spin_max_height.set_value(getattr(CFG, 'max_height', 0))
        g2.attach(self._spin_max_height, 1, 7, 1, 1)
        g2.attach(Gtk.Label(label="px"), 2, 7, 1, 1)

        g2.attach(field_label(tr('fld_sort')), 0, 8, 1, 1)
        self._combo_sort = Gtk.ComboBoxText()
        self._combo_sort.append('default', tr('sort_default'))
        self._combo_sort.append('idle',    tr('sort_idle'))
        self._combo_sort.set_active_id(getattr(CFG, 'sort_mode', 'default'))
        g2.attach(self._combo_sort, 1, 8, 2, 1)

        g2.attach(field_label(tr('fld_idle_format')), 0, 9, 1, 1)
        self._combo_idle = Gtk.ComboBoxText()
        self._combo_idle.append('none',    tr('idle_none'))
        self._combo_idle.append('loose',   tr('idle_loose'))
        self._combo_idle.append('precise', tr('idle_precise'))
        self._combo_idle.set_active_id(getattr(CFG, 'idle_format', 'none'))
        g2.attach(self._combo_idle, 1, 9, 2, 1)

        # ── Raccourci clavier ────────────────────────────────────────────────
        outer.pack_start(Gtk.Separator(), False, False, 0)
        outer.pack_start(section_label(tr('sec_shortcut')), False, False, 0)
        g3 = make_grid()

        self._chk_shortcut = Gtk.CheckButton(label=tr('fld_shortcut_enable'))
        self._chk_shortcut.set_active(CFG.shortcut_enable)
        g3.attach(self._chk_shortcut, 0, 0, 3, 1)

        g3.attach(field_label(tr('fld_hotkey')), 0, 1, 1, 1)
        self._entry_hotkey = Gtk.Entry()
        self._entry_hotkey.set_text(CFG.hotkey)
        self._entry_hotkey.set_placeholder_text(tr('hotkey_hint'))
        self._entry_hotkey.set_tooltip_text(tr('hotkey_hint'))
        g3.attach(self._entry_hotkey, 1, 1, 2, 1)
        # Hotkey/enable take effect only on Apply (no live rebinding preview).
        self._chk_shortcut.connect(
            'toggled', lambda c: self._entry_hotkey.set_sensitive(c.get_active()))
        self._entry_hotkey.set_sensitive(CFG.shortcut_enable)

        # Live preview — connecté après les set_active_id/set_value initiaux
        for widget, signal in [
            (self._lang_combo,    'changed'),
            (self._screen_combo,  'changed'),
            (self._corner_combo,  'changed'),
            (self._radio_corner,  'toggled'),
            (self._spin_mx,       'value-changed'),
            (self._spin_my,       'value-changed'),
            (self._spin_width,    'value-changed'),
            (self._chk_auto_width,'toggled'),
            (self._chk_show_topic,'toggled'),
            (self._spin_bg_alpha, 'value-changed'),
            (self._spin_columns,    'value-changed'),
            (self._spin_max_height, 'value-changed'),
            (self._combo_sort,      'changed'),
            (self._combo_idle,      'changed'),
        ]:
            widget.connect(signal, self._on_preview_change)

        content.show_all()

    def _on_preview_change(self, *_):
        self._parent._preview_settings(self.get_values())

    def _on_mode_toggled(self, radio: Gtk.RadioButton):
        sensitive = radio.get_active()
        for w in self._corner_widgets:
            w.set_sensitive(sensitive)

    def get_values(self) -> dict:
        screen_id = self._screen_combo.get_active_id()
        corner_id = self._corner_combo.get_active_id()
        # Si l'écran configuré était absent et que l'utilisateur n'a pas
        # explicitement choisi un autre écran, conserver la valeur d'origine
        # pour ne pas écraser la préférence en conf.
        if self._screen_user_changed and screen_id is not None:
            screen_val = int(screen_id)
        else:
            screen_val = self._cfg_screen
        return {
            'lang':       self._lang_combo.get_active_id() or 'fr',
            'free':       self._radio_free.get_active(),
            'screen':     screen_val,
            'corner':     corner_id or 'bottom-right',
            'margin_x':   int(self._spin_mx.get_value()),
            'margin_y':   int(self._spin_my.get_value()),
            'width':      int(self._spin_width.get_value()),
            'auto_width': self._chk_auto_width.get_active(),
            'columns':    int(self._spin_columns.get_value()),
            'max_height': int(self._spin_max_height.get_value()),
            'sort_mode':  self._combo_sort.get_active_id() or 'default',
            'idle_format': self._combo_idle.get_active_id() or 'none',
            'show_topic': self._chk_show_topic.get_active(),
            'refresh_ms': int(self._spin_refresh.get_value()),
            'snooze_sec': int(self._spin_snooze.get_value()),
            'bg_alpha':   int(self._spin_bg_alpha.get_value()),
            'shortcut_enable': self._chk_shortcut.get_active(),
            'hotkey':     self._entry_hotkey.get_text().strip() or '<Ctrl><Alt>q',
        }


# ── Main window ───────────────────────────────────────────────────────────────

class ClaudeWatcher(Gtk.Window):

    def __init__(self, cfg: argparse.Namespace):
        # Layer shell nécessite TOPLEVEL ; POPUP pour X11 (no-decoration natif).
        super().__init__(type=Gtk.WindowType.TOPLEVEL if IS_WAYLAND else Gtk.WindowType.POPUP)
        self.sessions      = []
        self._session_rows = []   # SessionRow ordonnées (nav clavier + anim)
        self._last_size    = None # dernière taille (w, h) appliquée — anti-churn resize
        self._last_rows_sig = None # structure des lignes (cols, pids) au dernier rebuild
        self._anim_tick    = 0
        self._snooze_until = 0
        self._snooze_timer = None
        self._kb_index        = -1
        self._kb_bind_retries = 0

        self.screen   = cfg.screen
        self.corner   = cfg.corner
        self.margin_x = cfg.margin_x
        self.margin_y = cfg.margin_y
        self._dragging   = False
        self._drag_off   = (0, 0)
        self._save_timer = 0
        self._alpha      = 1.0
        self._bg_alpha   = cfg.bg_alpha / 100.0

        # Position libre (--x/--y ou drag) : X11 seulement.
        # Sur Wayland, la position est gérée par gtk-layer-shell (anchor + margin).
        if not IS_WAYLAND and cfg.x is not None and cfg.y is not None:
            g = self._get_monitor_geom()
            self._user_pos = (g.x + cfg.x, g.y + cfg.y)
            self._save_position()
        elif not IS_WAYLAND and cfg.mode == 'free':
            # Mode libre explicite — charger la position sauvegardée.
            self._user_pos = self._load_position()
        else:
            # Mode ancré (corner) ou Wayland — ignorer position.json.
            self._user_pos = None

        self._tray      = None
        self._tray_menu = None
        self._hidden    = False
        if cfg.tray:
            self._init_tray()

        # ── Fenêtre ─────────────────────────────────────────────────────────
        self.set_title("Claude Code Watcher")
        self.set_decorated(False)
        # Redimensionnable au sens GTK pour que NOS geometry hints (largeur fixée
        # à w dans _apply_window_size) fassent autorité : set_resizable(False)
        # forcerait min = max = largeur NATURELLE, qui gonfle avec les compteurs
        # d'en-tête et fait sortir la fenêtre de l'écran. Sans décoration ni entrée
        # de barre des tâches, l'utilisateur ne peut de toute façon pas la redimensionner.
        self.set_resizable(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        if not IS_WAYLAND:
            # DOCK + keep_below + stick : gérés par gtk-layer-shell sur Wayland.
            self.set_type_hint(Gdk.WindowTypeHint.DOCK)
            self.set_keep_below(True)
            self.stick()
        if IS_WAYLAND and HAS_LAYER_SHELL:
            self._init_layer_shell()

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_app_paintable(True)
        self.connect('draw', self._draw_bg)

        # ── Layout ──────────────────────────────────────────────────────────
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.main_box.set_margin_top(12)
        self.main_box.set_margin_bottom(15)
        self.add(self.main_box)

        self._header = header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.set_margin_start(12)
        header.set_margin_end(10)
        header.set_margin_bottom(8)

        # Chevron toggle (roll-up): ▾ expanded / ▸ rolled. Click toggles shade.
        self._chevron = Gtk.Label()
        self._chevron.set_halign(Gtk.Align.START)
        chevron_evt = Gtk.EventBox()
        chevron_evt.set_visible_window(False)
        chevron_evt.add(self._chevron)
        chevron_evt.connect('button-press-event', self._on_chevron_press)
        header.pack_start(chevron_evt, False, False, 0)

        lbl_title = Gtk.Label()
        lbl_title.set_markup(
            f'<span foreground="{COLOR_TITLE}" font="Monospace 9" weight="500"'
            f' letter_spacing="1500">{tr("title")}</span>'
        )
        lbl_title.set_halign(Gtk.Align.START)
        header.pack_start(lbl_title, True, True, 0)

        self.lbl_counts = Gtk.Label()
        self.lbl_counts.set_halign(Gtk.Align.END)
        # Ellipsable : sans ça, des compteurs longs (« 1 attente · 1 travaille ·
        # 2 total ») donnent à l'en-tête une largeur naturelle > largeur épinglée,
        # ce qui élargit la fenêtre hors écran. Avec les geometry hints fixant la
        # largeur (_apply_window_size), le compteur s'ellipse plutôt que déborder.
        self.lbl_counts.set_ellipsize(Pango.EllipsizeMode.END)
        header.pack_start(self.lbl_counts, False, False, 0)

        # Header draggable + wheel shade
        header_evt = Gtk.EventBox()
        header_evt.add(header)
        header_evt.add_events(Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.SMOOTH_SCROLL_MASK)
        header_evt.connect('button-press-event', self._on_header_press)
        header_evt.connect('scroll-event',       self._on_header_scroll)
        self.main_box.pack_start(header_evt, False, False, 0)
        sep_top = self._sep()
        self.main_box.pack_start(sep_top, False, False, 0)

        # Conteneur des sessions : une grille (multi-colonnes, CFG.columns) dans
        # une zone scrollable bornée en hauteur. La grille répartit les lignes en
        # colonnes ; le ScrolledWindow plafonne la hauteur (CFG.max_height, borné
        # par l'écran) et fait apparaître une barre de défilement quand il y a
        # trop de sessions pour tenir verticalement.
        self.sessions_box = Gtk.Grid()
        self.sessions_box.set_column_homogeneous(True)
        # Gouttière entre colonnes + trait vertical dessiné dedans (multi-colonnes).
        self.sessions_box.set_column_spacing(COL_SPACING)
        self.sessions_box.connect_after('draw', self._draw_col_seps)

        self.sessions_scroll = Gtk.ScrolledWindow()
        self.sessions_scroll.set_name('sessions-scroll')
        self.sessions_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.sessions_scroll.set_shadow_type(Gtk.ShadowType.NONE)
        # La zone réclame sa hauteur naturelle (jusqu'au plafond) pour suivre le
        # nombre de sessions. La largeur naturelle, elle, n'est propagée qu'en
        # mode auto-width (géré dans _apply_window_size) — la propager en largeur
        # fixe ferait déborder la fenêtre hors écran sous policy NEVER.
        self.sessions_scroll.set_propagate_natural_height(True)
        self.sessions_scroll.add(self.sessions_box)
        self.main_box.pack_start(self.sessions_scroll, False, False, 0)
        sep_bottom = self._sep()
        self.main_box.pack_start(sep_bottom, False, False, 0)

        # Le ScrolledWindow interpose un Viewport qui peindrait le fond opaque du
        # thème par-dessus notre fond arrondi semi-transparent : on le force en
        # transparent (ciblé par #sessions-scroll pour ne pas toucher d'autres vues).
        _scroll_css = Gtk.CssProvider()
        _scroll_css.load_from_data(
            b'#sessions-scroll, #sessions-scroll viewport '
            b'{ background-color: transparent; background-image: none; }')
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), _scroll_css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        footer.set_margin_start(8)
        footer.set_margin_end(8)
        footer.set_margin_top(5)
        footer.set_margin_bottom(0)
        # Version label — colored by update state, clickable (opens About).
        self._latest_version = None
        self._update_state   = 'checking'   # checking | ok | old | unknown
        self._lbl_version = Gtk.Label()
        self._lbl_version.set_halign(Gtk.Align.END)
        ver_evt = Gtk.EventBox()
        ver_evt.set_visible_window(False)
        ver_evt.add(self._lbl_version)
        ver_evt.connect('button-press-event', self._on_version_press)
        ver_evt.connect('realize', self._on_version_realize)
        self._ver_evt = ver_evt
        self._render_version_label()
        footer.pack_end(ver_evt, False, False, 0)

        # Footer draggable too — same handler as header (widget-agnostic).
        footer_evt = Gtk.EventBox()
        footer_evt.set_visible_window(False)  # let the toplevel custom bg paint through
        footer_evt.add(footer)
        footer_evt.connect('button-press-event', self._on_header_press)
        self.main_box.pack_start(footer_evt, False, False, 0)

        # Shade (roll-up): everything below the header can be collapsed
        self._rolled = False
        self._roll_widgets = [sep_top, self.sessions_scroll, sep_bottom, footer_evt]
        self._update_chevron()

        # ── Init ────────────────────────────────────────────────────────────
        if cfg.auto_width:
            self.set_default_size(-1, -1)
        else:
            self.set_default_size(self._window_width(), -1)
        self._apply_window_size()
        self.connect('realize', self._on_realize)
        self.connect('enter-notify-event', self._on_enter_window)
        self.connect('leave-notify-event', self._on_leave_window)
        self.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.BUTTON_PRESS_MASK
                        | Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.SMOOTH_SCROLL_MASK)
        self.connect('motion-notify-event',  self._on_drag_motion)
        self.connect('button-release-event', self._on_drag_release)
        self.connect('button-press-event',   self._on_window_press)
        self.connect('scroll-event',         self._on_scroll)
        self._setup_status_monitor()
        self._refresh()
        self._refresh_timer_id = GLib.timeout_add(cfg.refresh_ms, self._refresh)
        GLib.timeout_add(600, self._tick_anim)
        self._check_latest_version_async()
        GLib.timeout_add_seconds(6 * 3600, self._recheck_version_tick)

    # ── Snooze ────────────────────────────────────────────────────────────────

    def _is_snoozed(self) -> bool:
        return time.time() < self._snooze_until

    def _snooze_wakeup(self):
        self._snooze_until = 0
        self._snooze_timer = None
        # Full unhide (mirrors _toggle_visibility): the widget is completely
        # gone while snoozed, not merely ghosted, so bring it back and
        # reposition it.
        if not self._hidden:
            Gtk.Widget.set_opacity(self, self._alpha)
            self.show_all()
            GLib.idle_add(self._reposition)
        if self._tray:
            self._update_tray_menu_labels()
        return False

    def _toggle_snooze(self):
        if self._is_snoozed():
            if self._snooze_timer is not None:
                GLib.source_remove(self._snooze_timer)
                self._snooze_timer = None
            self._snooze_wakeup()
        else:
            self._snooze_until = time.time() + CFG.snooze_sec
            # Hide entirely (not a 0.08 ghost) and wake up on its own after
            # snooze_sec via the timer below.
            self.hide()
            self._snooze_timer = GLib.timeout_add_seconds(CFG.snooze_sec, self._snooze_wakeup)
        # Retitle immediately — waiting for the next refresh tick leaves a
        # stale label if the menu is reopened right away.
        if self._tray:
            self._update_tray_menu_labels()

    def _on_enter_window(self, widget, event):
        # Snooze now hides the window outright, so there is nothing to reveal
        # on hover — kept as a no-op for the connected signal.
        return False

    def _on_leave_window(self, widget, event):
        return False

    def _on_window_press(self, _widget, event):
        # Session rows (focus) and the header (drag) only consume left
        # clicks, so middle/right click work anywhere on the widget.
        if event.button == 2:
            # Middle click: snooze (fade for CFG.snooze_sec) / wake up
            self._toggle_snooze()
            return True
        # Right click pops the same menu as the tray
        if event.button != 3:
            return False
        if self._tray_menu is None:
            self._tray_menu = self._build_tray_menu()
        self._update_tray_menu_labels()
        self._tray_menu.popup_at_pointer(event)
        return True

    def _update_chevron(self):
        # ▾ expanded (content shown below) / ▸ rolled (collapsed to a pill).
        glyph = '▸' if self._rolled else '▾'
        self._chevron.set_markup(
            f'<span foreground="{COLOR_TITLE}" font="Monospace 16">{glyph}</span>'
        )

    def _on_chevron_press(self, _widget, event):
        if event.button != 1:
            return False
        self._set_rolled(not self._rolled)
        return True  # consume — don't start a header drag

    # ── Version / update check ────────────────────────────────────────────────

    def _on_version_press(self, _widget, event):
        if event.button != 1:
            return False
        self._show_about(self.ABOUT_PAGE_VERSION)
        return True  # consume — don't start a footer drag

    def _on_version_realize(self, widget):
        # Hand cursor over the (input-only) version window → signals it's clickable.
        win = widget.get_window()
        if win:
            win.set_cursor(Gdk.Cursor.new_from_name(widget.get_display(), 'pointer'))

    def _render_version_label(self):
        """Paint the footer version label + tooltip from the current state."""
        color = {'ok': COLOR_VER_OK, 'old': COLOR_VER_OLD}.get(self._update_state, TEXT_DIM2)
        self._lbl_version.set_markup(f'<span font_desc="8" color="{color}">v{VERSION}</span>')

        if self._update_state == 'ok':
            tip = f"{tr('ver_uptodate')} — v{VERSION}"
        elif self._update_state == 'old':
            tip = (f"{tr('ver_outdated')} : v{self._latest_version}\n"
                   f"{tr('ver_current')} : v{VERSION}")
        elif self._update_state == 'unknown':
            tip = tr('ver_unknown')
        else:
            tip = tr('ver_checking')
        self._ver_evt.set_tooltip_text(f"{tip}\n{tr('ver_click_hint')}")

    def _check_latest_version_async(self):
        """Fetch the latest GitHub release tag off the main loop."""
        def worker():
            latest = None
            try:
                req = urllib.request.Request(
                    GITHUB_RELEASES_API,
                    headers={'User-Agent': 'claude-watcher-gtk',
                             'Accept': 'application/vnd.github+json'},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                latest = (data.get('tag_name') or '').lstrip('v') or None
            except Exception:
                latest = None  # offline / no release / rate-limited → unknown
            GLib.idle_add(self._apply_version_check, latest)
        threading.Thread(target=worker, daemon=True).start()

    def _recheck_version_tick(self):
        self._check_latest_version_async()
        return True  # keep the periodic timer alive

    def _apply_version_check(self, latest):
        if latest is None:
            self._update_state, self._latest_version = 'unknown', None
        else:
            self._latest_version = latest
            self._update_state = 'old' if _semver_tuple(latest) > _semver_tuple(VERSION) else 'ok'
        self._render_version_label()
        return False  # one-shot idle

    def _set_rolled(self, rolled: bool):
        # Shade: collapse everything below the header, WM roll-up style.
        # no_show_all keeps the periodic _rebuild_sessions() show_all() from
        # un-hiding the rows while rolled.
        if rolled == self._rolled:
            return
        self._rolled = rolled
        self._update_chevron()
        for w in self._roll_widgets:
            w.set_no_show_all(rolled)
            if rolled:
                w.hide()
            else:
                w.show_all()
        # Rolled: compact pill — drop the header/footer padding so the window
        # shrinks to the title's natural size. _apply_window_size gère la
        # bascule des contraintes de taille (pilule ↔ largeur fixe + hauteur).
        self._header.set_margin_bottom(0 if rolled else 8)
        self.main_box.set_margin_bottom(12 if rolled else 15)
        self._apply_window_size()
        # Re-anchor: size changed, bottom/right corners must stay put
        GLib.idle_add(self._reposition)

    def _on_header_scroll(self, _widget, event):
        # Plain wheel on the title bar shades/unshades; Shift+wheel keeps its
        # opacity meaning by bubbling up to the window scroll handler.
        if event.state & Gdk.ModifierType.SHIFT_MASK:
            return False
        if event.direction == Gdk.ScrollDirection.UP:
            self._set_rolled(True)
        elif event.direction == Gdk.ScrollDirection.DOWN:
            self._set_rolled(False)
        elif event.direction == Gdk.ScrollDirection.SMOOTH and event.delta_y:
            self._set_rolled(event.delta_y < 0)
        else:
            return False
        return True

    def _effective_alpha(self) -> float:
        # Single perceived opacity exposed to scroll + settings (nominal:
        # self._alpha is not touched by the snooze ghosting)
        return self._alpha * self._bg_alpha

    def _set_effective_alpha(self, e: float):
        # Decomposed over two layers around the saved base (CFG.bg_alpha):
        # above it the background densifies (text stays opaque), below it
        # the whole window fades — 0.2 floor keeps the widget findable.
        e = max(0.2, min(1.0, e))
        base = CFG.bg_alpha / 100.0
        if e >= base:
            self._alpha, self._bg_alpha = 1.0, e
        else:
            self._alpha, self._bg_alpha = e / base, base
        if not self._is_snoozed():
            Gtk.Widget.set_opacity(self, self._alpha)
        self.queue_draw()

    def _on_scroll(self, _widget, event):
        # Shift + wheel adjusts the effective opacity
        if not event.state & Gdk.ModifierType.SHIFT_MASK:
            return False
        if event.direction == Gdk.ScrollDirection.UP:
            delta = 0.05
        elif event.direction == Gdk.ScrollDirection.DOWN:
            delta = -0.05
        elif event.direction == Gdk.ScrollDirection.SMOOTH:
            delta = -0.05 * event.delta_y
        else:
            return False
        self._set_effective_alpha(self._effective_alpha() + delta)
        return True

    # ── Keyboard navigation ───────────────────────────────────────────────────

    _KB_NAV_KEYS = ('Up', 'Down', 'Return', 'KP_Enter', 'Escape')

    def _init_keybinder(self):
        Keybinder.init()
        result = Keybinder.bind(CFG.hotkey, lambda _k: self._activate_kb_nav())
        if not result:
            if self._kb_bind_retries < 10:
                self._kb_bind_retries += 1
                GLib.timeout_add(500, self._init_keybinder)
            # else: hotkey already taken by another app — give up silently
        return False

    def _rebind_hotkey(self, enable: bool, hotkey: str):
        """Re-register the global hotkey after a settings change."""
        if not HAS_KEYBINDER:
            CFG.shortcut_enable, CFG.hotkey = enable, hotkey
            return
        if enable == CFG.shortcut_enable and hotkey == CFG.hotkey:
            return
        # Drop any in-progress navigation and the old binding.
        if self._kb_index >= 0:
            self._kb_deactivate()
        if CFG.shortcut_enable and CFG.hotkey:
            try:
                Keybinder.unbind(CFG.hotkey)
            except Exception:
                pass
        CFG.shortcut_enable, CFG.hotkey = enable, hotkey
        self._kb_bind_retries = 0
        if enable and hotkey:
            self._init_keybinder()

    def _activate_kb_nav(self):
        if not self.sessions:
            return
        if self._kb_index >= 0:
            # Deuxième appui sur le raccourci : annule la nav
            self._kb_deactivate()
            return
        self._kb_select(0)
        for key in self._KB_NAV_KEYS:
            try:
                Keybinder.bind(key, self._on_keybinder_nav)
            except Exception:
                pass

    def _on_keybinder_nav(self, keystring):
        rows = self._session_rows
        if keystring == 'Up':
            self._kb_select(max(0, self._kb_index - 1))
        elif keystring == 'Down':
            self._kb_select(min(len(rows) - 1, self._kb_index + 1))
        elif keystring in ('Return', 'KP_Enter'):
            if 0 <= self._kb_index < len(rows):
                rows[self._kb_index]._do_focus()
            # Différer l'unbind : appeler Keybinder.unbind() depuis l'intérieur
            # du callback Keybinder provoque un crash par réentrance.
            GLib.idle_add(self._kb_deactivate)
        elif keystring == 'Escape':
            GLib.idle_add(self._kb_deactivate)

    def _kb_select(self, index: int):
        self._kb_index = index
        self._refresh_kb_highlight()

    def _kb_deactivate(self):
        for key in self._KB_NAV_KEYS:
            try:
                Keybinder.unbind(key)
            except Exception:
                pass
        self._kb_index = -1
        self._refresh_kb_highlight()

    def _refresh_kb_highlight(self):
        for i, row in enumerate(self._session_rows):
            row.set_kb_selected(i == self._kb_index)

    # ── Systray ───────────────────────────────────────────────────────────────

    def _tray_icon_path(self, color_hex: str) -> str:
        # Security: per-user cache dir, not /tmp — a predictable world-writable
        # path could be pre-created by another local user (icon spoofing,
        # attacker-controlled SVG fed to librsvg).
        cache = Path(GLib.get_user_cache_dir()) / 'claude-watcher'
        logo  = CONFIG_PATH.parent / 'claude-logo.svg'
        if logo.exists():
            try:
                # Deterministic per (color, logo mtime) → no rewrite on every
                # tick, and the cache self-invalidates when the logo changes.
                path = cache / f'tray-{color_hex.lstrip("#")}-{int(logo.stat().st_mtime)}.svg'
                if not path.exists():
                    svg = logo.read_text()
                    # Replace the logo's original fill color with the status color
                    svg = re.sub(r'(<path\b[^>]*\bfill=")[^"]*(")', rf'\g<1>{color_hex}\2', svg)
                    cache.mkdir(parents=True, exist_ok=True)
                    path.write_text(svg)
                return str(path)
            except OSError:
                pass  # disk full / perms — fall back to the PNG circle below
        # Fallback: plain colored circle as PNG
        size = 22
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        cr = cairo.Context(surface)
        c  = Gdk.RGBA()
        c.parse(color_hex)
        cr.set_source_rgba(c.red, c.green, c.blue, 1)
        cr.arc(size / 2, size / 2, size / 2 - 3, 0, 2 * math.pi)
        cr.fill()
        png_path = cache / f'tray-{color_hex.lstrip("#")}.png'
        try:
            cache.mkdir(parents=True, exist_ok=True)
            surface.write_to_png(str(png_path))
        except (OSError, cairo.Error):
            # Never raise into the GLib timeout: an exception in the callback
            # removes the refresh source and silently freezes the widget.
            pass
        return str(png_path)

    def _build_tray_menu(self) -> Gtk.Menu:
        """Tray menu, built ONCE — rebuilding it on every refresh leaks on
        the C side (dbusmenu export); dynamic labels are updated in place
        via _update_tray_menu_labels()."""
        menu = Gtk.Menu()
        # Kept as attributes so _update_tray_menu_labels() can retitle in place
        self._mi_show = mi_show = Gtk.MenuItem(label=tr('show') if self._hidden else tr('hide'))
        mi_show.connect('activate', lambda _m: self._toggle_visibility())
        snooze_label = tr('snooze_wake') if self._is_snoozed() else f"{tr('snooze_hide')} {CFG.snooze_sec // 60}m"
        self._mi_snooze = mi_snooze = Gtk.MenuItem(label=snooze_label)
        mi_snooze.connect('activate', lambda _m: self._toggle_snooze())
        self._mi_about = mi_about = Gtk.MenuItem(label=tr('about'))
        mi_about.connect('activate', lambda _m: self._show_about())
        self._mi_quit  = mi_quit  = Gtk.MenuItem(label=tr('quit'))
        mi_quit.connect('activate', lambda _m: Gtk.main_quit())
        self._mi_settings = mi_settings = Gtk.MenuItem(label=tr('settings_menu'))
        mi_settings.connect('activate', lambda _m: self._open_settings())
        for mi in (mi_show, mi_snooze, Gtk.SeparatorMenuItem(), mi_settings, Gtk.SeparatorMenuItem(), mi_about, Gtk.SeparatorMenuItem(), mi_quit):
            menu.append(mi)
        menu.show_all()
        return menu

    # Notebook page indices, in append order.
    ABOUT_PAGE_GENERAL = 0
    ABOUT_PAGE_VERSION = 1
    ABOUT_PAGE_CREDITS = 2

    def _show_about(self, page: int = ABOUT_PAGE_GENERAL):
        dlg = Gtk.Dialog(title="Claude Code Watcher", transient_for=self, modal=True)
        dlg.set_default_size(380, 300)
        dlg.set_position(Gtk.WindowPosition.CENTER)
        dlg.add_button(tr('close'), Gtk.ResponseType.CLOSE)

        nb = Gtk.Notebook()
        nb.set_border_width(8)
        nb.append_page(self._about_tab_general(), Gtk.Label(label=tr('tab_about')))
        nb.append_page(self._about_tab_version(), Gtk.Label(label=tr('tab_version')))
        nb.append_page(self._about_tab_credits(), Gtk.Label(label=tr('tab_credits')))
        dlg.get_content_area().pack_start(nb, True, True, 0)

        dlg.show_all()
        nb.set_current_page(page)  # must run after show_all() or GTK ignores it
        dlg.run()
        dlg.destroy()

    def _about_tab_general(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(18)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_size(str(CONFIG_DIR / 'claude-logo.svg'), 64, 64)
            box.pack_start(Gtk.Image.new_from_pixbuf(pb), False, False, 0)
        except Exception:
            pass
        name = Gtk.Label()
        name.set_markup('<span font="13" weight="bold">Claude Code Watcher</span>')
        box.pack_start(name, False, False, 0)
        desc = Gtk.Label(label="GTK3 desktop widget — monitors running Claude Code sessions.")
        desc.set_line_wrap(True)
        desc.set_justify(Gtk.Justification.CENTER)
        box.pack_start(desc, False, False, 0)
        box.pack_start(Gtk.LinkButton.new_with_label(
            "https://github.com/claude-watcher/gtk", "GitHub"), False, False, 0)
        lic = Gtk.Label()
        lic.set_markup('<span size="small">MIT License</span>')
        box.pack_start(lic, False, False, 0)
        return box

    def _about_tab_version(self) -> Gtk.Grid:
        grid = Gtk.Grid(column_spacing=14, row_spacing=10)
        grid.set_border_width(18)
        grid.set_halign(Gtk.Align.CENTER)
        grid.set_valign(Gtk.Align.CENTER)

        def add_row(r, label, value_markup):
            lbl = Gtk.Label()
            lbl.set_markup(f'<span color="{TEXT_DIM2}">{label}</span>')
            lbl.set_halign(Gtk.Align.END)
            val = Gtk.Label()
            val.set_markup(value_markup)
            val.set_halign(Gtk.Align.START)
            grid.attach(lbl, 0, r, 1, 1)
            grid.attach(val, 1, r, 1, 1)

        add_row(0, tr('ver_current'), f'<b>v{VERSION}</b>')
        add_row(1, tr('ver_latest'), f'v{self._latest_version}' if self._latest_version else '—')

        if self._update_state == 'ok':
            status = f'<span color="{COLOR_VER_OK}">✓ {tr("ver_uptodate")}</span>'
        elif self._update_state == 'old':
            status = f'<span color="{COLOR_VER_OLD}">⚠ {tr("ver_outdated")}</span>'
        else:
            status = f'<span color="{TEXT_DIM2}">{tr("ver_unknown")}</span>'
        add_row(2, tr('ver_status'), status)

        if self._update_state == 'old':
            cmd = ("pkill -f claude-watcher || true && curl -fsSL "
                   "https://github.com/claude-watcher/gtk/releases/latest/download/install.sh | bash")

            cmd_title = Gtk.Label()
            cmd_title.set_markup(f'<span color="{TEXT_DIM2}">{tr("update_cmd")} :</span>')
            cmd_title.set_halign(Gtk.Align.START)
            cmd_title.set_margin_top(8)
            grid.attach(cmd_title, 0, 3, 2, 1)

            # Command in a framed, shaded box (left) + copy button (right).
            cmd_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            cmd_row.pack_start(self._code_box(cmd), True, True, 0)
            copy_btn = Gtk.Button.new_with_label(tr('copy'))
            copy_btn.set_valign(Gtk.Align.CENTER)
            copy_btn.connect('clicked', lambda b: self._copy_to_clipboard(cmd, b))
            cmd_row.pack_start(copy_btn, False, False, 0)
            grid.attach(cmd_row, 0, 4, 2, 1)

            link = Gtk.LinkButton.new_with_label(RELEASES_URL, tr('see_releases'))
            link.set_halign(Gtk.Align.CENTER)
            grid.attach(link, 0, 5, 2, 1)
        return grid

    def _code_box(self, text: str) -> Gtk.Box:
        """A framed, shaded, monospace block holding a wrapping command line."""
        lbl = Gtk.Label()
        lbl.set_markup(f'<tt><span size="small">{GLib.markup_escape_text(text)}</span></tt>')
        lbl.set_selectable(True)
        lbl.set_line_wrap(True)
        lbl.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        lbl.set_max_width_chars(34)
        lbl.set_xalign(0.0)
        lbl.set_margin_top(8)
        lbl.set_margin_bottom(8)
        lbl.set_margin_start(10)
        lbl.set_margin_end(10)
        box = Gtk.Box()
        box.add(lbl)
        ctx = box.get_style_context()
        ctx.add_class('cmd-box')
        provider = Gtk.CssProvider()
        provider.load_from_data(
            b'.cmd-box { background-color: #15151c; '
            b'border: 1px solid #3a3a4a; border-radius: 6px; }')
        ctx.add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        return box

    def _copy_to_clipboard(self, text: str, button: Gtk.Button):
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)
        label = button.get_child()
        if not isinstance(label, Gtk.Label):
            return
        label.set_markup(f'<span color="{COLOR_VER_OK}" weight="bold">✓</span>')
        GLib.timeout_add_seconds(2, lambda: (label.set_text(tr('copy')), False)[1])

    def _about_tab_credits(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_border_width(18)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)

        title = Gtk.Label()
        title.set_markup(f'<span color="{TEXT_DIM2}">{tr("authors")} :</span>')
        title.set_valign(Gtk.Align.START)
        box.pack_start(title, False, False, 0)

        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        kardagan = Gtk.Label()
        kardagan.set_markup('<span font="11">kardagan</span>')
        kardagan.set_halign(Gtk.Align.START)
        names.pack_start(kardagan, False, False, 0)
        babs = Gtk.Label()
        babs.set_markup(f'<a href="https://github.com/babs">babs</a> '
                        f'<span color="{TEXT_DIM2}">(Damien Degois)</span>')
        babs.set_halign(Gtk.Align.START)
        names.pack_start(babs, False, False, 0)
        box.pack_start(names, False, False, 0)
        return box

    def _open_settings(self):
        dlg = SettingsDialog(self)
        response = dlg.run()
        if response == Gtk.ResponseType.OK:
            values = dlg.get_values()
            dlg.destroy()
            self._apply_settings(values)
        else:
            original = dlg._original_values
            dlg.destroy()
            self._preview_settings(original)

    def _preview_settings(self, values: dict):
        """Applique les changements visuels en mémoire sans écrire le config."""
        CFG.lang     = values['lang']
        CFG.screen   = values['screen']
        CFG.corner   = values['corner']
        CFG.margin_x = values['margin_x']
        CFG.margin_y = values['margin_y']
        CFG.show_topic = values['show_topic']  # lu par get_session_info_from_jsonl au _refresh()
        if values['bg_alpha'] != round(self._effective_alpha() * 100):
            self._set_effective_alpha(values['bg_alpha'] / 100.0)
        # _compute_xy lit les attributs d'instance, pas CFG — garder en sync
        self.screen   = values['screen']
        self.corner   = values['corner']
        self.margin_x = values['margin_x']
        self.margin_y = values['margin_y']

        CFG.width      = values['width']
        CFG.auto_width = values['auto_width']
        CFG.columns    = values['columns']
        CFG.max_height = values['max_height']
        # Tri + format d'inactivité : relus par scan_sessions / SessionRow au
        # _refresh() final ci-dessous.
        CFG.sort_mode   = values['sort_mode']
        CFG.idle_format = values['idle_format']
        # _apply_window_size recalcule largeur (colonnes × largeur) + plafond de
        # hauteur scrollable, et respecte le mode roulé / largeur auto. Le
        # ré-agencement des colonnes se fait dans le _refresh() final (lit CFG.columns).
        self._apply_window_size()

        if values['free']:
            if self._user_pos is None:
                wx, wy = self.get_position()
                self._user_pos = (wx, wy)
                self._save_position()
        else:
            if self._user_pos is not None:
                self._user_pos = None
                try:
                    POS_FILE.unlink(missing_ok=True)
                except Exception:
                    pass
            GLib.idle_add(self._reposition)

        self._refresh()

    def _apply_settings(self, values: dict):
        """Écrit config.ini et applique tous les paramètres."""
        cfg_file = configparser.ConfigParser()
        cfg_file.read(CONFIG_PATH)
        for section in ('general', 'display', 'features'):
            if section not in cfg_file:
                cfg_file[section] = {}
        cfg_file['general']['lang']       = values['lang']
        cfg_file['general']['hotkey']     = values['hotkey']
        cfg_file['features']['shortcut_enable'] = 'true' if values['shortcut_enable'] else 'false'
        cfg_file['features']['show_topic'] = 'true' if values['show_topic'] else 'false'
        cfg_file['display']['mode']       = 'free' if values['free'] else 'corner'
        cfg_file['display']['screen']     = str(values['screen'])
        cfg_file['display']['corner']     = values['corner']
        cfg_file['display']['margin_x']   = str(values['margin_x'])
        cfg_file['display']['margin_y']   = str(values['margin_y'])
        cfg_file['display']['width']      = str(values['width'])
        cfg_file['display']['auto_width'] = 'true' if values['auto_width'] else 'false'
        cfg_file['display']['columns']    = str(values['columns'])
        cfg_file['display']['max_height'] = str(values['max_height'])
        cfg_file['display']['sort_mode']  = values['sort_mode']
        cfg_file['display']['idle_format'] = values['idle_format']
        cfg_file['display']['refresh_ms'] = str(values['refresh_ms'])
        cfg_file['display']['snooze_sec'] = str(values['snooze_sec'])
        cfg_file['display']['bg_alpha']   = str(values['bg_alpha'])
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with CONFIG_PATH.open('w') as f:
                cfg_file.write(f)
        except OSError:
            pass

        new_refresh = values['refresh_ms']
        if new_refresh != CFG.refresh_ms:
            CFG.refresh_ms = new_refresh
            GLib.source_remove(self._refresh_timer_id)
            self._refresh_timer_id = GLib.timeout_add(CFG.refresh_ms, self._refresh)

        CFG.snooze_sec = values['snooze_sec']
        self._rebind_hotkey(values['shortcut_enable'], values['hotkey'])
        CFG.bg_alpha   = values['bg_alpha']  # new base = floor for shift+scroll
        # Renormalize unconditionally: even when the value didn't change, the
        # window/background decomposition must match what a restart would give
        # (preview skips equal values, leaving a scroll-faded window in place).
        self._set_effective_alpha(values['bg_alpha'] / 100.0)
        self._preview_settings(values)

    def _init_tray(self):
        if HAS_APPINDICATOR:
            self._tray = AppIndicator3.Indicator.new(
                'claude-watcher',
                self._tray_icon_path(TEXT_DIM),
                AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
            )
            self._tray.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            self._tray_menu = self._build_tray_menu()
            self._tray.set_menu(self._tray_menu)
        else:
            self._tray_menu = self._build_tray_menu()
            self._tray = Gtk.StatusIcon()
            self._tray.set_title("Claude Code Watcher")
            self._tray.connect('activate',   lambda _i: self._toggle_visibility())
            self._tray.connect('popup-menu', self._on_tray_menu_legacy)
            self._tray.set_from_pixbuf(
                Gdk.pixbuf_get_from_surface(
                    self._tray_icon_surface(TEXT_DIM), 0, 0, 22, 22))

    def _tray_icon_surface(self, color_hex: str, size: int = 22):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        cr = cairo.Context(surface)
        c  = Gdk.RGBA()
        c.parse(color_hex)
        cr.set_source_rgba(c.red, c.green, c.blue, 1)
        cr.arc(size / 2, size / 2, size / 2 - 3, 0, 2 * math.pi)
        cr.fill()
        return surface

    def _update_tray_menu_labels(self):
        self._mi_show.set_label(tr('show') if self._hidden else tr('hide'))
        if self._is_snoozed():
            snooze_lbl = tr('snooze_wake')
        elif CFG.snooze_sec < 60:
            snooze_lbl = f"{tr('snooze_hide')} {CFG.snooze_sec}s"
        else:
            snooze_lbl = f"{tr('snooze_hide')} {CFG.snooze_sec // 60}m"
        self._mi_snooze.set_label(snooze_lbl)
        self._mi_settings.set_label(tr('settings_menu'))
        self._mi_about.set_label(tr('about'))
        self._mi_quit.set_label(tr('quit'))

    def _update_tray(self, waiting: int, working: int, total: int):
        if not self._tray:
            return
        if waiting:   color = COLOR_WAITING
        elif working: color = COLOR_WORKING
        elif total:   color = COLOR_IDLE
        else:         color = TEXT_DIM
        tooltip = (
            f"{waiting} {tr('waiting')} · {working} {tr('working')} · {total} total"
            if total else tr('no_session')
        )
        if HAS_APPINDICATOR:
            self._tray.set_icon_full(self._tray_icon_path(color), tooltip)
            self._update_tray_menu_labels()
        else:
            self._tray.set_from_pixbuf(
                Gdk.pixbuf_get_from_surface(
                    self._tray_icon_surface(color), 0, 0, 22, 22))
            self._tray.set_tooltip_text(tooltip)

    def _toggle_visibility(self):
        if self._hidden:
            self._hidden = False
            self.show_all()
            GLib.idle_add(self._reposition)
        else:
            self._hidden = True
            self.hide()
        # Retitle immediately — waiting for the next refresh tick leaves a
        # stale label if the menu is reopened right away.
        if self._tray:
            self._update_tray_menu_labels()

    def _on_tray_menu_legacy(self, icon, button, activate_time):
        self._update_tray_menu_labels()
        self._tray_menu.popup(None, None, Gtk.StatusIcon.position_menu,
                              icon, button, activate_time)

    # ── UI helpers ────────────────────────────────────────────────────────────

    def _sep(self):
        sep = Gtk.DrawingArea()
        sep.set_size_request(-1, 1)
        sep.connect('draw', lambda w, cr: (
            cr.set_source_rgba(1, 1, 1, 0.07),
            cr.rectangle(0, 0, w.get_allocated_width(), 1),
            cr.fill()
        ))
        return sep

    def _draw_col_seps(self, widget, cr):
        """Trait vertical au milieu de chaque gouttière inter-colonnes.

        Dessiné en connect_after (par-dessus les lignes déjà rendues) plutôt
        qu'avec des colonnes de séparateurs : ça préserve les colonnes homogènes
        et l'indexation row-major (i % cols) de _rebuild_sessions.
        """
        cols = self._effective_cols()
        if cols < 2:
            return False
        w = widget.get_allocated_width()
        h = widget.get_allocated_height()
        s = self.sessions_box.get_column_spacing()
        colw = (w - s * (cols - 1)) / cols   # largeur d'une colonne (homogène)
        cr.set_source_rgba(1, 1, 1, 0.07)
        for c in range(1, cols):
            # Milieu de la gouttière entre la colonne c-1 et la colonne c.
            x = round(c * colw + (c - 1) * s + s / 2)
            cr.rectangle(x, 4, 1, max(0, h - 8))
            cr.fill()
        return False

    def _draw_bg(self, widget, cr):
        w, h, r = widget.get_allocated_width(), widget.get_allocated_height(), 10
        cr.set_source_rgba(*BG_RGB, self._bg_alpha)
        cr.move_to(r, 0)
        cr.line_to(w - r, 0)
        cr.arc(w - r, r,     r, -math.pi / 2, 0)
        cr.line_to(w, h - r)
        cr.arc(w - r, h - r, r,  0,            math.pi / 2)
        cr.line_to(r, h)
        cr.arc(r,     h - r, r,  math.pi / 2,  math.pi)
        cr.line_to(0, r)
        cr.arc(r,     r,     r,  math.pi,      -math.pi / 2)
        cr.close_path()
        cr.fill()

    # ── Positionnement ────────────────────────────────────────────────────────

    def _effective_cols(self) -> int:
        """Nb de colonnes réellement utilisé = min(config, nb de sessions).

        Inutile de réserver des colonnes vides : avec 4 colonnes configurées mais
        2 sessions, on n'en affiche que 2 (sinon la fenêtre s'étire et des
        séparateurs sont tracés sur du vide). Minimum 1 (état « aucune session »).
        """
        cols = max(1, getattr(CFG, 'columns', 1))
        n = len(self.sessions)
        return min(cols, n) if n else 1

    def _window_width(self) -> int:
        """Largeur totale = (largeur de colonne × nb colonnes effectif) + gouttières."""
        cols = self._effective_cols()
        return CFG.width * cols + COL_SPACING * (cols - 1)

    def _max_content_height(self) -> int:
        """Plafond de hauteur de la zone scrollable des sessions.

        Toujours borné par l'écran (hauteur du moniteur moins la marge basse et
        une réserve pour header/footer/séparateurs) ; CFG.max_height (si > 0) ne
        fait que réduire davantage. max_height vide/0 → seul l'écran limite.
        """
        geom = self._get_monitor_geom()
        screen_cap = max(120, geom.height - self.margin_y - 140)
        mh = getattr(CFG, 'max_height', 0)
        return min(screen_cap, mh) if mh else screen_cap

    def _apply_window_size(self):
        """(Ré)applique la largeur fenêtre et le plafond de hauteur scrollable.

        Idempotent — rejouable à chaque changement de config (colonnes, largeur,
        hauteur max, écran). Ne touche pas la largeur en mode roulé (pilule) ni
        en largeur auto (la fenêtre suit alors sa taille naturelle)."""
        self.sessions_scroll.set_max_content_height(self._max_content_height())
        # propagate_natural_width UNIQUEMENT en largeur auto : sous policy
        # horizontale NEVER, propager la largeur naturelle fait réclamer à la
        # grille la largeur PLEINE des lignes (labels non ellipsés) → débordement.
        self.sessions_scroll.set_propagate_natural_width(CFG.auto_width)

        if self._rolled:
            # Pilule enroulée : pas de contrainte, on laisse rétrécir au minimum.
            self.set_geometry_hints(None, None, Gdk.WindowHints(0))
            self._last_size = None
            self.set_size_request(-1, -1)
            self.resize(1, 1)
            return
        if CFG.auto_width:
            # Largeur auto : la fenêtre suit sa taille naturelle (largeur ET
            # hauteur). Resizable → on resize explicitement, sinon elle reste figée.
            self.set_geometry_hints(None, None, Gdk.WindowHints(0))
            self.set_size_request(-1, -1)
            nat_w = min(self.get_preferred_width()[1], self._window_width())
            nat_h = self.get_preferred_height()[1]
            # nat_* peut valoir 0 avant que la fenêtre soit réalisée (init) →
            # resize(_, 0) déclenche un Gtk-CRITICAL. On attend une taille valide.
            if nat_w > 0 and nat_h > 0 and self._last_size != (nat_w, nat_h):
                self._last_size = (nat_w, nat_h)
                self.resize(nat_w, nat_h)
            return

        # Largeur fixe : la largeur est FORCÉE via geometry hints (min = max = w)
        # — sinon set_resizable(False) dimensionnerait à la largeur NATURELLE, qui
        # gonfle dès que l'en-tête (compteurs « N attente · … ») dépasse w et fait
        # sortir la fenêtre de l'écran ; le contenu ellipsable s'adapte à w.
        # La hauteur, elle, reste libre dans le hint mais doit être posée
        # explicitement (fenêtre resizable) à la hauteur naturelle du contenu,
        # elle-même plafonnée par max_content_height du scroll (→ barre de défilement).
        w = self._window_width()
        geo = Gdk.Geometry()
        geo.min_width = geo.max_width = w
        geo.min_height = 1
        geo.max_height = 1 << 20
        self.set_geometry_hints(None, geo, Gdk.WindowHints.MIN_SIZE | Gdk.WindowHints.MAX_SIZE)
        self.set_size_request(w, -1)
        nat_h = self.get_preferred_height()[1]
        # nat_h == 0 avant réalisation de la fenêtre (init) → resize(w, 0) lève un
        # Gtk-CRITICAL ; on saute et le prochain refresh (fenêtre réalisée) posera
        # la bonne hauteur.
        if nat_h > 0 and self._last_size != (w, nat_h):
            self._last_size = (w, nat_h)
            self.resize(w, nat_h)

    def _get_monitor_geom(self):
        display = Gdk.Display.get_default()
        idx = max(0, min(self.screen, display.get_n_monitors() - 1))
        return display.get_monitor(idx).get_geometry()

    def _compute_xy(self, h: int) -> tuple[int, int]:
        """Coordonnées coin haut-gauche. Position libre si draggée ou --x/--y."""
        if self._user_pos is not None:
            return self._user_pos
        geom = self._get_monitor_geom()
        w = (min(self.get_preferred_width()[1], self._window_width())
             if CFG.auto_width else self._window_width())
        if self.corner in ('top-left', 'bottom-left'):
            x = geom.x + self.margin_x
        else:
            x = geom.x + geom.width - w - self.margin_x
        if self.corner in ('top-left', 'top-right'):
            y = geom.y + self.margin_y
        else:
            y = geom.y + geom.height - h - self.margin_y
        return x, y

    def _init_layer_shell(self):
        """Configure gtk-layer-shell pour le mode Wayland/GNOME.

        GNOME Shell ne supporte pas wlr-layer-shell. Si is_supported() retourne
        False, on re-lance sous XWayland (GDK_BACKEND=x11) pour retrouver le
        comportement overlay complet via le code X11 existant.
        """
        try:
            if not GtkLayerShell.is_supported():
                os.environ['GDK_BACKEND'] = 'x11'
                os.execv(sys.executable, [sys.executable] + sys.argv)
        except AttributeError:
            pass  # version sans is_supported() — on tente quand même
        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.BOTTOM)
        GtkLayerShell.set_namespace(self, 'claude-watcher')
        display = Gdk.Display.get_default()
        idx = max(0, min(self.screen, display.get_n_monitors() - 1))
        GtkLayerShell.set_monitor(self, display.get_monitor(idx))
        if 'bottom' in self.corner:
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.BOTTOM, True)
            GtkLayerShell.set_margin(self, GtkLayerShell.Edge.BOTTOM, self.margin_y)
        else:
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.TOP, True)
            GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, self.margin_y)
        if 'right' in self.corner:
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.RIGHT, True)
            GtkLayerShell.set_margin(self, GtkLayerShell.Edge.RIGHT, self.margin_x)
        else:
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.LEFT, True)
            GtkLayerShell.set_margin(self, GtkLayerShell.Edge.LEFT, self.margin_x)
        GtkLayerShell.auto_exclusive_zone_enable(self)

    def _on_realize(self, widget):
        self.show_all()
        if not IS_WAYLAND:
            _, h = self.get_preferred_height()
            self.move(*self._compute_xy(h))
            GLib.idle_add(self._apply_strut)
        if HAS_KEYBINDER and CFG.shortcut_enable and CFG.hotkey:
            # Délai 300ms : la fenêtre POPUP doit être complètement mappée sur
            # X11 avant que XGrabKey puisse s'enregistrer correctement.
            GLib.timeout_add(300, self._init_keybinder)

    def _apply_strut(self):
        """Réserve une bande X11 (_NET_WM_STRUT_PARTIAL) pour les fenêtres maximisées.

        Position libre (drag ou --x/--y) → pas de bord d'ancrage → pas de strut.
        Fullscreen ignore les struts par design X11 — comportement attendu.
        """
        if IS_WAYLAND:
            return False
        if self._user_pos is not None:
            return False
        try:
            win  = self.get_window()
            _, h = self.get_preferred_height()
            x, _ = self._compute_xy(h)
            band   = h + self.margin_y
            h_end  = x + self._window_width()
            strut  = [0] * 12
            if self.corner in ('top-left', 'top-right'):
                strut[2] = band
                strut[8], strut[9] = x, h_end
            else:
                strut[3] = band
                strut[10], strut[11] = x, h_end
            win.property_change(
                Gdk.Atom.intern('_NET_WM_STRUT_PARTIAL', False),
                Gdk.Atom.intern('CARDINAL', False),
                32, Gdk.PropMode.REPLACE, strut,
            )
        except Exception:
            pass
        return False

    def _reposition(self):
        if IS_WAYLAND:
            return False
        if self._user_pos is not None:
            return False
        _, h = self.get_preferred_height()
        self.move(*self._compute_xy(h))
        GLib.idle_add(self._apply_strut)
        return False

    # ── Drag & persistance position ───────────────────────────────────────────

    def _on_header_press(self, widget, event):
        if IS_WAYLAND or event.button != 1:
            return False
        if self._user_pos is None:
            return False  # mode ancré — drag désactivé
        self._dragging = True
        wx, wy = self.get_position()
        self._drag_off = (event.x_root - wx, event.y_root - wy)
        try:
            event.get_device().get_seat().grab(
                self.get_window(),
                Gdk.SeatCapabilities.POINTER,
                False, None, event, None,
            )
        except Exception:
            pass
        return True

    def _on_drag_motion(self, widget, event):
        if not self._dragging:
            return False
        x = int(event.x_root - self._drag_off[0])
        y = int(event.y_root - self._drag_off[1])
        self.move(x, y)
        self._user_pos = (x, y)
        return False

    def _on_drag_release(self, widget, event):
        if self._dragging and event.button == 1:
            self._dragging = False
            try:
                event.get_device().get_seat().ungrab()
            except Exception:
                pass
            self._schedule_save()
        return False

    def _schedule_save(self):
        if self._save_timer:
            GLib.source_remove(self._save_timer)
        self._save_timer = GLib.timeout_add(400, self._save_position_tick)

    def _save_position_tick(self):
        self._save_timer = 0
        self._save_position()
        return False

    def _load_position(self) -> tuple[int, int] | None:
        try:
            d = json.loads(POS_FILE.read_text())
            x, y = int(d['x']), int(d['y'])
            # Vérifier que la position est dans les bounds de l'espace d'affichage total
            display = Gdk.Display.get_default()
            n = display.get_n_monitors()
            for i in range(n):
                g = display.get_monitor(i).get_geometry()
                if g.x <= x < g.x + g.width and g.y <= y < g.y + g.height:
                    return x, y
            # Position hors-champ (ex: écran déconnecté) → retomber sur le coin configuré
            return None
        except Exception:
            return None

    def _save_position(self):
        if self._user_pos is None:
            return
        try:
            POS_FILE.parent.mkdir(parents=True, exist_ok=True)
            POS_FILE.write_text(
                json.dumps({'x': self._user_pos[0], 'y': self._user_pos[1]}) + '\n'
            )
        except Exception:
            pass

    # ── Refresh ───────────────────────────────────────────────────────────────

    def _setup_status_monitor(self):
        """inotify (Gio) sur les dossiers sessions/ — refresh immédiat.

        Claude réécrit <config>/sessions/<pid>.json à chaque changement d'état :
        on rafraîchit dès qu'un fichier bouge, sans attendre le tick de polling.
        Le dossier par défaut est surveillé d'emblée ; les CLAUDE_CONFIG_DIR
        custom sont ajoutés dynamiquement à mesure que le scan les expose
        (_sync_status_monitors), un monitor Gio par dossier.
        """
        self._status_monitors: dict[str, Gio.FileMonitor] = {}
        _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        self._watch_status_dir(_SESSIONS_DIR)

    def _watch_status_dir(self, path: Path) -> None:
        """Arme un monitor Gio sur `path` (idempotent ; skip si dossier absent)."""
        key = str(path)
        if key in self._status_monitors or not path.is_dir():
            return
        try:
            mon = Gio.File.new_for_path(key).monitor_directory(
                Gio.FileMonitorFlags.NONE, None,
            )
        except Exception:
            return
        mon.connect('changed', self._on_status_changed)
        self._status_monitors[key] = mon

    def _sync_status_monitors(self) -> None:
        """Surveille le sessions/ de chaque CLAUDE_CONFIG_DIR exposé par le scan."""
        for s in self.sessions:
            cfg = s.get('config_dir')
            if cfg:
                self._watch_status_dir(Path(cfg) / 'sessions')

    def _on_status_changed(self, _monitor, gfile, _other, event_type):
        if event_type in (Gio.FileMonitorEvent.CHANGED, Gio.FileMonitorEvent.CREATED):
            if gfile.get_basename().endswith('.json'):
                self._refresh()

    def _refresh(self):
        self.sessions = scan_sessions()
        self._sync_status_monitors()
        self._rebuild_sessions()
        GLib.idle_add(self._reposition)
        return True

    def _rebuild_sessions(self):
        waiting = sum(1 for s in self.sessions if s['waiting'])
        working = sum(1 for s in self.sessions if s['working'])
        total   = len(self.sessions)

        self._update_tray(waiting, working, total)

        parts = []
        if waiting:
            parts.append(f'<span foreground="{COLOR_WAITING}">{waiting} {tr("waiting")}</span>')
        if working:
            parts.append(f'<span foreground="{COLOR_WORKING}">{working} {tr("working")}</span>')
        if not self.sessions:
            parts.append(f'<span foreground="{TEXT_DIM}">{tr("no_session")}</span>')
        else:
            parts.append(f'<span foreground="{TEXT_DIM}">{total} total</span>')
        self.lbl_counts.set_markup(
            f'<span font="Monospace 8">{" · ".join(parts)}</span>'
        )

        cols = self._effective_cols()
        # Signature de structure : si colonnes + liste ordonnée des pids sont
        # inchangées, on met à jour les lignes EN PLACE (update_session) au lieu de
        # détruire/recréer. Sans ça, chaque refresh (toutes les 2 s ou à chaque
        # event inotify) recrée toutes les SessionRow → le hover sous le curseur
        # clignote et l'anim repart. Le destroy/recreate (qui libère les GdkWindow,
        # cf. fuite RSS ~20 Mo/min avec un simple remove) n'a lieu que sur un vrai
        # changement de structure : ajout/retrait/réordre de session, ou colonnes.
        sig = (cols, tuple(s['pid'] for s in self.sessions))
        if (sig == self._last_rows_sig
                and self.sessions
                and len(self._session_rows) == len(self.sessions)):
            for row, s in zip(self._session_rows, self.sessions):
                row.update_session(s)
        else:
            self._last_rows_sig = sig
            for child in self.sessions_box.get_children():
                # destroy() (pas remove()): libère le GdkWindow de l'EventBox et
                # déconnecte les closures — remove() seul garde les lignes vivantes
                # et la RSS grimpe de ~20 Mo/min.
                child.destroy()
            self._session_rows = []
            if not self.sessions:
                lbl = Gtk.Label()
                lbl.set_markup(
                    f'<span foreground="{TEXT_DIM}" font="Monospace 8">'
                    f'  {tr("no_session")}</span>'
                )
                lbl.set_halign(Gtk.Align.START)
                lbl.set_margin_top(8)
                lbl.set_margin_bottom(8)
                lbl.set_margin_start(12)
                self.sessions_box.attach(lbl, 0, 0, cols, 1)
            else:
                # Remplissage ligne par ligne (row-major) : index i → colonne
                # i%cols, rangée i//cols. hexpand pour que chaque colonne occupe sa
                # part égale de la largeur (grille homogène).
                for i, s in enumerate(self.sessions):
                    row = SessionRow(s)
                    row.set_hexpand(True)
                    self._session_rows.append(row)
                    self.sessions_box.attach(row, i % cols, i // cols, 1, 1)
            self.sessions_box.show_all()

        # On (re)fixe largeur + hauteur (la hauteur peut bouger si un sujet
        # apparaît/disparaît, même structure de pids).
        # La fenêtre étant resizable (pour que nos hints de largeur tiennent), elle
        # ne s'auto-dimensionne plus en hauteur → on resize explicitement à la
        # hauteur naturelle (plafonnée par le scroll). _apply_window_size ne
        # déclenche un resize que si la taille a réellement changé.
        self._apply_window_size()

        if self._kb_index >= 0:
            self._kb_index = min(self._kb_index, len(self.sessions) - 1)
            if self._kb_index >= 0:
                self._refresh_kb_highlight()
            else:
                self._kb_deactivate()

    def _tick_anim(self):
        self._anim_tick = (self._anim_tick + 1) % 6
        for row in self._session_rows:
            if row.session['waiting']:
                row._anim_tick = self._anim_tick
                row.dot.queue_draw()
        return True


# ── Utilitaire ────────────────────────────────────────────────────────────────

def dump_round():
    """Un tour de calcul d'état en texte, sans UI ni dépendance display/Wnck.

    Pour troubleshooter le classement working/waiting : montre les valeurs
    intermédiaires (statut registre brut, état JSONL brut) à côté de l'état
    final réconcilié — exactement ce que calcule `get_session_state`.
    """
    procs = get_claude_processes()
    if not procs:
        print('no claude session found')
        return
    for p in procs:
        pid = p['pid']
        cwd = get_cwd(pid)
        env = get_env(pid)
        config_dir = env.get('CLAUDE_CONFIG_DIR') or None
        if config_dir:
            config_dir = os.path.expanduser(config_dir)
            if not os.path.isabs(config_dir):
                config_dir = None
        reg = get_session_registry(pid, p['starttime'], config_dir)
        reg_status = reg.get('status') if reg else None
        session_id = reg.get('sessionId') if reg else None
        eff_cwd = cwd or (reg.get('cwd') if reg else None)
        jsonl_state, ctx, tool, topic, _ = get_session_info_from_jsonl(eff_cwd, config_dir, session_id)
        # Source de vérité : même appel que l'app, pour que `state` colle au badge.
        state, _, _, _, last_activity = get_session_state(pid, cwd, p['starttime'], config_dir)
        reg_mapped = _STATUS_MAP.get(reg_status or '', 'idle') if reg else '(no registry)'
        print(f"pid {pid}  {project_label(eff_cwd)}  ({format_elapsed(p['elapsed'])})")
        print(f"  cwd          {eff_cwd or '?'}")
        print(f"  config_dir   {display_config_dir(config_dir) or '(default)'}")
        print(f"  session_id   {session_id or '(none)'}")
        print(f"  reg.status   {reg_status!r} -> {reg_mapped}")
        print(f"  jsonl_state  {jsonl_state!r}")
        print(f"  => state     {state}{'  (reconciled from registry)' if reg and reg_mapped != state else ''}")
        print(f"  context_pct  {ctx}")
        print(f"  tool         {tool}")
        print(f"  topic        {topic}")
        idle_for = f"{int(time.time() - last_activity)}s" if last_activity else '(unknown)'
        print(f"  idle_for     {idle_for}")
        print()


def list_screens():
    display = Gdk.Display.get_default()
    for i in range(display.get_n_monitors()):
        m   = display.get_monitor(i)
        g   = m.get_geometry()
        tag = '  [primary]' if m.is_primary() else ''
        print(f'monitor {i}: {g.width}x{g.height} @ ({g.x},{g.y}){tag}')


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    global CFG
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    CFG = parse_args(load_config())
    if CFG.dump:
        dump_round()
        return
    if CFG.list_screens:
        list_screens()
        return
    app = ClaudeWatcher(CFG)
    app.show_all()
    Gtk.main()


if __name__ == '__main__':
    main()
