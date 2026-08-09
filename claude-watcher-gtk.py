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
import functools
import traceback
import urllib.error
import urllib.parse
import urllib.request
import warnings
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

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

# ── Constantes des sessions distantes ─────────────────────────────────────────
# Déclarées ICI, avec les autres constantes de module : load_config() lit
# REMOTE_POLL_MS et vivait 1400 lignes AVANT sa définition.

REMOTE_POLL_MS       = 2000              # défaut de [remotes] poll_ms
REMOTE_POLL_MIN_MS   = 250               # plancher : en-dessous on martèle l'hôte
REMOTE_TIMEOUT_S     = 5                 # connexion ET lecture (urlopen)
# Budget TOTAL de lecture, en horloge monotone. REMOTE_TIMEOUT_S est un timeout
# PAR OPÉRATION socket : un pair qui livre un octet toutes les 4 s ne le déclenche
# jamais et parquerait le thread indéfiniment, ce qui défait aussi stop().
REMOTE_READ_BUDGET_S = 5
REMOTE_READ_CHUNK    = 64 * 1024
REMOTE_MAX_BYTES     = 4 * 1024 * 1024   # bombe mémoire sinon : read() non borné
REMOTE_MAX_ROWS      = 500
REMOTE_MAX_ELAPSED_S = 10 * 365 * 24 * 3600   # 10 ans : un elapsed importé non borné
                                              # (2**63) rendrait « 2562047788015215h30m »
REMOTE_STALE_X       = 3                 # périmé après 3 × l'intervalle de poll
REMOTE_LABEL_MAX     = 12
REMOTE_BACKOFF_MAX_S = 60
# STRICTEMENT supérieur au plafond de backoff, sinon la constante ne peut jamais
# changer le comportement : un token invalide ne se corrige pas en réessayant.
REMOTE_AUTH_RETRY_S  = 300
REMOTE_SCHEMES       = ('http', 'https')  # file:// serait lu par l'ouvreur par défaut

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
    r = cfg['remotes']  if 'remotes'  in cfg else {}

    idle_fmt = d.get('idle_format', 'none').lower()

    # Machines distantes : une section [remote:<nom>] par hôte (url, token,
    # enabled, label). Aucune section → dict vide → aucun thread, aucun HTTP.
    remote_sections = {
        name.split(':', 1)[1]: dict(cfg[name])
        for name in cfg.sections()
        if name.startswith('remote:') and name.split(':', 1)[1]
    }
    try:
        poll_ms = int(r.get('poll_ms', REMOTE_POLL_MS))
    except (TypeError, ValueError):
        poll_ms = REMOTE_POLL_MS

    return {
        'remote_poll_ms': max(REMOTE_POLL_MIN_MS, poll_ms),
        'remote_sections': remote_sections,
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
        # Compteur/détail des subagents lancés : affiché par défaut.
        'show_agents': f.get('show_agents', 'true').lower() == 'true',
        # Démon Claude Code : affiché par défaut, balisé (D) ; masquable ici.
        'hide_daemons': f.get('hide_daemons', 'false').lower() == 'true',
        'refresh_ms': int(d.get('refresh_ms', 2000)),
        'snooze_sec': int(d.get('snooze_sec', 30)),
        'bg_alpha':   _parse_bg_alpha(d.get('bg_alpha', BG_ALPHA_DEFAULT)),
        'tray':             f.get('tray',             'true').lower() == 'true',
        'shortcut_enable':  f.get('shortcut_enable',  'true').lower() == 'true',
        'hotkey':           g.get('hotkey', '<Ctrl><Alt>q').strip(),
    }


def save_config(updates: dict[str, dict[str, str]]) -> None:
    """Écrit les clés données dans config.ini, section par section.

    Relit le fichier d'abord pour ne pas écraser les autres clés (config partagé
    avec la TUI). configparser ne conserve pas les commentaires en réécriture —
    comportement déjà admis ici.

    Le fichier est forcé en 0600 INCONDITIONNELLEMENT : il peut contenir les
    tokens des remotes ([remote:<nom>] token=). Sans branche « si un token est
    présent » — une branche laisserait une fenêtre où le fichier est écrit
    lisible par tous juste avant que le token n'y atterrisse.

    Le chmod a lieu AVANT l'écriture, et la création passe par os.open(0600) :
    touch(mode=0600, exist_ok=True) NE re-chmode PAS un fichier existant, donc
    sur le chemin de mise à niveau (un config.ini 0644 écrit par une version
    d'avant les remotes — le cas courant) le token était écrit lisible par tous,
    et le chmod d'après-coup ne refermait la fenêtre qu'une fois le secret sur
    le disque.
    """
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    for section, values in updates.items():
        if section not in cfg:
            cfg[section] = {}
        for k, v in values.items():
            cfg[section][k] = v
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if CONFIG_PATH.exists():
            CONFIG_PATH.chmod(0o600)
        fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as fh:
            cfg.write(fh)
    except OSError:
        pass


def parse_remote_flag(spec: str) -> tuple[str, str]:
    """`--remote NAME=URL` → (nom, url). Lève pour argparse si la forme est fausse."""
    name, sep, url = spec.partition('=')
    if not sep or not name.strip() or not url.strip():
        raise argparse.ArgumentTypeError(
            f"format attendu NAME=URL (reçu : {spec!r})")
    # « NAME=URL#TOKEN » vient d'un brouillon abandonné de la spec : le fragment
    # serait mangé par l'URL (/api/sessions jamais demandé), aucun en-tête d'auth
    # ne partirait, et le secret atterrirait NON RÉDIGÉ dans display_url. On le
    # refuse en nommant les formes réellement supportées plutôt que de l'accepter
    # silencieusement de travers.
    if '#' in url:
        raise argparse.ArgumentTypeError(
            f"'#' non supporté dans --remote (reçu : {spec!r}). Pour un token, utilisez "
            f"NAME=https://remote:TOKEN@hote/, la variable {remote_token_env('NAME')} "
            f"ou la clé token de la section [remote:NAME].")
    return name.strip(), url.strip()


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
    p.add_argument('--settings', action='store_true',
                   help="ouvre directement la fenêtre de paramètres au lancement.")
    # metavar neutre : « NOM=URL » s'affichait même sous --lang en (l'aide
    # argparse n'est pas traduite, autant ne pas panacher les langues).
    p.add_argument('--remote', dest='remote', action='append', metavar='NAME=URL',
                   type=parse_remote_flag, default=[],
                   help="ajoute une machine distante servant claude-watcher-webui "
                        "(répétable ; webui parle HTTP en clair, cf. README). L'URL "
                        "peut porter le token : "
                        "http://remote:TOKEN@hote:8000/ — ATTENTION, un token en "
                        "ligne de commande est lisible par TOUS les utilisateurs de la "
                        "machine via /proc/<pid>/cmdline ; préférez "
                        "CW_REMOTE_TOKEN_<NOM> ou la section [remote:<nom>] du "
                        "config.ini (forcé en 0600). Jamais persisté.")
    p.add_argument('--no-local', dest='no_local', action='store_true',
                   help="n'analyse pas /proc : n'affiche que les sessions distantes.")
    args = p.parse_args(argv)
    if (args.x is None) != (args.y is None):
        p.error('--x et --y doivent être fournis ensemble.')
    # L'incompatibilité de --dump avec les sessions distantes est vérifiée dans
    # main(), PAS ici : un remote peut aussi venir d'une section [remote:*] du
    # config.ini, que parse_args ne voit structurellement pas. Le contrôle placé
    # ici ne couvrait que --remote, donc un remote de config.ini passait et
    # dump_round() l'ignorait en silence — exactement le mensonge visé.
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
    args.show_agents = defaults['show_agents']
    args.hide_daemons = defaults['hide_daemons']
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
        'waiting_s':  'att',
        'working_s':  'trav',
        'bg_shell':   'shell de fond',
        'idle':       'inactif',
        'no_session': 'aucune session active',
        'attend':     'attend',
        'pid':        'pid',
        'agent':      'agent',
        'agents':     'agents',
        'tip_agents': 'Agents :',
        'daemon':     'démon',
        'tip_daemon': 'Démon Claude Code (pas une session).',
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
        'tab_general':   'Général',
        'tab_position':  'Position',
        'tab_display':   'Affichage',
        'authors':       'Auteurs',
        'close':         'Fermer',
        # menu contextuel (clic droit sur une session) + confirmation de fermeture
        'menu_focus':         'Focus',
        'menu_copy_pid':      'Copier le PID',
        'menu_kill':          'Fermer la session',
        'kill_confirm_title': 'Fermer la session ?',
        'kill_confirm_body':  'Fermer la session Claude « {proj} » (inactive depuis {idle}) ?\n'
                              'Le terminal reste ouvert.',
        'kill_failed':        'Impossible de fermer la session (process introuvable ou déjà terminé).',
        # dialogue paramètres
        'settings_title': 'Paramètres — Claude Code Watcher',
        'cancel':         'Annuler',
        'apply':          'Appliquer',
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
        'fld_show_agents': 'Afficher les sous-agents',
        'fld_hide_daemons': 'Masquer les démons',
        'fld_sort':        'Tri',
        'sort_default':    'Par défaut (projet)',
        'sort_idle':       'Par inactivité',
        'fld_idle_format': 'Durée d’inactivité',
        'idle_none':       'Masquée',
        'idle_loose':      'Approx. (HH:MM)',
        'idle_precise':    'Précise (1d 02:24:23)',
        'fld_refresh':    'Rafraîch.',
        'help_refresh':   ('Intervalle entre deux scans des sessions Claude et '
                           'mises à jour de l’affichage, en millisecondes.'),
        'fld_snooze':     'Veille',
        'help_snooze':    ('Durée pendant laquelle le widget reste masqué après '
                           '« Masquer pendant » (menu / clic milieu), en secondes.'),
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
        # machines distantes
        'tab_remotes':      'Distants',
        'rm_label':         'Distants',
        'rm_ok':            'ok',
        'rm_stale':         'périmé',
        'rm_down':          'injoignable',
        'rm_auth':          'auth refusée',
        'rm_starting':      'démarrage',
        'rm_dead':          'thread arrêté',
        'off':              'désactivée',
        'rm_stale_row':     'périmé',
        'tip_remote':       'Session distante ({label}) — lecture seule : ni focus, ni fermeture.',
        'rm_none':          'aucune session distante',
        'rm_col_name':      'Nom',
        'rm_col_url':       'URL',
        'rm_col_health':    'État',
        'rm_none_configured': 'Aucune machine distante configurée.',
        'rm_readonly_hint': ('Lecture seule : les machines distantes se déclarent dans '
                            '~/.config/claude-watcher/config.ini (sections [remote:<nom>]) '
                            'ou avec --remote NAME=URL, et sont lues au démarrage.'),
    },
    'en': {
        # main widget
        'title':      'CLAUDE CODE',
        'waiting':    'waiting',
        'working':    'working',
        'waiting_s':  'wait',
        'working_s':  'work',
        'bg_shell':   'background shell',
        'idle':       'idle',
        'no_session': 'no active session',
        'attend':     'waiting',
        'pid':        'pid',
        'agent':      'agent',
        'agents':     'agents',
        'tip_agents': 'Agents:',
        'daemon':     'daemon',
        'tip_daemon': 'Claude Code daemon (not a session).',
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
        'tab_general':   'General',
        'tab_position':  'Position',
        'tab_display':   'Display',
        'authors':       'Authors',
        'close':         'Close',
        # context menu (right-click on a session) + close confirmation
        'menu_focus':         'Focus',
        'menu_copy_pid':      'Copy PID',
        'menu_kill':          'Close session',
        'kill_confirm_title': 'Close session?',
        'kill_confirm_body':  'Close the Claude session “{proj}” (idle for {idle})?\n'
                              'The terminal stays open.',
        'kill_failed':        'Could not close the session (process gone or already exited).',
        # settings dialog
        'settings_title': 'Settings — Claude Code Watcher',
        'cancel':         'Cancel',
        'apply':          'Apply',
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
        'fld_show_agents': 'Show subagents',
        'fld_hide_daemons': 'Hide daemons',
        'fld_sort':        'Sort',
        'sort_default':    'Default (project)',
        'sort_idle':       'By idle time',
        'fld_idle_format': 'Idle duration',
        'idle_none':       'Hidden',
        'idle_loose':      'Approx. (HH:MM)',
        'idle_precise':    'Precise (1d 02:24:23)',
        'fld_refresh':    'Refresh',
        'help_refresh':   ('Interval between two scans of Claude sessions and '
                           'display updates, in milliseconds.'),
        'fld_snooze':     'Snooze',
        'help_snooze':    ('How long the widget stays hidden after "Hide for" '
                           '(menu / middle-click), in seconds.'),
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
        # remote machines
        'tab_remotes':      'Remotes',
        'rm_label':         'Remotes',
        'rm_ok':            'ok',
        'rm_stale':         'stale',
        'rm_down':          'down',
        'rm_auth':          'auth failed',
        'rm_starting':      'starting',
        'rm_dead':          'poller stopped',
        'off':              'off',
        'rm_stale_row':     'stale',
        'tip_remote':       'Remote session ({label}) — read-only: no focus, no close.',
        'rm_none':          'no remote session',
        'rm_col_name':      'Name',
        'rm_col_url':       'URL',
        'rm_col_health':    'Health',
        'rm_none_configured': 'No remote machine configured.',
        'rm_readonly_hint': ('Read-only: remote machines are declared in '
                            '~/.config/claude-watcher/config.ini ([remote:<name>] sections) '
                            'or with --remote NAME=URL, and are read at startup.'),
    },
}

def tr(key: str) -> str:
    # Repli en ANGLAIS, comme la TUI : `tr` est appelé par six aides du cœur
    # PARTAGÉ, donc un repli différent d'un client à l'autre ferait diverger la
    # sortie de fonctions par ailleurs identiques à l'octet près. Inatteignable
    # en pratique (CFG.lang est posé dans main() avant tout appel), ce qui est
    # précisément pourquoi personne ne l'aurait vu bouger.
    lang = getattr(CFG, 'lang', 'en')
    return STRINGS.get(lang, STRINGS['en']).get(key, key)

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
BG_SHELL_GLYPH = '⚙'         # marqueur « un shell de fond tourne »
COLOR_BACKGROUND = "#5c8a9e"   # muted teal — couleur du marqueur ⚙ : un shell de fond tourne

# Alpha values for the waiting-dot pulse (6 ticks @ 600 ms ≈ 3.6 s cycle)
_PULSE_ALPHAS = [0.35, 0.6, 0.9, 1.0, 0.9, 0.6]
COLOR_SNOOZE  = "#5a7a9a"
COLOR_CLAUDE  = "#cc785c"   # Claude brand orange — marque les instances CLAUDE_CONFIG_DIR custom
COLOR_REMOTE  = "#7a9ec2"   # bleu sourd — préfixe « <label>: » des lignes distantes
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


def _argv_value(argv: list[str], flag: str) -> str | None:
    """Valeur suivant `flag` dans une argv (cmdline splitée sur NUL), sinon None.

    Vide → None (`or None`). Flag absent ou en dernière position → None.
    """
    try:
        return argv[argv.index(flag) + 1] or None
    except (ValueError, IndexError):
        return None


