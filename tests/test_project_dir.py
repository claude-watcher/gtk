"""cwd → dossier de transcripts. Fichier IDENTIQUE dans les deux clients.

`cwd_to_project_dir` fait partie du socle porté des deux côtés, et les deux
copies avaient déjà divergé exactement ici — sur le repli appliqué quand la
racine projet est vide.
"""


def test_a_worktree_resolves_to_its_parent_project(watcher, tmp_path, monkeypatch):
    """Claude range le transcript d'un worktree sous le slug du projet PARENT."""
    projects = tmp_path / 'projects'
    (projects / '-home-u-proj').mkdir(parents=True)
    monkeypatch.setattr(watcher, 'CLAUDE_PROJECTS_DIR', projects)
    assert watcher.cwd_to_project_dir('/home/u/proj/.claude/worktrees/wt') == \
        projects / '-home-u-proj'


def test_a_rootless_worktree_is_not_the_projects_directory(watcher, tmp_path, monkeypatch):
    """`base / ''` VAUT `base` : sans garde, un cwd dont la racine projet est
    vide renvoyait le DOSSIER DES PROJETS lui-même — qui existe toujours — comme
    s'il était un projet, et l'état/le contexte étaient lus au mauvais endroit.

    Les deux clients pansaient le symptôme différemment (`root or ''` d'un côté,
    `root or cwd` de l'autre) ; aucun des deux replis n'est correct : sans
    racine, il n'y a tout simplement pas de projet à désigner.
    """
    projects = tmp_path / 'projects'
    projects.mkdir(parents=True)
    monkeypatch.setattr(watcher, 'CLAUDE_PROJECTS_DIR', projects)
    assert watcher.cwd_to_project_dir('/.claude/worktrees/wt') is None
