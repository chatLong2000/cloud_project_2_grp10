# Cloud project 2 - group 10

```
devices (5 simulateurs)  --UART pty/socat, CSV-->  edge  --MQTT (QoS 1)-->  broker (service externe, MQTT_URL)
```

| Service | Dossier | Rôle |
|---|---|---|
| `devices` | `simulator/` | 5 × `device_simulator.py`, chacun écrit sur un pty `socat` relié en TCP à l'edge (ports 5001-5005) |
| `edge` | `edge/` | `socat` recrée un pty par machine ; `edge.py` lit les ports série, valide le CSV, bufferise dans SQLite (`/data/buffer.db`) et publie un lot JSON par machine toutes les 10 s sur `coffee/<device_id>/batch` |

## Lancement

Le broker MQTT tourne dans un autre service ; l'edge s'y connecte via `MQTT_URL`.

1. Définir la connexion dans un fichier `.env` à la racine :
   ```
   MQTT_URL=mqtt://host.docker.internal:1883
   MQTT_USERNAME=user
   MQTT_PASSWORD=pass
   ```
   - `mqtt://` = TCP simple (1883 par défaut), `mqtts://` = TLS (8883 par défaut).
   - Les identifiants peuvent aussi être dans l'URL : `mqtt://user:pass@hôte:1883` (caractères spéciaux encodés, ex. `@` → `%40`).
   - Dans le conteneur, `localhost` désigne le conteneur lui-même : pour un broker sur la machine hôte, utiliser `host.docker.internal`.
2. En TLS (`mqtts://`), placer dans `certs/` `ca.crt` (sinon les CA système sont utilisées) et, pour l'authentification X.509, `edge.crt` / `edge.key`. `./certs/gen_certs.sh` génère un jeu de certificats de test.
3. Démarrer :
   ```bash
   docker compose up -d --build
   docker compose logs -f edge
   ```

## Comportement de l'edge

- **Validation** : 8 champs, types, `device_status` connu, compteurs ≥ 0, température plausible. Les lignes rejetées sont journalisées, l'en-tête CSV est ignoré.
- **Sondes muettes** : non détectées par l'edge, la détection se fera côté cloud à partir des lots reçus.
- **Store and forward** : les lignes ne sont supprimées de SQLite qu'après le PUBACK du broker, rien n'est perdu si le broker est injoignable.

Variables utiles (voir `docker-compose.yml` et `Settings` dans `edge/edge.py`) : `NUM_DEVICES`, `DEVICE_PREFIX` (noms des machines, `coffee-machine` par défaut), `BATCH_INTERVAL_S`, `MQTT_URL`, `MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_CLIENT_ID`, `MQTT_CA`, `MQTT_CERT`, `MQTT_KEY`, `MQTT_TOPIC_PREFIX`.

Les logs ground truth des simulateurs sont écrits dans le volume `ground-truth` (`/data/ground_truth_<n>.log`).
