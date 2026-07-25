"""Table modèle → fenêtre de contexte.

Le même tableau existe dans les trois applications (webui/tui/gtk) : ce test
existe pour qu'une copie oubliée lors d'une sortie de modèle échoue ici plutôt
que d'afficher un ctx% faux pendant des mois.
"""


def test_default_is_1m(watcher):
    # 1M sur tous les plans : tout ce qui n'est pas dans la liste 200k, y
    # compris les alias nus et les modèles inconnus (futurs).
    for model in ('claude-opus-5', 'claude-opus-4-8', 'claude-sonnet-5', 'opus', 'sonnet', None):
        assert watcher.context_window_for(model) == 1_000_000, model


def test_hard_200k(watcher):
    for model in (
        'claude-haiku-4-5-20251001',
        'claude-opus-4-5-20251101',
        'claude-opus-4-1-20250805',
        'claude-sonnet-4-5-20250929',
        'claude-opus-4-20250514',
        'claude-3-7-sonnet-20250219',
        'anthropic.claude-opus-4-5-20251101-v1:0',
    ):
        assert watcher.context_window_for(model) == 200_000, model
    # Un modèle 200k ne peut pas dépasser sa fenêtre : pas de promotion.
    assert watcher.context_window_for('claude-haiku-4-5-20251001', 150_000) == 200_000


def test_gated_models_promote_on_evidence(watcher):
    # Opus 4.6 / Sonnet 4.6 : 1M seulement si le plan l'accorde — on part sur
    # 200k et on promeut dès qu'un message dépasse les 200k.
    for model in ('claude-opus-4-6', 'claude-sonnet-4-6'):
        assert watcher.context_window_for(model) == 200_000, model
        assert watcher.context_window_for(model, 199_000) == 200_000, model
        assert watcher.context_window_for(model, 250_000) == 1_000_000, model
