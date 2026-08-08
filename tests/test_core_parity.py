"""Le cœur « sessions distantes » ne doit pas dériver entre le widget et la TUI.

Les deux clients portent le MÊME bloc (constantes, adaptateur, fetch, poller,
aides de présentation) parce qu'ils consomment la même API et doivent en tirer
le même comportement. Jusqu'ici, seul un commentaire l'affirmait — et un
commentaire ne retient personne : les deux copies avaient déjà divergé (santé,
horloges, plafonds) sans que rien ne le signale.

Ce test compare symbole par symbole, sur l'AST :

* docstrings et commentaires sont retirés — les deux fichiers ont le droit de
  s'expliquer dans leurs propres termes (« labels Pango » ici, « cellules
  Textual » là-bas) ;
* les jetons qui NOMMENT le client sont normalisés (`gtk`/`tui`, `GTK`/`Textual`)
  — l'en-tête User-Agent doit différer, c'est même souhaitable ;
* tout le reste doit être identique, à l'octet d'AST près.

Il couvre DEUX socles : le cœur « sessions distantes » et le socle de DÉTECTION
(17 symboles + leurs constantes, cf. `SHARED_DETECTION_*`), que les listes
gardées ne regardaient pas du tout jusqu'ici.

Le fichier de la TUI est cherché à côté du dépôt (`../tui/`), à l'emplacement de
checkout de la CI (`_tui/`), ou via `CW_TUI_SCRIPT`. Absent : le test est SKIPPÉ
en local (on ne force personne à cloner le voisin) mais ÉCHOUE sous `CI`, sinon
le garde-fou ne tournerait jamais là où il compte.
"""

import ast
import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Symboles du cœur partagé. Un ajout ici doit être fait dans les DEUX dépôts —
# c'est précisément le point.
SHARED_CONSTANTS = [
    'REMOTE_POLL_MS', 'REMOTE_POLL_MIN_MS', 'REMOTE_TIMEOUT_S', 'REMOTE_READ_BUDGET_S',
    'REMOTE_READ_CHUNK', 'REMOTE_MAX_BYTES', 'REMOTE_MAX_ROWS', 'REMOTE_MAX_ELAPSED_S',
    'REMOTE_STALE_X', 'REMOTE_LABEL_MAX', 'REMOTE_BACKOFF_MAX_S', 'REMOTE_AUTH_RETRY_S',
    'REMOTE_SCHEMES', 'COLOR_REMOTE', '_BOOL_TRUE', '_BOOL_FALSE', '_ANSI_RE', '_CTRL_RE',
]

SHARED_SYMBOLS = [
    'session_key', 'clean_remote_str', '_as_int', '_as_float', 'mask_query', 'redact_secrets',
    'split_remote_url', 'parse_remote_flag', 'remote_token_env', 'remote_enabled',
    'enabled_remotes',
    'resolve_remotes', 'adapt_remote_agents', 'remote_last_activity', 'adapt_remote_row',
    'adapt_remote_payload', '_NoRedirect', '_REMOTE_OPENER', 'remote_endpoint', 'read_capped',
    'fetch_remote',
    'remote_health', 'remote_health_text', 'remote_status_text', 'local_config_dirs',
    'remotes_bar_text', 'remotes_bar_tooltip', 'empty_state_text', 'remote_stale_text',
    'session_tooltip', 'remote_rstate', 'remotes_panel_rows', 'RemotePoller',
    # APPELÉS par des aides gardées ci-dessus, donc capables d'en faire diverger
    # la sortie sans que rien ne bouge dans leur corps :
    #   format_elapsed — deux aides gardées le rendent ;
    #   tr             — SIX aides gardées le rendent, et son repli de langue
    #                    avait DÉJÀ dérivé (STRINGS['fr'] côté GTK, STRINGS['en']
    #                    côté TUI, désormais aligné). Un garde-fou
    #                    qui certifie une fonction identique à l'octet pendant
    #                    que son texte affiché diverge ne garde rien.
    # (Le contenu de STRINGS lui-même est comparé par le test dédié plus bas :
    #  les deux clients n'ont pas les mêmes écrans, donc pas les mêmes clés.)
    'format_elapsed', 'tr',
]

