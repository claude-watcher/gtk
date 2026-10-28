"""Câblage GTK de `hide_sdk_sessions` (specs/005, 005-AC6), contrôlé sur la SOURCE.

gi est stubé : SettingsDialog / ClaudeWatcher sont des MagicMock, leurs méthodes ne
s'exécutent pas ici. On vérifie chaque étape du chemin de `hide_daemons` : valeurs
d'origine, case à cocher, aperçu, get_values, CFG relu par scan_sessions, save_config.
"""

import ast
import inspect
import re
import textwrap
from pathlib import Path


def _src(watcher) -> str:
    return Path(watcher.__file__).read_text()


def _method_body(watcher, name: str) -> str:
    return _src(watcher).split(f'def {name}(self', 1)[1].split('\n    def ', 1)[0]


def test_the_dialog_shows_the_checkbox_in_both_languages(watcher):
    """005-AC6 : case dans l'onglet Affichage, libellée en FR et en EN."""
    assert watcher.STRINGS['fr']['fld_hide_sdk_sessions'].startswith('Masquer les sessions SDK')
    assert watcher.STRINGS['en']['fld_hide_sdk_sessions'].startswith('Hide SDK sessions')
    dialog = _src(watcher).split('class SettingsDialog', 1)[1].split('\nclass ', 1)[0]
    assert "Gtk.CheckButton(label=tr('fld_hide_sdk_sessions'))" in dialog
    assert "self._chk_hide_sdk_sessions.set_active(getattr(CFG, 'hide_sdk_sessions', True))" in dialog
    assert "'hide_sdk_sessions': getattr(CFG, 'hide_sdk_sessions', True)," in dialog
    assert "(self._chk_hide_sdk_sessions, 'toggled')," in dialog
    assert "'hide_sdk_sessions': self._chk_hide_sdk_sessions.get_active()," in dialog


def test_preview_and_apply_reach_cfg_and_the_config_file(watcher):
    """005-AC6 : aperçu → CFG (relu au prochain _refresh), Appliquer → [features]."""
    assert "CFG.hide_sdk_sessions = values['hide_sdk_sessions']" in _method_body(
        watcher, '_preview_settings')
    apply = _method_body(watcher, '_apply_settings')
    features = apply.split("'features': {", 1)[1].split('}', 1)[0]
    assert "'hide_sdk_sessions': 'true' if values['hide_sdk_sessions'] else 'false'," in features
    assert 'self._preview_settings(values)' in apply


def test_install_sh_lists_every_features_key(watcher):
    """005-AC6 : le config.ini de départ d'install.sh documente chaque clé [features] lue."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(watcher.load_config)))
    read = {n.args[0].value for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name) and n.func.value.id == 'f'
            and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)}
    install = (Path(watcher.__file__).parent / 'install.sh').read_text()
    block = install.split('\n[features]\n', 1)[1].split('\nEOF\n', 1)[0]
    listed = set(re.findall(r'^#?\s*([a-z_]+)\s*=', block, re.M))
    assert 'hide_sdk_sessions' in read
    assert read <= listed, f'absentes de install.sh : {sorted(read - listed)}'
