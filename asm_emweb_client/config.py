from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

ENV_HOST = "SMGR_BASE_URL"
ENV_USER = "SMGR_USER"
ENV_PASSWORD = "SMGR_PASSWORD"

SETTING_LABELS = {
    "host": ENV_HOST,
    "user": ENV_USER,
    "password": ENV_PASSWORD,
}

FLAG_LABELS = {
    "host": "--base-url",
    "user": "--user",
    "password": "--password",
}

SETTING_DISPLAY = {
    "host": "base URL",
    "user": "user",
    "password": "password",
}


class ConfigError(Exception):
    """Raised when a required setting cannot be resolved from any source."""


@dataclass(frozen=True)
class Config:
    host: str
    user: str
    password: str


def resolve_config(
    cli: Optional[Mapping[str, Optional[str]]] = None,
    env: Optional[Mapping[str, str]] = None,
    dotenv_values: Optional[Mapping[str, Optional[str]]] = None,
) -> Config:
    """Resolve host/user/password with precedence CLI flag > env > dotenv.

    ``cli`` values that are None or empty are treated as not provided. Missing
    values raise :class:`ConfigError` naming the setting and its env/flag names.
    """
    cli = cli or {}
    env = env if env is not None else os.environ
    dotenv_values = dotenv_values or {}

    resolved: dict = {}
    for key in ("host", "user", "password"):
        flag = (cli.get(key) or "").strip()
        env_var = (env.get(SETTING_LABELS[key]) or "").strip()
        dotenv_var = (dotenv_values.get(SETTING_LABELS[key]) or "").strip()

        if flag:
            resolved[key] = flag
        elif env_var:
            resolved[key] = env_var
        elif dotenv_var:
            resolved[key] = dotenv_var
        else:
            raise ConfigError(
                f"Missing required setting: {SETTING_DISPLAY[key]}. "
                f"Provide {FLAG_LABELS[key]} or set {SETTING_LABELS[key]}"
            )

    return Config(host=resolved["host"], user=resolved["user"], password=resolved["password"])