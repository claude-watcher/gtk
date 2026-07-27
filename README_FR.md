# Claude Code Watcher — GTK

> [English version](README.md)

Un widget de bureau GTK3 pour Ubuntu qui surveille toutes les sessions Claude Code actives sur la machine et les affiche dans un overlay persistant — à la manière d'un moniteur Conky.

<p align="center">
  <img src="doc/demo-fr.gif" alt="Le widget GTK Claude Code Watcher suit plusieurs sessions et bascule en disposition deux colonnes" width="720">
</p>

## Fonctionnalités

- Détecte automatiquement toutes les sessions Claude Code actives
- Affiche l'état de chaque session en **temps réel** :
  - **Attente** (orange) — Claude a répondu, attend votre saisie
  - **Travaille** (amber) — Claude traite votre message, avec le nom de l'outil
  - **Idle** (vert) — session en pause
- Utilisation de la fenêtre de contexte (`ctx%`) affichée si disponible
- Nombre de sous-agents lancés par session (`N agents`), chacun détaillé dans l'infobulle de la ligne — désactivable dans les réglages
- Démon de fond affiché en ligne `(D)` non-focusable (masquable dans les réglages)
- Clic sur une session pour focus le terminal correspondant
- Clic droit sur une session pour son menu (focus, copier le PID, ou fermer une session inactive — envoie `SIGTERM`)
- Clic droit sur l'en-tête pour le menu contextuel global (afficher/masquer, snooze, réglages, quitter)
- Clic molette pour snoozer/réveiller (estompe le widget pendant une durée configurable)
- **Maj + molette** pour ajuster l'opacité en direct
- Molette sur la barre de titre — ou le chevron ▾/▸ — pour enrouler/dérouler le widget
- Disposition multi-colonnes pour de nombreuses sessions, avec hauteur max configurable et barre de défilement au-delà
- Raccourci clavier global configurable (défaut `<Ctrl><Alt>q`) pour lancer la navigation clavier
- Drag de l'en-tête ou du pied pour repositionner librement — la position est mémorisée
- Icône systray avec indicateur d'état global
- Pied de page affichant la version installée avec un indicateur de mise à jour (vert = à jour, rouge = une nouvelle version disponible)
- Langue auto-détectée depuis la locale système (`fr` / `en`)
- **Machines distantes** — les sessions d'autres hôtes servant `claude-watcher-webui`, fusionnées dans la même liste et balisées `<nom>:<projet>` (lecture seule ; voir [Sessions distantes](#sessions-distantes))

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
curl -fsSL https://github.com/claude-watcher/gtk/releases/download/v1.4.0/install.sh | bash
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
--settings          ouvre la fenêtre de paramètres au lancement
--remote NAME=URL   surveille une machine servant claude-watcher-webui (répétable)
--no-local          n'affiche que les sessions distantes (aucun scan /proc local)
```

## Sessions distantes

Pointez le widget vers d'autres machines qui font tourner
[`claude-watcher-webui`](https://github.com/claude-watcher/webui) : leurs sessions
apparaissent dans la même liste, balisées `<nom>:<projet>` (convention scp). Les lignes
distantes sont en **lecture seule** : ni focus, ni fermeture — le menu clic droit ne les
propose pas. Un remote qui ne répond plus est marqué périmé avec l'âge de ses données, et
chaque remote configuré figure dans la zone d'état du pied de fenêtre avec sa santé —
`lab ok 3` (joignable) n'est jamais confondu avec `lab injoignable`. La fenêtre de
réglages les liste en lecture seule dans l'onglet **Distants**, les désactivés grisés.

### D'abord, sur la machine distante

Il y a une moitié serveur, et elle n'est pas optionnelle :

1. Installez et **lancez**
   [`claude-watcher-webui`](https://github.com/claude-watcher/webui) sur cette machine —
   le widget n'est qu'un consommateur de son `GET /api/sessions`.
2. webui écoute par défaut sur `APP_HOST=127.0.0.1` : tel quel, il n'est joignable **que
   depuis la machine elle-même**. Pour le regarder d'ailleurs, il faut soit élargir
   l'écoute, soit tunneliser (voir plus bas).
3. Une écoute non-loopback (`0.0.0.0` par exemple) **sans** `APP_AUTH_TOKEN` est
   **refusée au démarrage** — posez un token, ou acceptez explicitement le risque avec
   `APP_ALLOW_INSECURE_BIND=true`. Ce token est celui que vous donnerez au widget.

> **webui parle HTTP en clair.** Il ne termine aucun TLS (il n'existe pas de réglage
> `ssl_certfile`), donc `https://box:8000/` ne marche **pas** contre lui : la connexion
> échoue sur `SSL: RECORD_LAYER_FAILURE`. Utilisez `http://`, ou placez un reverse proxy
> (nginx, Caddy, Traefik) devant et pointez le widget vers l'URL `https://` du proxy.

La forme la plus sûre ne demande aucun proxy et garde le token hors du réseau — un tunnel
SSH vers une URL loopback :

```bash
ssh -N -L 8001:127.0.0.1:8000 box &          # webui reste sur 127.0.0.1 côté « box »
/usr/bin/python3 ~/.local/share/claude-watcher/claude-watcher --remote lab=http://127.0.0.1:8001
```

### Déclarer des remotes

Les remotes permanents se déclarent dans `~/.config/claude-watcher/config.ini` (partagé
avec la TUI : une seule déclaration pour les deux) :

