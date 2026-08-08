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
    reg = {'procStart': STARTTIME, 'sessionId': 'sess-1', 'status': 'shell',
           'statusUpdatedAt': 1_700_000_000_000}
    if registry_cwd is not None:
        reg['cwd'] = registry_cwd
    (tmp_path / 'sessions').mkdir()
    (tmp_path / 'sessions' / f'{PID}.json').write_text(json.dumps(reg))
    # Claude slugifie le cwd (chaque non-alphanumérique → '-') : /tmp/proj → -tmp-proj.
    proj = tmp_path / 'projects' / '-tmp-proj'
    proj.mkdir(parents=True)
    # Tour TERMINÉ (stop_reason 'end_turn') sous un statut 'shell' figé → 'background'.
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


def test_a_resumed_session_reads_its_own_transcript_not_a_neighbours(watcher, tmp_path):
    """`claude -r <id>` depuis un autre dossier : le JSONL reste sous son projet
    d'ORIGINE. Le repli « .jsonl le plus récent du slug du cwd » attribuait alors
    l'état d'une session voisine (vue 'background' pendant qu'elle travaillait)."""
    _instance(tmp_path)
    # Le transcript de sess-1 vit ailleurs ; sous le slug du cwd, seule une session
    # voisine (tour terminé, plus récente) est présente.
    origin = tmp_path / 'projects' / '-tmp-autre-projet'
    origin.mkdir(parents=True)
    (tmp_path / 'projects' / '-tmp-proj' / 'sess-1.jsonl').rename(origin / 'sess-1.jsonl')
    (tmp_path / 'projects' / '-tmp-proj' / 'voisine.jsonl').write_text(
        json.dumps({'type': 'assistant',
                    'message': {'model': 'claude-opus-4-8', 'stop_reason': 'end_turn',
                                'usage': {'input_tokens': 50_000}, 'content': []}}) + '\n')
    state, ctx, _tool, topic, _, _ = watcher.get_session_state(
        PID, REG_CWD, STARTTIME, config_dir=str(tmp_path))
    assert (state, ctx, topic) == ('background', 5, 'Titre IA')


def test_no_transcript_found_keeps_the_registry_state(watcher, tmp_path):
    """Transcript introuvable : on garde l'état du registre ('working') plutôt que
    de le dégrader sur la foi d'un JSONL qui n'est pas celui de la session."""
    _instance(tmp_path)
    (tmp_path / 'projects' / '-tmp-proj' / 'sess-1.jsonl').unlink()
    (tmp_path / 'projects' / '-tmp-proj' / 'voisine.jsonl').write_text(
        json.dumps({'type': 'assistant',
                    'message': {'model': 'claude-opus-4-8', 'stop_reason': 'end_turn',
                                'usage': {'input_tokens': 50_000}, 'content': []}}) + '\n')
    state, ctx, _tool, topic, _, _ = watcher.get_session_state(
        PID, REG_CWD, STARTTIME, config_dir=str(tmp_path))
    assert (state, ctx, topic) == ('working', None, None)


def test_a_mid_turn_system_event_does_not_read_as_a_finished_turn(watcher, tmp_path):
    """Un `system` de MILIEU de tour (ici l'avis « Backgrounding after the current
    tool finishes… ») ne prouve rien : seuls turn_duration/stop_hook_summary/
    away_summary marquent une fin de tour. Le lire comme telle dégradait en
    'background' une session qui tournait."""
    _instance(tmp_path)
    (tmp_path / 'projects' / '-tmp-proj' / 'sess-1.jsonl').write_text(
        json.dumps({'type': 'ai-title', 'aiTitle': 'Titre IA'}) + '\n'
        + json.dumps({'type': 'assistant',
                      'message': {'model': 'claude-opus-4-8', 'stop_reason': 'tool_use',
                                  'usage': {'input_tokens': 50_000},
                                  'content': [{'type': 'tool_use', 'name': 'Bash'}]}}) + '\n'
        + json.dumps({'type': 'system', 'subtype': 'informational',
                      'content': 'Backgrounding after the current tool finishes…'}) + '\n')
    state, ctx, tool, topic, _, _ = watcher.get_session_state(
        PID, REG_CWD, STARTTIME, config_dir=str(tmp_path))
    assert (state, ctx, tool, topic) == ('working', 5, 'Bash', 'Titre IA')


