# VM Monitoring & Analytics

`setup_vm.ipynb` crée une VM EC2 et y déploie la stack [Grafana](../Grafana/README.md) (Analyzer sur Timestream + Grafana), accessible sur `http://<ip-publique>:3000`.

## Prérequis

- AWS CLI configurée (`aws configure`, région `us-east-1`)
- Le jeu de rejeu généré (voir [Analyser](../Analyser/README.md))

## Lancer

```bash
cd AWS/VM
uv sync
uv run jupyter lab setup_vm.ipynb
```

Exécuter les cellules 0 à 9 : l'étape 9 affiche l'URL de Grafana (utilisateur `admin`, mot de passe de l'étape 2).

| Action | Étapes |
|---|---|
| Redéployer après un changement de code | 5 puis 8 |
| Changer qui peut accéder (`GRAFANA_CIDRS`) | 0, 4 puis 8 |
| Nouvelle URL après redémarrage de la VM | 9 |
| Arrêter / tout supprimer | 10 / 11 |

## Config (étape 0)

| Variable | Rôle |
|---|---|
| `GRAFANA_CIDRS` | `[resources.caller_public_cidr()]` : votre IP seulement · `["0.0.0.0/0"]` : tout Internet · `[]` : tunnel SSM seulement |
| `ANALYSER_SOURCE` (étape 8) | `timestream` (défaut) ou `file` |

Grafana est servi en HTTP (sans TLS). Coût : environ 20 USD/mois allumée, quasi nul arrêtée.
