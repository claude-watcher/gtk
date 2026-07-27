"""cwd de résolution du transcript. Fichier IDENTIQUE dans les deux clients.

Le slug du transcript se calcule sur le cwd de DÉMARRAGE de la session, que le
registre enregistre ; le cwd /proc, lui, dérive dès qu'on renomme le dossier ou
qu'on fait un `cd` en cours de session.

Les deux clients appliquaient la précédence INVERSE de celle du serveur
(`if reg and not cwd: cwd = reg.get('cwd')` — le cwd /proc gagnait). Résultat
mesuré sur une même session : le serveur rendait
`('background', 5, None, 'Titre IA', <ts>, 'sess-1')` et les clients
`('working', None, None, None, None, 'sess-1')` — état, % de contexte, sujet ET
last_activity perdus d'un coup, parce que le slug du cwd dérivé ne désigne aucun
dossier projet.
"""

import json

PID, STARTTIME = 4321, 7
# cwd de démarrage (celui du registre) et cwd /proc après renommage du dossier.
REG_CWD, LIVE_CWD = '/tmp/proj', '/tmp/proj-renomme'


def _instance(tmp_path, registry_cwd: str | None = REG_CWD):
    """Instance CLAUDE_CONFIG_DIR : registre + transcript sous tmp_path."""
    reg = {'procStart': STARTTIME, 'sessionId': 'sess-1', 'status': 'busy',
           'statusUpdatedAt': 1_700_000_000_000}
    if registry_cwd is not None:
        reg['cwd'] = registry_cwd
    (tmp_path / 'sessions').mkdir()
    (tmp_path / 'sessions' / f'{PID}.json').write_text(json.dumps(reg))
    # Claude slugifie le cwd (chaque non-alphanumérique → '-') : /tmp/proj → -tmp-proj.
    proj = tmp_path / 'projects' / '-tmp-proj'
    proj.mkdir(parents=True)
    # Tour TERMINÉ (stop_reason 'end_turn') sous un statut 'busy' figé → 'background'.
    (proj / 'sess-1.jsonl').write_text(
        json.dumps({'type': 'ai-title', 'aiTitle': 'Titre IA'}) + '\n'
        + json.dumps({'type': 'assistant',
                      'message': {'model': 'claude-opus-4-8', 'stop_reason': 'end_turn',
                                  'usage': {'input_tokens': 50_000}, 'content': []}}) + '\n')


def test_the_registry_cwd_resolves_the_transcript_when_proc_drifted(watcher, tmp_path):
    """Le cwd du REGISTRE prime : sinon on slugifie un chemin qui n'existe pas et
    on perd silencieusement tout ce que le transcript porte."""
    _instance(tmp_path)
    state, ctx, _tool, topic, last_activity, session_id = watcher.get_session_state(
        PID, LIVE_CWD, STARTTIME, config_dir=str(tmp_path))
    assert (state, ctx, topic) == ('background', 5, 'Titre IA')
    assert last_activity == 1_700_000_000.0
    assert session_id == 'sess-1'


def test_the_live_cwd_is_the_fallback_when_the_registry_has_none(watcher, tmp_path):
    """Registre sans `cwd` (version de Claude antérieure) : le cwd /proc reste le
    seul candidat. La précédence est « registre SINON /proc », pas « registre seul »."""
    _instance(tmp_path, registry_cwd=None)
    state, ctx, _tool, topic, _, _ = watcher.get_session_state(
        PID, REG_CWD, STARTTIME, config_dir=str(tmp_path))
    assert (state, ctx, topic) == ('background', 5, 'Titre IA')
