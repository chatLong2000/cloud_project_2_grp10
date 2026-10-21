# VM Monitoring & Analytics

`setup_vm.ipynb` crée sur AWS la **VM EC2 « Monitoring & Analytics »** de l'architecture (`doc/G10-architecture-V2.md`) et y déploie le Scenario Analyzer et Grafana OSS avec `AWS/Grafana/docker-compose.local.yml`, sans modification. Pour l'instant, l'Analyzer lit le **rejeu de fichiers** ; DynamoDB viendra ensuite

## Prérequis

```bash
brew install awscli && brew install --cask session-manager-plugin   # tunnel vers Grafana
aws configure                                                        # région us-east-1, droits EC2, IAM, S3, SSM
cd AWS/Analyser && uv run python -m analyser.generator --devices 10 --hours 8 --seed 42 --out data/replay/ref
```

## Lancer

```bash
cd AWS/VM
uv sync
uv run jupyter lab setup_vm.ipynb
```

Exécuter les cellules 0 à 9. À la fin, l'étape 9 affiche la commande du tunnel :

```bash
aws ssm start-session --region us-east-1 --target <instance-id> \
  --document-name AWS-StartPortForwardingSession --parameters portNumber=3000,localPortNumber=3000
```

Ouvrir ensuite http://localhost:3000, avec l'utilisateur `admin` et ce mot de passe :

```bash
aws ssm get-parameter --name /coffee-vm/grafana-admin-password --with-decryption \
  --query Parameter.Value --output text --region us-east-1
```

**Redéployer** après une modification du code : relancer les étapes 5 et 8. Pour **relancer le rejeu**, exécuter l'étape 8 avec `REPLAY_RESTART = True`.

## Ressources créées

Toutes portent les tags `Project=cloud-project-2-grp10` et `Component=vm`, et leurs identifiants sont notés dans `.state.json` (ignoré par git).

| Ressource | Nom | Sécurité |
|---|---|---|
| Paramètre SSM `SecureString` | `/coffee-vm/grafana-admin-password` (`/<PREFIX>/…`) | Généré une seule fois, jamais affiché ; lu sur la VM au déploiement |
| Rôle IAM + instance profile | `coffee-vm-role` | `AmazonSSMManagedInstanceCore`, plus lecture de la seule archive et du seul paramètre ; politique de confiance limitée à EC2, remise à jour à chaque exécution |
| Security group | `coffee-vm-sg` | Aucune règle entrante |
| Bucket S3 | `coffee-vm-<compte>` | Privé (accès public bloqué), chiffré SSE-S3, accès refusé hors TLS, propriétaire vérifié à chaque accès |
| Instance EC2 | `coffee-vm` | Amazon Linux 2023, `t3.small`, IMDSv2 obligatoire (hop limit 1), gp3 20 Go chiffré, pas de clé SSH |

Une instance déjà créée est réutilisée. Son security group et son instance profile sont alors réalignés s'ils ont changé, par exemple après un nettoyage interrompu.

**Premier démarrage** : le user-data installe Docker, `rsync` et `tar`, puis `docker compose` (v5.5.1) et `buildx` (v0.37.2), téléchargés en HTTPS (TLS 1.2 ou plus) à des versions épinglées, avec vérification de leur SHA-256 publiée. Il limite aussi la taille des logs Docker (3 fichiers de 10 Mo par conteneur).

**Archive** (environ 2 Mo) : une liste blanche, avec le code et le Dockerfile de l'Analyzer, le compose, le provisioning et le dashboard Grafana, et le jeu de rejeu. Un fichier qui ressemble à un secret (`.env`, `*.pem`, `*.key`, `*.crt`) dans ces dossiers bloque sa création. Sa SHA-256 est notée dans `.state.json`, et la VM la vérifie avant de déployer.

**Déploiement** : tout est préparé dans un dossier à part (archive vérifiée, `.env` réservé à root), puis synchronisé avec `rsync` sur l'application en service. Un échec avant la synchronisation laisse l'application telle quelle, et les dossiers montés par les conteneurs restent valides.

## Coût (us-east-1, à la demande)

| Poste | Par mois |
|---|---|
| `t3.small` allumée en continu | ~15 USD |
| Volume gp3 20 Go | ~1,6 USD |
| S3 (2 Mo), Parameter Store (niveau standard), Session Manager | ~0 |

Une VM arrêtée (étape 10) ne coûte plus que son disque. L'étape 11 supprime tout.