def test_a_stuck_busy_status_is_not_announced_as_background_work(watcher, tmp_path):
    """'busy' collé (sous-agents interrompus, session mise en fond : le registre
    cesse d'être mis à jour) ne veut PAS dire qu'un travail de fond tourne. Seul
    'shell' le prouve ; ici on prend l'état du JSONL, tour terminé = 'waiting'."""
    _instance(tmp_path)
    reg_path = tmp_path / 'sessions' / f'{PID}.json'
    reg = json.loads(reg_path.read_text())
    reg['status'] = 'busy'
    reg_path.write_text(json.dumps(reg))
    state, ctx, _tool, topic, _, _ = watcher.get_session_state(
        PID, REG_CWD, STARTTIME, config_dir=str(tmp_path))
    assert (state, ctx, topic) == ('waiting', 5, 'Titre IA')


def test_a_malformed_session_id_never_reaches_the_filesystem(watcher, tmp_path):
    """Le sessionId construit des chemins (chemin direct ET motif glob). Un id
    piégé lisait le .jsonl d'une session voisine ('*') ou un fichier HORS de
    l'arbre des projets ('../..'), ce qui est exactement ce que la résolution par
    id devait empêcher."""
    _instance(tmp_path)
    proj = tmp_path / 'projects' / '-tmp-proj'
    (proj / 'voisine.jsonl').write_text(
        json.dumps({'type': 'assistant',
                    'message': {'model': 'claude-opus-4-8', 'stop_reason': 'end_turn',
                                'usage': {'input_tokens': 50_000}, 'content': []}}) + '\n')
    dehors = tmp_path / 'dehors'
    dehors.mkdir()
    (dehors / 'secret.jsonl').write_text(
        json.dumps({'type': 'assistant',
                    'message': {'model': 'claude-opus-4-8', 'stop_reason': 'end_turn',
                                'usage': {'input_tokens': 900_000}, 'content': []}}) + '\n')
    reg_path = tmp_path / 'sessions' / f'{PID}.json'
    for sid in ('*', '../../dehors/secret', '../dehors/secret', 'sess-1/../voisine'):
        reg = json.loads(reg_path.read_text())
        reg['sessionId'] = sid
        reg_path.write_text(json.dumps(reg))
        state, ctx, _tool, topic, _, _ = watcher.get_session_state(
            PID, REG_CWD, STARTTIME, config_dir=str(tmp_path))
        # Aucun transcript résolu → aucune réconciliation, l'état du registre
        # tient ('shell' → working) et rien n'est lu du disque.
        assert (state, ctx, topic) == ('working', None, None), sid
    assert watcher._find_transcript('*', proj) is None


def test_the_resolved_path_is_re_resolved_when_the_transcript_moves(watcher, tmp_path):
    """Le chemin trouvé hors du slug du cwd est mémorisé : s'il disparaît (projet
    renommé, transcript déplacé), la résolution doit repartir au lieu de servir un
    chemin mort."""
    _instance(tmp_path)
    projects = tmp_path / 'projects'
    a, b = projects / '-tmp-a', projects / '-tmp-b'
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    (projects / '-tmp-proj' / 'sess-1.jsonl').rename(a / 'sess-1.jsonl')
    proj = projects / '-tmp-proj'
    assert watcher._find_transcript('sess-1', proj) == a / 'sess-1.jsonl'
    (a / 'sess-1.jsonl').rename(b / 'sess-1.jsonl')
    assert watcher._find_transcript('sess-1', proj) == b / 'sess-1.jsonl'


def test_a_memorised_absence_never_hides_a_transcript_created_later(watcher, tmp_path):
    """L'absence est mémorisée pour éviter de rebalayer tous les projets à chaque
    tick. Ça ne doit pas aveugler une session fraîche, dont le JSONL apparaît
    après coup sous le slug de son cwd."""
    _instance(tmp_path)
    proj = tmp_path / 'projects' / '-tmp-proj'
    jsonl = proj / 'sess-1.jsonl'
    contenu = jsonl.read_text()
    jsonl.unlink()
    assert watcher._find_transcript('sess-1', proj) is None
    jsonl.write_text(contenu)
    assert watcher._find_transcript('sess-1', proj) == jsonl
