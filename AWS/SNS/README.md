# Amazon SNS (Alerting)

Ce dossier contient les outils de déploiement et de configuration d'**Amazon SNS** (Simple Notification Service) pour le projet IoT SDATAWAY (Groupe 10).

## Rôle dans l'architecture

Amazon SNS assure la diffusion des alertes générées par le **Scenario Analyzer** (tournant sur la VM de monitoring) vers les opérateurs :
- **Topic SNS** : `GRP10-SdatawayAlertsTopic`
- **Déclencheurs** : Événements d'alerte détectés (`thermal_runaway`, `memory_leak_fota`, `out_of_memory`, `silent_device` / `device_offline`).
- **Canaux de diffusion** : Email (souscription opérateur via confirmation d'adresse), extensible à SMS ou Webhooks.

## Déploiement

Le notebook `deploy_sns.ipynb` configure le topic et les abonnements étape par étape de manière idempotente :

1. Ouvrir le notebook :
   ```bash
   cd AWS/SNS
   # Lancer avec votre environnement Jupyter (par exemple depuis le venv racine ou Analyser)
   jupyter lab deploy_sns.ipynb
   ```
2. Renseigner optionnellement votre adresse email dans la variable `ALERT_EMAILS` (ou la laisser vide `[]` pour gérer les abonnements manuellement dans la console AWS).
3. Exécuter les cellules jusqu'à la génération de `sns_config.json`.
4. Pour tester : basculer `CONFIRM_TEST = True` dans la cellule de test.
5. Pour nettoyer : basculer `CONFIRM_DELETE = True` dans la dernière cellule de Teardown.
