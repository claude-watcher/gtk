"""Sessions Agent SDK masquées (specs/005). Fichier IDENTIQUE dans les deux clients.

T3 Code et les scripts SDK lancent de vraies sessions `claude`, pilotées depuis leur
propre UI. `claude` signe leur env `CLAUDE_CODE_ENTRYPOINT=sdk-*` : chaque ligne porte
la valeur brute (`entrypoint`), et `hide_sdk_sessions` (actif par défaut) les masque
après la fusion local/distant, à côté de `hide_daemons`.

Faux /proc dans tmp_path : `Path` du module est redirigé, `get_env` le lit en dur.
"""

from pathlib import Path

import pytest

REMOTE = {'name': 'lab', 'label': 'lab'}
IDLE = ('idle', None, None, None, None, None, False)


@pytest.fixture
def local_scan(watcher, tmp_path, monkeypatch):
    """`scan_local_sessions` sur un faux /proc : environ réel, résolveurs neutralisés."""
    procs: list[dict] = []
    monkeypatch.setattr(watcher, 'Path', lambda p: tmp_path / Path(p).relative_to('/'))
    monkeypatch.setattr(watcher, 'scan_proc', lambda collect_agents=True: (procs, {}))
    monkeypatch.setattr(watcher, 'get_all_windows', lambda: [])
    monkeypatch.setattr(watcher, 'get_cwd', lambda pid: None)
    monkeypatch.setattr(watcher, 'get_parent_terminal', lambda pid, window_pids=None: None)
    monkeypatch.setattr(watcher, 'find_best_window', lambda *a, **k: None)
    monkeypatch.setattr(watcher, 'get_session_state', lambda *a, **k: IDLE)
    monkeypatch.setattr(watcher, 'session_agents', lambda *a, **k: [])
    monkeypatch.setattr(watcher, 'worktree_of', lambda cwd, found: (cwd, None))
    for key, val in (('hide_daemons', False), ('show_agents', True), ('no_local', False),
                     ('sort_mode', 'default')):
        monkeypatch.setattr(watcher.CFG, key, val, raising=False)

    def add(pid: int, environ: bytes | None, daemon: bool = False) -> None:
        d = tmp_path / 'proc' / str(pid)
        d.mkdir(parents=True)
        if environ is None:
            (d / 'environ').mkdir()  # IsADirectoryError : env illisible
        else:
            (d / 'environ').write_bytes(environ)
        procs.append({'pid': pid, 'starttime': 100, 'elapsed': 5, 'start_unix': 0.0,
                      'is_daemon': daemon})

    return add


def _entrypoints(watcher) -> dict[int, str | None]:
    return {s['pid']: s['entrypoint'] for s in watcher.scan_local_sessions()}


def test_local_rows_carry_the_raw_entrypoint(watcher, local_scan):
    """005-AC1 : valeur brute de l'env ; absente, vide ou env illisible → None. Démon compris."""
    local_scan(1, b'HOME=/home/u\0CLAUDE_CODE_ENTRYPOINT=sdk-ts\0')
    local_scan(2, b'HOME=/home/u\0')
    local_scan(3, b'CLAUDE_CODE_ENTRYPOINT=\0')
    local_scan(4, None)
    local_scan(5, b'CLAUDE_CODE_ENTRYPOINT=cli\0', daemon=True)
    assert _entrypoints(watcher) == {1: 'sdk-ts', 2: None, 3: None, 4: None, 5: 'cli'}


def test_a_non_utf8_byte_elsewhere_does_not_lose_the_entrypoint(watcher, local_scan):
    """005-AC1 : un octet non UTF-8 ailleurs dans environ vidait tout le dict (décodage strict)."""
    local_scan(1, b'LANG=\xff\xfe\0CLAUDE_CODE_ENTRYPOINT=sdk-ts\0X=caf\xe9\0')
    assert _entrypoints(watcher) == {1: 'sdk-ts'}


def _row(**kw) -> dict:
    base = {'pid': 42, 'project': 'proj', 'cwd': '/home/u/proj', 'state': 'idle',
            'idle_seconds': 10, 'elapsed': 300, 'daemon': False}
    base.update(kw)
    return base


@pytest.mark.parametrize('raw, expected', [
    ('sdk-ts', 'sdk-ts'),
    ('sdk-' + 'x' * 100, ('sdk-' + 'x' * 100)[:40]),
    (42, None),
    (['sdk-ts'], None),
    (None, None),
])
def test_remote_rows_carry_a_bounded_entrypoint(watcher, raw, expected):
    """005-AC3 : chaîne copiée et bornée ; non-chaîne → None, la ligne est gardée."""
    row = watcher.adapt_remote_row(_row(entrypoint=raw), REMOTE, 1000.0)
    assert row is not None and row['entrypoint'] == expected


