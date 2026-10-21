# Scenario Analyzer

Worker Python qui détecte les scénarios de panne des machines à café (`doc/G10-architecture-V2.md` §4) et sert une API JSON à Grafana (`doc/adr/0001-grafana-datasource.md`). En local, il lit un **rejeu de fichiers** au format des lots de l'edge. Sur AWS, une source DynamoDB prendra le relais sans changement des détecteurs.

```
batches.jsonl ──> FileReplaySource ──> Engine (détecteurs + alertes) ──> API JSON ──> Grafana (Infinity)
                  (DynamoDB plus tard)          │
                                                └──> alertes : logs + alerts.jsonl (SNS plus tard)
```

## Installation

```bash
cd AWS/Analyser
uv sync --group dev --group notebooks
```

## Données de rejeu

Le générateur pilote le vrai simulateur (`simulator/`, non modifié) sur une horloge simulée. Il produit des heures de données en quelques secondes :

```bash
uv run python -m analyser.generator --devices 10 --hours 8 --seed 42 --out data/replay/ref
```

- `batches.jsonl` : un lot edge par ligne, `{"edge_id", "device_id", "sent_at", "readings": [...]}`, horodatages en epoch ms ;
- `ground_truth.csv` : `timestamp,device_id,scenario,event`, c'est-à-dire les scénarios réellement injectés, qui servent de référence d'évaluation.

Le scénario `silent_device` est activé dans le jeu généré (`--no-silent` pour le désactiver). `data/` n'est pas versionné.

## Notebooks

```bash
cd notebooks && uv run jupyter lab
```

| Notebook | Contenu |
|---|---|
| `01_explore_data` | Télémétrie par machine, épisodes de vérité terrain, zooms sur chaque scénario |
| `02_detectors` | Alertes produites, comparaison avec la vérité terrain (rappel, précision, délai) |
| `03_tuning` | Sensibilité de chaque seuil, justification des valeurs par défaut |

Les notebooks importent le package et `notebooks/_viz.py` (graphiques uniquement) ; ils regénèrent le jeu de référence s'il manque. Vider les sorties avant de commiter.

## Lancer l'Analyzer

```bash
ANALYSER_REPLAY_PATH=data/replay/ref/batches.jsonl ANALYSER_REPLAY_MODE=realtime \
ANALYSER_REPLAY_START_OFFSET_S=3600 uv run python -m analyser
curl localhost:8000/api/devices
```

Pour l'ensemble avec Grafana : voir `AWS/Grafana/README.md`.

| Endpoint | Rôle |
|---|---|
| `GET /health` | État du dernier cycle (503 avant le premier cycle ou après un échec) |
| `GET /api/devices` | État courant de chaque machine ; `meta` : `total`, `online`, `offline`, `with_open_alerts` |
| `GET /api/telemetry?device_id=&from=&to=` | Lectures (epoch ms, 5 dernières minutes par défaut) |
| `GET /api/alerts?from=&to=&state=open\|resolved\|all` | Alertes actives sur la période ; `meta` : `open`, `critical` |

Toutes les réponses suivent l'enveloppe `{success, data, error, meta}`. L'API n'est exposée que sur le réseau Docker interne.

## Configuration (variables `ANALYSER_*`)