# Socle de DÉTECTION — porté à l'identique lui aussi, et jusqu'ici couvert par
# RIEN : les deux listes ci-dessus ne contiennent que le cœur « distant ».
# Mesuré : faire dériver `_WORKTREE_MARKER` entre les deux copies laissait ce
# fichier à « 55 passed » sous CI=1. Ces symboles décident du projet attribué à
# une session, de son transcript, de son état et de son % de contexte — une
# dérive ici change en silence ce que l'utilisateur voit.
#
# `scan_proc` et `scan_sessions` sont INCLUS, à dessein : ils divergent par
# conception entre le SERVEUR et les clients (formes de retour différentes,
# `proc_root` injectable côté webui), ce qui est exactement pourquoi le garde de
# parité de webui les laisse de côté — mais les deux CLIENTS en portent la même
# copie, et rien ne justifie qu'elle dérive entre eux. Aucune divergence
# tui↔gtk n'est donc déclarée sur ce bloc : les 17 symboles sont comparés.
SHARED_DETECTION_CONSTANTS = [
    '_CLK_TCK', '_SESSIONS_DIR', 'CLAUDE_PROJECTS_DIR', '_STATUS_MAP', '_TURN_END_SUBTYPES',
    '_WORKTREE_MARKER',
    'DEFAULT_CONTEXT_WINDOW', 'CONTEXT_200K', '_JSONL_TAIL_BYTES',
]

SHARED_DETECTION_SYMBOLS = [
    '_argv_value', 'scan_proc', 'get_cwd', 'get_env', 'split_worktree', 'cwd_to_project_dir',
    'context_window_for', '_read_tail_lines', '_read_topic', '_parse_session_lines',
    '_find_transcript',
    'get_session_info_from_jsonl', 'get_session_registry', 'get_session_state',
    'project_label', 'resolve_config_dir', 'display_config_dir', 'scan_sessions',
]

# Clés de STRINGS qui alimentent le cœur partagé (santé, péremption, infobulles
# des lignes distantes). Le RESTE du dictionnaire a le droit de différer — les
# deux clients n'ont pas les mêmes écrans.
SHARED_STRING_PREFIXES = ('rm_', 'tip_')

# DIVERGENCES ASSUMÉES, nommées une par une plutôt que tolérées en masse :
#
# session_path_cell (TUI seulement) — la TUI rend un CHEMIN tronqué par la
#   GAUCHE, donc elle doit réserver le budget du préfixe « <label>: » avant de
#   tronquer. GTK rend le nom de PROJET dans un label Pango ellipsé en mode END :
#   le préfixe placé en tête survit à la troncature par construction. Le
#   pendant GTK est session_project_markup, qui produit du markup Pango et n'a
#   donc rien à partager. Porter l'arithmétique de la TUI ici n'ajouterait qu'un
#   calcul mort.
DIVERGENCES = {'session_path_cell', 'session_project_markup'}

# Jetons qui nomment le client : ils DOIVENT différer (User-Agent, mentions du
# toolkit dans un message). Normalisés avant comparaison.
_CLIENT_TOKENS = [
    ('claude-watcher-gtk', 'claude-watcher-<client>'),
    ('claude-watcher-tui', 'claude-watcher-<client>'),
]


def _tui_script() -> Path | None:
    env = os.environ.get('CW_TUI_SCRIPT')
    candidates = [Path(env)] if env else []
    candidates += [REPO.parent / 'tui' / 'claude-watcher-tui.py',
                   REPO / '_tui' / 'claude-watcher-tui.py']
    return next((c for c in candidates if c.is_file()), None)


