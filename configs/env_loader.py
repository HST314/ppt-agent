"""Minimal .env loader for local demos."""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path, *, override: bool = False) -> None:
    """Load simple KEY=VALUE pairs from a dotenv file into ``os.environ``.

    The project does not require python-dotenv, so this intentionally supports
    only the common local-demo form: blank lines, comments, optional ``export``,
    and quoted or unquoted values.
    """

    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