def scan_proc(collect_agents: bool = True) -> tuple[list[dict], dict[str, list[dict]]]:
    """Une seule passe /proc → (sessions/démons 'claude', subagents par parent).

    Les deux consommateurs devaient chacun énumérer /proc ; on le fait UNE fois
    par tick au lieu de deux. Une session interactive et le démon partagent
    comm=='claude' (le démon ne se distingue que par `claude daemon run …`) ;
    un subagent lancé (Task/essaim) tourne le binaire versionné (comm=version,
    donc invisible au filtre comm) et se repère à ses tokens argv exacts
    `--agent-id`/`--parent-session-id` — match sur token exact (argv NUL-splitée)
    pour éviter les faux positifs d'un substring noyé dans un plus gros argument.

    `collect_agents=False` (feature désactivée) saute entièrement la détection des
    subagents : aucun cmdline lu pour les process non-'claude' → zéro surcoût.

    elapsed = uptime − starttime (champ 22 de /proc/<pid>/stat, ticks depuis le
    boot). comm est lu EN PREMIER : un échec de lecture cmdline ne fait jamais
    perdre une session claude (elle est juste traitée comme non-démon).
    """
    try:
        uptime = float(Path('/proc/uptime').read_text().split()[0])
    except Exception:
        return [], {}
    procs: list[dict] = []
    agents: dict[str, list[dict]] = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # comm est tronqué à 15 car (TASK_COMM_LEN) — 'claude' y tient.
            # read_bytes+decode(errors='ignore') : un comm non-UTF-8 (nom posé via
            # prctl par un process quelconque) lèverait UnicodeDecodeError avec
            # read_text() — PAS un OSError → crash du scan à chaque tick.
            comm = (entry / 'comm').read_bytes().decode(errors='ignore').strip()
        except OSError:
            continue
        if comm == 'claude':
            try:
                stat = (entry / 'stat').read_text()
                # Le champ 2 (comm) est entre parenthèses et peut contenir des
                # espaces ; parser après le dernier ')' réaligne les index.
                fields = stat[stat.rindex(')') + 2:].split()
                starttime = int(fields[19])  # champ 22 global = index 19 après comm
                elapsed = int(uptime - starttime / _CLK_TCK)
            except Exception:
                continue
            # cmdline seulement pour distinguer le démon ; illisible (course avec
            # un exec/exit) → non-démon, on ne perd pas la session pour autant.
            try:
                argv = (entry / 'cmdline').read_bytes().decode(errors='ignore').split('\0')
            except OSError:
                argv = []
            procs.append({'pid': int(entry.name), 'elapsed': elapsed,
                          'start_unix': time.time() - elapsed, 'starttime': starttime,
                          'is_daemon': len(argv) > 1 and argv[1] == 'daemon'})
            continue
        if not collect_agents:
            continue
        # Subagent : comm ≠ 'claude', on doit lire cmdline pour le repérer.
        try:
            argv = (entry / 'cmdline').read_bytes().decode(errors='ignore').split('\0')
        except OSError:
            continue
        if '--agent-id' not in argv:
            continue
        parent = _argv_value(argv, '--parent-session-id')
        if not parent:
            continue
        # --agent-name peut manquer (agents anonymes) : repli sur la partie locale
        # de l'id (<name>@<team>).
        name = _argv_value(argv, '--agent-name') or (_argv_value(argv, '--agent-id') or '?').split('@', 1)[0]
        model = (_argv_value(argv, '--model') or '').removeprefix('claude-')
        agents.setdefault(parent, []).append({
            'pid':   int(entry.name),
            'name':  name,
            'type':  _argv_value(argv, '--agent-type'),
            'model': model or None,
        })
    for lst in agents.values():
        lst.sort(key=lambda a: a['name'])
    return procs, agents


def resolve_config_dir(env: dict[str, str]) -> str | None:
    """CLAUDE_CONFIG_DIR d'un process, `~` résolu et validé absolu.

    Un chemin relatif est rejeté (sans le cwd de la session, il pointerait sur
    le cwd du watcher → registre/JSONL/watch au mauvais endroit) → None. None
    aussi si la variable est absente.
    """
    config_dir = env.get('CLAUDE_CONFIG_DIR') or None
    if config_dir:
        config_dir = os.path.expanduser(config_dir)
        if not os.path.isabs(config_dir):
            return None
    return config_dir


def kill_session(s: dict) -> bool:
    """Ferme une session Claude via SIGTERM, avec garde anti-recyclage de PID.

    Prend le DICT de session, pas des primitives : la garde « session distante »
    doit vivre au point d'étranglement, pas dans chaque appelant. Le pid d'une
    ligne distante désigne un process LOCAL sans rapport — un kill qui fuit ne
    rate pas, il tue la mauvaise chose sur CETTE machine.

    Réutilise get_session_registry, qui ne renvoie le registre QUE si procStart
    == starttime : un None ici = process disparu ou PID recyclé entre le scan et
    le clic → on ne tire pas (pas d'innocent tué). SIGTERM laisse Claude flusher
    son transcript et sortir proprement (pas de SIGKILL). Retourne True si le
    signal est parti.
    """
    if s.get('remote'):
        return False
    pid = s['pid']
    if get_session_registry(pid, s.get('starttime', 0), s.get('config_dir')) is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


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


_WORKTREE_MARKER = '/.claude/worktrees/'


def split_worktree(cwd: str | None) -> tuple[str | None, str | None]:
    """Sépare un cwd de worktree Claude en (racine projet, nom du worktree).

    <projet>/.claude/worktrees/<nom>[/sous-dossier] → (<projet>, <nom>).
    Hors worktree → (cwd, None). C'est la source unique du marqueur worktree.
    """
    if cwd and _WORKTREE_MARKER in cwd:
        root, _, rest = cwd.partition(_WORKTREE_MARKER)
        return root, rest.split('/', 1)[0]
    return cwd, None


def git_worktree(cwd: str | None) -> tuple[str | None, str | None]:
    """Worktree git ORDINAIRE : (racine du dépôt principal, nom du worktree).

    Rien à voir avec le layout Claude (`<projet>/.claude/worktrees/<nom>`, cf.
    `split_worktree`) : un `git worktree add` pose son checkout n'importe où, et
    ce cwd n'a alors aucun marqueur dans son chemin. La preuve est sur le disque
    — la racine d'un worktree porte un `.git` FICHIER (et non un dossier) qui
    pointe vers `<dépôt>/.git/worktrees/<nom>`. Un `.git` dossier = checkout
    principal, on s'arrête. Un `gitdir:` sans `/worktrees/` = sous-module.

    Purement pour l'AFFICHAGE : Claude range le transcript d'un worktree git
    ordinaire sous le slug de son PROPRE cwd, donc `cwd_to_project_dir` ne doit
    surtout pas remonter à la racine ici. Hors worktree → (None, None).
    """
    if not cwd:
        return None, None
    start = Path(cwd)
    for d in (start, *start.parents):
        dot = d / '.git'
        try:
            if dot.is_dir():
                return None, None
            if not dot.is_file():
                continue
            head = dot.read_text(errors='ignore')[:4096].strip()
        except OSError:
            return None, None
        if not head.startswith('gitdir:'):
            return None, None
        gitdir = head[len('gitdir:'):].strip().split('\n', 1)[0]
        # `worktree.useRelativePaths` écrit un gitdir RELATIF au dossier du
        # worktree ; le résoudre garde la racine du dépôt exploitable.
        target = Path(gitdir) if Path(gitdir).is_absolute() else (d / gitdir)
        parts = target.parts
        try:
            i = len(parts) - 1 - parts[::-1].index('worktrees')
        except ValueError:
            return None, None
        # <dépôt>/.git/worktrees/<nom> : la racine est ce qui précède le '.git'.
        if i < 2 or parts[i - 1] != '.git' or i + 1 >= len(parts):
            return None, None
        return str(Path(*parts[:i - 1])), parts[i + 1]
    return None, None


def worktree_of(cwd: str | None, transcript_found: bool) -> tuple[str | None, str | None]:
    """(projet à AFFICHER, nom du worktree) — sinon (cwd, None).

    Worktree Claude : le marqueur est dans le chemin, donc potentiellement
    fortuit ; on ne le retient que si le transcript a bien été résolu sous la
    racine parente. Worktree git ordinaire : le `.git` fichier EST la preuve,
    aucune confirmation à chercher.
    """
    root, name = split_worktree(cwd)
    if name is not None and transcript_found:
        return root, name
    # Marqueur Claude non confirmé : on ne s'arrête pas là. Un worktree Claude
    # EST un worktree git, et son `.git` fichier prouve ce que le chemin ne fait
    # que suggérer.
    root, name = git_worktree(cwd)
    return (root, name) if name else (cwd, None)


def cwd_to_project_dir(cwd: str | None, config_dir: str | None = None) -> Path | None:
    if not cwd:
        return None
    # Instance CLAUDE_CONFIG_DIR custom → ses JSONL vivent dans <config_dir>/projects,
    # pas dans ~/.claude/projects. Sinon état/contexte lus au mauvais endroit.
    base = Path(config_dir) / 'projects' if config_dir else CLAUDE_PROJECTS_DIR
    # Worktree Claude : Claude range le transcript sous le slug du PROJET PARENT,
    # pas du cwd du worktree. On retombe sur la racine projet. Inoffensif hors
    # worktree ; au pire le dir n'existe pas → None.
    root, _ = split_worktree(cwd)
    # Racine VIDE ('/.claude/worktrees/wt') : le slug serait '' et `base / ''`
    # vaut `base`, donc on rendrait le DOSSIER DES PROJETS comme s'il était un
    # projet — et il existe toujours. Aucun repli ne convient ici (ni '' ni le
    # cwd du worktree, qui n'est pas l'endroit où Claude range le transcript) :
    # sans racine, il n'y a pas de projet à désigner.
    if not root:
        return None
    # Claude slugifie le cwd en remplaçant CHAQUE non-alphanumérique par '-'
    # (pas seulement '/'), donc 'geoffrey.laurent' → 'geoffrey-laurent'.
    slug = re.sub(r'[^a-zA-Z0-9]', '-', root)
    path = base / slug
    return path if path.exists() else None


DEFAULT_CONTEXT_WINDOW = 1_000_000

# Modèles qui démarrent à 200k — deux cas, même hypothèse de départ :
#   - 200k ferme : le modèle n'a pas de fenêtre 1M ;
#   - sous condition : Opus 4.6 / Sonnet 4.6 n'atteignent le 1M que via le
#     « extended context » de Claude Code, qui dépend du plan (Opus : inclus en
#     Max/Team/Enterprise, crédits en Pro ; Sonnet 4.6 : crédits sur tous les
#     plans) et se désactive avec CLAUDE_CODE_DISABLE_1M_CONTEXT=1.
# Comparés en sous-chaîne de `message.model`, qui peut être daté
# (`claude-opus-4-5-20251101`), préfixé plateforme (`anthropic.claude-opus-4-5…`)
# ou un alias nu (`opus`, `haiku`).
CONTEXT_200K = (
    'haiku',  # toutes les générations Haiku, y compris haiku-4-5
    'opus-4-5',
    'opus-4-1',
    'opus-4-2025',  # claude-opus-4-20250514
    'sonnet-4-5',
    'sonnet-4-2025',  # claude-sonnet-4-20250514
    'claude-3',
    'claude-2',
    'opus-4-6',  # sous condition
    'sonnet-4-6',  # sous condition
)


def context_window_for(model: str | None, observed_tokens: int = 0) -> int:
    """Fenêtre de contexte (tokens) déduite du modèle et de l'usage observé.

    Le JSONL ne trace pas la taille de fenêtre, et rien ne distingue un modèle
    « sous condition » resté à 200k du même modèle passé à 1M : le sélecteur
    `[1m]` de Claude Code n'arrive jamais dans `message.model` (une session
    `claude-opus-5[1m]` journalise `claude-opus-5`). D'où : 1M par défaut (tout
    modèle hors CONTEXT_200K est en 1M sur tous les plans), 200k pour
    CONTEXT_200K, puis promotion à 1M dès qu'un message dépasse les 200k — ce
    que seule une vraie fenêtre 1M permet.

    Partir sur 200k garde l'erreur du bon côté : un ctx% surévalué alerte tôt,
    un ctx% sous-évalué masque une session au bord de la compaction.
    """
    m = (model or '').lower()
    if any(tag in m for tag in CONTEXT_200K):
        return 1_000_000 if observed_tokens > 200_000 else 200_000
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


# Sous-types d'évènement `system` qui marquent une FIN DE TOUR — les seuls qui
# prouvent que Claude a rendu la main. Les autres (`informational`, `api_error`,
# `local_command`, `compact_boundary`…) surviennent EN COURS de tour : les lire
# comme un tour terminé faisait dégrader en 'background' une session qui
# travaillait (registre 'busy' recoupé avec un JSONL cru inactif).
_TURN_END_SUBTYPES = {'turn_duration', 'stop_hook_summary', 'away_summary'}


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
            elif kind == 'system' and ev.get('subtype') in _TURN_END_SUBTYPES:
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
                        window = context_window_for(msg.get('model'), total)
                        context_pct = min(100, round(total * 100 / window))
        if state is not None and context_pct is not None:
            break
    return state, context_pct, tool


# Un sessionId sert à CONSTRUIRE des chemins (chemin direct et motif glob) : sans
# garde, un id piégé dans le registre lit hors de l'arbre des projets
# ('../../ailleurs/secret') ou rouvre le bug du voisin en jouant le joker ('*').
# On exige UN SEUL composant de nom de fichier, sans '..' — et pas une forme
# d'UUID : le jour où Claude change de format d'identifiant, une garde trop
# étroite ferait disparaître ctx/sujet de TOUTES les lignes, en silence.
_SID_RE = re.compile(r'(?!\.\.?\Z)[A-Za-z0-9._-]+\Z')


# (racine projets, session_id) → chemin du JSONL, quand il n'est pas sous le slug
# du cwd (session reprise depuis un autre dossier). Évite de re-scanner tous les
# projets à chaque rafraîchissement. La racine fait partie de la clé : deux
# CLAUDE_CONFIG_DIR peuvent porter le même sessionId.
_SID_PATH_CACHE: dict[tuple[str, str], Path | None] = {}


