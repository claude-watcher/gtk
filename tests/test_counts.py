"""Compteurs de l'en-tête : la rédaction s'adapte à la place, cf. specs/002.

Le label était écrit une fois puis TRONQUÉ par l'ellipse : l'utilisateur perdait
les derniers compteurs — le total avec — pendant que les premiers restaient longs.
Fichier IDENTIQUE dans les deux clients.
"""


def _plain(watcher, level, waiting=3, working=1, bg=2, total=12):
    return watcher.counts_sep(level).join(
        t for t, _ in watcher.counts_segments(waiting, working, bg, total, level))


def test_each_level_is_shorter_than_the_one_above(watcher):
    """Un repli qui n'économise rien ne sert à rien."""
    widths = [len(_plain(watcher, lvl)) for lvl in range(3)]
    assert widths[0] > widths[1] > widths[2], widths


def test_the_richest_level_that_fits_is_chosen(watcher):
    """On mesure AVANT de poser : le budget décide, pas l'ellipse."""
    measure = lambda lvl: len(_plain(watcher, lvl))  # noqa: E731
    assert watcher.fit_level(measure, measure(0)) == 0
    assert watcher.fit_level(measure, measure(1)) == 1
    assert watcher.fit_level(measure, measure(2)) == 2
    # Sous le plus dense, on garde le plus dense : mieux vaut tronqué que vide.
    assert watcher.fit_level(measure, 1) == 2


def test_an_unallocated_label_keeps_the_richest_level(watcher):
    """Largeur 0 = pas encore dessiné. Dégrader sur une mesure qu'on n'a pas
    afficherait des chiffres nus au premier tick, puis sauterait au texte long."""
    assert watcher.fit_level(lambda lvl: 999, 0) == 0


def test_a_zero_counter_is_never_written(watcher):
    """« 0 attente » occupe exactement la place qui manque."""
    for lvl in range(3):
        txt = _plain(watcher, lvl, waiting=0, working=0, bg=0, total=4)
        assert '0' not in txt.replace('4', ''), (lvl, txt)


def test_the_background_shell_counter_appears_only_when_there_is_one(watcher):
    for lvl in range(3):
        assert watcher.BG_SHELL_GLYPH not in _plain(watcher, lvl, bg=0)
        assert watcher.BG_SHELL_GLYPH in _plain(watcher, lvl, bg=1)


def test_the_chosen_text_fits_the_budget_when_any_level_does(watcher):
    """Garde de bout en bout : pour tout budget au-dessus du plus dense, le texte
    rendu tient. C'est la promesse de la feature, pas un détail de forme."""
    measure = lambda lvl: len(_plain(watcher, lvl))  # noqa: E731
    for budget in range(measure(2), measure(0) + 3):
        chosen = watcher.fit_level(measure, budget)
        assert measure(chosen) <= budget, (budget, chosen)
