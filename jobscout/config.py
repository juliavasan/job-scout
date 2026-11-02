"""Settings + credentials, all from environment variables (.env locally, GitHub Secrets in CI).

Nothing personal lives in the repo: profiles are the subfolders of your shared
Drive folder, and briefs/results live in those folders.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    pass


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None


@dataclass
class Profile:
    """One person = one subfolder of the root Drive folder."""

    name: str
    folder_id: str = ""
    max_new_rows: int = 25
    max_age_days: int = 7
    min_fit_score: int = 6
    max_queries: int = 4
    pages_per_query: int = 1

    @classmethod
    def from_env(cls, name: str, folder_id: str) -> "Profile":
        return cls(
            name=name,
            folder_id=folder_id,
            max_new_rows=_int_env("JOBSCOUT_MAX_NEW_ROWS", 25),
            max_age_days=_int_env("JOBSCOUT_MAX_AGE_DAYS", 7),
            min_fit_score=_int_env("JOBSCOUT_MIN_FIT_SCORE", 6),
            max_queries=_int_env("JOBSCOUT_MAX_QUERIES", 4),
            pages_per_query=_int_env("JOBSCOUT_PAGES_PER_QUERY", 1),
        )


def root_folder_id() -> str:
    return require_env("JOBSCOUT_ROOT_FOLDER_ID")


def service_account_info() -> dict:
    blob = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if blob:
        try:
            return json.loads(blob)
        except json.JSONDecodeError as e:
            raise ConfigError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON") from e
    path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if path and Path(path).exists():
        return json.loads(Path(path).read_text())
    raise ConfigError(
        "Google credentials missing: set GOOGLE_SERVICE_ACCOUNT_FILE (path to the key file) "
        "or GOOGLE_SERVICE_ACCOUNT_JSON (its contents)."
    )


def require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise ConfigError(f"environment variable {name} is not set")
    return val