def _find_transcript(session_id: str, project_dir: Path) -> Path | None:
    """JSONL de la session : sous le slug du cwd, sinon scan des autres projets."""
    if not _SID_RE.match(session_id):
        return None
    cand = project_dir / f'{session_id}.jsonl'
    if cand.is_file():
        return cand
    root = project_dir.parent
    key = (str(root), session_id)
    if key in _SID_PATH_CACHE:
        hit = _SID_PATH_CACHE[key]
        # Absence mémorisée (None) : sûr UNIQUEMENT parce que le chemin direct
        # ci-dessus est testé avant, à chaque appel — un transcript qui apparaît
        # plus tard sous le slug du cwd est donc toujours vu.
        if hit is None or hit.is_file():
            return hit
    if len(_SID_PATH_CACHE) > 200:
        _SID_PATH_CACHE.clear()
    for p in root.glob(f'*/{session_id}.jsonl'):
        _SID_PATH_CACHE[key] = p
        return p
    _SID_PATH_CACHE[key] = None
    return None


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

    Si `session_id` est fourni, cible <session_id>.jsonl (sous le slug du cwd,
    sinon dans le projet où il vit réellement) et rien d'autre ; sinon retombe
    sur le .jsonl le plus récent du projet. Court-circuit par mtime + lecture du seul tail
    (relecture complète si le tail tronqué n'a pas livré état + pct).
    """
    project_dir = cwd_to_project_dir(cwd, config_dir)
    if not project_dir:
        return None, None, None, None, None
    if session_id:
        # Le transcript ne vit pas forcément sous le slug du cwd : une session
        # reprise (`claude -r <id>`) depuis un autre dossier garde son JSONL
        # d'origine. Retomber ici sur « le .jsonl le plus récent du projet »
        # lirait l'état d'une session VOISINE et l'attribuerait à celle-ci
        # (statut, ctx%, sujet). Mieux vaut rien que faux : pas de repli.
        latest = _find_transcript(session_id, project_dir)
        if latest is None:
            return None, None, None, None, None
    else:
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


def get_session_state(
    pid: int, cwd: str | None,
    starttime: int = 0,
    config_dir: str | None = None,
) -> tuple[str, int | None, str | None, str | None, float | None, str | None, bool]:
    """État de la session. Retourne (state, context_pct, tool_name, topic,
    last_activity, session_id, bg_shell) — session_id sert à rattacher les
    subagents (--parent-session-id) à leur session, None si le registre est
    absent ; bg_shell dit qu'un shell de fond a survécu au tour. L'état ne décrit
    QUE la disponibilité de Claude : un shell de fond est un détail SUR la
    session, pas ce qu'elle est.

    Le registre ~/.claude/sessions/<pid>.json (champ `status`, temps réel) est
    prioritaire quand il existe ; selon la version de Claude Code il peut être
    absent, auquel cas l'état est déduit du JSONL. Le JSONL fournit dans tous
    les cas le % de contexte et l'outil courant (absents du registre).

    `sessionId` du registre, quand il existe, donne le chemin EXACT du JSONL ;
    sinon on devine par slug du cwd.
    """
    reg = get_session_registry(pid, starttime, config_dir)
    session_id = reg.get('sessionId') if reg else None
    bg_shell = False
    # Le slug du transcript se calcule sur le cwd de DÉMARRAGE de la session, que
    # le registre enregistre. Le cwd /proc dérive dès que le dossier est renommé
    # ou que l'utilisateur fait un `cd` en cours de session — le slugifier
    # désignerait un dossier projet inexistant et perdrait silencieusement
    # état/ctx/sujet/last_activity. On préfère donc le cwd du REGISTRE pour
    # résoudre le transcript ; le cwd vivant reste le libellé affiché (affaire de
    # l'appelant). Précédence identique côté serveur (webui/detect.py) : la même
    # session doit se lire pareil en local et via l'API.
    transcript_cwd = (reg.get('cwd') if reg else None) or cwd
    jsonl_state, context_pct, tool, topic, last_activity = get_session_info_from_jsonl(
        transcript_cwd, config_dir, session_id)
    if reg:
        # /rename : un nom choisi par l'utilisateur (champ `name` sans
        # nameSource='derived' — 'derived' = nom auto-généré, redondant avec le
        # cwd) prime sur le titre IA du JSONL comme sujet affiché. Même
        # interrupteur features.show_topic que le sujet classique.
        reg_name = reg.get('name')
        if reg_name and reg.get('nameSource') != 'derived' \
                and getattr(CFG, 'show_topic', True):
            topic = reg_name
        status = reg.get('status', '')
        state = _STATUS_MAP.get(status, 'idle')
        # Un statut 'shell'/'busy' peut rester FIGÉ après la fin du tour, mais les
        # deux ne disent pas la même chose : 'shell' = un shell de fond tourne
        # vraiment (état de basse priorité, waiting > working > background > idle) ;
        # 'busy' = rien ne tourne, le registre a juste cessé d'être écrit
        # (sous-agents interrompus, session mise en fond). 'compacting' est EXCLU :
        # vrai travail de fond, bref. jsonl_state None (JSONL introuvable) ou
        # 'working' (tour en cours, sous-agents compris) → aucune réconciliation.
        if status in ('shell', 'busy') and jsonl_state in ('waiting', 'idle'):
            state = jsonl_state
            bg_shell = status == 'shell'
        # Idle-since : instant EXACT du dernier changement d'état du registre
        # (ms epoch). Prioritaire sur le mtime du JSONL, qui bouge pour des
        # écritures de fond (résumés, todos) sans refléter l'inactivité réelle —
        # il sur-estimait la fraîcheur. Fallback mtime si le champ est absent
        # (version de Claude antérieure).
        ts = reg.get('statusUpdatedAt') or reg.get('updatedAt')
        if ts is not None:
            try:
                last_activity = float(ts) / 1000.0
            except (TypeError, ValueError):
                pass
    else:
        state = jsonl_state or 'idle'
    return state, context_pct, tool, topic, last_activity, session_id, bg_shell


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
    # loose : même découpage que precise mais SANS les secondes (résolution
    # minute) → ne change qu'une fois par minute, attire moins l'œil.
    d, rem = divmod(s, 86400)
    h, m = divmod(rem // 60, 60)
    clock = f'{h:02d}:{m:02d}'
    return f'{d}d {clock}' if d else clock


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


def focus_terminal(s: dict) -> bool:
    """Ramène au premier plan le terminal d'une session. Prend le DICT de session.

    Même raison que kill_session : la garde « session distante » est au point
    d'étranglement. Une session distante n'a pas de fenêtre ici — et son
    window_id/terminal_pid désigneraient une fenêtre locale sans rapport.
    """
    if s.get('remote'):
        return False
    window_id       = s.get('window_id')
    terminal_pid    = s.get('terminal_pid')
    kitty_socket    = s.get('kitty_socket')
    kitty_window_id = s.get('kitty_window_id')
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


def scan_local_sessions() -> list[dict]:
    """Scan /proc de CETTE machine → lignes de session, non triées."""
    all_windows = get_all_windows()
    window_pids = {w['pid'] for w in all_windows}

    procs, subagents = scan_proc(getattr(CFG, 'show_agents', True))

    sessions = []
    for p in procs:
        pid      = p['pid']
        # Démon : pas une session focusable (ni terminal, ni JSONL, ni registre
        # keyé par pid). On court-circuite tout le résolveur fenêtre/état et on
        # émet une ligne minimale balisée `daemon` — ou rien si masqué en conf.
        if p.get('is_daemon'):
            if getattr(CFG, 'hide_daemons', False):
                continue
            cwd = get_cwd(pid)
            sessions.append({
                'pid':             pid,
                'starttime':       p['starttime'],
                'project':         project_label(cwd),
                'worktree':        None,
                'topic':           None,
                'cwd':             cwd or '?',
                'elapsed':         p['elapsed'],
                'waiting':         False,
                'working':         False,
                'bg_shell':        False,
                'context_pct':     None,
                'tool':            None,
                'terminal_pid':    None,
                'window_id':       None,
                'kitty_socket':    None,
                'kitty_window_id': None,
                'config_dir':      resolve_config_dir(get_env(pid)),
                'last_activity':   None,
                'agents':          [],
                'daemon':          True,
            })
            continue
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
        window_id: str | None
        if raw_wid:
            # WINDOWID est un entier décimal ; wmctrl -ia attend 0x...
            try:
                window_id = hex(int(raw_wid))
            except ValueError:
                window_id = raw_wid
        else:
            window_id = find_best_window(term_pid, cwd, all_windows)

        config_dir = resolve_config_dir(env)
        state, context_pct, tool, topic, last_activity, session_id, bg_shell = get_session_state(
            pid, cwd, p['starttime'], config_dir)
        # Worktree Claude « confirmé » (marqueur détecté ET transcript résolu) ou
        # worktree git ordinaire (prouvé par son `.git` fichier) : on affiche le
        # VRAI projet (racine du dépôt) en titre, le nom du worktree en sous-ligne.
        # Sinon comportement inchangé (label = cwd brut, pas de sous-ligne).
        wt_root, wt_name = worktree_of(cwd, last_activity is not None)
        sessions.append({
            'pid':             pid,
            'starttime':       p['starttime'],
            'project':         project_label(wt_root),
            'worktree':        wt_name,
            'topic':           topic,
            'cwd':             cwd or '?',
            'elapsed':         p['elapsed'],
            'waiting':         state == 'waiting',
            'working':         state == 'working',
            'bg_shell':        bg_shell,
            'context_pct':     context_pct,
            'tool':            tool,
            'terminal_pid':    term_pid,
            'window_id':       window_id,
            'kitty_socket':    kitty_socket,
            'kitty_window_id': kitty_window_id,
            'config_dir':      config_dir,
            'last_activity':   last_activity,
            'agents':          subagents.get(session_id, []) if session_id else [],
        })
    return sessions


def scan_sessions(remote_rows: list[dict] | None = None) -> list[dict]:
    """Sessions locales + distantes, triées. `remote_rows` vient du cache du
    poller (déjà adaptées) : AUCUN HTTP ici, la fonction tourne dans la boucle UI.
    """
    sessions: list[dict] = list(remote_rows or [])
    if not getattr(CFG, 'no_local', False):
        sessions.extend(scan_local_sessions())
    # --hide-daemons / --no-agents s'appliquent APRÈS la fusion : filtrés dans le
    # seul scan local, ils laissaient passer les lignes de démon distantes et les
    # compteurs d'agents distants — l'option ne faisait donc que la moitié de ce
    # qu'elle annonce. Les lignes distantes sont COPIÉES (dict(...)) : elles
    # appartiennent au cache du poller, les muter le corromprait durablement.
    if getattr(CFG, 'hide_daemons', False):
        sessions = [s for s in sessions if not s.get('daemon')]
    if not getattr(CFG, 'show_agents', True):
        sessions = [dict(s, agents=[]) if s.get('agents') else s for s in sessions]
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
            if s['waiting']:      bucket = 0
            elif s['working']:    bucket = 1
            else:                 bucket = 2
            la = s.get('last_activity')
            idle = ((now - la) if la is not None else float('inf')) if bucket == 2 else 0.0
            return (bucket, idle, s['project'].lower())
        sessions.sort(key=_sort_key)
    else:
        sessions.sort(key=lambda s: (
            not s['waiting'], not s['working'], s['project'].lower()))
    return sessions

def session_key(s: dict) -> str:
    """Clé de ligne. Le pid seul NE SUFFIT PAS dès qu'il y a des remotes : un pid
    1234 local et un pid 1234 distant sont deux process différents et
    collisionneraient (signature de structure identique → lignes mises à jour en
    place avec les données d'une AUTRE session).

    On clé sur `remote_name` (le NOM de la section / du drapeau, unique par
    construction) et JAMAIS sur `remote` (le label, élidé à REMOTE_LABEL_MAX) :
    « build-server-01 » et « build-server-02 » donnent le même label élidé, donc
    la même clé — et la signature de structure confondrait les deux machines.
    """
    r = s.get('remote_name') or s.get('remote')
    return f"{r}:{s['pid']}" if r else str(s['pid'])


# ── Sessions distantes ────────────────────────────────────────────────────────
# Cœur partagé (stdlib uniquement, aucune dépendance à GTK) : agrège les
# sessions d'autres machines servies par claude-watcher-webui (GET
# /api/sessions). La TUI porte le même cœur + ses propres aides de présentation
# (les constantes REMOTE_* vivent en tête de fichier). La parité du cœur est
# vérifiée mécaniquement par tests/test_core_parity.py — un commentaire ne
# retient personne, un test si.

# Séquences ANSI (CSI/OSC/Fe) puis caractères de contrôle restants. Les chaînes
# du payload sont écrites par une AUTRE machine et atterrissent dans des labels
# Pango : sans ce nettoyage, un remote peut casser la mise en page (\r, \n) ou
# usurper le label d'un autre remote. \n et \t sautent aussi — une ligne de
# session tient sur une ligne.
_ANSI_RE = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]|\x1b\[[0-9;?]*[ -/]*[@-~]')
# En plus des contrôles C0/C1 : les contrôles de FORMAT Unicode. U+202E (RLO) et
# ses voisins réordonnent visuellement une chaîne — c'est la primitive d'usurpation
# de label que la spec nomme elle-même ; U+2028/2029 sont des sauts de ligne pour
# tout moteur de rendu et casseraient l'invariant « une ligne de session = une
# ligne » ; U+200B–200D et U+2060–2069 sont invisibles ou isolent la direction.
_CTRL_RE = re.compile(
    '[\x00-\x1f\x7f-\x9f'
    '\u061c'                    # ARABIC LETTER MARK
    '\u200b-\u200f'             # ZWSP/ZWNJ/ZWJ, LRM/RLM
    '\u2028-\u202e'             # LS/PS, LRE/RLE/PDF/LRO/RLO
    '\u2060-\u2069'             # word joiner, invisibles, LRI/RLI/FSI/PDI
    '\ufeff'                    # BOM (espace insécable de largeur nulle)
    ']')


def clean_remote_str(v: object, limit: int = 200) -> str | None:
    """Chaîne venue du réseau → sûre pour l'affichage, ou None. FRONTIÈRE DE CONFIANCE."""
    if not isinstance(v, str):
        return None
    return _CTRL_RE.sub('', _ANSI_RE.sub('', v))[:limit] or None


def _as_int(v: object) -> int | None:
    # isinstance(True, int) est vrai en Python : un booléen n'est pas un pid.
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _as_float(v: object) -> float | None:
    """Nombre fini, ou None. UNIQUE point d'entrée des flottants du réseau.

    isinstance(float('inf'), float) est VRAI : sans le test de finitude,
    `idle_seconds: Infinity` donnait last_activity = -inf, et format_idle levait
    OverflowError DANS UN CALLBACK GLib.timeout_add — qui RETIRE la source
    définitivement : le widget entier se fige, sessions locales comprises, et la
    trace part sur un stderr qu'un widget lancé depuis le bureau n'a pas. Et ce
    n'est pas réservé à un hôte hostile : json.dumps ÉMET `Infinity` par défaut
    et json.loads l'accepte, donc un webui simplement buggé suffit. Un entier
    gigantesque (10**400) fait lever float() lui-même — même traitement.
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except (OverflowError, ValueError):
        return None
    return f if math.isfinite(f) else None


def mask_query(query: str) -> str:
    """Valeurs de la query masquées : `?key=s3cr3t` → `?key=***`.

    Le webui n'accepte PLUS le token en query : il ne lit que les en-têtes, et
    son middleware d'accès journalise `query_params` à chaque requête — un token
    posé là serait à la fois refusé et écrit en clair dans le log du serveur.
    L'URL qu'on nous DONNE peut malgré tout en porter un, par habitude ou en
    suivant une doc plus ancienne, et sans masquage il ressortait en clair dans
    l'infobulle de ligne, la barre d'état et la boîte de dialogue de paramètres.
    On masque TOUTE valeur : deviner laquelle est le secret est précisément ce
    qu'on ne veut pas parier, et le masquage ne coûte rien.
    """
    if not query:
        return ''
    return '&'.join(f'{k}=***' if sep else k
                    for k, sep, _v in (p.partition('=') for p in query.split('&')))


def redact_secrets(msg: str, url: str) -> str:
    """Masque dans un message d'erreur toute valeur de query de l'URL interrogée.

    `display_url` est rédigée, mais l'URL réellement passée à urllib garde sa
    query — il le FAUT : on ne réécrit pas l'URL qu'on nous a donnée (un reverse
    proxy devant le webui peut exiger ses propres paramètres). Or n'importe
    quelle exception qui cite l'URL (URL invalide, échec de connexion, timeout)
    recopie donc dans `st['error']` tout secret que cette query contiendrait,
    lequel est rendu dans l'infobulle de ligne, celle de la barre d'état, l'état
    vide et la sortie de `--once`. Un simple espace collé dans la valeur suffit à
    déclencher le cas.

    On masque la valeur, pas la clé : c'est la valeur qui est le secret, et la
    remplacer telle quelle couvre aussi bien la forme brute que celle réécrite
    par urllib.
    """
    query = urllib.parse.urlsplit(url).query
    if not query:
        return msg
    for part in query.split('&'):
        _k, sep, value = part.partition('=')
        # Seuil de longueur : le remplacement est une SOUS-CHAÎNE, donc une valeur
        # courte mutile le diagnostic sans rien protéger — `?key=e` transformait
        # « TimeoutError: timed out » en « Tim***outError: tim***d out ». En
        # dessous de 4 caractères, ce n'est pas un secret qu'on défend.
        if sep and len(value) >= 4:
            msg = msg.replace(value, '***').replace(urllib.parse.quote(value, safe=''), '***')
    return msg


def split_remote_url(url: str) -> tuple[str, str | None, str]:
    """URL avec userinfo → (url propre, token, url rédigée pour affichage).

    urllib NE SAIT PAS traiter le userinfo (vérifié) : laissé en place,
    Request.host devient « remote:tok@hote:8000 », aucun en-tête d'auth n'est
    ajouté et la connexion meurt sur une résolution DNS de cette chaîne. On
    découpe donc nous-mêmes, on garde le token de côté (envoyé en X-API-Key) et
    on reconstruit une URL propre.

    Le token est le MOT DE PASSE (« https://remote:tok@hote/ ») et, à défaut, le
    NOM D'UTILISATEUR (« https://tok@hote/ ») — même règle que le serveur, donc
    les deux bouts ne peuvent pas diverger. `(pwd or user)` et non
    `(pwd if has_pwd else user)` : sur « https://tok:@hote/ » (mot de passe vide),
    la seconde forme donnait None côté client là où le serveur retient « tok ».

    La rédaction se fait ICI, à l'unique point d'analyse : le token vit dans la
    chaîne d'URL, donc tout chemin qui affiche cette chaîne fuit tant qu'elle
    n'est pas rédigée en amont. La QUERY est masquée dans les deux branches :
    elle survit à l'absence de userinfo, et elle peut porter un secret que le
    webui n'accepte plus mais que l'utilisateur y a laissé quand même.
    On découpe la netloc à la main (et pas via u.hostname/u.port) pour préserver
    la casse et les crochets d'une adresse IPv6.
    """
    u = urllib.parse.urlsplit(url)
    shown_query = mask_query(u.query)
    userinfo, sep, hostport = u.netloc.rpartition('@')
    if not sep:
        return url, None, urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, shown_query, ''))
    user, has_pwd, pwd = userinfo.partition(':')
    token = (pwd or user) or None
    clean = urllib.parse.urlunsplit((u.scheme, hostport, u.path, u.query, ''))
    masked = f'{user}:***@{hostport}' if has_pwd else f'***@{hostport}'
    return clean, token, urllib.parse.urlunsplit((u.scheme, masked, u.path, shown_query, ''))


def remote_token_env(name: str) -> str:
    """Nom de la variable d'environnement portant le token : `CW_REMOTE_TOKEN_<NOM>`."""
    return 'CW_REMOTE_TOKEN_' + re.sub(r'[^A-Za-z0-9]', '_', name).upper()


# Sémantique de configparser.getboolean, à la lettre. Seul « false » désactivait :
# « no », « 0 » et « off » laissaient le remote ACTIF, donc le token continuait de
# partir vers un hôte que l'utilisateur croyait éteint.
_BOOL_TRUE  = frozenset({'1', 'yes', 'true', 'on'})
_BOOL_FALSE = frozenset({'0', 'no', 'false', 'off'})


def remote_enabled(value: object, where: str) -> bool:
    """`enabled = …` → booléen. Lève ValueError sur une valeur ininterprétable.

    On REFUSE bruyamment plutôt que de retomber sur « activé » : le mode de panne
    d'une faute de frappe doit être « le watcher te le dit », pas « ton token
    part quand même ».
    """
    if isinstance(value, bool):
        return value
    v = str(value).strip().lower()
    if v in _BOOL_TRUE:
        return True
    if v in _BOOL_FALSE:
        return False
    raise ValueError(
        f"{where} enabled = {value!r} : valeur booléenne invalide "
        f"(attendu {'/'.join(sorted(_BOOL_TRUE))} ou {'/'.join(sorted(_BOOL_FALSE))})")


def enabled_remotes(remotes: list[dict]) -> list[dict]:
    """Les remotes actifs. UNIQUE application du filtre `enabled` : la liste
    complète (désactivés compris) reste disponible pour l'écran de paramètres,
    qui doit pouvoir montrer qu'un remote a bien été analysé mais est éteint."""
    return [r for r in remotes if r.get('enabled', True)]


def resolve_remotes(sections: dict[str, dict], flags: list[tuple[str, str]] | None,
                    env: Mapping[str, str] | None = None) -> list[dict]:
    """Sections [remote:<nom>] + drapeaux --remote → liste de remotes résolus.

    Le drapeau NOMME un remote, il ne l'efface pas : son URL gagne pour ce run,
    les autres clés de la section (token, label) survivent. Un remote déclaré en
    ligne de commande est forcément voulu maintenant → enabled. Rien n'est jamais
    réécrit dans le config.ini.

    Ordre de résolution du token : userinfo de l'URL → CW_REMOTE_TOKEN_<NOM> →
    section → aucun.

    Lève ValueError sur un `enabled` ininterprétable ou sur deux noms qui
    retombent sur la MÊME variable d'environnement (cf. plus bas).
    """
    env = os.environ if env is None else env
    merged: dict[str, dict] = {}
    for name, sec in sections.items():
        merged[name] = {
            'name':    name,
            'url':     (sec.get('url') or '').strip(),
            'token':   (sec.get('token') or '').strip() or None,
            'label':   (sec.get('label') or '').strip() or name,
            'enabled': remote_enabled(sec.get('enabled', 'true'), f'[remote:{name}]'),
        }
    for name, url in flags or []:
        r = merged.setdefault(name, {'name': name, 'token': None, 'label': name})
        r['url'], r['enabled'] = url, True

    # `a-b`, `a.b`, `a_b` (et `lab` / `LAB`) donnent tous CW_REMOTE_TOKEN_A_B :
    # le token d'un hôte de confiance partirait vers un hôte sans rapport. On
    # détecte la collision ICI, au moment de résoudre, plutôt que de choisir
    # arbitrairement un gagnant.
    by_env: dict[str, list[str]] = {}
    for name, r in merged.items():
        if r.get('url'):   # une section sans url est ignorée : ne pas la compter
            by_env.setdefault(remote_token_env(name), []).append(name)
    for var, names in by_env.items():
        if len(names) > 1 and var in env:
            raise ValueError(
                f"remotes {', '.join(sorted(names))} : mêmes variable d'environnement "
                f"de token ({var}). Renommez-en un — sinon le token de l'un partirait "
                f"vers l'autre.")

    remotes = []
    for r in merged.values():
        if not r['url']:
            continue
        r['url'], url_token, r['display_url'] = split_remote_url(r['url'])
        r['token'] = url_token or env.get(remote_token_env(r['name'])) or r['token']
        # Label trop bavard : élidé ici, une fois — sinon il mangerait le projet.
        if len(r['label']) > REMOTE_LABEL_MAX:
            r['label'] = r['label'][:REMOTE_LABEL_MAX - 1] + '…'
        remotes.append(r)
    return remotes


def adapt_remote_agents(raw: object) -> list[dict]:
    """Liste d'agents du payload → liste nettoyée (sortie de adapt_remote_row).

    Une entrée sans nom exploitable est jetée : elle n'aurait rien à afficher.
    """
    agents: list[dict] = []
    if not isinstance(raw, list):
        return agents
    for a in raw[:50]:
        if not isinstance(a, dict):
            continue
        name = clean_remote_str(a.get('name'), 60)
        if name:
            agents.append({'pid':   _as_int(a.get('pid')),
                           'name':  name,
                           'type':  clean_remote_str(a.get('type'), 60),
                           'model': clean_remote_str(a.get('model'), 60)})
    return agents


def remote_last_activity(idle: float | None, received_at: float,
                         age_seconds: float) -> float | None:
    """Instant de dernière activité, en horloge MURALE LOCALE.

    On n'importe qu'une DURÉE (inactivité mesurée là-bas + âge du snapshot) et on
    la soustrait à l'instant de réception LOCAL : immunisé contre une dérive
    d'horloge murale entre les deux machines. Le rendu compare ensuite
    last_activity à time.time() local, exactement comme pour une session locale
    — c'est pourquoi cette valeur reste en horloge murale alors que la
    péremption d'un remote, elle, est mesurée en monotone.
    """
    if idle is None:
        return None
    # Les DEUX termes sous le même plafond, pas seulement `idle` : `_as_float` ne
    # rejette que les non-finis, donc un `age_seconds` de 1e308 rouvrait la cellule
    # de 311 caractères que ce plafond ferme — le correctif avait borné un opérande
    # en laissant son voisin sur l'ancienne hypothèse.
    return received_at - min(float(REMOTE_MAX_ELAPSED_S),
                            max(0.0, idle) + max(0.0, age_seconds))


def adapt_remote_row(row: object, remote: dict, received_at: float,
                     age_seconds: float = 0.0) -> dict | None:
    """Ligne d'API → dict de session locale, ou None si la ligne est inexploitable.

    Frontière de confiance : la FORME est validée autant que le contenu (chaque
    champ est converti, une ligne qui ne rentre pas est jetée, les autres
    passent). Un champ absent dégrade, il n'échoue pas — on met à jour un client
    avant d'avoir mis à jour tous les hôtes qu'il regarde.
    """
    if not isinstance(row, dict):
        return None
    pid = _as_int(row.get('pid'))
    if pid is None:
        return None
    state = row.get('state')
    bg_shell = row.get('bg_shell') is True
    # Serveur d'avant `bg_shell` : son 'background' VOULAIT dire « inactive, mais
    # un shell de fond tourne ». On le traduit au lieu de le jeter — un client à
    # jour lit des hôtes qui ne le sont pas encore, et perdre le signal en
    # silence serait pire qu'une ligne de traduction.
    if state == 'background':
        state, bg_shell = 'idle', True
    if state not in ('waiting', 'working', 'idle', 'daemon'):
        state = 'idle'
    idle = _as_float(row.get('idle_seconds'))
    last_activity = remote_last_activity(idle, received_at, age_seconds)
    pct = _as_int(row.get('context_pct'))
    cwd = clean_remote_str(row.get('cwd'), 300) or '?'
    agents = adapt_remote_agents(row.get('agents'))
    return {
        'pid':             pid,
        'starttime':       0,
        'project':         clean_remote_str(row.get('project'), 80) or '?',
        'worktree':        clean_remote_str(row.get('worktree'), 80),
        'display_cwd':     clean_remote_str(row.get('display_cwd'), 300) or cwd,
        'last_activity':   last_activity,
        'topic':           clean_remote_str(row.get('topic'), 400),
        'cwd':             cwd,
        # Plafonné : un elapsed importé sans borne (2**63) s'affiche
        # « 2562047788015215h30m » et fait déborder la cellule.
        'elapsed':         min(REMOTE_MAX_ELAPSED_S, max(0, _as_int(row.get('elapsed')) or 0)),
        'waiting':         state == 'waiting',
        'working':         state == 'working',
        'bg_shell':        bg_shell,
        'context_pct':     min(100, max(0, pct)) if pct is not None else None,
        'tool':            clean_remote_str(row.get('tool'), 40),
        # Rien de local ne doit pouvoir être visé depuis une ligne distante.
        'terminal_pid':    None,
        'window_id':       None,
        'kitty_socket':    None,
        'kitty_window_id': None,
        # Affichage seulement : _sync_status_monitors ne DOIT PAS en faire un
        # chemin local (le ~/.claude d'un remote existe aussi ici).
        'config_dir':      clean_remote_str(row.get('config_dir'), 60),
        'agents':          agents,
        'daemon':          bool(row.get('daemon')) or state == 'daemon',
        'remote':          remote['label'],
        'remote_name':     remote['name'],
    }


def adapt_remote_payload(payload: object, remote: dict,
                         received_at: float) -> tuple[list[dict], int]:
    """Payload /api/sessions → (lignes de session, nombre de lignes ANNONCÉES).

    Les mauvaises lignes sont jetées. Le total annoncé est renvoyé à part pour
    que la zone d'état puisse dire « 500/612 » : tronquer à REMOTE_MAX_ROWS en
    silence donne une liste qui a l'air complète.
    """
    if not isinstance(payload, dict):
        return [], 0
    rows = payload.get('sessions')
    if not isinstance(rows, list):
        return [], 0
    # age_seconds absent = webui antérieur à la mise en cache : 0.0, et le remote
    # marche. Coût maximal : un TTL de précision sur l'inactivité affichée.
    age = max(0.0, _as_float(payload.get('age_seconds')) or 0.0)
    adapted = (adapt_remote_row(r, remote, received_at, age) for r in rows[:REMOTE_MAX_ROWS])
    return [r for r in adapted if r is not None], len(rows)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Ne suit AUCUNE redirection : la redirection devient une HTTPError 3xx.

    Mesuré : l'ouvreur par défaut d'urllib suit les 3xx en REJOUANT les en-têtes
    de la requête — donc notre X-API-Key — vers la cible, y compris sur un autre
    hôte et en dégradant https → http. Un remote mal saisi ou compromis
    exfiltrerait le token avec une seule 302. Aucun besoin légitime de
    redirection ici : /api/sessions est servi directement.
    """

    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int,
                         msg: str, headers: Any, newurl: str) -> None:
        return None


_REMOTE_OPENER = urllib.request.build_opener(_NoRedirect)


def remote_endpoint(url: str) -> str:
    """URL de base → endpoint /api/sessions, en joignant sur le CHEMIN.

    `'https://box/?x=1'.rstrip('/') + '/api/sessions'` donnait
    « https://box/?x=1/api/sessions » : la query avalait le chemin et le endpoint
    n'était jamais demandé. La query reçue est préservée telle quelle — on ne
    réécrit pas l'URL qu'on nous a donnée (un reverse proxy devant le webui peut
    exiger ses propres paramètres). Le token, lui, n'y est JAMAIS ajouté par
    nous : il part en en-tête `X-API-Key` (cf. fetch_remote), seule forme que le
    webui accepte encore — et la query, elle, est journalisée côté serveur.
    """
    u = urllib.parse.urlsplit(url)
    if u.scheme not in REMOTE_SCHEMES:
        # file:// serait lu par l'ouvreur par défaut d'urllib : une faute de
        # frappe deviendrait une lecture de fichier local rendue comme des
        # sessions vivantes.
        raise ValueError(f"schéma non supporté : {u.scheme or '(aucun)'} "
                         f"(attendu {' ou '.join(REMOTE_SCHEMES)})")
    return urllib.parse.urlunsplit(
        (u.scheme, u.netloc, u.path.rstrip('/') + '/api/sessions', u.query, ''))


def read_capped(resp: Any, deadline: float) -> bytes:
    """Corps de réponse, au plus REMOTE_MAX_BYTES + 1 octets et avant `deadline`.

    Lecture par tranches, et pas un `read(MAX + 1)` unique : le timeout d'urlopen
    est PAR OPÉRATION socket, donc un pair qui livre un octet toutes les 4 s ne le
    déclenche jamais et parque le thread indéfiniment — ce qui défait aussi
    stop(). Le budget total est vérifié entre deux tranches (horloge monotone :
    un pas NTP ne doit pas rallonger ni écourter le budget).
    """
    chunks: list[bytes] = []
    total = 0
    while total <= REMOTE_MAX_BYTES:
        if time.monotonic() > deadline:
            raise TimeoutError('lecture trop lente')
        chunk = resp.read(min(REMOTE_READ_CHUNK, REMOTE_MAX_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b''.join(chunks)


def fetch_remote(remote: dict,
                 opener: Callable[..., Any] = _REMOTE_OPENER.open,
                 ) -> tuple[list[dict], str | None, int | None, int]:
    """Interroge un remote → (lignes, erreur, code HTTP, lignes annoncées).

    Ne lève JAMAIS : cette fonction est le corps d'un thread de poll, et une
    exception qui s'en échappe tue le thread pour de bon.

    La construction de la Request est DANS le try : `--remote lab=myhost` (sans
    schéma) lève ValueError, et hors du try elle tuait le thread au premier tour
    sans rien enregistrer — infobulle vide, état vide, « aucune session active »
    pour un remote mal configuré.

    timeout couvre connexion ET chaque opération socket ; la lecture a en plus un
    budget total ; le corps est plafonné à 4 MiB (un read() non borné sur une
    socket distante est une bombe mémoire que l'UI ne survit pas) ; les
    redirections ne sont pas suivies (cf. _NoRedirect).
    """
    try:
        req = urllib.request.Request(remote_endpoint(remote['url']),
                                     headers={'User-Agent': 'claude-watcher-gtk',
                                              'Accept': 'application/json'})
        if remote.get('token'):
            req.add_header('X-API-Key', remote['token'])
        with opener(req, timeout=REMOTE_TIMEOUT_S) as resp:
            raw = read_capped(resp, time.monotonic() + REMOTE_READ_BUDGET_S)
        if len(raw) > REMOTE_MAX_BYTES:
            return [], f'> {REMOTE_MAX_BYTES // (1024 * 1024)} MiB', None, 0
        payload = json.loads(raw.decode('utf-8', 'replace'))
    except urllib.error.HTTPError as e:
        return [], f'HTTP {e.code}', e.code, 0
    except Exception as e:
        # Tout le reste (URLError, timeout, URL invalide, JSON invalide,
        # décodage…) : le thread ne doit jamais mourir sur un hôte qui répond
        # n'importe quoi — ni sur une URL mal saisie.
        # Le texte d'erreur est SOUS CONTRÔLE DU SERVEUR (mesuré : une ligne de
        # statut bidon arrive telle quelle dans BadStatusLine, échappements ANSI
        # compris) et finit à l'écran → même nettoyage que le reste du payload.
        msg = redact_secrets(f'{type(e).__name__}: {e}', remote.get('url', ''))
        return [], clean_remote_str(msg, 120) or type(e).__name__, None, 0
    rows, total = adapt_remote_payload(payload, remote, time.time())
    return rows, None, None, total


def remote_health(st: dict, poll_s: float, now: float) -> tuple[str, float | None]:
    """(santé, âge de la donnée) — 'ok' | 'stale' | 'auth' | 'down' | 'starting' | 'dead'.

    `now` est une horloge MONOTONE, comme le `received_mono` qu'elle compare : la
    péremption est TOUT le contrat de cette fonctionnalité, et en horloge murale
    un pas NTP arrière ou un portable qui sort de veille rendait `max(0, now-ra)`
    positif et donc frais — un remote mort depuis une journée lisait « ok ».
    (`last_activity`, lui, reste en horloge murale : il est comparé à time.time()
    au rendu. Deux horloges, deux métiers.)

    « jamais répondu » (down) et « répondait, ne répond plus » (stale) sont
    distincts : un remote qui n'a JAMAIS répondu n'a aucune ligne à marquer
    périmée, il serait invisible sans la zone d'état. « starting » les distingue
    tous deux du cas normal « le premier poll n'est pas encore revenu », qui
    n'est pas une panne.

    'dead' : le thread de poll n'est plus là. Sans cet état, un thread mort après
    un premier succès lisait « ok » puis « périmé » POUR TOUJOURS — une donnée
    vieille d'un jour indiscernable d'un hôte ayant manqué deux polls.
    """
    ra = st.get('received_mono')
    age = None if ra is None else max(0.0, now - ra)
    if st.get('alive') is False:
        return 'dead', age
    if age is not None and age <= REMOTE_STALE_X * poll_s:
        return 'ok', age
    if st.get('status') in (401, 403):
        return 'auth', age
    if age is None:
        return ('down' if st.get('error') else 'starting'), None
    return 'stale', age


def remote_health_text(st: dict, poll_s: float, now: float) -> str:
    """Santé seule : « ok 3 » (joignable, 3 sessions) vs « injoignable » /
    « périmé 42s ». Confondre les deux premiers est exactement le mode de panne
    que cette fonctionnalité doit éviter : sans ça, les deux donnent la même
    liste vide.

    « ok 500/612 » quand le payload dépassait REMOTE_MAX_ROWS : tronquer en
    silence donne une liste qui a l'air complète.
    """
    health, age = remote_health(st, poll_s, now)
    if health == 'ok':
        shown = len(st.get('rows') or [])
        total = st.get('total') or shown
        count = f'{shown}/{total}' if total > shown else str(shown)
        return f"{tr('rm_ok')} {count}"
    if health == 'auth':
        return tr('rm_auth')
    if health == 'starting':
        return tr('rm_starting')
    if health == 'dead':
        return tr('rm_dead')
    if health == 'stale' and age is not None:
        return f"{tr('rm_stale')} {format_elapsed(age)}"
    return tr('rm_down')


def remote_status_text(remote: dict, st: dict, poll_s: float, now: float) -> str:
    """Fragment de la zone d'état : « lab ok 0 » / « lab injoignable »."""
    return f"{remote['label']} {remote_health_text(st, poll_s, now)}"


def local_config_dirs(sessions: list[dict]) -> list[str]:
    """config_dir des lignes LOCALES uniquement.

    Le config_dir d'une ligne distante est un chemin de l'AUTRE machine, et le
    ~/.claude d'un remote existe aussi ici : la boucle naïve poserait un monitor
    local pour le compte d'un remote. Même classe de bug que la collision de pid,
    rayon d'action plus faible.
    """
    return [s['config_dir'] for s in sessions
            if s.get('config_dir') and not s.get('remote')]


# Trois niveaux de rédaction des compteurs, du plus riche au plus dense. On MESURE
# avant de poser, au lieu de poser puis de constater l'ellipse : `is_ellipsized()`
# n'est vrai qu'après allocation, donc y réagir crée une boucle de relayout à
# borner. Niveau 2 en chiffres nus séparés par '/' et non en emoji : la plupart
# des terminaux rendent un emoji sur DEUX cellules, ce qui fausserait la mesure
# côté TUI.
def counts_segments(waiting: int, working: int, bg_shell: int, total: int,
                    level: int) -> list[tuple[str, str]]:
    """Segments (texte, couleur) de la zone de compteurs, pour un niveau donné.

    Un compteur à zéro n'est jamais écrit : « 0 attente » occupe la place qui
    manque justement. Le séparateur est l'affaire de l'appelant.
    """
    seg: list[tuple[str, str]] = []
    if level >= 2:
        if waiting:  seg.append((str(waiting), COLOR_WAITING))
        if working:  seg.append((str(working), COLOR_WORKING))
        if bg_shell: seg.append((f'{bg_shell}{BG_SHELL_GLYPH}', COLOR_BACKGROUND))
        seg.append((str(total), TEXT_DIM2))
        return seg
    if level == 1:
        if waiting:  seg.append((f"{waiting} {tr('waiting_s')}", COLOR_WAITING))
        if working:  seg.append((f"{working} {tr('working_s')}", COLOR_WORKING))
        if bg_shell: seg.append((f'{bg_shell}{BG_SHELL_GLYPH}', COLOR_BACKGROUND))
        seg.append((str(total), TEXT_DIM2))
        return seg
    if waiting:  seg.append((f"{waiting} {tr('waiting')}", COLOR_WAITING))
    if working:  seg.append((f"{working} {tr('working')}", COLOR_WORKING))
    if bg_shell: seg.append((f'{bg_shell} {BG_SHELL_GLYPH}', COLOR_BACKGROUND))
    seg.append((f"{total} total", TEXT_DIM2))
    return seg


def counts_sep(level: int) -> str:
    """Séparateur des compteurs : '/' au niveau le plus dense, ' · ' sinon."""
    return '/' if level >= 2 else ' · '


def fit_level(measure, budget: int, levels: int = 3) -> int:
    """Premier niveau (0 = le plus riche) dont le rendu tient dans `budget`.

    `measure(level) -> largeur`, dans l'unité de l'appelant (pixels côté GTK,
    cellules côté TUI). Budget nul ou négatif (label pas encore alloué) → niveau
    le plus riche : on ne dégrade pas sur une mesure qu'on n'a pas.
    """
    if budget <= 0:
        return 0
    for lvl in range(levels):
        if measure(lvl) <= budget:
            return lvl
    return levels - 1


def remotes_bar_text(remotes: list[dict], stat: dict[str, dict],
                     poll_s: float, now: float) -> str:
    """Contenu de la zone d'état : un fragment par remote CONFIGURÉ, session ou pas."""
    return f"{tr('rm_label')}: " + ' · '.join(
        remote_status_text(r, stat.get(r['name'], {}), poll_s, now) for r in remotes)


def remotes_bar_tooltip(remotes: list[dict], stat: dict[str, dict]) -> str:
    """Infobulle de la zone d'état : URL RÉDIGÉE + erreur courante par remote."""
    lines = []
    for r in remotes:
        st = stat.get(r['name'], {})
        lines.append(f"{r['label']} — {st.get('display_url', r.get('display_url', ''))}"
                     + (f"\n  {st['error']}" if st.get('error') else ''))
    return '\n'.join(lines)


def empty_state_text(remotes: list[dict], stat: dict[str, dict]) -> str:
    """« aucune session active » serait un mensonge quand des remotes ont été
    interrogés sans succès : on dit lesquels, et pourquoi."""
    failed = [f"{r['label']}: {stat[r['name']].get('error')}"
              for r in remotes
              if stat.get(r['name'], {}).get('error')]
    return '\n'.join([tr('rm_none'), *failed]) if failed else tr('no_session')


def remote_stale_text(rstate: dict | None) -> str | None:
    """« ⚠ périmé 42s » quand la donnée d'un remote dépasse 3 × l'intervalle de poll.

    La ligne est CONSERVÉE (la jeter ferait clignoter la liste au moindre poll
    manqué) mais elle affiche l'âge de la donnée.
    """
    if not rstate or rstate.get('health') in ('ok', 'starting'):
        return None
    age = rstate.get('age')
    return f"⚠ {tr('rm_stale_row')}" + (f' {format_elapsed(age)}' if age is not None else '')


def session_tooltip(s: dict, rstate: dict | None = None) -> str:
    """Infobulle de ligne : chemin complet + sujet complet (les labels tronquent)
    + liste des sous-agents.

    Ligne distante : URL RÉDIGÉE (le token vit dans l'URL) + erreur courante —
    seul moyen de savoir quelle machine se cache derrière un label. C'est aussi
    là que la lecture seule est ÉNONCÉE : le widget retire les affordances
    (focus, fermeture) au lieu de les laisser échouer, exactement comme pour une
    ligne de démon, et l'infobulle dit pourquoi. Cacher sans dire serait un
    silence ; dire sans cacher serait une promesse non tenue.
    """
    tip = s['cwd']
    label = s.get('remote')
    if label:
        tip = (f"{tip}\n\n{tr('tip_remote').format(label=label)}"
               f"\n{(rstate or {}).get('display_url', '')}")
        if rstate and rstate.get('error'):
            tip = f"{tip}\n{rstate['error']}"
        stale = remote_stale_text(rstate)
        if stale:
            tip = f"{tip}\n{stale}"
    if s.get('daemon'):
        return f"{tip}\n\n{tr('tip_daemon')}"
    if s.get('bg_shell'):
        # Le marqueur de ligne est un glyphe ; l'infobulle est le seul endroit qui
        # dit ce qu'il signifie.
        tip = f"{tip}\n\n{BG_SHELL_GLYPH} {tr('bg_shell')}"
    topic = (s.get('topic') or '').strip()
    if topic:
        tip = f'{tip}\n\nTopic: {topic}'
    agents = s.get('agents') or []
    if agents:
        lines = []
        for a in agents:
            detail = ', '.join(x for x in (a.get('type'), a.get('model')) if x)
            lines.append(f" • {a['name']}" + (f' ({detail})' if detail else ''))
        tip = f"{tip}\n\n{tr('tip_agents')}\n" + '\n'.join(lines)
    return tip


def remote_rstate(s: dict, stat: dict[str, dict], poll_s: float,
                  now_mono: float) -> dict | None:
    """État du remote d'une ligne : santé, âge de la donnée, URL rédigée, erreur.
    None pour une ligne locale. `now_mono` est une horloge MONOTONE."""
    if not s.get('remote'):
        return None
    st = stat.get(s.get('remote_name') or '')
    if st is None:
        return None
    health, age = remote_health(st, poll_s, now_mono)
    return {'health': health, 'age': age, 'error': st.get('error'),
            'display_url': st.get('display_url', '')}


def remotes_panel_rows(remotes: list[dict], stat: dict[str, dict],
                       poll_s: float, now: float) -> list[tuple[str, str, str]]:
    """(nom, URL rédigée, santé) par remote configuré — panneau lecture seule des
    paramètres. Les remotes DÉSACTIVÉS y figurent aussi : sans eux, rien ne dit
    qu'un `[remote:*]` a bien été analysé mais est éteint."""
    rows = []
    for r in remotes:
        st = stat.get(r['name'], {})
        health = (remote_health_text(st, poll_s, now) if r.get('enabled', True)
                  else tr('off'))
        rows.append((r['name'], st.get('display_url', r.get('display_url', '')), health))
    return rows


class RemotePoller:
    """Un thread démon par remote actif ; cache {nom: état} sous verrou.

    Chaque thread boucle SÉQUENTIELLEMENT (requête, puis attente) : un hôte lent
    ne ralentit que lui-même, les requêtes ne s'empilent jamais, et un remote mort
    n'affecte pas les autres. `sessions()` et `snapshot()` ne font AUCUN HTTP —
    ils sont appelés depuis la boucle GTK.

    Aucun widget n'est touché ici : le thread appelle `notify` (côté GTK :
    GLib.idle_add), qui replanifie le rafraîchissement dans la boucle principale.
    La TUI se rafraîchit sur son propre timer et ne passe pas de notify.

    La liste reçue est déjà filtrée sur `enabled` (cf. enabled_remotes, appliqué
    une seule fois à la résolution) : ce constructeur ne refiltre pas.
    """

    def __init__(self, remotes: list[dict], poll_ms: int = REMOTE_POLL_MS,
                 notify: Callable[[], None] | None = None) -> None:
        self.remotes = list(remotes)
        self.poll_s = max(REMOTE_POLL_MIN_MS / 1000, poll_ms / 1000)
        self._notify = notify
        self._lock = threading.Lock()
        self._stop = threading.Event()
        # `received_mono` et non `received_at` : c'est une horloge MONOTONE, et
        # le nom doit l'annoncer — la confondre avec l'horloge murale est
        # exactement le bug que la péremption ne peut pas se permettre.
        self._state = {r['name']: {'rows': [], 'received_mono': None, 'error': None,
                                   'status': None, 'total': 0, 'alive': None,
                                   'display_url': r.get('display_url', '')}
                       for r in self.remotes}

    def start(self) -> None:
        for r in self.remotes:
            with self._lock:
                self._state[r['name']]['alive'] = True
            threading.Thread(target=self._loop, args=(r,), daemon=True,
                             name=f"remote-{r['name']}").start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._state.items()}

    def sessions(self) -> list[dict]:
        """Dernières lignes connues de tous les remotes (conservées en cas d'échec :
        les jeter ferait clignoter la liste au moindre poll manqué, et le marqueur
        « périmé » dit déjà qu'elles sont vieilles)."""
        with self._lock:
            return [row for st in self._state.values() for row in st['rows']]

    def _backoff(self, fails: int, status: int | None) -> float:
        delay = min(self.poll_s * 2 ** min(fails, 5), REMOTE_BACKOFF_MAX_S)
        if status in (401, 403):
            # Mauvais token : réessayer toutes les 2 s ne le corrigera pas.
            delay = max(delay, REMOTE_AUTH_RETRY_S)
        return delay

    def _loop(self, remote: dict) -> None:
        """Boucle de poll. Ne lève JAMAIS — et c'est GARANTI ici, pas promis.

        Rien ne gardait ce corps : une levée (depuis notify — donc depuis
        GLib.idle_add côté GTK —, depuis un callback, depuis n'importe quoi)
        terminait le thread. Et comme `received_mono` était déjà posé, le remote
        lisait « ok » pendant 3 × l'intervalle puis « périmé » POUR TOUJOURS —
        un instantané vieux d'un jour indiscernable d'un hôte ayant manqué deux
        polls. D'où : corps gardé, erreur enregistrée, et `alive=False` en sortie
        pour que le thread disparu ne puisse plus jamais lire « ok ».
        """
        fails = 0
        try:
            while not self._stop.is_set():
                try:
                    rows, error, status, total = fetch_remote(remote)
                    with self._lock:
                        st = self._state[remote['name']]
                        st['error'], st['status'] = error, status
                        if error is None:
                            st['rows'], st['total'] = rows, total
                            st['received_mono'] = time.monotonic()
                    # Le thread ne touche AUCUN widget : il demande juste à la
                    # boucle principale de se rafraîchir (GLib.idle_add côté
                    # appelant).
                    if self._notify is not None and not self._stop.is_set():
                        self._notify()
                    if error is None:
                        fails, delay = 0, self.poll_s
                    else:
                        fails += 1
                        delay = self._backoff(fails, status)
                except Exception as e:
                    fails += 1
                    delay = self._backoff(fails, None)
                    msg = redact_secrets(f'{type(e).__name__}: {e}', remote.get('url', ''))
                    with self._lock:
                        self._state[remote['name']]['error'] = (
                            clean_remote_str(msg, 120)
                            or type(e).__name__)
                self._stop.wait(delay)
        finally:
            with self._lock:
                self._state[remote['name']]['alive'] = False


# ── Session row ───────────────────────────────────────────────────────────────

def session_project_markup(s: dict) -> str:
    """Markup Pango du libellé projet.

    Ligne distante : préfixe « <label>: » (convention scp/rsync, pas besoin de
    légende) ; une ligne locale reste NUE. Le préfixe est en TÊTE du markup et
    l'ellipsage du label est en mode END : le marqueur survit donc à la
    troncature par construction — là où la TUI doit réserver le budget avant de
    tronquer par la gauche.
    """
    prefix = (f'<span foreground="{COLOR_CLAUDE}" weight="bold">(D)</span> '
              if s.get('daemon') else '')
    label = s.get('remote')
    if label:
        prefix += (f'<span foreground="{COLOR_REMOTE}">'
                   f'{GLib.markup_escape_text(label)}:</span>')
    return (f'<span foreground="{TEXT_PRIMARY}" font="Monospace 9" weight="500">'
            f'{prefix}{GLib.markup_escape_text(s["project"])}</span>')


class SessionRow(Gtk.EventBox):
    def __init__(self, session: dict, rstate: dict | None = None):
        super().__init__()
        self.session  = session
        # État du remote de cette ligne (santé, âge, URL rédigée, erreur), calculé
        # par la fenêtre depuis le cache du poller. None pour une ligne locale.
        self.rstate   = rstate
        self._hovered     = False
        self._kb_selected = False
        self._ctx_menu    = None
        # La ligne peut être détruite (rebuild de structure) pendant que son menu
        # contextuel est ouvert : on le referme pour ne pas laisser des entrées
        # périmées (focus/copie/kill d'une session disparue) activables.
        self.connect('destroy', self._on_destroyed)

        # Survol : chemin de travail complet + sujet complet (les labels tronquent
        # — projet aux 2 derniers segments, sujet à la 1re ligne ellipsée)
        # + détail des subagents.
        self.set_tooltip_text(session_tooltip(session, rstate))
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
        # Sous-ligne worktree : « ↳ WT: <nom> » sous le projet quand la session
        # tourne dans un worktree Claude confirmé. Masquée sinon, sans gap.
        self.lbl_worktree = Gtk.Label()
        self.lbl_worktree.set_halign(Gtk.Align.FILL)
        self.lbl_worktree.set_xalign(0.0)
        self.lbl_worktree.set_hexpand(True)
        self.lbl_worktree.set_ellipsize(Pango.EllipsizeMode.END)
        self.lbl_worktree.set_max_width_chars(40)
        self.lbl_worktree.set_no_show_all(True)
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
        info.pack_start(self.lbl_project,  False, False, 0)
        info.pack_start(self.lbl_worktree, False, False, 0)
        info.pack_start(self.lbl_topic,    False, False, 0)
        info.pack_start(self.lbl_meta,     False, False, 0)
        box.pack_start(info, True, True, 0)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        right.set_valign(Gtk.Align.CENTER)
        self.badge = Gtk.Label()
        self.badge.set_halign(Gtk.Align.END)
        # Subagent count, right under the state badge; hidden (no gap) when the
        # session has no spawned subagents.
        self.lbl_agents = Gtk.Label()
        self.lbl_agents.set_halign(Gtk.Align.END)
        self.lbl_agents.set_no_show_all(True)
        self.lbl_ctx = Gtk.Label()
        self.lbl_ctx.set_halign(Gtk.Align.END)
        self.lbl_ctx.set_ellipsize(Pango.EllipsizeMode.END)
        self.lbl_ctx.set_max_width_chars(16)
        right.pack_start(self.badge,      False, False, 0)
        right.pack_start(self.lbl_agents, False, False, 0)
        right.pack_start(self.lbl_ctx,    False, False, 0)
        box.pack_end(right, False, False, 0)

        self._update_labels()
        self.show_all()

    def update_session(self, session: dict, rstate: dict | None = None):
        """Met à jour la ligne EN PLACE (pas de recréation).

        Réécrit tooltip + labels + couleur du point depuis la nouvelle session,
        sans détruire le widget : l'état de survol (hover) et la sélection clavier
        sont préservés, et l'anim de pulse n'est pas réinitialisée. Appelé par
        _rebuild_sessions quand la structure (pids/colonnes) est inchangée.
        """
        self.session = session
        self.rstate  = rstate
        self.set_tooltip_text(session_tooltip(session, rstate))
        self._update_labels()
        self.dot.queue_draw()

    def _update_labels(self):
        s = self.session
        daemon = s.get('daemon')
        if daemon:
            # Démon : ni actif ni inactif — point/badge neutres (gris), non pulsé.
            color, badge_txt = TEXT_DIM2, tr('daemon')
        elif s['waiting']:
            color, badge_txt = COLOR_WAITING, tr('attend')
        elif s['working']:
            color, badge_txt = COLOR_WORKING, tr('working')
        else:
            color, badge_txt = COLOR_IDLE, tr('idle')
        self._dot_color = color
        # Préfixe « (D) » (démon) et « <label>: » (ligne distante) : cf.
        # session_project_markup.
        self.lbl_project.set_markup(session_project_markup(s))
        worktree = s.get('worktree')
        if worktree:
            self.lbl_worktree.set_markup(
                f'<span foreground="{COLOR_CLAUDE}" font="Monospace 8">'
                f'↳ WT: {GLib.markup_escape_text(worktree)}</span>'
            )
            self.lbl_worktree.set_visible(True)
        else:
            self.lbl_worktree.set_visible(False)
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
        stale = remote_stale_text(self.rstate)
        if stale:
            meta += (
                f' <span foreground="{COLOR_WAITING}" font="Monospace 8">'
                f'{GLib.markup_escape_text(stale)}</span>'
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
        elif not s['working'] and not s['waiting'] \
                and idle_fmt != 'none' and la is not None:
            # Session inactive : la colonne outil (vide en idle) sert la durée
            # d'inactivité = now − dernière activité. Un shell de fond ne la
            # supprime plus : Claude a rendu la main, la session EST inactive —
            # le shell se dit dans son marqueur, pas en volant cette cellule.
            self.lbl_ctx.set_markup(
                f'<span foreground="{TEXT_DIM2}" font="Monospace 8">'
                f'{GLib.markup_escape_text(format_idle(time.time() - la, idle_fmt))}</span>'
            )
        else:
            self.lbl_ctx.set_text('')
        self.badge.set_markup(
            f'<span foreground="{color}" font="Monospace 8">{badge_txt}</span>'
        )
        # Sous-ligne de DÉTAILS : sous-agents et shell de fond y cohabitent. Le
        # marqueur n'est pas collé au badge, qui doit rester court — la ligne
        # d'état est déjà en concurrence de largeur avec le titre et le préfixe
        # d'une ligne distante.
        n_agents = len(s.get('agents') or [])
        detail = []
        if n_agents:
            detail.append(f'<span foreground="{COLOR_CLAUDE}">'
                          f'{n_agents} {tr("agents") if n_agents > 1 else tr("agent")}</span>')
        if s.get('bg_shell'):
            detail.append(f'<span foreground="{COLOR_BACKGROUND}">{BG_SHELL_GLYPH} sh</span>')
        if detail:
            self.lbl_agents.set_markup(
                f'<span font="Monospace 8">{" ".join(detail)}</span>')
            self.lbl_agents.set_visible(True)
        else:
            self.lbl_agents.set_visible(False)

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
        # Curseur « main » pour signaler le clic-focus ; flèche par défaut pour le
        # démon et les sessions distantes, qui ne sont pas focusables.
        cursor_name = ('default' if (self.session.get('daemon') or self.session.get('remote'))
                       else 'pointer')
        self.get_window().set_cursor(Gdk.Cursor.new_from_name(self.get_display(), cursor_name))
        self.queue_draw()

    def _on_leave(self, *_):
        self._hovered = False
        self.queue_draw()

    def set_kb_selected(self, selected: bool):
        if self._kb_selected != selected:
            self._kb_selected = selected
            self.queue_draw()

    def _do_focus(self):
        # Le démon n'a pas de terminal : rien à focus. Une session distante non
        # plus, et son window_id/terminal_pid viseraient une fenêtre LOCALE sans
        # rapport (la garde vit aussi dans focus_terminal, au point
        # d'étranglement). Garde unique couvrant les trois entrées (clic gauche,
        # menu, Entrée clavier).
        if self.session.get('daemon') or self.session.get('remote'):
            return
        focus_terminal(self.session)

    def _on_destroyed(self, *_):
        if self._ctx_menu is not None:
            self._ctx_menu.popdown()
            self._ctx_menu = None

    def _on_click(self, widget, event):
        if event.button == 1:
            self._do_focus()
            return True  # don't bubble up to the window background menu
        if event.button == 3:
            self._show_context_menu(event)
            return True
        return False

    def _show_context_menu(self, event):
        s = self.session
        # Référence gardée sur self : sinon le menu est ramassé par le GC avant
        # même de s'afficher.
        self._ctx_menu = menu = Gtk.Menu()
        # Pas de « Focus » pour le démon ni pour une session distante : aucun
        # terminal à activer ici.
        if not s.get('daemon') and not s.get('remote'):
            item_focus = Gtk.MenuItem.new_with_label(tr('menu_focus'))
            item_focus.connect('activate', lambda _m: self._do_focus())
            menu.append(item_focus)
        item_pid = Gtk.MenuItem.new_with_label(f"{tr('menu_copy_pid')} ({s['pid']})")
        item_pid.connect(
            'activate',
            lambda _m: Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(str(s['pid']), -1))
        menu.append(item_pid)
        # « Fermer » réservé aux sessions inactives : on ne propose pas de tuer
        # une session qui travaille ou attend une réponse (tour en cours). Exclu
        # aussi pour le démon — pas une session (pas de registre keyé par pid),
        # le kill échouerait systématiquement avec un message trompeur — et pour
        # une session distante : lecture seule (kill_session refuserait de toute
        # façon, ici c'est l'UI qui ne le propose pas).
        if not s['waiting'] and not s['working'] and not s.get('daemon') \
                and not s.get('remote'):
            item_kill = Gtk.MenuItem.new_with_label(tr('menu_kill'))
            item_kill.connect('activate', lambda _m: self._confirm_kill())
            menu.append(item_kill)
        menu.show_all()
        menu.popup_at_pointer(event)

    def _confirm_kill(self):
        s = self.session
        la = s.get('last_activity')
        idle_txt = format_idle(time.time() - la, 'precise') if la is not None else '?'
        dlg = Gtk.MessageDialog(
            transient_for=self.get_toplevel(), modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=tr('kill_confirm_title'),
        )
        dlg.format_secondary_text(
            tr('kill_confirm_body').format(proj=s['project'], idle=idle_txt))
        # Le widget est un DOCK toujours-au-dessus : sans keep_above le dialogue
        # passe DERRIÈRE. Centré écran plutôt que sur le petit widget du coin.
        dlg.set_keep_above(True)
        dlg.set_position(Gtk.WindowPosition.CENTER)
        resp = dlg.run()
        dlg.destroy()
        if resp == Gtk.ResponseType.OK:
            self._do_kill()

    def _do_kill(self):
        s = self.session
        if kill_session(s):
            return  # la ligne disparaîtra au prochain scan (process terminé)
        warn = Gtk.MessageDialog(
            transient_for=self.get_toplevel(), modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.CLOSE,
            text=tr('kill_failed'),
        )
        warn.set_keep_above(True)
        warn.set_position(Gtk.WindowPosition.CENTER)
        warn.run()
        warn.destroy()

# ── Settings dialog ──────────────────────────────────────────────────────────

class SettingsDialog(Gtk.Dialog):
    """Dialogue de configuration — accessible depuis le systray."""

    def __init__(self, parent: 'ClaudeWatcher',
                 remote_stat: dict[str, dict] | None = None,
                 remote_poll_s: float = REMOTE_POLL_MS / 1000):
        # L'état des remotes est PASSÉ, pas pioché dans parent._poller : le
        # dialogue n'a pas à savoir qu'un poller existe (il n'en existe aucun
        # quand rien n'est déclaré), et il lui faut de toute façon la liste
        # complète, que le poller ne détient pas.
        super().__init__(title=tr('settings_title'), modal=True)
        self._parent = parent
        remote_stat = remote_stat or {}
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
            'show_agents': getattr(CFG, 'show_agents', True),
            'hide_daemons': getattr(CFG, 'hide_daemons', False),
            'refresh_ms': CFG.refresh_ms,
            'snooze_sec': CFG.snooze_sec,
            # Effective on-screen value, not CFG: shift+scroll moves it away
            # from the configured base — the dialog must show (and restore)
            # what's on screen, while CFG keeps the base until Apply.
            'bg_alpha':   round(parent._effective_alpha() * 100),
        }
        self.set_default_size(500, -1)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.add_button(tr('cancel'), Gtk.ResponseType.CANCEL)
        ok_btn = self.add_button(tr('apply'), Gtk.ResponseType.OK)
        ok_btn.get_style_context().add_class('suggested-action')

        content = self.get_content_area()
        nb = Gtk.Notebook()
        nb.set_border_width(8)
        content.add(nb)

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

        # Respiration entre les deux blocs de colonnes appariés : marge à gauche
        # du premier widget du bloc droit (col 3), sinon les deux blocs se collent.
        def gutter(w: Gtk.Widget) -> Gtk.Widget:
            w.set_margin_start(22)
            return w

        def info_label(text: str, tooltip: str) -> Gtk.Box:
            # Label + icône info (explication au survol), aligné comme field_label.
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
            box.set_halign(Gtk.Align.END)
            box.set_valign(Gtk.Align.CENTER)
            box.pack_start(Gtk.Label(label=text), False, False, 0)
            icon = Gtk.Image.new_from_icon_name('dialog-information-symbolic', Gtk.IconSize.MENU)
            icon.set_tooltip_text(tooltip)
            box.pack_start(icon, False, False, 0)
            return box

        def make_page(title_key: str) -> Gtk.Grid:
            # 6 colonnes logiques : deux blocs [label|champ|unité] côte à côte.
            g = Gtk.Grid()
            g.set_row_spacing(8)
            g.set_column_spacing(10)
            g.set_margin_start(14)
            g.set_margin_end(14)
            g.set_margin_top(12)
            g.set_margin_bottom(12)
            nb.append_page(g, Gtk.Label(label=tr(title_key)))
            return g

        # ── Onglet Général : langue, timings, raccourci ─────────────────────
        gg = make_page('tab_general')

        gg.attach(field_label(tr('fld_lang')), 0, 0, 1, 1)
        self._lang_combo = Gtk.ComboBoxText()
        self._lang_combo.append('fr', tr('lang_fr'))
        self._lang_combo.append('en', tr('lang_en'))
        self._lang_combo.set_active_id(CFG.lang)
        gg.attach(self._lang_combo, 1, 0, 2, 1)

        gg.attach(info_label(tr('fld_refresh'), tr('help_refresh')), 0, 1, 1, 1)
        self._spin_refresh = Gtk.SpinButton.new_with_range(500, 10000, 500)
        self._spin_refresh.set_value(CFG.refresh_ms)
        gg.attach(self._spin_refresh, 1, 1, 1, 1)
        gg.attach(Gtk.Label(label="ms"), 2, 1, 1, 1)

        gg.attach(gutter(info_label(tr('fld_snooze'), tr('help_snooze'))), 3, 1, 1, 1)
        self._spin_snooze = Gtk.SpinButton.new_with_range(10, 3600, 10)
        self._spin_snooze.set_value(CFG.snooze_sec)
        gg.attach(self._spin_snooze, 4, 1, 1, 1)
        gg.attach(Gtk.Label(label="s"), 5, 1, 1, 1)

        gg.attach(Gtk.Separator(), 0, 2, 6, 1)
        gg.attach(section_label(tr('sec_shortcut')), 0, 3, 6, 1)

        self._chk_shortcut = Gtk.CheckButton(label=tr('fld_shortcut_enable'))
        self._chk_shortcut.set_active(CFG.shortcut_enable)
        gg.attach(self._chk_shortcut, 0, 4, 6, 1)

        gg.attach(field_label(tr('fld_hotkey')), 0, 5, 1, 1)
        self._entry_hotkey = Gtk.Entry()
        self._entry_hotkey.set_text(CFG.hotkey)
        self._entry_hotkey.set_placeholder_text(tr('hotkey_hint'))
        self._entry_hotkey.set_tooltip_text(tr('hotkey_hint'))
        gg.attach(self._entry_hotkey, 1, 5, 5, 1)
        # Hotkey/enable take effect only on Apply (no live rebinding preview).
        self._chk_shortcut.connect(
            'toggled', lambda c: self._entry_hotkey.set_sensitive(c.get_active()))
        self._entry_hotkey.set_sensitive(CFG.shortcut_enable)

        # ── Onglet Position ──────────────────────────────────────────────────
        gp = make_page('tab_position')

        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        self._radio_corner = Gtk.RadioButton(label=tr('mode_corner'))
        self._radio_free   = Gtk.RadioButton.new_with_label_from_widget(
            self._radio_corner, tr('mode_free'))
        mode_box.pack_start(self._radio_corner, False, False, 0)
        mode_box.pack_start(self._radio_free,   False, False, 0)
        gp.attach(field_label(tr('fld_mode')), 0, 0, 1, 1)
        gp.attach(mode_box, 1, 0, 5, 1)

        gp.attach(field_label(tr('fld_screen')), 0, 1, 1, 1)
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
        gp.attach(self._screen_combo, 1, 1, 5, 1)

        gp.attach(field_label(tr('fld_corner')), 0, 2, 1, 1)
        self._corner_combo = Gtk.ComboBoxText()
        for val, key in [
            ('bottom-right', 'corner_br'),
            ('bottom-left',  'corner_bl'),
            ('top-right',    'corner_tr'),
            ('top-left',     'corner_tl'),
        ]:
            self._corner_combo.append(val, tr(key))
        self._corner_combo.set_active_id(CFG.corner)
        gp.attach(self._corner_combo, 1, 2, 2, 1)

        gp.attach(field_label(tr('fld_margin_x')), 0, 3, 1, 1)
        self._spin_mx = Gtk.SpinButton.new_with_range(0, 500, 1)
        self._spin_mx.set_value(CFG.margin_x)
        gp.attach(self._spin_mx, 1, 3, 1, 1)
        gp.attach(Gtk.Label(label="px"), 2, 3, 1, 1)

        gp.attach(gutter(field_label(tr('fld_margin_y'))), 3, 3, 1, 1)
        self._spin_my = Gtk.SpinButton.new_with_range(0, 500, 1)
        self._spin_my.set_value(CFG.margin_y)
        gp.attach(self._spin_my, 4, 3, 1, 1)
        gp.attach(Gtk.Label(label="px"), 5, 3, 1, 1)

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

        # ── Onglet Affichage ─────────────────────────────────────────────────
        gd = make_page('tab_display')

        # Toggles groupés sur deux colonnes.
        self._chk_auto_width = Gtk.CheckButton(label=tr('fld_auto_width'))
        self._chk_auto_width.set_active(CFG.auto_width)
        gd.attach(self._chk_auto_width, 0, 0, 3, 1)

        self._chk_show_topic = Gtk.CheckButton(label=tr('fld_show_topic'))
        self._chk_show_topic.set_active(getattr(CFG, 'show_topic', True))
        gd.attach(gutter(self._chk_show_topic), 3, 0, 3, 1)

        self._chk_hide_daemons = Gtk.CheckButton(label=tr('fld_hide_daemons'))
        self._chk_hide_daemons.set_active(getattr(CFG, 'hide_daemons', False))
        gd.attach(self._chk_hide_daemons, 0, 1, 3, 1)

        self._chk_show_agents = Gtk.CheckButton(label=tr('fld_show_agents'))
        self._chk_show_agents.set_active(getattr(CFG, 'show_agents', True))
        gd.attach(gutter(self._chk_show_agents), 3, 1, 3, 1)

        gd.attach(Gtk.Separator(), 0, 2, 6, 1)

        self._lbl_width = field_label(tr('fld_width'))
        gd.attach(self._lbl_width, 0, 3, 1, 1)
        self._spin_width = Gtk.SpinButton.new_with_range(200, 800, 10)
        self._spin_width.set_value(CFG.width)
        gd.attach(self._spin_width, 1, 3, 1, 1)
        gd.attach(Gtk.Label(label="px"), 2, 3, 1, 1)

        gd.attach(gutter(field_label(tr('fld_columns'))), 3, 3, 1, 1)
        self._spin_columns = Gtk.SpinButton.new_with_range(1, 6, 1)
        self._spin_columns.set_value(getattr(CFG, 'columns', 1))
        gd.attach(self._spin_columns, 4, 3, 1, 1)

        # « Hauteur max » : icône info (tooltip au survol) plutôt qu'un « (0 = écran) »
        # accolé — l'explication complète tient dans le tooltip.
        gd.attach(info_label(tr('fld_max_height'), tr('help_max_height')), 0, 4, 1, 1)
        # 0 = pas de limite propre (l'écran borne) ; pas-50 px ; plafond large.
        self._spin_max_height = Gtk.SpinButton.new_with_range(0, 4000, 50)
        self._spin_max_height.set_value(getattr(CFG, 'max_height', 0))
        gd.attach(self._spin_max_height, 1, 4, 1, 1)
        gd.attach(Gtk.Label(label="px"), 2, 4, 1, 1)

        gd.attach(field_label(tr('fld_bg_alpha')), 0, 5, 1, 1)
        # 20 floor mirrors _set_effective_alpha — lower values would silently snap
        self._spin_bg_alpha = Gtk.SpinButton.new_with_range(20, 100, 1)
        self._spin_bg_alpha.set_value(round(parent._effective_alpha() * 100))
        gd.attach(self._spin_bg_alpha, 1, 5, 1, 1)
        gd.attach(Gtk.Label(label="%"), 2, 5, 1, 1)
        btn_bg_default = Gtk.Button(label=f"{tr('btn_default')} ({BG_ALPHA_DEFAULT})")
        # set_value fires value-changed → live preview updates immediately
        btn_bg_default.connect(
            'clicked', lambda _b: self._spin_bg_alpha.set_value(BG_ALPHA_DEFAULT))
        gd.attach(gutter(btn_bg_default), 3, 5, 2, 1)

        gd.attach(Gtk.Separator(), 0, 6, 6, 1)

        gd.attach(field_label(tr('fld_sort')), 0, 7, 1, 1)
        self._combo_sort = Gtk.ComboBoxText()
        self._combo_sort.append('default', tr('sort_default'))
        self._combo_sort.append('idle',    tr('sort_idle'))
        self._combo_sort.set_active_id(getattr(CFG, 'sort_mode', 'default'))
        gd.attach(self._combo_sort, 1, 7, 4, 1)

        gd.attach(field_label(tr('fld_idle_format')), 0, 8, 1, 1)
        self._combo_idle = Gtk.ComboBoxText()
        self._combo_idle.append('none',    tr('idle_none'))
        self._combo_idle.append('loose',   tr('idle_loose'))
        self._combo_idle.append('precise', tr('idle_precise'))
        self._combo_idle.set_active_id(getattr(CFG, 'idle_format', 'none'))
        gd.attach(self._combo_idle, 1, 8, 4, 1)

        # ── Onglet Distants (LECTURE SEULE) ──────────────────────────────────
        # Les remotes sont lus au démarrage : les modifier ici voudrait dire
        # démarrer/arrêter des threads depuis l'UI, plus gros que la fonction
        # qu'il servirait. On les AFFICHE (nom, URL rédigée, santé), on n'y
        # touche pas.
        # La liste vient de CFG (NON filtrée sur `enabled`) et pas du poller : ce
        # dernier ne connaît que les remotes actifs, si bien qu'un `[remote:*]`
        # éteint était totalement invisible ici — rien ne disait qu'il avait été
        # analysé. Les éteints sont affichés grisés, avec « désactivée » en santé.
        gr = make_page('tab_remotes')
        remotes = list(getattr(CFG, 'remotes', None) or [])
        if not remotes:
            lbl_none = Gtk.Label(label=tr('rm_none_configured'))
            lbl_none.set_halign(Gtk.Align.START)
            gr.attach(lbl_none, 0, 0, 6, 1)
        else:
            for col, key in enumerate(('rm_col_name', 'rm_col_url', 'rm_col_health')):
                head = Gtk.Label()
                head.set_markup(f'<b>{GLib.markup_escape_text(tr(key))}</b>')
                head.set_halign(Gtk.Align.START)
                gr.attach(head, col * 2, 0, 2, 1)
            # URL RÉDIGÉE et santé calculées par remotes_panel_rows (partagé avec
            # la TUI) : le dialogue ne fait plus que poser des Gtk.Label.
            panel = remotes_panel_rows(remotes, remote_stat, remote_poll_s, time.monotonic())
            for i, (r, cells) in enumerate(zip(remotes, panel, strict=True), start=1):
                for col, text in enumerate(cells):
                    lbl = Gtk.Label(label=text)
                    lbl.set_halign(Gtk.Align.START)
                    lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
                    lbl.set_max_width_chars(34)
                    lbl.set_tooltip_text(text)
                    lbl.set_sensitive(r.get('enabled', True))
                    gr.attach(lbl, col * 2, i, 2, 1)
        hint = Gtk.Label(label=tr('rm_readonly_hint'))
        hint.set_halign(Gtk.Align.START)
        hint.set_line_wrap(True)
        hint.set_max_width_chars(64)
        hint.set_margin_top(12)
        gr.attach(hint, 0, len(remotes) + 2, 6, 1)

        # Live preview — connecté après les set_active_id/set_value initiaux
        for widget, signal_name in [
            (self._lang_combo,    'changed'),
            (self._screen_combo,  'changed'),
            (self._corner_combo,  'changed'),
            (self._radio_corner,  'toggled'),
            (self._spin_mx,       'value-changed'),
            (self._spin_my,       'value-changed'),
            (self._spin_width,    'value-changed'),
            (self._chk_auto_width,'toggled'),
            (self._chk_show_topic,'toggled'),
            (self._chk_show_agents,  'toggled'),
            (self._chk_hide_daemons, 'toggled'),
            (self._spin_bg_alpha, 'value-changed'),
            (self._spin_columns,    'value-changed'),
            (self._spin_max_height, 'value-changed'),
            (self._combo_sort,      'changed'),
            (self._combo_idle,      'changed'),
        ]:
            widget.connect(signal_name, self._on_preview_change)

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
            'show_agents': self._chk_show_agents.get_active(),
            'hide_daemons': self._chk_hide_daemons.get_active(),
            'refresh_ms': int(self._spin_refresh.get_value()),
            'snooze_sec': int(self._spin_snooze.get_value()),
            'bg_alpha':   int(self._spin_bg_alpha.get_value()),
            'shortcut_enable': self._chk_shortcut.get_active(),
            'hotkey':     self._entry_hotkey.get_text().strip() or '<Ctrl><Alt>q',
        }


def _never_dies(keep: bool):
    """Garde des callbacks PÉRIODIQUES branchés sur GLib.timeout_add.

    Portée réelle, dite franchement plutôt que promise trop large : les cinq
    sources récurrentes, celles dont la perte est définitive et silencieuse. Les
    callbacks ONE-SHOT (idle_add, timeout_add non réarmé) ne sont pas décorés —
    une exception y coûte une action manquée, pas une source morte pour la durée
    du processus. Le test de garde énumère les cinq ; s'il en manquait un, c'est
    lui qui le dirait, pas ce commentaire.

    GLib RETIRE DÉFINITIVEMENT une source dont le callback lève (mesuré sur une
    vraie boucle GTK3 : la fonction tourne UNE fois, puis plus jamais). Une seule
    ligne fautive — par exemple `row.session['waiting']` sur une ligne dérivée
    d'un remote au payload inattendu — et l'animation, la vérification de
    version ou l'enregistrement de position s'arrêtent pour toute la session,
    avec la trace sur un stderr qu'un lancement depuis le bureau n'a pas.

    UNE seule implémentation appliquée aux cinq sites, plutôt qu'un try/except
    recopié à la main à chacun : c'est justement la copie manquante qui a fait
    le trou — seul _refresh était gardé, ses quatre voisins ne l'étaient pas.

    `keep` porte la convention de retour de la source EN CAS D'ERREUR, et il n'y
    a pas de défaut sûr : True ré-arme une source périodique (ce qu'on veut),
    mais ré-armerait aussi un one-shot, soit une boucle sans fin. D'où le
    paramètre obligatoire, tranché site par site.
    """
    def decorate(fn):
        @functools.wraps(fn)
        def guarded(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception:
                traceback.print_exc()
                return keep
        return guarded
    return decorate


# ── Main window ───────────────────────────────────────────────────────────────

class ClaudeWatcher(Gtk.Window):

    def __init__(self, cfg: argparse.Namespace):
        # Layer shell nécessite TOPLEVEL ; POPUP pour X11 (no-decoration natif).
        super().__init__(type=Gtk.WindowType.TOPLEVEL if IS_WAYLAND else Gtk.WindowType.POPUP)
        self.sessions: list[dict] = []
        self._session_rows: list[Any] = []   # SessionRow ordonnées (nav clavier + anim)
        # dernière taille (w, h) appliquée — anti-churn resize
        self._last_size: tuple[int, int] | None = None
        # structure des lignes (colonnes, clés ordonnées) au dernier rebuild
        self._last_rows_sig: tuple[int, tuple[str, ...]] | None = None
        self._anim_tick    = 0
        self._snooze_until = 0
        self._snooze_timer = None
        self._kb_index        = -1
        self._kb_bind_retries = 0

        # Machines distantes. Aucun remote déclaré → aucun poller, aucun thread,
        # aucun HTTP : comportement d'avant, à l'octet près.
        # enabled_remotes ici : le poller ne refiltre plus (le filtre vit à un
        # seul endroit), donc lui passer la liste complète ferait interroger un
        # remote explicitement désactivé. La liste COMPLÈTE reste sur CFG pour
        # l'onglet de paramètres, qui doit montrer les éteints.
        remotes = enabled_remotes(getattr(cfg, 'remotes', None) or [])
        self._remote_stat: dict[str, dict] = {}
        # Protège _remote_pending : la marque est posée depuis les threads de
        # poll (cf. _notify_remote_update), un check-then-set nu y est une course.
        self._lock = threading.Lock()
        self._remote_pending = False
        # Drapeau MONOTONE de destruction (posé par le signal `destroy`, jamais
        # remis à False). Gtk.Widget.in_destruction() ne répond True que PENDANT
        # dispose (mesuré : True au cours du destroy, False après) : une source
        # idle planifiée avant la destruction et servie après passait donc le
        # garde et touchait des widgets morts. Ce drapeau, lui, ne rajeunit pas.
        self._destroyed = False
        self._poller = RemotePoller(
            remotes, getattr(cfg, 'remote_poll_ms', REMOTE_POLL_MS),
            notify=self._notify_remote_update) if remotes else None

        self.screen   = cfg.screen
        self.corner   = cfg.corner
        self.margin_x = cfg.margin_x
        self.margin_y = cfg.margin_y
        self._dragging   = False
        self._drag_off   = (0, 0)
        self._save_timer = 0
        self._alpha      = 1.0
        self._counts_first = True
        self._bg_alpha   = cfg.bg_alpha / 100.0

        # Position libre (--x/--y ou drag) : X11 seulement.
        # Sur Wayland, la position est gérée par gtk-layer-shell (anchor + margin).
        if not IS_WAYLAND and cfg.x is not None and cfg.y is not None:
            g = self._get_monitor_geom()
            self._user_pos: tuple[int, int] | None = (g.x + cfg.x, g.y + cfg.y)
            self._save_position()
        elif not IS_WAYLAND and cfg.mode == 'free':
            # Mode libre explicite — charger la position sauvegardée.
            self._user_pos = self._load_position()
        else:
            # Mode ancré (corner) ou Wayland — ignorer position.json.
            self._user_pos = None

        self._tray: Any = None
        self._tray_menu: Any = None
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

        # Zone d'état des remotes (pied de fenêtre) : TOUS les remotes configurés
        # y figurent, même sans session. Un remote qui n'a JAMAIS répondu (URL
        # fausse, token invalide, hôte éteint au démarrage) n'a aucune ligne à
        # marquer périmée — sans cette zone il serait purement invisible.
        self.lbl_remotes = Gtk.Label()
        self.lbl_remotes.set_halign(Gtk.Align.START)
        # Enroulé, PAS ellipsé : la zone doit montrer TOUS les remotes (c'est sa
        # raison d'être) — un ellipsage cacherait justement le remote muet qu'on
        # veut voir. Elle passe donc à la ligne au lieu de couper.
        self.lbl_remotes.set_line_wrap(True)
        self.lbl_remotes.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        # Borne la largeur NATURELLE : en mode auto_width, une liste de remotes
        # bavarde élargirait la fenêtre hors écran (même piège que lbl_counts).
        self.lbl_remotes.set_max_width_chars(40)
        self.lbl_remotes.set_no_show_all(True)   # rien à afficher sans remote
        footer.pack_start(self.lbl_remotes, True, True, 0)

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
        if self._poller:
            self._poller.start()
        # Les threads sont daemon (ils ne retiendraient pas le process), mais on
        # les arrête proprement avec la fenêtre : sans ça un poll en cours lui
        # survit jusqu'à son timeout.
        self.connect('destroy', self._stop_poller)
        self.connect('destroy', self._mark_destroyed)
        self._refresh()
        self._refresh_timer_id = GLib.timeout_add(cfg.refresh_ms, self._refresh)
        GLib.timeout_add(600, self._tick_anim)
        self._check_latest_version_async()
        GLib.timeout_add_seconds(6 * 3600, self._recheck_version_tick)

    # ── Snooze ────────────────────────────────────────────────────────────────

    def _is_snoozed(self) -> bool:
        return time.time() < self._snooze_until

    @_never_dies(keep=False)
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

    @_never_dies(keep=True)
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
        if not rolled:
            # show_all() ne réaffiche PAS lbl_remotes (no_show_all) : sans ça la
            # zone d'état reste vide jusqu'au prochain tick après un déroulement.
            self._update_remotes_bar()
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
        snooze_label = tr('snooze_wake') if self._is_snoozed() else f"{tr('snooze_hide')} {CFG.snooze_sec // 60}m"  # noqa: E501 (ligne préexistante, cf. pyproject.toml)
        self._mi_snooze = mi_snooze = Gtk.MenuItem(label=snooze_label)
        mi_snooze.connect('activate', lambda _m: self._toggle_snooze())
        self._mi_about = mi_about = Gtk.MenuItem(label=tr('about'))
        mi_about.connect('activate', lambda _m: self._show_about())
        self._mi_quit  = mi_quit  = Gtk.MenuItem(label=tr('quit'))
        mi_quit.connect('activate', lambda _m: Gtk.main_quit())
        self._mi_settings = mi_settings = Gtk.MenuItem(label=tr('settings_menu'))
        mi_settings.connect('activate', lambda _m: self._open_settings())
        for mi in (mi_show, mi_snooze, Gtk.SeparatorMenuItem(), mi_settings, Gtk.SeparatorMenuItem(), mi_about, Gtk.SeparatorMenuItem(), mi_quit):  # noqa: E501 (ligne préexistante, cf. pyproject.toml)
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
        dlg = SettingsDialog(
            self,
            remote_stat=self._poller.snapshot() if self._poller else {},
            remote_poll_s=self._poller.poll_s if self._poller else REMOTE_POLL_MS / 1000)
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
        CFG.show_agents = values['show_agents']    # relu par scan_sessions au _refresh()
        CFG.hide_daemons = values['hide_daemons']  # relu par scan_sessions au _refresh()
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
        """Écrit config.ini (via save_config, qui force 0600) et applique tout."""
        save_config({
            'general': {
                'lang':   values['lang'],
                'hotkey': values['hotkey'],
            },
            'features': {
                'shortcut_enable': 'true' if values['shortcut_enable'] else 'false',
                'show_topic':      'true' if values['show_topic'] else 'false',
                'show_agents':     'true' if values['show_agents'] else 'false',
                'hide_daemons':    'true' if values['hide_daemons'] else 'false',
            },
            'display': {
                'mode':        'free' if values['free'] else 'corner',
                'screen':      str(values['screen']),
                'corner':      values['corner'],
                'margin_x':    str(values['margin_x']),
                'margin_y':    str(values['margin_y']),
                'width':       str(values['width']),
                'auto_width':  'true' if values['auto_width'] else 'false',
                'columns':     str(values['columns']),
                'max_height':  str(values['max_height']),
                'sort_mode':   values['sort_mode'],
                'idle_format': values['idle_format'],
                'refresh_ms':  str(values['refresh_ms']),
                'snooze_sec':  str(values['snooze_sec']),
                'bg_alpha':    str(values['bg_alpha']),
            },
        })

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

    def _update_tray(self, waiting: int, working: int, bg_shell: int, total: int):
        if not self._tray:
            return
        # La couleur classe l'URGENCE : un shell de fond n'en est pas une, la
        # session est disponible. Il est compté dans l'infobulle, pas ici.
        if waiting:   color = COLOR_WAITING
        elif working: color = COLOR_WORKING
        elif total:   color = COLOR_IDLE
        else:         color = TEXT_DIM
        tooltip = (
            f"{waiting} {tr('waiting')} · {working} {tr('working')} · "
            f"{bg_shell} {BG_SHELL_GLYPH} · {total} total"
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

        if self._rolled and CFG.auto_width:
            # Pilule enroulée (largeur auto) : pas de contrainte, rétrécit au minimum.
            self.set_geometry_hints(None, None, Gdk.WindowHints(0))
            self._last_size = None
            self.set_size_request(-1, -1)
            self.resize(1, 1)
            return
        # En largeur fixe, l'en-tête enroulé conserve la largeur configurée (il
        # retombe dans le chemin « largeur fixe » plus bas) pour rester aligné
        # avec la fenêtre déployée ; seules les lignes masquées libèrent la hauteur.
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

    @_never_dies(keep=False)
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
        """Surveille le sessions/ de chaque CLAUDE_CONFIG_DIR exposé par le scan.

        Les lignes DISTANTES sont exclues (cf. local_config_dirs).
        """
        for cfg in local_config_dirs(self.sessions):
            self._watch_status_dir(Path(cfg) / 'sessions')

    def _on_status_changed(self, _monitor, gfile, _other, event_type):
        if event_type in (Gio.FileMonitorEvent.CHANGED, Gio.FileMonitorEvent.CREATED):
            if gfile.get_basename().endswith('.json'):
                self._refresh()

    def _stop_poller(self, *_):
        if self._poller:
            self._poller.stop()

    def _mark_destroyed(self, *_):
        """Pose le drapeau monotone lu par _remote_update_tick (cf. __init__)."""
        self._destroyed = True

    def _notify_remote_update(self):
        """Appelé DEPUIS un thread de poll : ne touche à aucun widget, replanifie
        juste un rafraîchissement dans la boucle GTK (idle_add est la seule
        primitive GLib sûre depuis un autre thread).

        Coalescé : trois remotes qui répondent en même temps ne déclenchent qu'un
        seul scan local. La marque est posée SOUS LE VERROU : en lecture-puis-
        écriture non protégée, deux threads pouvaient la lire False en même temps
        et planifier deux idle — la coalescence promise par cette docstring
        n'était alors qu'un vœu.
        """
        with self._lock:
            if self._remote_pending:
                return
            self._remote_pending = True
        GLib.idle_add(self._remote_update_tick)

    def _remote_update_tick(self):
        with self._lock:          # même verrou que la pose : une seule vérité
            self._remote_pending = False
        # La fenêtre peut avoir été détruite entre le GLib.idle_add (fait depuis
        # un thread de poll) et l'exécution de cette source : _notify_remote_update
        # relit `_stop`, mais rien n'empêche l'arrêt de tomber juste après. Un
        # _refresh() sur une fenêtre détruite touche des widgets morts.
        #
        # PAS in_destruction() : ce prédicat n'est vrai que PENDANT dispose
        # (mesuré — True au cours du destroy, False après), donc il laissait
        # passer précisément le cas visé, une idle planifiée avant la destruction
        # et servie après. Le drapeau posé par le signal `destroy` est monotone.
        if self._destroyed:
            return False
        self._refresh()
        return False   # source idle à usage unique

    def _empty_state_text(self) -> str:
        return empty_state_text(self._poller.remotes if self._poller else [],
                                self._remote_stat)

    def _update_remotes_bar(self):
        """Zone d'état du pied de fenêtre. Masquée tant qu'aucun remote n'est
        configuré (widget invisible, zéro pixel pris)."""
        self.lbl_remotes.set_visible(bool(self._poller))
        if not self._poller:
            return
        remotes = self._poller.remotes
        bar = remotes_bar_text(remotes, self._remote_stat, self._poller.poll_s,
                               time.monotonic())
        self.lbl_remotes.set_markup(
            f'<span foreground="{COLOR_REMOTE}" font="Monospace 8">'
            f'{GLib.markup_escape_text(bar)}</span>'
        )
        self.lbl_remotes.set_tooltip_text(remotes_bar_tooltip(remotes, self._remote_stat))

    def _rstate_for(self, s: dict) -> dict | None:
        """État du remote d'une ligne (cf. remote_rstate). L'horloge est MONOTONE :
        la péremption ne doit pas pouvoir rajeunir sur un pas NTP arrière."""
        if not self._poller:
            return None
        return remote_rstate(s, self._remote_stat, self._poller.poll_s, time.monotonic())

    @_never_dies(keep=True)
    def _refresh(self):
        """Deux garde-fous, deux portées — ils ne font pas le même travail.

        Le décorateur empêche la SOURCE de mourir (cf. _never_dies). Le try
        interne, lui, borne la casse d'un tick : une seule ligne fautive ne doit
        coûter que le contenu de ce tour, on préfère une image figée à un widget
        mort pour la session.

        Lecture du cache du poller uniquement : AUCUN HTTP ici (on tourne dans la
        boucle GTK, un hôte lent figerait la fenêtre).
        """
        try:
            remote_rows = self._poller.sessions() if self._poller else None
            self._remote_stat = self._poller.snapshot() if self._poller else {}
            self.sessions = scan_sessions(remote_rows)
            self._sync_status_monitors()
            self._rebuild_sessions()
        except Exception:
            traceback.print_exc()
        # HORS du try : le repositionnement ne dépend pas des lignes. Dedans, un
        # défaut de ligne passager le sautait aussi et laissait la fenêtre mal
        # dimensionnée jusqu'au prochain tick propre — deux pannes pour une.
        GLib.idle_add(self._reposition)
        return True

    def _rebuild_sessions(self):
        waiting  = sum(1 for s in self.sessions if s['waiting'])
        working  = sum(1 for s in self.sessions if s['working'])
        bg_shell = sum(1 for s in self.sessions if s['bg_shell'])
        total    = len(self.sessions)

        self._update_tray(waiting, working, bg_shell, total)

        if not self.sessions:
            self.lbl_counts.set_markup(
                f'<span font="Monospace 8" foreground="{TEXT_DIM}">{tr("no_session")}</span>')
        else:
            def plain(level):
                return counts_sep(level).join(
                    t for t, _ in counts_segments(waiting, working, bg_shell, total, level))

            # Budget = largeur de l'EN-TÊTE moins le naturel des AUTRES enfants.
            # Surtout PAS la largeur allouée au label : avec ellipsize=END son
            # minimum est quasi nul et le titre (expand=True) prend le reste, si
            # bien qu'elle mesure le texte déjà posé, pas la place disponible —
            # une fois dégradé en '4/8' le niveau ne remontait jamais (mesuré :
            # budget figé à 1px sur tous les ticks, y compris après élargissement).
            # Mesurer plutôt que réagir à l'ellipse évite une boucle de relayout.
            # Le PREMIER passage ne mesure rien de fiable : la fenêtre n'a pas
            # encore la largeur de ses lignes (elles arrivent avec ce tour-ci), on
            # lirait un en-tête étroit et on afficherait des chiffres nus dès
            # l'ouverture. Budget négatif au premier tick → niveau le plus riche,
            # la vraie mesure a lieu au tick suivant.
            budget = -1 if self._counts_first else (
                self._header.get_allocated_width() - sum(
                    c.get_preferred_width()[1] for c in self._header.get_children()
                    if c is not self.lbl_counts))
            self._counts_first = False
            level = fit_level(
                lambda lvl: self.lbl_counts.create_pango_layout(plain(lvl)).get_pixel_size()[0],
                budget)
            parts = [f'<span foreground="{c}">{GLib.markup_escape_text(t)}</span>'
                     for t, c in counts_segments(waiting, working, bg_shell, total, level)]
            self.lbl_counts.set_markup(
                f'<span font="Monospace 8">{counts_sep(level).join(parts)}</span>')
        self._update_remotes_bar()

        cols = self._effective_cols()
        # Signature de structure : si colonnes + liste ordonnée des pids sont
        # inchangées, on met à jour les lignes EN PLACE (update_session) au lieu de
        # détruire/recréer. Sans ça, chaque refresh (toutes les 2 s ou à chaque
        # event inotify) recrée toutes les SessionRow → le hover sous le curseur
        # clignote et l'anim repart. Le destroy/recreate (qui libère les GdkWindow,
        # cf. fuite RSS ~20 Mo/min avec un simple remove) n'a lieu que sur un vrai
        # changement de structure : ajout/retrait/réordre de session, ou colonnes.
        # session_key (et pas le pid nu) : un pid 1234 local et un pid 1234
        # distant sont deux process différents, la signature les confondrait et
        # une ligne serait mise à jour avec les données de l'autre.
        sig = (cols, tuple(session_key(s) for s in self.sessions))
        if (sig == self._last_rows_sig
                and self.sessions
                and len(self._session_rows) == len(self.sessions)):
            for row, s in zip(self._session_rows, self.sessions, strict=True):
                row.update_session(s, self._rstate_for(s))
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
                    f'  {GLib.markup_escape_text(self._empty_state_text())}</span>'
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
                    row = SessionRow(s, self._rstate_for(s))
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

    @_never_dies(keep=True)
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

    Volontairement LOCAL : ces valeurs intermédiaires n'existent que dans /proc
    et les JSONL de CETTE machine, une ligne distante n'en expose aucune. C'est
    pourquoi main() REFUSE --dump avec --no-local ou avec un remote ACTIF, quelle
    que soit sa provenance — drapeau --remote ou section [remote:<nom>] du
    config.ini — au lieu de les ignorer en silence, ce qu'il faisait : un
    scan_proc() direct rend ces déclarations sans effet.
    """
    procs, subagents = scan_proc()
    if not procs:
        print('no claude session found')
        return
    for p in procs:
        pid = p['pid']
        cwd = get_cwd(pid)
        env = get_env(pid)
        config_dir = resolve_config_dir(env)
        if p.get('is_daemon'):
            print(f"pid {pid}  {project_label(cwd)}  ({format_elapsed(p['elapsed'])})")
            print(f"  cwd          {cwd or '?'}")
            print(f"  config_dir   {display_config_dir(config_dir) or '(default)'}")
            print("  => DAEMON    (marqué (D), pas une session)")
            print()
            continue
        reg = get_session_registry(pid, p['starttime'], config_dir)
        reg_status = reg.get('status') if reg else None
        session_id = reg.get('sessionId') if reg else None
        eff_cwd = cwd or (reg.get('cwd') if reg else None)
        jsonl_state, ctx, tool, _, _ = get_session_info_from_jsonl(eff_cwd, config_dir, session_id)
        # Source de vérité : même appel que l'app, pour que `state` et `topic`
        # collent à l'affichage (topic = /rename éventuel, sinon titre IA).
        state, _, _, topic, last_activity, _, bg_shell = get_session_state(
            pid, cwd, p['starttime'], config_dir)
        # Worktree : même logique que scan_sessions (label = projet parent).
        wt_root, wt_name = worktree_of(eff_cwd, last_activity is not None)
        claude_wt = split_worktree(eff_cwd)[1]
        reg_mapped = _STATUS_MAP.get(reg_status or '', 'idle') if reg else '(no registry)'
        print(f"pid {pid}  {project_label(wt_root)}  ({format_elapsed(p['elapsed'])})")  # noqa: E501 (ligne préexistante, cf. pyproject.toml)
        print(f"  cwd          {eff_cwd or '?'}")
        print(f"  worktree     {wt_name or ('(detected, unconfirmed)' if claude_wt else '(none)')}")  # noqa: E501 (ligne préexistante, cf. pyproject.toml)
        print(f"  config_dir   {display_config_dir(config_dir) or '(default)'}")
        print(f"  session_id   {session_id or '(none)'}")
        print(f"  reg.status   {reg_status!r} -> {reg_mapped}")
        print(f"  jsonl_state  {jsonl_state!r}")
        print(f"  => state     {state}{'  (reconciled from registry)' if reg and reg_mapped != state else ''}")  # noqa: E501 (ligne préexistante, cf. pyproject.toml)
        print(f"  context_pct  {ctx}")
        print(f"  tool         {tool}")
        print(f"  topic        {topic}")
        ag = subagents.get(session_id, []) if session_id else []
        ag_txt = f"{len(ag)}: " + ', '.join(a['name'] for a in ag) if ag else '(none)'
        print(f"  agents       {ag_txt}")
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
    conf = load_config()
    CFG = parse_args(conf)
    # Remotes : sections du config.ini + drapeaux --remote (les drapeaux gagnent,
    # rien n'est persisté). Liste vide = comportement d'avant, à l'octet près.
    CFG.remote_poll_ms = conf['remote_poll_ms']
    try:
        CFG.remotes = resolve_remotes(conf['remote_sections'], CFG.remote)
    except ValueError as e:
        # `enabled` ininterprétable, ou deux noms de remote sur la même variable
        # d'environnement de token : on refuse de démarrer PLUTÔT que d'envoyer
        # le token quelque part par défaut. Message, pas traceback.
        raise SystemExit(f"claude-watcher: {e}") from None
    # --dump est un diagnostic PUREMENT LOCAL : il montre les valeurs
    # intermédiaires (registre vs JSONL) que seul /proc fournit, et une machine
    # distante n'en expose aucune. On refuse la combinaison plutôt que de laisser
    # croire qu'elle a été prise en compte.
    #
    # ICI et pas dans parse_args : c'est le seul point où les DEUX sources de
    # remotes sont visibles. parse_args ne connaît que le drapeau --remote, donc
    # un remote déclaré en section [remote:*] du config.ini passait le contrôle
    # et dump_round() l'ignorait sans un mot. La liste est filtrée sur `enabled`
    # — un remote explicitement désactivé n'est pas interrogé, donc ne pas le
    # rendre n'est pas un mensonge.
    if CFG.dump and (CFG.no_local or enabled_remotes(CFG.remotes)):
        raise SystemExit('claude-watcher: --dump est un diagnostic local : '
                         'incompatible avec --no-local et avec toute machine distante '
                         '(--remote, ou une section [remote:<nom>] du config.ini).')
    if CFG.dump:
        dump_round()
        return
    if CFG.list_screens:
        list_screens()
        return
    app = ClaudeWatcher(CFG)
    app.show_all()
    # Ouvrir les paramètres une fois la fenêtre mappée et la boucle démarrée :
    # _open_settings/_preview_settings s'appuient sur une fenêtre réalisée.
    # Retour False explicite : source idle à usage unique (ne pas ré-armer la
    # nested main loop de _open_settings quel que soit son futur retour).
    if CFG.settings:
        GLib.idle_add(lambda: (app._open_settings(), False)[1])
    Gtk.main()


if __name__ == '__main__':
    main()
