"""cwd → dossier de transcripts. Fichier IDENTIQUE dans les deux clients.

`cwd_to_project_dir` fait partie du socle porté des deux côtés, et les deux
copies avaient déjà divergé exactement ici — sur le repli appliqué quand la
racine projet est vide.
"""

from pathlib import Path


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


def _git_worktree(tmp_path, gitdir: str):
    """Checkout de worktree git : un `.git` FICHIER pointant vers le dépôt."""
    wt = tmp_path / 'worktrees' / 'feat-011'
    (wt / 'src').mkdir(parents=True)
    (wt / '.git').write_text(f'gitdir: {gitdir}\n')
    return wt


def test_a_plain_git_worktree_is_recognised_from_its_dot_git_file(watcher, tmp_path):
    """`git worktree add` pose le checkout N'IMPORTE OÙ : aucun marqueur dans le
    chemin, contrairement au layout Claude. Seul le `.git` fichier le prouve."""
    repo = tmp_path / 'repo'
    wt = _git_worktree(tmp_path, f'{repo}/.git/worktrees/feat-011')
    assert watcher.git_worktree(str(wt)) == (str(repo), 'feat-011')
    # Depuis un sous-dossier aussi : on remonte jusqu'à la racine du worktree.
    assert watcher.git_worktree(str(wt / 'src')) == (str(repo), 'feat-011')


def test_a_relative_gitdir_still_yields_the_repository_root(watcher, tmp_path):
    """`worktree.useRelativePaths` écrit un gitdir relatif au worktree."""
    wt = _git_worktree(tmp_path, '../../repo/.git/worktrees/feat-011')
    root, name = watcher.git_worktree(str(wt))
    assert (Path(root).resolve(), name) == ((tmp_path / 'repo').resolve(), 'feat-011')


def test_a_submodule_is_not_a_worktree(watcher, tmp_path):
    """Un sous-module a lui aussi un `.git` fichier, mais pointe vers
    `.git/modules/<nom>` — le confondre inventerait un worktree."""
    wt = _git_worktree(tmp_path, f'{tmp_path}/repo/.git/modules/libfoo')
    assert watcher.git_worktree(str(wt)) == (None, None)


def test_an_ordinary_checkout_is_not_a_worktree(watcher, tmp_path):
    """`.git` DOSSIER = checkout principal : on s'arrête là, sans remonter vers
    un éventuel dépôt parent."""
    (tmp_path / 'repo' / '.git').mkdir(parents=True)
    assert watcher.git_worktree(str(tmp_path / 'repo')) == (None, None)


def test_an_unconfirmed_claude_worktree_keeps_the_raw_cwd(watcher, tmp_path):
    """Marqueur Claude dans le chemin mais transcript non résolu : le marqueur
    peut être fortuit, on n'affiche ni racine parente ni sous-ligne."""
    cwd = '/home/u/proj/.claude/worktrees/wt'
    assert watcher.worktree_of(cwd, True) == ('/home/u/proj', 'wt')
    assert watcher.worktree_of(cwd, False) == (cwd, None)


def test_an_unconfirmed_claude_marker_falls_back_to_the_disk_proof(watcher, tmp_path):
    """Un worktree Claude EST un worktree git : quand le transcript n'a pas
    confirmé le marqueur, le `.git` fichier tranche au lieu de laisser la ligne
    non marquée."""
    repo = tmp_path / 'repo'
    wt = tmp_path / 'proj' / '.claude' / 'worktrees' / 'wt'
    wt.mkdir(parents=True)
    (wt / '.git').write_text(f'gitdir: {repo}/.git/worktrees/wt\n')
    assert watcher.worktree_of(str(wt), False) == (str(repo), 'wt')
