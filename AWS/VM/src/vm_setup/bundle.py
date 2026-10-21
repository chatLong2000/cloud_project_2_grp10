"""
Deployment bundle: the files the VM needs to run AWS/Grafana/docker-compose.local.yml.

Paths stay relative to the repository root, so the compose file works
unchanged on the VM (`build: ../Analyser`, default REPLAY_DIR). Only an
allow-list is packed: no .env, venv, tests, notebooks or certificates. The
archive is reproducible (fixed owner and dates), so an unchanged repository
gives the same bytes.
"""

import gzip
import io
import tarfile
from pathlib import Path

# Files or directories, relative to the repository root.
BUNDLE_PATHS = (
    "AWS/Analyser/Dockerfile",
    "AWS/Analyser/.dockerignore",
    "AWS/Analyser/requirements.txt",
    "AWS/Analyser/pyproject.toml",
    "AWS/Analyser/README.md",
    "AWS/Analyser/src",
    "AWS/Grafana/docker-compose.local.yml",
    "AWS/Grafana/provisioning",
    "AWS/Grafana/dashboards",
)
REPLAY_PATH = "AWS/Analyser/data/replay/ref/batches.jsonl"  # default REPLAY_DIR of the compose file
SKIPPED_NAMES = {"__pycache__", ".DS_Store", ".ipynb_checkpoints"}
SECRET_SUFFIXES = (".pem", ".key", ".crt", ".p12")  # never shipped: the bundle is refused instead
GENERATE_HINT = (
    "cd AWS/Analyser && uv run python -m analyser.generator --devices 10 --hours 8 --seed 42 --out data/replay/ref"
)


class BundleError(ValueError):
    pass


def bundle_files(repo_root: Path | str) -> list[str]:
    """Sorted paths (relative to `repo_root`) of every file that goes into the bundle."""
    root = Path(repo_root)
    if not (root / REPLAY_PATH).is_file():
        raise BundleError(f"replay dataset missing: {REPLAY_PATH}. Generate it with: {GENERATE_HINT}")
    files = [REPLAY_PATH]
    for entry in BUNDLE_PATHS:
        path = root / entry
        if path.is_file():
            files.append(entry)
        elif path.is_dir():
            files.extend(
                p.relative_to(root).as_posix()
                for p in path.rglob("*")
                if p.is_file() and not SKIPPED_NAMES.intersection(p.relative_to(root).parts) and p.suffix != ".pyc"
            )
        else:
            raise BundleError(f"required file missing from the repository: {entry}")
    secrets = [f for f in files if _looks_secret(Path(f).name)]
    if secrets:
        raise BundleError(f"refusing to ship files that look like secrets: {', '.join(secrets)}")
    return sorted(files)


def _looks_secret(name: str) -> bool:
    return name == ".env" or name.startswith(".env.") or name.endswith(SECRET_SUFFIXES)


def build_bundle(repo_root: Path | str) -> bytes:
    """tar.gz of bundle_files(), reproducible."""
    root = Path(repo_root)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in bundle_files(root):
            info = tarfile.TarInfo(name)  # regular file with the content, even if the source is a symlink
            info.size = (root / name).stat().st_size
            info.uname = info.gname = "root"
            info.mode = 0o644
            with (root / name).open("rb") as f:
                tar.addfile(info, f)
    return gzip.compress(raw.getvalue(), mtime=0)
