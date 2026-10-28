"""
Shell scripts run on the VM.

user_data()        first boot (cloud-init): Docker, plus the docker compose and
                   buildx CLI plugins, pinned and checked against their
                   published SHA-256. Amazon Linux 2023 ships the AWS CLI.
deploy_commands()  SSM Run Command: fetch the bundle from S3, write the
                   Grafana .env with the password read on the VM from
                   Parameter Store (never sent through SSM or printed) and
                   the Analyser source (Timestream by default) and the
                   Grafana bind address (all interfaces when public), then
                   docker compose up.
status_commands()  SSM Run Command: container health and Grafana /api/health,
                   parsed by parse_status().
"""

import json
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass

COMPOSE_VERSION = "v5.5.1"
COMPOSE_SHA256 = "db1889184726840f75c4f9c001048430d4f25b3be3cb084d3ddd762bc0aed576"
BUILDX_VERSION = "v0.37.2"
BUILDX_SHA256 = "982ca20490b45ed1ec8d99795974d3d874a358f75938c9c237305010e6b7e548"

MARKER_DIR = "/var/lib/coffee-vm"
READY_MARKER = f"{MARKER_DIR}/ready"
FAILED_MARKER = f"{MARKER_DIR}/failed"
BOOT_LOG = "/var/log/cloud-init-output.log"
BASE_DIR = "/opt/coffee"
APP_DIR = f"{BASE_DIR}/app"
GRAFANA_DIR = f"{APP_DIR}/AWS/Grafana"
COMPOSE = "docker compose -f docker-compose.local.yml"
SERVICES = ("analyser", "grafana")
GRAFANA_MARKER = "---grafana-health---"
READY_TIMEOUT_S = 600  # how long a deployment waits for the first-boot script
LOOPBACK_BIND, PUBLIC_BIND = "127.0.0.1", "0.0.0.0"  # host address Grafana's port is published on

PLUGINS_DIR = "/usr/local/lib/docker/cli-plugins"
_PLUGINS = (
    (
        f"https://github.com/docker/compose/releases/download/{COMPOSE_VERSION}/docker-compose-linux-x86_64",
        COMPOSE_SHA256,
        "docker-compose",
    ),
    (
        f"https://github.com/docker/buildx/releases/download/{BUILDX_VERSION}/buildx-{BUILDX_VERSION}.linux-amd64",
        BUILDX_SHA256,
        "docker-buildx",
    ),
)

DOCKER_DAEMON_CONFIG = '{"log-driver": "json-file", "log-opts": {"max-size": "10m", "max-file": "3"}}'

_PATTERNS = {
    "bucket": r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]",
    "key": r"[A-Za-z0-9][A-Za-z0-9/._-]*",
    "parameter_name": r"/[A-Za-z0-9/._-]+",
    "region": r"[a-z]{2}(-[a-z]+)+-\d",
    "sha256": r"[0-9a-f]{64}",
    "account_id": r"\d{12}",
    "analyser_source": r"file|timestream",
    "sns_topic_arn": r"arn:aws:sns:[a-z0-9-]+:\d{12}:[A-Za-z0-9_.-]+",
}


def _checked(name: str, value: str) -> str:
    """Value safe to embed in a shell command, or ValueError."""
    if not re.fullmatch(_PATTERNS[name], value) or ".." in value:
        raise ValueError(f"invalid {name}: {value!r}")
    return shlex.quote(value)


def user_data() -> str:
    """First-boot script. Idempotent: if it failed, it can be run again (sudo bash it on the VM)."""
    lines = [
        "#!/bin/bash",
        "set -euo pipefail",
        f"mkdir -p {MARKER_DIR} && rm -f {FAILED_MARKER}",
        f"trap 'touch {FAILED_MARKER}' ERR",  # lets a deployment fail fast with the boot log
        "dnf install -y docker rsync tar",  # tar/rsync: used by the deployment, not guaranteed on minimal images
        f"mkdir -p {PLUGINS_DIR} /etc/docker",
        f"echo '{DOCKER_DAEMON_CONFIG}' > /etc/docker/daemon.json",  # bounded container logs on a 20 GB disk
    ]
    for url, sha256, name in _PLUGINS:
        target = f"{PLUGINS_DIR}/{name}"
        lines += [
            f"curl --proto '=https' --tlsv1.2 --retry 5 --retry-all-errors -fsSL -o {target}.tmp {url}",
            f'echo "{sha256}  {target}.tmp" | sha256sum -c -',
            f"mv {target}.tmp {target} && chmod 755 {target}",
        ]
    lines += ["systemctl enable --now docker", f"touch {READY_MARKER}"]
    return "\n".join(lines) + "\n"


