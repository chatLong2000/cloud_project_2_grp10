# Grafana

Stack Docker : **Scenario Analyzer** + **Grafana OSS** (plugin Infinity). La datasource et le dashboard **Machines à café — flotte** sont provisionnés automatiquement. Le même compose sert en local et sur la VM.

## Prérequis

- Docker
- Le jeu de rejeu généré (voir [Analyser](../Analyser/README.md))

## Lancer (local)

```bash
cd AWS/Grafana
cp .env.example .env   # choisir GRAFANA_ADMIN_PASSWORD
docker compose -f docker-compose.local.yml up -d --build
```

Ouvrir http://localhost:3000 (compte du `.env`). Arrêt : `docker compose -f docker-compose.local.yml down` (`-v` pour effacer les volumes).

## Config (`.env`)

| Variable | Rôle |
|---|---|
| `GRAFANA_ADMIN_PASSWORD` | Obligatoire |
| `ANALYSER_SOURCE` | `file` (défaut) ou `timestream` |
| `GRAFANA_BIND` | `127.0.0.1` (défaut) ; `0.0.0.0` pour exposer Grafana publiquement |

Déploiement public sur AWS : voir [VM](../VM/README.md).
