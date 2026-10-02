from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


def normalize_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg://", 1)
    return database_url


@dataclass(frozen=True)
class Settings:
    service_name: str = "hermes-mobile-relay"
    version: str = "0.1.0"
    environment: str = "development"
    public_base_url: str = "http://127.0.0.1:8000/v1"
    database_url: str = "sqlite:///./relay.db"
    internal_api_key: str = "replace-me"
    access_token_ttl_seconds: int = 3600
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 30
    pairing_code_ttl_seconds: int = 900
    phone_pairing_code_ttl_seconds: int = 600
    phone_pairing_max_attempts_per_code: int = 5
    phone_pairing_max_attempts_per_ip: int = 5
    phone_pairing_rate_limit_window_seconds: int = 300
    host_enrollment_code_ttl_seconds: int = 900
    default_user_display_name: str = "Hermes User"
    hermes_adapter: str = "mock"
    hermes_command: str = "hermes"
    hermes_workdir: str | None = None
    hermes_provider: str | None = None
    hermes_model: str | None = None
    hermes_toolsets: str | None = None
    hermes_source: str = "tool"
    hermes_history_limit: int = 20
    connector_sync_wait_seconds: int = 0
    connector_job_lease_seconds: int = 180
    connector_heartbeat_timeout_seconds: int = 30
    connector_idle_poll_interval_seconds: float = 1.0
    connector_sensor_ack_timeout_seconds: float = 3.0
    connector_rpc_timeout_seconds: float = 30.0
    talk_delegate_timeout_seconds: float = 90.0
    sse_keepalive_seconds: int = 30
    connector_setup_secret: str | None = None
    allow_open_device_registration: bool = False
    apns_key_path: str | None = None
    apns_key_contents: str | None = None
    apns_key_id: str | None = None
    apns_team_id: str | None = None
    apns_bundle_id: str = "io.hermesmobile.HermesMobile"
    apns_environment: str = "development"
    app_presence_stale_seconds: int = 120

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            environment=os.getenv("RELAY_ENVIRONMENT", "development"),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000/v1"),
            database_url=normalize_database_url(os.getenv("DATABASE_URL", "sqlite:///./relay.db")),
            internal_api_key=os.getenv("INTERNAL_API_KEY", "replace-me"),
            access_token_ttl_seconds=int(os.getenv("ACCESS_TOKEN_TTL_SECONDS", "3600")),
            refresh_token_ttl_seconds=int(os.getenv("REFRESH_TOKEN_TTL_SECONDS", str(60 * 60 * 24 * 30))),
            pairing_code_ttl_seconds=int(os.getenv("PAIRING_CODE_TTL_SECONDS", "900")),
            phone_pairing_code_ttl_seconds=int(os.getenv("PHONE_PAIRING_CODE_TTL_SECONDS", "600")),
            phone_pairing_max_attempts_per_code=int(os.getenv("PHONE_PAIRING_MAX_ATTEMPTS_PER_CODE", "5")),
            phone_pairing_max_attempts_per_ip=int(os.getenv("PHONE_PAIRING_MAX_ATTEMPTS_PER_IP", "5")),
            phone_pairing_rate_limit_window_seconds=int(os.getenv("PHONE_PAIRING_RATE_LIMIT_WINDOW_SECONDS", "300")),
            host_enrollment_code_ttl_seconds=int(os.getenv("HOST_ENROLLMENT_CODE_TTL_SECONDS", "900")),
            default_user_display_name=os.getenv("DEFAULT_USER_DISPLAY_NAME", "Hermes User"),
            hermes_adapter=os.getenv("HERMES_ADAPTER", "mock"),
            hermes_command=os.getenv("HERMES_COMMAND", "hermes"),
            hermes_workdir=os.getenv("HERMES_WORKDIR") or None,
            hermes_provider=os.getenv("HERMES_PROVIDER") or None,
            hermes_model=os.getenv("HERMES_MODEL") or None,
            hermes_toolsets=os.getenv("HERMES_TOOLSETS") or None,
            hermes_source=os.getenv("HERMES_SOURCE", "tool"),
            hermes_history_limit=int(os.getenv("HERMES_HISTORY_LIMIT", "20")),
            connector_sync_wait_seconds=int(os.getenv("CONNECTOR_SYNC_WAIT_SECONDS", "0")),
            connector_job_lease_seconds=int(os.getenv("CONNECTOR_JOB_LEASE_SECONDS", "180")),
            connector_heartbeat_timeout_seconds=int(os.getenv("CONNECTOR_HEARTBEAT_TIMEOUT_SECONDS", "30")),
            connector_idle_poll_interval_seconds=float(os.getenv("CONNECTOR_IDLE_POLL_INTERVAL_SECONDS", "1.0")),
            connector_sensor_ack_timeout_seconds=float(os.getenv("CONNECTOR_SENSOR_ACK_TIMEOUT_SECONDS", "3.0")),
            connector_rpc_timeout_seconds=float(os.getenv("CONNECTOR_RPC_TIMEOUT_SECONDS", "30.0")),
            talk_delegate_timeout_seconds=float(os.getenv("TALK_DELEGATE_TIMEOUT_SECONDS", "90.0")),
            connector_setup_secret=os.getenv("CONNECTOR_SETUP_SECRET") or None,
            allow_open_device_registration=os.getenv("ALLOW_OPEN_DEVICE_REGISTRATION", "").lower() in ("1", "true", "yes"),
            apns_key_path=os.getenv("APNS_KEY_PATH") or None,
            apns_key_contents=os.getenv("APNS_KEY_CONTENTS") or None,
            apns_key_id=os.getenv("APNS_KEY_ID") or None,
            apns_team_id=os.getenv("APNS_TEAM_ID") or None,
            apns_bundle_id=os.getenv("APNS_BUNDLE_ID", "io.hermesmobile.HermesMobile"),
            apns_environment=os.getenv("APNS_ENVIRONMENT", "development"),
            app_presence_stale_seconds=int(os.getenv("APP_PRESENCE_STALE_SECONDS", "120")),
        )