def deploy_commands(
    bucket: str,
    key: str,
    sha256: str,
    account_id: str,
    parameter_name: str,
    region: str,
    restart_replay: bool = False,
    analyser_source: str = "timestream",
    public: bool = False,
    sns_topic_arn: str = "",
) -> list[str]:
    """
    `sha256` of the uploaded bundle: a bundle replaced in S3 after the upload is refused.
    `analyser_source`: "timestream" (the AWS table) or "file" (the replay shipped in the bundle).
    `public`: Grafana listens on every interface (reachable through the security group's
    public port) instead of the loopback only (SSM tunnel).
    `sns_topic_arn`: optional Amazon SNS topic ARN for alert delivery.
    """
    bucket, key, sha256 = _checked("bucket", bucket), _checked("key", key), _checked("sha256", sha256)
    account_id, region = _checked("account_id", account_id), _checked("region", region)
    parameter_name = _checked("parameter_name", parameter_name)
    analyser_source = _checked("analyser_source", analyser_source)
    grafana_bind = PUBLIC_BIND if public else LOOPBACK_BIND
    bundle, staging = f"{BASE_DIR}/bundle.tar.gz", f"{APP_DIR}.new"
    wait_boot = f"until [ -f {READY_MARKER} ]; do [ -f {FAILED_MARKER} ] && exit 3; sleep 5; done"
    commands = [
        "set -euo pipefail",
        f"timeout {READY_TIMEOUT_S} bash -c '{wait_boot}'"
        f" || {{ echo 'first-boot setup (user-data) did not complete, see {BOOT_LOG}:' >&2;"
        f" tail -n 20 {BOOT_LOG} >&2 || true; exit 1; }}",
        f"mkdir -p {BASE_DIR} {APP_DIR}",
        # Everything is prepared in a staging directory: a failure leaves the running app untouched.
        f"aws s3api get-object --bucket {bucket} --key {key} --expected-bucket-owner {account_id}"
        f" --region {region} {bundle} > /dev/null",
        f'echo "{sha256}  {bundle}" | sha256sum -c --quiet -',
        f"rm -rf {staging} && mkdir {staging}",
        f"tar --no-same-owner --no-same-permissions -xzf {bundle} -C {staging}",
        "umask 077",
        f"password=$(aws ssm get-parameter --name {parameter_name} --with-decryption"
        f" --query Parameter.Value --output text --region {region})",
        f"printf 'GRAFANA_ADMIN_PASSWORD=%s\\n' \"$password\" > {staging}/AWS/Grafana/.env",
        "unset password",
        f"echo ANALYSER_SOURCE={analyser_source} >> {staging}/AWS/Grafana/.env",
        f"echo GRAFANA_BIND={grafana_bind} >> {staging}/AWS/Grafana/.env",
    ]
    if sns_topic_arn:
        commands.append(f"echo ANALYSER_SNS_TOPIC_ARN={_checked('sns_topic_arn', sns_topic_arn)} >> {staging}/AWS/Grafana/.env")
    commands.extend([
        # Synced in place, not swapped: the containers' bind mounts keep pointing at live directories.
        f"rsync -a --delete {staging}/ {APP_DIR}/ && rm -rf {staging}",
        f"cd {GRAFANA_DIR}",
        f"{COMPOSE} up -d --build",
        f"{COMPOSE} up -d --force-recreate --no-deps grafana",  # reloads provisioning (data source, dashboards)
    ])
    if restart_replay:
        commands.append(f"{COMPOSE} up -d --force-recreate --no-deps analyser")
    return commands


def status_commands() -> list[str]:
    return [
        "set -uo pipefail",
        f"cd {GRAFANA_DIR} 2>/dev/null && {COMPOSE} ps --all --format json",
        f"echo '{GRAFANA_MARKER}'",
        "curl -fsS --max-time 5 http://127.0.0.1:3000/api/health || echo unreachable",
    ]


@dataclass(frozen=True)
class Status:
    services: Mapping[str, str]  # compose service -> health ("healthy", "starting", ...)
    grafana_ok: bool

    @property
    def ready(self) -> bool:
        return all(self.services.get(s) == "healthy" for s in SERVICES) and self.grafana_ok

    def __str__(self) -> str:
        services = ", ".join(f"{s}={self.services.get(s, 'absent')}" for s in SERVICES)
        return f"{services}, Grafana /api/health: {'oui' if self.grafana_ok else 'non'}"


def _containers(text: str) -> list[dict]:
    """`docker compose ps --format json`: one object per line, or a JSON array (older compose)."""
    containers: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(("{", "[")):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:  # truncated output: that container just counts as not ready
            continue
        containers.extend(parsed if isinstance(parsed, list) else [parsed])
    return containers


def parse_status(output: str) -> Status:
    ps_part, _, health_part = output.partition(GRAFANA_MARKER)
    services = {}
    for container in _containers(ps_part):
        running = container.get("State") == "running"
        services[container.get("Service", "?")] = (container.get("Health") or "running") if running else "stopped"
    try:
        grafana_ok = json.loads(health_part.strip() or "{}").get("database") == "ok"
    except json.JSONDecodeError:
        grafana_ok = False
    return Status(services=services, grafana_ok=grafana_ok)
