"""Profile + credential loading. Nothing personal is hard-coded here."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    pass


@dataclass
class Profile:
    name: str
    sheet_id: str
    max_new_rows: int = 25
    max_age_days: int = 7
    min_fit_score: int = 6
    max_queries: int = 4
    pages_per_query: int = 1


def _profiles_text() -> str:
    text = os.environ.get("PROFILES_YAML", "").strip()
    if text:
        return text
    path = Path(os.environ.get("PROFILES_FILE", "profiles.yaml"))
    if path.exists():
        return path.read_text()
    raise ConfigError(
        "No profiles found. Create profiles.yaml (see profiles.example.yaml) "
        "or set the PROFILES_YAML environment variable."
    )


def load_profiles() -> dict[str, Profile]:
    data = yaml.safe_load(_profiles_text()) or {}
    raw = data.get("profiles", data)
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("profiles mapping is empty or malformed")
    out: dict[str, Profile] = {}
    known = set(Profile.__dataclass_fields__) - {"name"}
    for name, cfg in raw.items():
        if isinstance(cfg, str):
            cfg = {"sheet_id": cfg}
        if not isinstance(cfg, dict) or not cfg.get("sheet_id"):
            raise ConfigError(f"profile {name!r} needs a sheet_id")
        unknown = set(cfg) - known
        if unknown:
            raise ConfigError(f"profile {name!r} has unknown keys: {sorted(unknown)}")
        out[str(name)] = Profile(name=str(name), **cfg)
    return out


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
        "Google credentials missing: set GOOGLE_SERVICE_ACCOUNT_JSON (key contents) "
        "or GOOGLE_SERVICE_ACCOUNT_FILE (path to the key file)."
    )


def require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise ConfigError(f"environment variable {name} is not set")
    return val
