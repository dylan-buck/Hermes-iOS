"""APNs (Apple Push Notification service) client for sending push notifications.

Uses HTTP/2 via httpx with JWT bearer token authentication.
Requires a .p8 key file from Apple Developer Portal.

Environment variables:
    APNS_KEY_PATH         — path to the .p8 private key file
    APNS_KEY_CONTENTS     — raw .p8 key text (alternative to KEY_PATH for Docker/Fly)
    APNS_KEY_ID           — 10-char key identifier from Apple
    APNS_TEAM_ID          — 10-char team identifier from Apple
    APNS_BUNDLE_ID        — default app bundle identifier (io.hermesmobile.HermesMobile)
    APNS_ENVIRONMENT      — default environment: "development" or "production"
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
import time
from pathlib import Path
import tempfile

import httpx

from .config import Settings

logger = logging.getLogger("hermes.relay.apns")

APNS_DEVELOPMENT_URL = "https://api.development.push.apple.com"
APNS_PRODUCTION_URL = "https://api.push.apple.com"


class PushResult(Enum):
    """Outcome of an APNs push attempt."""
    SENT = "sent"                    # 200 — accepted by APNs
    TOKEN_INVALID = "token_invalid"  # 410 Gone — token is permanently invalid
    REJECTED = "rejected"            # 400/403/etc — request error, but token may be valid
    TRANSIENT = "transient"          # Network error, timeout, or 5xx — retry later


class APNsClient:
    """Sends push notifications to Apple's APNs service.

    The client holds the JWT signing key and can send to any environment/topic
    on a per-call basis. Each push registration stores its own environment and
    bundle ID; the caller passes those through so the right APNs endpoint and
    topic are used.
    """

    def __init__(
        self,
        *,
        key_path: str,
        key_id: str,
        team_id: str,
        default_bundle_id: str = "io.hermesmobile.HermesMobile",
        default_environment: str = "development",
    ):
        self.key_id = key_id
        self.team_id = team_id
        self.default_bundle_id = default_bundle_id
        self.default_environment = default_environment

        key_file = Path(key_path)
        if not key_file.exists():
            raise FileNotFoundError(f"APNs key file not found: {key_path}")
        self._private_key = key_file.read_text().strip()

        self._token: str | None = None
        self._token_issued_at: float = 0
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(http2=True, timeout=30.0)
        return self._client

    def _build_jwt(self) -> str:
        import jwt
        now = int(time.time())
        return jwt.encode(
            {"iss": self.team_id, "iat": now},
            self._private_key,
            algorithm="ES256",
            headers={"alg": "ES256", "kid": self.key_id},
        )

    def _get_token(self) -> str:
        now = time.time()
        if self._token is None or (now - self._token_issued_at) > 3000:
            self._token = self._build_jwt()
            self._token_issued_at = now
        return self._token

    def _base_url_for(self, environment: str | None) -> str:
        env = environment or self.default_environment
        return APNS_PRODUCTION_URL if env == "production" else APNS_DEVELOPMENT_URL

    async def send_silent_push(
        self,
        device_token: str,
        *,
        bundle_id: str | None = None,
        environment: str | None = None,
    ) -> PushResult:
        """Send a silent (content-available) push to wake the app."""
        topic = bundle_id or self.default_bundle_id
        base_url = self._base_url_for(environment)
        url = f"{base_url}/3/device/{device_token}"

        headers = {
            "authorization": f"bearer {self._get_token()}",
            "apns-topic": topic,
            "apns-push-type": "background",
            "apns-priority": "5",
        }
        payload = {"aps": {"content-available": 1}}

        return await self._send(url, headers, payload, device_token)

    async def send_alert_push(
        self,
        device_token: str,
        *,
        title: str,
        body: str,
        category: str | None = None,
        bundle_id: str | None = None,
        environment: str | None = None,
        user_info: dict | None = None,
    ) -> PushResult:
        """Send a visible alert push notification."""
        topic = bundle_id or self.default_bundle_id
        base_url = self._base_url_for(environment)
        url = f"{base_url}/3/device/{device_token}"

        headers = {
            "authorization": f"bearer {self._get_token()}",
            "apns-topic": topic,
            "apns-push-type": "alert",
            "apns-priority": "10",
        }
        aps: dict = {"alert": {"title": title, "body": body}, "sound": "default"}
        if category:
            aps["category"] = category
        payload = {"aps": aps}
        if user_info:
            payload.update(user_info)

        return await self._send(url, headers, payload, device_token)

    async def _send(
        self, url: str, headers: dict, payload: dict, device_token: str
    ) -> PushResult:
        try:
            client = await self._get_client()
            response = await client.post(url, headers=headers, json=payload)

            if response.status_code == 200:
                logger.info("APNs push sent to %s...", device_token[:8])
                return PushResult.SENT

            if response.status_code == 410:
                logger.info("APNs token %s... is permanently invalid (410 Gone)", device_token[:8])
                return PushResult.TOKEN_INVALID

            if response.status_code >= 500:
                logger.warning("APNs server error %d for %s...", response.status_code, device_token[:8])
                return PushResult.TRANSIENT

            logger.warning("APNs push rejected %d for %s...: %s", response.status_code, device_token[:8], response.text)
            return PushResult.REJECTED

        except (httpx.TimeoutException, httpx.ConnectError, OSError) as e:
            logger.error("APNs transient error for %s...: %s", device_token[:8], e)
            return PushResult.TRANSIENT
        except Exception as e:
            logger.error("APNs unexpected error for %s...: %s", device_token[:8], e)
            return PushResult.REJECTED

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()


def create_apns_client(settings: Settings) -> APNsClient | None:
    """Create an APNs client from Settings, or None if not configured."""
    key_id = settings.apns_key_id
    team_id = settings.apns_team_id

    if not key_id or not team_id:
        logger.info("APNs not configured (missing APNS_KEY_ID or APNS_TEAM_ID)")
        return None

    key_path = settings.apns_key_path
    key_contents = settings.apns_key_contents

    if not key_path and not key_contents:
        logger.info("APNs not configured (missing APNS_KEY_PATH or APNS_KEY_CONTENTS)")
        return None

    if not key_path and key_contents:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".p8", delete=False)
        tmp.write(key_contents)
        tmp.close()
        key_path = tmp.name
        logger.info("APNs key loaded from APNS_KEY_CONTENTS")

    try:
        client = APNsClient(
            key_path=key_path,
            key_id=key_id,
            team_id=team_id,
            default_bundle_id=settings.apns_bundle_id,
            default_environment=settings.apns_environment,
        )
        logger.info("APNs client initialized (%s, bundle: %s)", settings.apns_environment, settings.apns_bundle_id)
        return client
    except FileNotFoundError as e:
        logger.warning("APNs key file not found: %s", e)
        return None
    except Exception as e:
        logger.error("APNs client initialization failed: %s", e)
        return None
