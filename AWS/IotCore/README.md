# AWS IoT Core

`setup_iot_core.ipynb` connecte l'edge à AWS IoT Core, étape par étape : thing, certificat X.509, policy, endpoint dans `.env`, test de publication, logs CloudWatch et règle d'ingestion vers la Lambda Ingestor. Chaque cellule peut être relancée sans risque ; la dernière supprime tout (désactivée par défaut).

```bash
cd AWS/IotCore
uv venv && uv pip install -r requirements.txt
uv run jupyter lab setup_iot_core.ipynb
```

Prérequis : identifiants AWS configurés (`aws configure`) avec les droits IoT Core, IAM (création de rôle), CloudWatch Logs et Lambda. La région se règle dans la première cellule (`REGION`).

Le notebook écrit :
- `certs/ca.crt`, `certs/edge.crt`, `certs/edge.key` (les certificats de test locaux sont déplacés dans `certs/local-backup/`) ;
- `.env` à la racine (`MQTT_URL`, `MQTT_CLIENT_ID`, `BATCH_MAX_ROWS`) ;
- `AWS/IotCore/.state.json` (ARN du certificat, utilisé pour la reprise et le nettoyage).

Aucun de ces fichiers ne doit être commité (voir `.gitignore`).