def test_a_remote_row_without_entrypoint_is_kept(watcher):
    """005-AC3 : serveur d'avant 005 (champ absent) → None, la ligne passe."""
    row = watcher.adapt_remote_row(_row(), REMOTE, 1000.0)
    assert row is not None and row['entrypoint'] is None


@pytest.mark.parametrize('ep, expected', [
    ('sdk-ts', True), ('sdk-py', True), ('sdk-cli', True),
    (None, False), ('cli', False), ('claude-vscode', False), ('sdk', False),
    ('SDK-ts', False), ('xsdk-ts', False), (42, False),
])
def test_is_sdk_session(watcher, ep, expected):
    """005-AC4 : règle = entrypoint chaîne commençant par `sdk-`."""
    assert watcher.is_sdk_session({'entrypoint': ep}) is expected


def _fleet(watcher, local_scan) -> list[dict]:
    """Locales sur faux /proc + distantes adaptées : sdk-*, None, cli, claude-vscode."""
    local_scan(1, b'CLAUDE_CODE_ENTRYPOINT=sdk-ts\0')
    local_scan(2, b'HOME=/home/u\0')
    local_scan(3, b'CLAUDE_CODE_ENTRYPOINT=cli\0')
    local_scan(4, b'CLAUDE_CODE_ENTRYPOINT=claude-vscode\0')
    return [watcher.adapt_remote_row(_row(pid=pid, entrypoint=ep), REMOTE, 1000.0)
            for pid, ep in ((11, 'sdk-py'), (12, None), (13, 'claude-vscode'))]


def _pids(sessions: list[dict]) -> set[int]:
    return {s['pid'] for s in sessions}


def test_setting_on_hides_sdk_rows_local_and_remote(watcher, local_scan, monkeypatch):
    """005-AC4 : sdk-* absentes, locales ET distantes ; None / cli / claude-vscode restent."""
    remote = _fleet(watcher, local_scan)
    monkeypatch.setattr(watcher.CFG, 'hide_sdk_sessions', True, raising=False)
    assert _pids(watcher.scan_sessions(remote)) == {2, 3, 4, 12, 13}


@pytest.mark.parametrize('hide_daemons, expected', [(False, {5, 15}), (True, set())])
def test_a_daemon_is_governed_by_hide_daemons_alone(watcher, local_scan, monkeypatch,
                                                    hide_daemons, expected):
    """005-AC4 : un démon `sdk-ts` n'est pas une session, seul hide_daemons le masque."""
    local_scan(5, b'CLAUDE_CODE_ENTRYPOINT=sdk-ts\0', daemon=True)
    remote = [watcher.adapt_remote_row(_row(pid=15, state='daemon', daemon=True,
                                            entrypoint='sdk-ts'), REMOTE, 1000.0)]
    monkeypatch.setattr(watcher.CFG, 'hide_sdk_sessions', True, raising=False)
    monkeypatch.setattr(watcher.CFG, 'hide_daemons', hide_daemons, raising=False)
    assert _pids(watcher.scan_sessions(remote)) == expected


def test_setting_off_lists_every_row_as_before(watcher, local_scan, monkeypatch):
    """005-AC5 : désactivé, toutes les lignes restent ; seul le filtre sépare les deux listes."""
    remote = _fleet(watcher, local_scan)
    monkeypatch.setattr(watcher.CFG, 'hide_sdk_sessions', False, raising=False)
    off = watcher.scan_sessions(remote)
    monkeypatch.setattr(watcher.CFG, 'hide_sdk_sessions', True, raising=False)
    on = watcher.scan_sessions(remote)
    assert _pids(off) == {1, 2, 3, 4, 11, 12, 13}
    assert [s for s in off if not watcher.is_sdk_session(s)] == on


@pytest.fixture
def cfg_file(watcher, monkeypatch, tmp_path):
    path = tmp_path / 'config.ini'
    monkeypatch.setattr(watcher, 'CONFIG_PATH', path)
    monkeypatch.setattr(watcher, 'CONFIG_DIR', tmp_path)
    return path


@pytest.mark.parametrize('content', ['', '[features]\nhide_daemons = true\n'])
def test_default_is_on_when_the_key_is_missing(watcher, cfg_file, content):
    """005-AC6 : clé absente (fichier vide ou [features] sans elle) → masquées."""
    cfg_file.write_text(content)
    assert watcher.load_config()['hide_sdk_sessions'] is True
    assert watcher.parse_args(watcher.load_config(), []).hide_sdk_sessions is True


def test_the_setting_round_trips_through_features(watcher, cfg_file):
    """005-AC6 : persistée dans [features], relue par load_config, autres clés intactes."""
    cfg_file.write_text('[features]\nhide_daemons = true\n')
    watcher.save_config({'features': {'hide_sdk_sessions': 'false'}})
    assert watcher.load_config()['hide_sdk_sessions'] is False
    assert watcher.load_config()['hide_daemons'] is True
    watcher.save_config({'features': {'hide_sdk_sessions': 'true'}})
    assert watcher.load_config()['hide_sdk_sessions'] is True
