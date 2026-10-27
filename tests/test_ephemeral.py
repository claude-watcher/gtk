"""Runs `claude` headless éphémères masqués (specs/004). Fichier IDENTIQUE dans les deux clients.

T3 Code lance des `claude --no-session-persistence` headless de quelques secondes
(titres de threads) : pas de transcript, rien à surveiller, ils clignotaient comme
une ligne en double. `scan_proc` les ignore, sur tokens argv EXACTS, et seulement
en mode non interactif (`-p`/`--print`/`--output-format`/`--input-format`) : le
flag n'agit qu'en mode print, une session interactive persiste son transcript.

Faux /proc dans tmp_path : `Path` du module est redirigé, `scan_proc` le lit en dur.
"""

from pathlib import Path

import pytest

UUID = '0b6c2a3e-6f1d-4c55-9a7e-2f3b8d1e4c90'


@pytest.fixture
def fake_proc(watcher, tmp_path, monkeypatch):
    """Construit /proc/<pid>/{comm,stat,cmdline} ; argv=None → cmdline illisible."""
    root = tmp_path / 'proc'
    root.mkdir()
    (root / 'uptime').write_text('1000.0 0.0\n')
    monkeypatch.setattr(watcher, 'Path', lambda p: tmp_path / Path(p).relative_to('/'))

    def add(pid: int, argv: list[str] | None, comm: str = 'claude'):
        d = root / str(pid)
        d.mkdir()
        (d / 'comm').write_text(comm + '\n')
        # 19 champs après comm avant starttime (champ 22 global).
        (d / 'stat').write_text(f'{pid} ({comm}) S ' + '0 ' * 18 + '100 0\n')
        if argv is None:
            # Lire un dossier lève IsADirectoryError (OSError) : simule la course exec/exit.
            (d / 'cmdline').mkdir()
        else:
            (d / 'cmdline').write_bytes('\0'.join(argv).encode() + b'\0')

    return add


def _pids(watcher) -> dict[int, bool]:
    procs, _ = watcher.scan_proc(collect_agents=False)
    return {p['pid']: p['is_daemon'] for p in procs}


T3_EPHEMERAL = ['claude', '--output-format', 'stream-json', '--verbose', '--input-format',
                'stream-json', '--setting-sources=user,project,local', '--strict-mcp-config',
                '--permission-mode', 'default', '--no-session-persistence',
                '--settings', '{"disableAllHooks":true}']


def test_non_interactive_no_session_persistence_is_hidden(watcher, fake_proc):
    """004-AC1 : `--no-session-persistence` + token non interactif → aucune ligne."""
    fake_proc(10, T3_EPHEMERAL)  # --output-format stream-json, sans -p
    fake_proc(11, ['claude', '-p', 'hi', '--no-session-persistence'])
    fake_proc(12, ['claude', '--print', 'hi', '--no-session-persistence'])
    fake_proc(13, ['claude', '--output-format=json', '--no-session-persistence'])
    fake_proc(14, ['claude', '--input-format=stream-json', '--no-session-persistence'])
    fake_proc(15, ['claude', '--input-format', 'stream-json', '--no-session-persistence'])
    fake_proc(16, ['claude', '--output-format', 'json', '--no-session-persistence'])
    assert _pids(watcher) == {}


def test_other_claude_processes_are_unchanged(watcher, fake_proc):
    """004-AC2 : interactif, `-p`, session T3 (`--session-id`), démon → listés, flag démon intact."""
    fake_proc(20, ['claude'])
    fake_proc(21, ['claude', '-p', 'summarize this'])
    fake_proc(22, ['claude', '--output-format', 'stream-json', '--verbose', '--input-format',
                   'stream-json', f'--session-id={UUID}'])
    fake_proc(23, ['claude', 'daemon', 'run'])
    assert _pids(watcher) == {20: False, 21: False, 22: False, 23: True}


def test_interactive_no_session_persistence_is_listed(watcher, fake_proc):
    """004-AC2 : flag ignoré hors mode print → la session interactive persiste, listée.
    Un `--print` noyé dans un prompt n'en fait pas un run non interactif (004-AC3)."""
    fake_proc(24, ['claude', '--no-session-persistence'])
    fake_proc(25, ['claude', '--no-session-persistence', 'explain --print to me'])
    assert _pids(watcher) == {24: False, 25: False}


def test_substring_inside_an_argument_does_not_hide(watcher, fake_proc):
    """004-AC3 : la chaîne noyée dans `--settings {...}` ou un prompt ne masque rien."""
    fake_proc(30, ['claude', '--settings', '{"note":"--no-session-persistence"}'])
    fake_proc(31, ['claude', '-p', 'explain --no-session-persistence'])
    fake_proc(32, ['claude', '--output-format', 'json', '--settings',
                   '{"note":"--no-session-persistence"}'])
    assert _pids(watcher) == {30: False, 31: False, 32: False}


def test_unreadable_cmdline_is_still_listed(watcher, fake_proc):
    """004-AC4 : cmdline illisible → listé comme aujourd'hui (non-démon)."""
    fake_proc(40, None)
    assert _pids(watcher) == {40: False}


def test_scan_proc_is_under_parity_guard():
    """004-AC5 : `scan_proc` reste sous le garde de parité gtk/tui — le filtre ne
    peut pas dériver d'un client à l'autre sans faire rougir test_core_parity.py."""
    import ast
    tree = ast.parse((Path(__file__).parent / 'test_core_parity.py').read_text())
    shared = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                  and any(getattr(t, 'id', None) == 'SHARED_DETECTION_SYMBOLS' for t in n.targets))
    assert 'scan_proc' in shared
