# Grafana (local)

Stack locale : **Scenario Analyzer** en rejeu temps réel + **Grafana OSS 13** avec le plugin **Infinity**, qui lit l'API JSON de l'Analyzer (`doc/adr/0001-grafana-datasource.md`). La datasource et le dashboard sont provisionnés depuis ce dossier : aucun clic n'est nécessaire.

## Lancement

1. Générer le jeu de rejeu (une fois) :
   ```bash
   cd AWS/Analyser && uv run python -m analyser.generator --devices 10 --hours 8 --seed 42 --out data/replay/ref
   ```
2. Créer `AWS/Grafana/.env` à partir de `.env.example` et choisir un mot de passe administrateur (`.env` n'est pas versionné).
3. Démarrer :
   ```bash
   cd AWS/Grafana
   docker compose -f docker-compose.local.yml up -d --build
   docker compose -f docker-compose.local.yml ps   # analyser et grafana « healthy »
   ```
4. Ouvrir http://localhost:3000 et se connecter avec le compte du `.env`. Le dashboard **Machines à café — flotte** est la page d'accueil.

Arrêt : `docker compose -f docker-compose.local.yml down`. Ajouter `-v` pour effacer aussi les volumes Grafana et alertes.

## Ce que montre le dashboard

Par défaut, les 5 dernières minutes, rafraîchies toutes les 10 s :

- tuiles : machines en ligne et hors ligne, alertes ouvertes et critiques ;
- état de la flotte : statut, température, heap, puissance, défauts transitoires et alertes ouvertes de chaque machine ;
- pour la machine choisie (variable `Machine`) : température (lignes à 98 °C et 112 °C), heap libre (lignes à 5 000 et 15 000 o), puissance, chronologie des statuts ;
- alertes actives sur la période affichée, avec leur sévérité.

Au démarrage, le rejeu est positionné à `REPLAY_START_OFFSET_S` (1 h par défaut) : la première heure est préchargée, puis les lots arrivent au rythme réel. À la fin du rejeu (8 h), les machines passent `device_offline` après 5 min, ce qui illustre ce scénario.

## Fichiers

| Fichier | Rôle |
|---|---|
| `docker-compose.local.yml` | Services `analyser` (non publié) et `grafana` (publié sur `127.0.0.1` seulement) |
| `provisioning/datasources/infinity.yaml` | Datasource Infinity, limitée à `http://analyser:8000` (`allowedHosts`) |
| `provisioning/dashboards/dashboards.yaml` | Chargement des dashboards du dossier `dashboards/` (lecture seule) |
| `dashboards/coffee-fleet.json` | Le dashboard |