@pytest.fixture(scope='module')
def tui_source() -> str:
    path = _tui_script()
    if path is None:
        msg = ('script de la TUI introuvable (cherché ../tui/, _tui/, $CW_TUI_SCRIPT) — '
               'la parité du cœur partagé ne peut pas être vérifiée')
        if os.environ.get('CI'):
            pytest.fail(msg)     # en CI l'absence est une panne, pas une dispense
        pytest.skip(msg)
    return path.read_text()


@pytest.fixture(scope='module')
def gtk_source() -> str:
    return (REPO / 'claude-watcher-gtk.py').read_text()


def _strip_docstrings(node: ast.AST) -> ast.AST:
    for sub in ast.walk(node):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = sub.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                sub.body = body[1:] or [ast.Pass()]
    return node


def _normalise(text: str) -> str:
    for src, dst in _CLIENT_TOKENS:
        text = text.replace(src, dst)
    return re.sub(r'\s+', ' ', text).strip()


def _definitions(source: str) -> dict[str, str]:
    """{nom: code normalisé} pour chaque def / class / affectation de module."""
    out: dict[str, str] = {}
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[node.name] = _normalise(ast.unparse(_strip_docstrings(node)))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = _normalise(ast.unparse(node.value))
    return out


@pytest.fixture(scope='module')
def defs(gtk_source, tui_source) -> tuple[dict[str, str], dict[str, str]]:
    return _definitions(gtk_source), _definitions(tui_source)


@pytest.mark.parametrize('name', SHARED_CONSTANTS + SHARED_SYMBOLS
                         + SHARED_DETECTION_CONSTANTS + SHARED_DETECTION_SYMBOLS)
def test_the_shared_core_is_identical_in_both_clients(defs, name):
    gtk, tui = defs
    assert name in gtk, f'{name} manque au widget GTK'
    assert name in tui, f'{name} manque à la TUI'
    assert gtk[name] == tui[name], (
        f'{name} a dérivé entre les deux clients. Portez la correction des deux côtés, '
        f"ou déclarez la divergence dans DIVERGENCES en disant POURQUOI.")


def _shared_strings(source: str) -> dict[tuple[str, str], str]:
    """{(langue, clé): texte} pour les clés de STRINGS que le cœur partagé rend."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == 'STRINGS' for t in node.targets):
            table = ast.literal_eval(node.value)
            return {(lang, k): v for lang, d in table.items() for k, v in d.items()
                    if k.startswith(SHARED_STRING_PREFIXES)}
    raise AssertionError('STRINGS introuvable')


def test_the_strings_the_shared_core_renders_are_identical(gtk_source, tui_source):
    """`tr` est comparé plus haut, mais comparer la FONCTION ne dit rien de ce
    qu'elle renvoie : six aides gardées rendent des clés `rm_*` / `tip_*`, et
    deux textes différents derrière la même clé font diverger la sortie de
    fonctions par ailleurs identiques à l'octet d'AST près."""
    gtk, tui = _shared_strings(gtk_source), _shared_strings(tui_source)
    for key in sorted(set(gtk) & set(tui)):
        assert gtk[key] == tui[key], (
            f'{key} : texte divergent entre les deux clients — '
            f'{gtk[key]!r} vs {tui[key]!r}')


def test_no_shared_helper_was_added_on_one_side_only(defs):
    """Une aide ajoutée d'un seul côté est la forme la plus discrète de la
    dérive : chaque symbole `remote_*` / `*_remote*` doit exister des deux côtés
    ou figurer dans DIVERGENCES."""
    gtk, tui = defs
    interesting = re.compile(r'remote|REMOTE|session_path_cell|session_project_markup')
    gtk_names = {n for n in gtk if interesting.search(n)}
    tui_names = {n for n in tui if interesting.search(n)}
    assert (gtk_names ^ tui_names) <= DIVERGENCES, (
        f'symboles présents d’un seul côté : {sorted((gtk_names ^ tui_names) - DIVERGENCES)}')
