"""Application settings.

The project intentionally supports **exactly three** application settings (see
``.env`` / ``.env.example``) and all of them are DeepSeek model settings.
Everything else -- database location, bootstrap behaviour, CORS -- has safe
defaults in code so no extra configuration can drift between environments.

The one other variable the process reads is ``PORT``, and it is not a setting:
``server.py`` uses it to pick the port Uvicorn binds, which is a container
concern rather than application configuration.

The DeepSeek API key is read only inside this process. It must never be sent to
the browser, written to logs, or committed to git.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# backend/app/config.py -> backend/
BACKEND_DIR = Path(__file__).resolve().parent.parent

# The SQLite file lives inside a Docker volume in containers. On the host it
# resolves to backend/data/ which is git-ignored.
DEFAULT_DATABASE_PATH = BACKEND_DIR / "data" / "clinic.db"


@dataclass(frozen=True)
class Settings:
    deepseek_api_key: str
    deepseek_model: str
    deepseek_base_url: str
    database_path: Path

    @property
    def deepseek_configured(self) -> bool:
        """Whether a key is present. The key itself is never returned to callers."""
        return bool(self.deepseek_api_key)

    @property
    def database_url(self) -> str:
        return f"sqlite+pysqlite:///{self.database_path}"


def _read_env_file(path: Path) -> dict[str, str]:
    """Minimal dotenv parser.

    Deliberately does not depend on python-dotenv so containers behave
    identically whether or not a .env file was mounted. Lines that are blank,
    comments, or missing ``=`` are skipped. Surrounding quotes are stripped.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def load_settings() -> Settings:
    # Process environment wins over the .env file, which wins over defaults.
    file_values = _read_env_file(BACKEND_DIR.parent / ".env")

    def get(name: str, default: str = "") -> str:
        if name in os.environ and os.environ[name] != "":
            return os.environ[name].strip()
        return file_values.get(name, default).strip()

    return Settings(
        deepseek_api_key=get("DEEPSEEK_API_KEY"),
        deepseek_model=get("DEEPSEEK_MODEL", "deepseek-flash") or "deepseek-flash",
        deepseek_base_url=get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        or "https://api.deepseek.com",
        database_path=DEFAULT_DATABASE_PATH,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
