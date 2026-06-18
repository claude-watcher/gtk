# Claude Code Watcher — GTK

> [English version](README.md)

Un widget de bureau GTK3 pour Ubuntu qui surveille toutes les sessions Claude Code actives sur la machine et les affiche dans un overlay persistant — à la manière d'un moniteur Conky.

## Fonctionnalités

- Détecte automatiquement toutes les sessions Claude Code actives
- Affiche l'état de chaque session en **temps réel** :
  - **Attente** (orange) — Claude a répondu, attend votre saisie
  - **Travaille** (amber) — Claude traite votre message, avec le nom de l'outil
  - **Idle** (vert) — session en pause
- Utilisation de la fenêtre de contexte (`ctx%`) affichée si disponible
- Clic sur une session pour focus le terminal correspondant
- Clic droit pour le menu contextuel (afficher/masquer, snooze, réglages, quitter)
- Clic molette pour snoozer/réveiller (estompe le widget pendant une durée configurable)
- **Maj + molette** pour ajuster l'opacité en direct
- Molette sur la barre de titre — ou le chevron ▾/▸ — pour enrouler/dérouler le widget
- Raccourci clavier global configurable (défaut `<Ctrl><Alt>q`) pour lancer la navigation clavier
- Drag de l'en-tête ou du pied pour repositionner librement — la position est mémorisée
- Icône systray avec indicateur d'état global
- Langue auto-détectée depuis la locale système (`fr` / `en`)

> [!NOTE]
> Le focus au clic est limité sous GNOME Wayland. Le reste du widget fonctionne
> normalement. Détails (en anglais) dans [`doc/ARCHITECTURE.md`](doc/ARCHITECTURE.md#click-to-focus).

## Prérequis

- Ubuntu / Debian (X11 ou Wayland/GNOME)
- Python 3 (`/usr/bin/python3`)
- GTK3 + bibliothèques GObject introspection

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-wnck-3.0 gir1.2-appindicator3-0.1 wmctrl xdotool
```

Optionnel — requis pour le focus Kitty :
- `allow_remote_control yes` + `listen_on unix:/tmp/kitty` dans `kitty.conf`

## Installation

```bash
curl -fsSL https://github.com/claude-watcher/gtk/releases/latest/download/install.sh | bash
```

Épingler une version précise plutôt que la dernière :

```bash
curl -fsSL https://github.com/claude-watcher/gtk/releases/download/v1.5.1/install.sh | bash
```

Pour **monter de version**, relance simplement la commande `latest`.

L'installateur :
1. Installe les dépendances apt manquantes
2. Installe le script dans `~/.local/share/claude-watcher/`
3. Crée `~/.config/claude-watcher/config.ini` (ignoré s'il existe déjà)
4. Ajoute une entrée au menu des applications et enregistre l'autostart pour lancer le widget à la connexion

Pour **désinstaller** (supprime le script et les entrées de bureau ; conserve ta config) :

```bash
./install.sh --uninstall
```

<details>
<summary>Depuis un clone local (développement)</summary>

```bash
git clone https://github.com/claude-watcher/gtk
cd gtk
./install.sh          # installe le script du clone, sans téléchargement
```
</details>

> **Aucun hook à installer :** l'état provient des fichiers de session propres à
> Claude Code — rien à ajouter dans ton `settings.json`.

> **Important :** Utiliser impérativement `/usr/bin/python3`, pas un Python
> Homebrew/pyenv — ceux-ci n'ont pas accès aux bindings GTK système.

## Utilisation

Le widget démarre automatiquement après l'installation. Pour le lancer manuellement,
utilise l'entrée **Claude Code Watcher** du menu des applications, ou :

```bash
/usr/bin/python3 ~/.local/share/claude-watcher/claude-watcher &
```

Il démarre ancré en **bas à droite** de l'écran configuré. Glisser l'en-tête pour
le repositionner librement — la position est sauvegardée et restaurée au prochain lancement.

Tous les réglages sont éditables depuis l'écran **Réglages** (clic droit →
Réglages) — pas besoin de toucher à un fichier de config à la main.

### Options CLI

```
--screen N          index du monitor
--corner CORNER     bottom-right | bottom-left | top-right | top-left
--x PX --y PX       position absolue (désactive l'ancrage au coin)
--margin-x PX       marge horizontale depuis le coin
--margin-y PX       marge verticale depuis le coin
--no-tray           désactive l'icône systray
--list-screens      affiche les monitors détectés et quitte
```

## Comment ça marche

Pour les détails techniques — détection des sessions, internals du focus au clic,
spécificités de la fenêtre GTK et limitations connues — voir
[`doc/ARCHITECTURE.md`](doc/ARCHITECTURE.md) (en anglais).
