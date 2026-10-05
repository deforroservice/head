"""Endpoints and credential handling for the EUDR Information System (TRACES NT)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

SERVICE_PATH = "/tracesnt/ws/EUDRDueDiligenceStatementServiceV3"

ENDPOINTS = {
    "production": "https://eudr.webcloud.ec.europa.eu" + SERVICE_PATH,
    "acceptance": "https://acceptance.eudr.webcloud.ec.europa.eu" + SERVICE_PATH,
    "mock": "http://127.0.0.1:8085" + SERVICE_PATH,
}

# Credentials the local mock server accepts out of the box.
MOCK_USERNAME = "sandbox"
MOCK_AUTH_KEY = "sandbox-auth-key"
MOCK_CLIENT_ID = "eudr-test"


@dataclass(frozen=True)
class Credentials:
    """Web Services Access credentials from the operator's TRACES profile."""

    username: str
    auth_key: str = field(repr=False)
    client_id: str


class ConfigError(Exception):
    pass


def credentials_from_env(environment: str, profile: str | None = None) -> Credentials:
    """Read credentials from environment variables.

    Variables are ``TRACES_USERNAME``, ``TRACES_AUTH_KEY`` and ``TRACES_CLIENT_ID``.
    With a profile (one per DEFORRO client / operator) they are prefixed, e.g.
    ``ACME_TRACES_USERNAME``. The mock environment falls back to sandbox defaults.
    """
    prefix = f"{profile.upper().replace('-', '_')}_" if profile else ""
    username = os.environ.get(prefix + "TRACES_USERNAME")
    auth_key = os.environ.get(prefix + "TRACES_AUTH_KEY")
    client_id = os.environ.get(prefix + "TRACES_CLIENT_ID")
    if environment == "mock":
        return Credentials(
            username or MOCK_USERNAME, auth_key or MOCK_AUTH_KEY, client_id or MOCK_CLIENT_ID
        )
    missing = [
        name
        for name, value in (
            ("TRACES_USERNAME", username),
            ("TRACES_AUTH_KEY", auth_key),
            ("TRACES_CLIENT_ID", client_id),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            "missing credentials for %s: set %s"
            % (environment, ", ".join(prefix + name for name in missing))
        )
    return Credentials(username, auth_key, client_id)  # type: ignore[arg-type]


def resolve_endpoint(environment: str, override: str | None = None) -> str:
    if override:
        return override
    try:
        return ENDPOINTS[environment]
    except KeyError:
        raise ConfigError(f"unknown environment {environment!r}") from None