| Variable | Défaut | Rôle |
|---|---|---|
| `REPLAY_PATH` | (obligatoire) | Fichier `batches.jsonl` |
| `REPLAY_MODE` | `instant` | `instant` (tout d'un coup) ou `realtime` (au fil de l'horloge) |
| `REPLAY_START_OFFSET_S` | `0` | Position du rejeu ramenée à « maintenant » ; tout ce qui précède est préchargé |
| `POLL_INTERVAL_S` | `30` | Période des cycles d'analyse |
| `WINDOW_S` | `3600` | Historique gardé en mémoire par machine (servi à Grafana) |
| `ALERTS_PATH` | (aucun) | Fichier JSONL des événements d'alerte |
| `API_HOST` / `API_PORT` | `127.0.0.1` / `8000` (`0.0.0.0` dans l'image) | Écoute de l'API |
| `MAX_QUERY_RANGE_S` / `MAX_ALERTS` | `86400` / `1000` | Garde-fous de l'API |

Seuils de détection, avec leurs valeurs par défaut justifiées dans `03_tuning` :

| Variable | Défaut | Scénario |
|---|---|---|
| `SETPOINT_C`, `THERMAL_DELTA_C`, `THERMAL_CONSECUTIVE`, `THERMAL_CRITICAL_C` | 94, 4, 3, 112 | `thermal_runaway` : WARNING au-delà de consigne + Δ sur N lectures, CRITICAL sur bit overheat ou température critique |
| `LEAK_MIN_DROP_BYTES`, `LEAK_MIN_SAMPLES`, `LEAK_STREAK` | 2000, 10, 2 | `memory_leak_fota` : baisse du heap en `standby` après N mises à jour consécutives |
| `OOM_CRITICAL_FREE_BYTES` | 5000 | `out_of_memory` : bit OOM ou heap critique |
| `SILENT_GAP_S` | 10 | `silent_device` : trou entre deux mesures |
| `OFFLINE_AFTER_S` | 300 | `device_offline` : plus aucune donnée |

Une valeur invalide arrête le démarrage (code 2) avec le nom de la variable fautive.

## Robustesse et limites connues

- Les lectures datées de plus de 5 min dans le futur (horloge défaillante) sont écartées. Sans cela, une seule lecture fautive ferait passer toutes les lectures suivantes pour des retards.
- Si l'analyse d'une machine échoue, ses lectures du cycle sont ignorées et le reste de la flotte est quand même traité.
- Ces cas, comme les lectures en retard, sont comptés dans `/health` : `future_readings`, `device_errors`, `late_readings`.
- Les alertes ouvertes ne sortent jamais de l'historique borné (`MAX_ALERTS`).
- En rejeu `realtime`, chaque redémarrage rejoue le fichier depuis le début : `alerts.jsonl` reçoit de nouveau les mêmes alertes, avec de nouveaux identifiants. Il faudra des identifiants déterministes avant de brancher SNS.
- `/api/telemetry` sans `device_id` renvoie toutes les lectures de la période, sans plafond ni sous-échantillonnage. Le dashboard filtre toujours par machine, mais il faudra un paramètre de limite ou de sous-échantillonnage pour une flotte de 50 machines sur de longues périodes.

## Structure

Modules plats, à lire dans cet ordre :

| Module | Rôle |
|---|---|
| `models.py` | Vocabulaire (`Reading`, `Alert`, scénarios, sévérités, bits d'erreur) et validation d'un lot edge (`parse_batch`) |
| `config.py` | `Settings` et `Thresholds`, lus depuis `ANALYSER_*` |
| `detectors.py` | Une fonction pure par scénario, `step(state, reading, thresholds) -> (state, Detection)`, et la table `DETECTORS` |
| `alerts.py` | `track()` : ouverture / escalade / résolution sans doublon ; sinks log et JSONL |
| `engine.py` | `process(snapshot, fetched, settings)` : un cycle d'analyse immuable ; `FleetSnapshot` et ses vues pour l'API |
| `source.py` | `TelemetrySource` (`Protocol`) et `FileReplaySource` (`instant` / `realtime`) ; DynamoDB à venir |
| `api.py` | `AnalyserService` (snapshot courant, cycles, health), boucle de fond, routes FastAPI |
| `__main__.py` | Lecture de la config, assemblage, uvicorn |
| `generator.py`, `replay_files.py` | Jeu de rejeu à partir du simulateur, formats de fichiers |
| `evaluation.py` | Comparaison avec la vérité terrain, balayage des seuils (notebooks) |

