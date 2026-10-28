# Scenario Analyzer

Worker Python qui détecte les scénarios de panne des machines à café et sert une API JSON à Grafana. Il lit soit un **rejeu de fichiers** (local), soit la table **Timestream** `SdatawayIoTDB.telemetry` (AWS).

## Prérequis

- [uv](https://docs.astral.sh/uv/)
- Pour Timestream : identifiants AWS avec `timestream:Select` sur la table et `timestream:DescribeEndpoints`

## Lancer

```bash
cd AWS/Analyser
uv sync --group dev --group notebooks

# Jeu de rejeu (une fois, requis aussi par Grafana et la VM)
uv run python -m analyser.generator --devices 10 --hours 8 --seed 42 --out data/replay/ref

# Rejeu de fichiers
ANALYSER_REPLAY_PATH=data/replay/ref/batches.jsonl ANALYSER_REPLAY_MODE=realtime uv run python -m analyser
# ou base AWS
ANALYSER_SOURCE=timestream uv run python -m analyser

curl localhost:8000/api/devices
```

Notebooks d'exploration et de réglage des seuils : `cd notebooks && uv run jupyter lab` (vider les sorties avant de commiter).

## Config

| Variable | Défaut | Rôle |
|---|---|---|
| `ANALYSER_SOURCE` | `file` | `file` (rejeu) ou `timestream` |
| `ANALYSER_REPLAY_PATH` | (obligatoire si `file`) | Fichier `batches.jsonl` |

Toutes les autres variables `ANALYSER_*` (Timestream, seuils, API) ont des valeurs par défaut : voir `src/analyser/config.py`.

Voir aussi : [Grafana](../Grafana/README.md) (stack locale), [VM](../VM/README.md) (déploiement AWS).