```ini
[remotes]
poll_ms = 2000              # intervalle d'interrogation, distinct de refresh_ms.
                            # Défaut 2000, plancher 250 — en dessous vous
                            # martelez l'hôte plus que vous ne le surveillez.

[remote:lab]
url = http://box:8000/      # SEULE clé obligatoire ; une section sans elle est ignorée
token = s3cr3t
enabled = true              # 1/yes/true/on · 0/no/false/off. Toute autre valeur
                            # est refusée au démarrage plutôt que prise pour « on »
label = lab                 # optionnel, défaut : le nom de la section
```

Le fichier est forcé en mode `0600` à chaque écriture du widget, puisqu'il peut contenir
des tokens. Si vous le créez ou l'éditez à la main, faites
`chmod 600 ~/.config/claude-watcher/config.ini` vous-même — rien ne re-chmode un fichier
que le widget n'a jamais écrit.

Pour jeter un œil ponctuel à une machine, utilisez le drapeau — il n'est jamais écrit dans
le fichier de config :

```bash
/usr/bin/python3 ~/.local/share/claude-watcher/claude-watcher --remote lab=http://box:8000
/usr/bin/python3 ~/.local/share/claude-watcher/claude-watcher --remote lab=http://remote:s3cr3t@box:8000/
CW_REMOTE_TOKEN_LAB=s3cr3t /usr/bin/python3 ~/.local/share/claude-watcher/claude-watcher --remote lab=http://box:8000
```

Ordre de résolution du token, premier trouvé gagne :

1. le userinfo de l'URL — `https://remote:<token>@hote/` (le token est le **mot de
   passe** ; `https://<token>@hote/` sans deux-points marche aussi)
2. `CW_REMOTE_TOKEN_<NOM>` — le nom en majuscules, non-alphanumériques remplacés par `_`
   (`--remote my-lab=…` → `CW_REMOTE_TOKEN_MY_LAB`)
3. la clé `token` de la section `[remote:<nom>]` correspondante
4. aucun — le remote est interrogé sans authentification

Quelle que soit sa provenance, le token part dans un **en-tête** `X-API-Key`, jamais dans
un paramètre de query — webui n'accepte le token qu'en en-tête (`X-API-Key`,
`Authorization: Bearer`, `Authorization: Basic`), et il journalise `query_params` à chaque
requête : un token dans l'URL serait à la fois refusé et écrit en clair dans le log du
serveur. Une query présente dans l'URL du remote est tout de même transmise telle quelle —
le widget ne réécrit pas votre URL, et un reverse proxy peut avoir besoin de ses propres
paramètres — mais elle ne vous authentifiera pas, et elle est masquée partout où le widget
l'affiche.

> **Le token doit être en ASCII.** Les valeurs d'en-tête HTTP sont en latin-1 : un token
> hors de cette plage s'authentifierait comme une autre chaîne ; webui refuse un tel token
> au démarrage plutôt que de servir des 401 inexplicables.

> **Un token passé dans `--remote` est visible par tous les utilisateurs de la machine**
> via `/proc/<pid>/cmdline`, lisible par tous (`-r--r--r--`), alors que
> `/proc/<pid>/environ` n'est lisible que par son propriétaire (`-r--------`). Sur une
> machine partagée, préférez `CW_REMOTE_TOKEN_<NOM>` ou le fichier de config (`0600`).

> **Un token envoyé à un remote `http://` circule en clair**, et le widget ne vous en
> empêchera pas. Utilisez un tunnel SSH vers une URL loopback, ou un reverse proxy qui
> termine le `https://` (les certificats sont alors vérifiés, sans option pour le
> désactiver).

Seules les URL `http` et `https` sont interrogées : un `--remote lab=box` sans schéma ou
une coquille `file://` sont signalés comme une erreur sur ce remote, pas exécutés.

### Modes de panne, et ce que le widget en fait

| Situation | Comportement |
|---|---|
| Hôte lent ou figé | timeout de 5 s (connexion et lecture) **et** budget total de lecture de 5 s ; un thread par remote, donc seul cet hôte est ralenti |
| Réponse énorme | lecture plafonnée à 4 Mio, poll compté en échec |
| Échecs répétés | backoff exponentiel, plafonné à 60 s |
| HTTP 401 / 403 | affiché comme une erreur d'auth, réessayé au plus toutes les 5 min |
| Redirections | **non suivies** — une 302 rejouerait votre token vers la cible |
| Plus de 500 sessions | tronqué, et la zone d'état affiche `lab ok 500/612` |
| Premier poll en cours | `lab démarrage`, pas `lab injoignable` |
| Thread de poll disparu | `lab thread arrêté` — jamais un `ok` trompeur |

Les remotes sont lus au démarrage : en ajouter ou en retirer demande un redémarrage.
Pointer un remote vers votre propre machine avec le scan local actif liste chaque session
deux fois — une fois nue, une fois préfixée ; c'est un choix de configuration, pas un bug.

`--dump` est un diagnostic **local** (il affiche les valeurs brutes registre/JSONL derrière
chaque état, qui n'existent que sur cette machine) : le combiner avec `--no-local`, ou avec
une machine distante *activée* — déclarée par `--remote` comme par une section
`[remote:<nom>]` du fichier de configuration — est refusé plutôt qu'ignoré en silence. Un
remote laissé à `enabled = false` n'est jamais interrogé, il ne bloque donc pas le
diagnostic.

## Comment ça marche

Pour les détails techniques — détection des sessions, internals du focus au clic,
spécificités de la fenêtre GTK et limitations connues — voir
[`doc/ARCHITECTURE.md`](doc/ARCHITECTURE.md) (en anglais).
