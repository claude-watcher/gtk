"""Charge le script mono-fichier sans ses dépendances GTK.

`claude-watcher-gtk.py` importe gi/GTK au niveau module (l'UI et les
helpers vivent dans le même fichier, par choix : un seul script à déployer).
Les tests ne visent que les helpers purs, donc on fabrique des modules factices
pour gi/cairo : ça évite d'installer une stack GTK complète en CI pour
vérifier une fonction de calcul.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / 'claude-watcher-gtk.py'
STUBBED_ROOTS = ('gi', 'cairo')


class _StubLoader:
    """Produit un module vide dont tout attribut est un MagicMock."""

    def create_module(self, spec):
        module = ModuleType(spec.name)
        module.__getattr__ = lambda name: MagicMock()  # type: ignore[attr-defined]
        return module

    def exec_module(self, module):
        pass


class _StubFinder:
    """Fabrique à la demande n'importe quel sous-module des racines stubées.

    Un finder plutôt qu'une liste en dur : un nouvel import `gi.foo` dans
    le script ne doit pas casser les tests des helpers.
    """

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] not in STUBBED_ROOTS:
            return None
        # _StubLoader n'implémente pas load_module (déprécié) : suffisant à l'exécution.
        return importlib.util.spec_from_loader(fullname, _StubLoader(), is_package=True)  # type: ignore[arg-type]


@pytest.fixture(scope='session')
def watcher():
    """Le script importé comme module (nom de fichier non importable tel quel)."""
    sys.meta_path.insert(0, _StubFinder())
    try:
        spec = importlib.util.spec_from_file_location('claude_watcher_gtk', SCRIPT)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules['claude_watcher_gtk'] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.meta_path[:] = [f for f in sys.meta_path if not isinstance(f, _StubFinder)]
