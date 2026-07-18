from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import inspect
import logging
import uuid

logger = logging.getLogger("hermes.relay")

import json

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .apns import PushResult, create_apns_client
from .config import Settings
from .database import Database
from .hermes_adapter import build_hermes_adapter
from .models import Conversation, HermesHost, Message, PushRegistration
from .pairing import HostSetupCodePayload, format_phone_pairing_code, build_host_setup_code
from .rate_limit import PhonePairingRateLimiter
from .schemas import (
    ConnectorSetupRequest,
    DeviceRegisterRequest,
    DeviceAppStateRequest,
    HostEnrollmentCodeCreateRequest,
    HostRedeemRequest,
    InboxActionRequest,
    InternalInboxCreateRequest,
    MessageCreateRequest,
    PairingRedeemRequest,
    PhonePairingRedeemRequest,
    SensorHealthRequest,
    SensorLocationRequest,
    PushRegisterRequest,
    RefreshRequest,
    VoiceTurnCreateRequest,
)
from .security import AuthContext, get_auth_context, get_db, get_settings, require_internal_key
from .services import (
    activate_hermes_host_connection,
    append_message,
    archive_current_conversation,
    authenticate_hermes_host,
    build_connector_websocket_url,
    claim_next_message_job,
    complete_message_job,
    conversation_history_before_message,
    create_phone_pairing_code,
    create_voice_session,
    create_host_enrollment_invite,
    create_inbox_item,
    create_message_job,
    current_hermes_host_for_user,
    active_push_registrations_for_user,
    deactivate_hermes_host_connection,
    device_is_foreground,
    end_voice_session,
    ensure_default_user,
    fail_message_job,
    get_inbox_item_for_user,
    get_message_job,
    get_message_job_for_user_message,
    get_or_create_current_conversation,
    get_voice_session,
    inject_voice_transcript,
    get_user_message_by_client_message_id,
    hermes_host_is_online,
    list_conversation_messages,
    list_inbox_actions,
    list_inbox_items,
    list_message_jobs_for_conversation,
    record_audit,
    record_inbox_action,
    redeem_phone_pairing_code,
    redeem_host_enrollment_invite,
    redeem_pairing_invite,
    refresh_auth_session,
    revoke_auth_session,
    revoke_current_hermes_host,
    rotate_auth_session,
    serialize_conversation,
    serialize_hermes_host,
    serialize_inbox_item,
    serialize_message,
    serialize_voice_session,
    serialize_voice_turn,
    setup_connector_account,
    touch_hermes_host_connection,
    update_device_app_state,
    upsert_device,
    upsert_push_registration,
    mark_voice_session_started,
    process_message_job_with_adapter,
    record_voice_turn,
)
from .talk_mcp import register_talk_mcp_routes


def success(data: dict) -> dict:
    return {
        "data": data,
        "meta": {
            "requestId": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    }


def success_response(data: dict, *, status_code: int = status.HTTP_200_OK) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=jsonable_encoder(success(data)))


def parse_bearer_token(authorization_header: str | None) -> str | None:
    if authorization_header is None:
        return None
    prefix = "Bearer "
    if not authorization_header.startswith(prefix):
        return None
    return authorization_header[len(prefix) :].strip() or None


def reply_state_for_job(job_status: str) -> str:
    if job_status == "completed":
        return "delivered"
    if job_status == "failed":
        return "failed"
    return "pending"


@dataclass
class ConnectorSession:
    websocket: WebSocket
    host_id: str
    connection_nonce: str
    busy: bool = False


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    if settings.internal_api_key == "replace-me":
        if settings.environment not in ("development", "test"):
            raise RuntimeError(
                "INTERNAL_API_KEY is set to the default 'replace-me'. "
                "Set a strong random value via the INTERNAL_API_KEY env var before "
                "running in production. This is a security requirement."
            )
        logger.warning(
            "SECURITY: INTERNAL_API_KEY is set to the default 'replace-me'. "
            "This is acceptable in development but must be changed for production."
        )

    database = Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.create_all()
        app.state.apns_client = create_apns_client(settings)

        async def _cleanup_stale_job_queues():
            """Periodically remove event queues for completed/failed jobs."""
            while True:
                await asyncio.sleep(60)
                try:
                    stale_ids = []
                    with database.session() as db:
                        for job_id in list(app.state.job_event_queues.keys()):
                            job = get_message_job(db, job_id=job_id)
                            if job is None or job.status in ("completed", "failed"):
                                stale_ids.append(job_id)
                    for job_id in stale_ids:
                        app.state.job_event_queues.pop(job_id, None)
                        app.state.job_event_buffers.pop(job_id, None)
                    if stale_ids:
                        logger.info("Cleaned up %d stale job event queues", len(stale_ids))
                except Exception:
                    logger.warning("Error in job queue cleanup", exc_info=True)

        cleanup_task = asyncio.create_task(_cleanup_stale_job_queues())
        try:
            yield
        finally:
            cleanup_task.cancel()
            if app.state.apns_client:
                close_method = getattr(app.state.apns_client, "close", None)
                if callable(close_method):
                    close_result = close_method()
                    if inspect.isawaitable(close_result):
                        await close_result

    app = FastAPI(title=settings.service_name, version=settings.version, lifespan=lifespan)
    app.state.settings = settings
    app.state.database = database
    app.state.hermes_adapter = build_hermes_adapter(settings)
    app.state.phone_pairing_rate_limiter = PhonePairingRateLimiter(
        max_attempts=settings.phone_pairing_max_attempts_per_ip,
        window_seconds=settings.phone_pairing_rate_limit_window_seconds,
    )
    app.state.connector_sessions: dict[str, ConnectorSession] = {}
    app.state.sensor_delivery_waiters: dict[str, asyncio.Future[bool]] = {}
    app.state.connector_rpc_waiters: dict[str, asyncio.Future[dict]] = {}
    app.state.job_event_queues: dict[str, list[asyncio.Queue]] = {}
    app.state.job_event_buffers: dict[str, list[dict]] = {}

    def subscribe_job_events(job_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1024)
        app.state.job_event_queues.setdefault(job_id, []).append(queue)
        # Replay any events that were buffered before this subscriber connected.
        for event in app.state.job_event_buffers.pop(job_id, []):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                break
        return queue

    def unsubscribe_job_events(job_id: str, queue: asyncio.Queue) -> None:
        queues = app.state.job_event_queues.get(job_id, [])
        if queue in queues:
            queues.remove(queue)
        if not queues:
            app.state.job_event_queues.pop(job_id, None)

    def ensure_job_event_buffer(job_id: str) -> None:
        """Pre-create the event queue list so events are buffered even before SSE subscribes."""
        app.state.job_event_queues.setdefault(job_id, [])

    def publish_job_event(job_id: str, event: dict) -> None:
        queues = app.state.job_event_queues.get(job_id, [])
        if not queues:
            # No subscribers yet — buffer the event on a replay list so late
            # SSE subscribers can catch up.
            buffer = app.state.job_event_buffers.setdefault(job_id, [])
            if len(buffer) < 500:  # cap buffer to prevent unbounded memory growth
                buffer.append(event)
            return
        for queue in queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "SSE event queue full for job %s, dropping %s event",
                    job_id,
                    event.get("event", "unknown"),
                )

    def require_connector_host(
        authorization: str | None = Header(default=None),
        db: Session = Depends(get_db),
    ) -> HermesHost:
        connector_token = parse_bearer_token(authorization)
        if connector_token is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing connector credential.")
        return authenticate_hermes_host(db, connector_token=connector_token)

    async def wait_for_job_completion(job_id: str, timeout_seconds: int) -> object | None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            with database.session() as db:
                job = get_message_job(db, job_id=job_id)
                if job is None or job.status in {"completed", "failed"}:
                    return job
            await asyncio.sleep(0.25)
        return None

    def connector_session_for_user(user_id: str) -> ConnectorSession | None:
        return app.state.connector_sessions.get(user_id)

    def set_connector_session(user_id: str, session: ConnectorSession) -> None:
        app.state.connector_sessions[user_id] = session

    def clear_connector_session(user_id: str | None, connection_nonce: str | None) -> None:
        if user_id is None or connection_nonce is None:
            return
        session = connector_session_for_user(user_id)
        if session is None or session.connection_nonce != connection_nonce:
            return
        app.state.connector_sessions.pop(user_id, None)

    async def maybe_send_message_push(
        *,
        db: Session,
        user_id: str,
        conversation_id: str,
        message_id: str,
        message_text: str,
    ) -> None:
        apns_client = app.state.apns_client
        if apns_client is None:
            return

        preview = " ".join(message_text.split()).strip()
        if not preview:
            return
        preview = preview[:160]

        for device, registration in active_push_registrations_for_user(db, user_id=user_id):
            if device_is_foreground(device, stale_seconds=settings.app_presence_stale_seconds):
                continue

            result = await apns_client.send_alert_push(
                registration.apns_token,
                title="Hermes",
                body=preview,
                bundle_id=registration.bundle_id,
                environment=registration.push_environment,
                user_info={
                    "conversationId": conversation_id,
                    "messageId": message_id,
                },
            )
            if result == PushResult.TOKEN_INVALID:
                registration.is_active = False
                db.commit()
                logger.info("Deactivated invalid APNs token for device %s", device.id)
            elif result != PushResult.SENT:
                logger.warning("APNs delivery %s for device %s", result.value, device.id)

    def resolve_sensor_delivery(delivery_id: str | None, *, delivered: bool) -> None:
        if delivery_id is None:
            return
        waiter = app.state.sensor_delivery_waiters.pop(delivery_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(delivered)

    def resolve_connector_rpc_response(
        request_id: str | None,
        *,
        success: bool,
        result: dict | None = None,
        error: str | None = None,
    ) -> None:
        if request_id is None:
            return
        waiter = app.state.connector_rpc_waiters.pop(request_id, None)
        if waiter is None or waiter.done():
            return
        if success:
            waiter.set_result(result or {})
        else:
            waiter.set_exception(RuntimeError(error or "Connector RPC failed."))

    async def send_connector_rpc(
        user_id: str,
        *,
        method: str,
        params: dict | None = None,
        timeout_seconds: float | None = None,
    ) -> dict:
        session = connector_session_for_user(user_id)
        if session is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Hermes host is offline.")

        request_id = str(uuid.uuid4())
        waiter: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        app.state.connector_rpc_waiters[request_id] = waiter

        try:
            await session.websocket.send_json(
                {
                    "type": "rpc.request",
                    "version": 1,
                    "requestId": request_id,
                    "method": method,
                    "params": params or {},
                }
            )
        except Exception as error:
            clear_connector_session(user_id, session.connection_nonce)
            app.state.connector_rpc_waiters.pop(request_id, None)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Hermes host is unavailable.") from error

        try:
            return await asyncio.wait_for(waiter, timeout_seconds or settings.connector_rpc_timeout_seconds)
        except asyncio.TimeoutError as error:
            app.state.connector_rpc_waiters.pop(request_id, None)
            raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Hermes host did not respond in time.") from error

    async def forward_sensor_payload(
        *,
        user_id: str,
        payload: dict,
        ack_timeout_seconds: float,
    ) -> JSONResponse:
        session = connector_session_for_user(user_id)
        if session is None or session.busy:
            return success_response({"deliveryState": "retry"}, status_code=status.HTTP_202_ACCEPTED)

        delivery_id = str(uuid.uuid4())
        message = dict(payload)
        message["deliveryId"] = delivery_id
        waiter: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        app.state.sensor_delivery_waiters[delivery_id] = waiter

        try:
            await session.websocket.send_json(message)
        except Exception:
            clear_connector_session(user_id, session.connection_nonce)
            app.state.sensor_delivery_waiters.pop(delivery_id, None)
            return success_response({"deliveryState": "retry"}, status_code=status.HTTP_202_ACCEPTED)

        try:
            delivered = await asyncio.wait_for(waiter, timeout=ack_timeout_seconds)
        except asyncio.TimeoutError:
            app.state.sensor_delivery_waiters.pop(delivery_id, None)
            return success_response({"deliveryState": "retry"}, status_code=status.HTTP_202_ACCEPTED)

        status_code = status.HTTP_200_OK if delivered else status.HTTP_202_ACCEPTED
        delivery_state = "delivered" if delivered else "retry"
        return success_response({"deliveryState": delivery_state}, status_code=status_code)

    app.state.send_connector_rpc = send_connector_rpc
    register_talk_mcp_routes(app)

    def build_message_response_payload(db: Session, *, conversation_id: str, job_id: str) -> tuple[dict, int]:
        job = get_message_job(db, job_id=job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Message job not found.")

        conversation = db.get(Conversation, conversation_id)
        if conversation is None:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Conversation not found.")

        user_message = db.get(Message, job.user_message_id)
        messages = list_conversation_messages(db, conversation_id=conversation_id)
        jobs = list_message_jobs_for_conversation(db, conversation_id=conversation_id)

        payload = {
            "replyState": reply_state_for_job(job.status),
            "jobId": job.id,
            "conversation": serialize_conversation(conversation, messages, jobs=jobs),
            "userMessage": serialize_message(user_message, job=job) if user_message else None,
        }

        if job.usage_data:
            payload["usage"] = job.usage_data

        if job.diff_data:
            payload["diff"] = job.diff_data

        if job.result_message_id:
            result_message = db.get(Message, job.result_message_id)
            if result_message is not None:
                payload["message"] = serialize_message(result_message, job=job)

        status_code = status.HTTP_202_ACCEPTED if job.status in {"queued", "running"} else status.HTTP_200_OK
        return payload, status_code

    def strip_current_job_result_from_pending_payload(payload_data: dict, *, job_id: str) -> None:
        payload_data.pop("message", None)
        conversation_payload = payload_data.get("conversation")
        if not isinstance(conversation_payload, dict):
            return

        messages = conversation_payload.get("messages")
        if not isinstance(messages, list):
            return

        conversation_payload["messages"] = [
            message
            for message in messages
            if not (message.get("jobId") == job_id and message.get("role") != "user")
        ]

    _ROLE_MAP = {
        "voice_user": "user",
        "voice_hermes": "hermes",
    }

    def _normalize_role(role: str) -> str:
        """Map voice transcript roles to standard roles for the connector."""
        return _ROLE_MAP.get(role, role)

    def build_job_execute_payload(db: Session, *, job_id: str) -> dict:
        job = get_message_job(db, job_id=job_id)
        if job is None:
            raise RuntimeError("Message job not found.")

        user_message = db.get(Message, job.user_message_id)
        if user_message is None:
            raise RuntimeError("User message not found for job.")

        history = conversation_history_before_message(
            db,
            conversation_id=job.conversation_id,
            message_id=user_message.id,
        )

        # Extract voice transcript messages so they can be injected into the
        # Hermes agent's context even when it uses its own session history.
        voice_transcript_lines: list[str] = []
        regular_history: list[dict] = []
        for message in history:
            if message.source == "voice_transcript" and message.role != "system":
                speaker = "User" if message.role in ("voice_user", "user") else "Hermes"
                voice_transcript_lines.append(f"{speaker}: {message.text}")
            else:
                regular_history.append({
                    "role": _normalize_role(message.role),
                    "text": message.text,
                })

        job_data: dict = {
            "id": job.id,
            "conversationId": job.conversation_id,
            "latestUserMessage": user_message.text,
            "history": regular_history,
            "sessionId": job.session_id_snapshot,
            "timeoutSeconds": settings.connector_job_lease_seconds,
        }
        if voice_transcript_lines:
            job_data["voiceTranscriptContext"] = "\n".join(voice_transcript_lines)
        if user_message.attachments_data:
            job_data["attachments"] = user_message.attachments_data
        return {
            "type": "job.execute",
            "version": 1,
            "job": job_data,
        }

    @app.get("/v1/health")
    def health() -> dict:
        return success({"status": "ok"})

    @app.get("/v1/version")
    def version(request_settings: Settings = Depends(get_settings)) -> dict:
        return success(
            {
                "service": request_settings.service_name,
                "version": request_settings.version,
                "environment": request_settings.environment,
            }
        )

    @app.post("/v1/connector/setup")
    def connector_setup(
        payload: ConnectorSetupRequest,
        db: Session = Depends(get_db),
        request_settings: Settings = Depends(get_settings),
    ) -> dict:
        # If a setup secret is configured, require it. Open access in dev when unset.
        if request_settings.connector_setup_secret:
            if payload.installationSecret != request_settings.connector_setup_secret:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or missing installation secret.")

        user, host, connector_token = setup_connector_account(
            db,
            settings=request_settings,
            platform=payload.connector.platform,
            hostname=payload.connector.hostname,
            hermes_command=payload.connector.hermesCommand,
            hermes_version=payload.connector.hermesVersion,
            connector_version=payload.connector.connectorVersion,
        )
        record_audit(
            db,
            actor_type="connector",
            actor_id=host.id,
            action="connector.setup",
            entity_type="hermes_host",
            entity_id=host.id,
            payload={"userId": user.id},
        )
        db.commit()
        return success(
            {
                "user": {
                    "id": user.id,
                    "displayName": user.display_name,
                },
                "host": {
                    "id": host.id,
                    "userId": host.user_id,
                },
                "connectorCredential": connector_token,
                "webSocketURL": build_connector_websocket_url(request_settings.public_base_url),
                "relayURL": request_settings.public_base_url,
            }
        )

    @app.post("/v1/connector/phone-pairing-codes")
    def create_phone_pairing(
        host: HermesHost = Depends(require_connector_host),
        db: Session = Depends(get_db),
        request_settings: Settings = Depends(get_settings),
    ) -> dict:
        pairing_code, normalized_code = create_phone_pairing_code(
            db,
            settings=request_settings,
            host=host,
        )
        display_code = format_phone_pairing_code(normalized_code)
        record_audit(
            db,
            actor_type="connector",
            actor_id=host.id,
            action="phone_pairing_code.create",
            entity_type="phone_pairing_code",
            entity_id=pairing_code.id,
            payload={"displayCode": display_code},
        )
        db.commit()
        return success(
            {
                "code": normalized_code,
                "displayCode": display_code,
                "expiresAt": pairing_code.expires_at,
            }
        )

    @app.post("/v1/phone-pairing/redeem")
    def redeem_phone_pairing(
        payload: PhonePairingRedeemRequest,
        request: Request,
        db: Session = Depends(get_db),
        request_settings: Settings = Depends(get_settings),
    ) -> dict:
        client_host = request.client.host if request.client else ""
        rate_limiter: PhonePairingRateLimiter = request.app.state.phone_pairing_rate_limiter
        if rate_limiter.is_limited(client_host):
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many pairing attempts. Try again later.")

        try:
            pairing_code, user, device, auth_session, access_token, refresh_token = redeem_phone_pairing_code(
                db,
                settings=request_settings,
                raw_code=payload.code,
                platform=payload.device.platform,
                installation_id=str(payload.device.installationId),
                device_name=payload.device.deviceName,
                device_model=payload.device.deviceModel,
                system_version=payload.device.systemVersion,
                app_version=payload.device.appVersion,
                build_number=payload.device.buildNumber,
                bundle_id=payload.device.bundleId,
                environment=payload.client.environment,
            )
        except HTTPException as error:
            rate_limiter.register_failure(client_host)
            raise error

        record_audit(
            db,
            actor_type="app",
            actor_id=device.id,
            action="phone_pairing.redeem",
            entity_type="phone_pairing_code",
            entity_id=pairing_code.id,
            payload={"installationId": str(payload.device.installationId)},
        )
        db.commit()

        return success(
            {
                "user": {
                    "id": user.id,
                    "displayName": user.display_name,
                },
                "deviceId": device.id,
                "deviceRegistered": True,
                "session": {
                    "connectionStatus": "connected",
                    "isMockMode": False,
                    "backendEndpoint": request_settings.public_base_url,
                    "lastSyncAt": auth_session.updated_at,
                },
                "auth": {
                    "accessToken": access_token,
                    "refreshToken": refresh_token,
                    "expiresAt": auth_session.access_expires_at,
                },
            }
        )

    # ── Sensor data forwarding ─────────────────────────────────

    @app.post("/v1/device/sensor/location")
    async def sensor_location(
        payload: SensorLocationRequest,
        request_settings: Settings = Depends(get_settings),
        auth: AuthContext = Depends(get_auth_context),
    ) -> JSONResponse:
        return await forward_sensor_payload(
            user_id=auth.user.id,
            ack_timeout_seconds=request_settings.connector_sensor_ack_timeout_seconds,
            payload={
                "type": "sensor.location",
                "latitude": payload.latitude,
                "longitude": payload.longitude,
                "altitude": payload.altitude,
                "accuracy": payload.accuracy,
                "address": payload.address,
                "recordedAt": payload.recordedAt,
            },
        )

    @app.post("/v1/device/sensor/health")
    async def sensor_health(
        payload: SensorHealthRequest,
        request_settings: Settings = Depends(get_settings),
        auth: AuthContext = Depends(get_auth_context),
    ) -> JSONResponse:
        return await forward_sensor_payload(
            user_id=auth.user.id,
            ack_timeout_seconds=request_settings.connector_sensor_ack_timeout_seconds,
            payload={
                "type": "sensor.health",
                "samples": [
                    {
                        "metric": s.metric,
                        "value": s.value,
                        "unit": s.unit,
                        "startAt": s.startAt,
                        "endAt": s.endAt,
                    }
                    for s in payload.samples
                ],
            },
        )

    def _meta() -> dict:
        return {"requestId": str(uuid.uuid4()), "timestamp": datetime.now(timezone.utc).isoformat()}

    @app.post("/v1/device/register")
    def register_device(
        payload: DeviceRegisterRequest,
        db: Session = Depends(get_db),
        request_settings: Settings = Depends(get_settings),
    ) -> dict:
        user = ensure_default_user(db, request_settings)
        device = upsert_device(
            db,
            user=user,
            platform=payload.device.platform,
            installation_id=str(payload.device.installationId),
            device_name=payload.device.deviceName,
            device_model=payload.device.deviceModel,
            system_version=payload.device.systemVersion,
            app_version=payload.device.appVersion,
            build_number=payload.device.buildNumber,
            bundle_id=payload.device.bundleId,
            environment=payload.client.environment,
        )
        auth_session, access_token, refresh_token = rotate_auth_session(
            db,
            settings=request_settings,
            user=user,
            device=device,
        )

        record_audit(
            db,
            actor_type="app",
            actor_id=device.id,
            action="device.register",
            entity_type="device",
            entity_id=device.id,
            payload={"installationId": str(payload.device.installationId)},
        )
        db.commit()

        return success(
            {
                "deviceId": device.id,
                "deviceRegistered": True,
                "session": {
                    "connectionStatus": "connected",
                    "isMockMode": False,
                    "backendEndpoint": request_settings.public_base_url,
                    "lastSyncAt": None,
                },
                "auth": {
                    "accessToken": access_token,
                    "refreshToken": refresh_token,
                    "expiresAt": auth_session.access_expires_at,
                },
            }
        )

    @app.post("/v1/pairing/redeem")
    def redeem_pairing(
        payload: PairingRedeemRequest,
        db: Session = Depends(get_db),
        request_settings: Settings = Depends(get_settings),
    ) -> dict:
        invite, user, device, auth_session, access_token, refresh_token = redeem_pairing_invite(
            db,
            settings=request_settings,
            invite_token=payload.inviteToken,
            display_name=payload.displayName,
            platform=payload.device.platform,
            installation_id=str(payload.device.installationId),
            device_name=payload.device.deviceName,
            device_model=payload.device.deviceModel,
            system_version=payload.device.systemVersion,
            app_version=payload.device.appVersion,
            build_number=payload.device.buildNumber,
            bundle_id=payload.device.bundleId,
            environment=payload.client.environment,
        )

        record_audit(
            db,
            actor_type="app",
            actor_id=device.id,
            action="pairing.redeem",
            entity_type="pairing_invite",
            entity_id=invite.id,
            payload={"installationId": str(payload.device.installationId)},
        )
        db.commit()

        return success(
            {
                "user": {
                    "id": user.id,
                    "displayName": user.display_name,
                },
                "deviceId": device.id,
                "deviceRegistered": True,
                "session": {
                    "connectionStatus": "connected",
                    "isMockMode": False,
                    "backendEndpoint": request_settings.public_base_url,
                    "lastSyncAt": auth_session.updated_at,
                },
                "auth": {
                    "accessToken": access_token,
                    "refreshToken": refresh_token,
                    "expiresAt": auth_session.access_expires_at,
                },
            }
        )

    @app.post("/v1/hosts/enrollment-codes")
    def create_host_enrollment_code(
        payload: HostEnrollmentCodeCreateRequest | None = Body(default=None),
        auth: AuthContext = Depends(get_auth_context),
        db: Session = Depends(get_db),
        request_settings: Settings = Depends(get_settings),
    ) -> dict:
        invite, invite_token = create_host_enrollment_invite(db, settings=request_settings, user_id=auth.user.id)
        setup_code = build_host_setup_code(
            HostSetupCodePayload(
                relay_url=request_settings.public_base_url,
                enrollment_token=invite_token,
                expires_at=invite.expires_at,
            )
        )
        host = current_hermes_host_for_user(db, user_id=auth.user.id)
        record_audit(
            db,
            actor_type="user",
            actor_id=auth.user.id,
            action="host.enrollment_code.create",
            entity_type="host_enrollment_invite",
            entity_id=invite.id,
            payload={"displayName": payload.displayName if payload else None},
        )
        db.commit()
        return success(
            {
                "setupCode": setup_code,
                "expiresAt": invite.expires_at,
                "relayHost": request_settings.public_base_url,
                "host": serialize_hermes_host(db, host=host, settings=request_settings),
            }
        )

    @app.get("/v1/hosts/current")
    def current_host(
        auth: AuthContext = Depends(get_auth_context),
        db: Session = Depends(get_db),
        request_settings: Settings = Depends(get_settings),
    ) -> dict:
        host = current_hermes_host_for_user(db, user_id=auth.user.id)
        return success({"host": serialize_hermes_host(db, host=host, settings=request_settings)})

    @app.get("/v1/commands")
    async def command_catalog(
        auth: AuthContext = Depends(get_auth_context),
    ) -> dict:
        """Return the full slash command catalog from the connected Hermes host.

        Includes built-in gateway commands and installed skill commands.
        The iOS app uses this to populate its slash command autocomplete menu.
        """
        try:
            result = await send_connector_rpc(
                auth.user.id,
                method="commands.catalog",
                timeout_seconds=10.0,
            )
            return success(result)
        except HTTPException:
            # Host offline — return empty catalog, iOS falls back to built-in list
            return success({"commands": [], "skills": []})

    @app.post("/v1/hosts/current/revoke")
    def revoke_current_host(
        auth: AuthContext = Depends(get_auth_context),
        db: Session = Depends(get_db),
    ) -> dict:
        host = revoke_current_hermes_host(db, user_id=auth.user.id)
        record_audit(
            db,
            actor_type="user",
            actor_id=auth.user.id,
            action="host.revoke",
            entity_type="hermes_host",
            entity_id=host.id if host else None,
        )
        db.commit()
        return success({"revoked": host is not None})

    @app.get("/v1/talk/readiness")
    async def talk_readiness(
        auth: AuthContext = Depends(get_auth_context),
        db: Session = Depends(get_db),
        request_settings: Settings = Depends(get_settings),
    ) -> dict:
        host = current_hermes_host_for_user(db, user_id=auth.user.id)
        host_data = serialize_hermes_host(db, host=host, settings=request_settings)
        if host is None:
            return success(
                {
                    "ready": False,
                    "hostOnline": False,
                    "configured": False,
                    "blockedReason": "Connect a Hermes host before starting talk mode.",
                    "host": host_data,
                }
            )
        if not hermes_host_is_online(db, host=host, settings=request_settings):
            return success(
                {
                    "ready": False,
                    "hostOnline": False,
                    "configured": False,
                    "blockedReason": "Your Hermes host is offline.",
                    "host": host_data,
                }
            )

        prewarm = await send_connector_rpc(auth.user.id, method="talk.prewarm")
        blocked_reason = prewarm.get("blockedReason") or prewarm.get("lastValidationError")
        return success(
            {
                "ready": bool(prewarm.get("configured")) and not blocked_reason,
                "hostOnline": True,
                "configured": bool(prewarm.get("configured")),
                "blockedReason": blocked_reason,
                "host": host_data,
                "preferredModels": prewarm.get("preferredModels"),
                "selectedModel": prewarm.get("selectedModel"),
                "voice": prewarm.get("voice"),
                "voiceContextUpdatedAt": prewarm.get("voiceContextUpdatedAt"),
            }
        )

    @app.post("/v1/talk/session")
    async def create_talk_session(
        auth: AuthContext = Depends(get_auth_context),
        db: Session = Depends(get_db),
        request_settings: Settings = Depends(get_settings),
    ) -> JSONResponse:
        host = current_hermes_host_for_user(db, user_id=auth.user.id)
        if host is None or not hermes_host_is_online(db, host=host, settings=request_settings):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Your Hermes host must be online to start talk mode.")

        voice_session, relay_tool_token = create_voice_session(
            db,
            user_id=auth.user.id,
            host_id=host.id,
        )
        relay_mcp_url = f"{request_settings.public_base_url}/talk/mcp?token={relay_tool_token}"

        try:
            bootstrap = await send_connector_rpc(
                auth.user.id,
                method="talk.session.create",
                params={
                    "voiceSessionId": voice_session.id,
                    "relayMcpURL": relay_mcp_url,
                },
                timeout_seconds=request_settings.connector_rpc_timeout_seconds,
            )
        except HTTPException:
            with database.session() as cleanup_db:
                end_voice_session(cleanup_db, voice_session_id=voice_session.id)
            raise
        except RuntimeError as error:
            with database.session() as cleanup_db:
                end_voice_session(cleanup_db, voice_session_id=voice_session.id, last_error=str(error))
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except Exception as error:  # noqa: BLE001
            with database.session() as cleanup_db:
                end_voice_session(cleanup_db, voice_session_id=voice_session.id, last_error=str(error))
            raise

        started = mark_voice_session_started(
            db,
            voice_session_id=voice_session.id,
            realtime_session_id=(bootstrap.get("session") or {}).get("id"),
            realtime_model=bootstrap.get("model"),
            realtime_voice=bootstrap.get("voice"),
        )
        record_audit(
            db,
            actor_type="user",
            actor_id=auth.user.id,
            action="talk.session.create",
            entity_type="voice_session",
            entity_id=voice_session.id,
            payload={"model": bootstrap.get("model"), "voice": bootstrap.get("voice")},
        )
        db.commit()
        return success_response(
            {
                "voiceSession": serialize_voice_session(started or voice_session),
                "bootstrap": {
                    "clientSecret": bootstrap.get("clientSecret"),
                    "expiresAt": bootstrap.get("expiresAt"),
                    "session": bootstrap.get("session") or {},
                    "model": bootstrap.get("model"),
                    "voice": bootstrap.get("voice"),
                },
            }
        )

    @app.post("/v1/talk/session/{voice_session_id}/end")
    async def end_talk_session(
        voice_session_id: str,
        auth: AuthContext = Depends(get_auth_context),
        db: Session = Depends(get_db),
    ) -> dict:
        voice_session = get_voice_session(db, voice_session_id=voice_session_id)
        if voice_session is None or voice_session.user_id != auth.user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Talk session not found.")

        if voice_session.status == "active":
            try:
                await send_connector_rpc(
                    auth.user.id,
                    method="talk.session.end",
                    params={"voiceSessionId": voice_session.id},
                )
            except HTTPException:
                pass
            end_voice_session(db, voice_session_id=voice_session.id)

        record_audit(
            db,
            actor_type="user",
            actor_id=auth.user.id,
            action="talk.session.end",
            entity_type="voice_session",
            entity_id=voice_session.id,
        )
        db.commit()
        return success({"ended": True, "voiceSession": serialize_voice_session(voice_session)})

    @app.post("/v1/talk/session/{voice_session_id}/turns")
    def create_talk_turn(
        voice_session_id: str,
        payload: VoiceTurnCreateRequest,
        auth: AuthContext = Depends(get_auth_context),
        db: Session = Depends(get_db),
    ) -> dict:
        voice_session = get_voice_session(db, voice_session_id=voice_session_id)
        if voice_session is None or voice_session.user_id != auth.user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Talk session not found.")

        turn = record_voice_turn(
            db,
            voice_session_id=voice_session.id,
            client_turn_id=str(payload.clientTurnId) if payload.clientTurnId else None,
            role=payload.role,
            source=payload.source,
            text=payload.text.strip(),
        )
        record_audit(
            db,
            actor_type="user",
            actor_id=auth.user.id,
            action="talk.turn.create",
            entity_type="voice_turn",
            entity_id=turn.id,
            payload={
                "voiceSessionId": voice_session.id,
                "role": payload.role,
                "source": payload.source,
                "clientTurnId": str(payload.clientTurnId) if payload.clientTurnId else None,
            },
        )
        db.commit()
        return success({"turn": serialize_voice_turn(turn)})

    @app.post("/v1/talk/session/{voice_session_id}/inject")
    def inject_talk_transcript(
        voice_session_id: str,
        auth: AuthContext = Depends(get_auth_context),
        db: Session = Depends(get_db),
    ) -> dict:
        """Inject finalized voice turns into the current chat conversation."""
        voice_session = get_voice_session(db, voice_session_id=voice_session_id)
        if voice_session is None or voice_session.user_id != auth.user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Talk session not found.")

        conversation = inject_voice_transcript(
            db,
            voice_session_id=voice_session.id,
            user_id=auth.user.id,
        )

        record_audit(
            db,
            actor_type="user",
            actor_id=auth.user.id,
            action="talk.transcript.inject",
            entity_type="voice_session",
            entity_id=voice_session.id,
        )
        db.commit()

        messages = list_conversation_messages(db, conversation_id=conversation.id)
        return success({
            "conversation": serialize_conversation(conversation=conversation, messages=messages),
        })

    @app.post("/v1/hosts/redeem")
    def redeem_host(
        payload: HostRedeemRequest,
        db: Session = Depends(get_db),
        request_settings: Settings = Depends(get_settings),
    ) -> dict:
        invite, host, connector_token = redeem_host_enrollment_invite(
            db,
            settings=request_settings,
            invite_token=payload.enrollmentToken,
            connector_display_name=payload.displayName,
            platform=payload.connector.platform,
            hostname=payload.connector.hostname,
            hermes_command=payload.connector.hermesCommand,
            hermes_version=payload.connector.hermesVersion,
            connector_version=payload.connector.connectorVersion,
        )
        record_audit(
            db,
            actor_type="connector",
            actor_id=host.id,
            action="host.redeem",
            entity_type="host_enrollment_invite",
            entity_id=invite.id,
            payload={"hostname": payload.connector.hostname},
        )
        db.commit()
        return success(
            {
                "host": {
                    "id": host.id,
                    "userId": host.user_id,
                },
                "connectorCredential": connector_token,
                "webSocketURL": build_connector_websocket_url(request_settings.public_base_url),
                "relayURL": request_settings.public_base_url,
            }
        )

    @app.get("/v1/session")
    def session(
        auth: AuthContext = Depends(get_auth_context),
        db: Session = Depends(get_db),
        request_settings: Settings = Depends(get_settings),
    ) -> dict:
        push_registered = db.scalar(
            select(PushRegistration).where(
                PushRegistration.device_id == auth.device.id,
                PushRegistration.is_active.is_(True),
            )
        )

        return success(
            {
                "user": {
                    "id": auth.user.id,
                    "displayName": auth.user.display_name,
                },
                "device": {
                    "id": auth.device.id,
                    "registered": True,
                },
                "session": {
                    "connectionStatus": "connected",
                    "isMockMode": False,
                    "backendEndpoint": request_settings.public_base_url,
                    "lastSyncAt": auth.device.last_seen_at,
                },
                "push": {
                    "tokenRegistered": push_registered is not None,
                },
            }
        )

    @app.post("/v1/auth/refresh")
    def refresh_auth(
        payload: RefreshRequest,
        db: Session = Depends(get_db),
        request_settings: Settings = Depends(get_settings),
    ) -> dict:
        auth_session, access_token, refresh_token = refresh_auth_session(
            db,
            settings=request_settings,
            refresh_token=payload.refreshToken,
        )
        record_audit(
            db,
            actor_type="app",
            actor_id=auth_session.device_id,
            action="auth.refresh",
            entity_type="auth_session",
            entity_id=auth_session.id,
        )
        db.commit()

        return success(
            {
                "accessToken": access_token,
                "refreshToken": refresh_token,
                "expiresAt": auth_session.access_expires_at,
            }
        )

    @app.post("/v1/auth/revoke")
    def revoke_auth(
        auth: AuthContext = Depends(get_auth_context),
        db: Session = Depends(get_db),
    ) -> dict:
        revoke_auth_session(db, auth_session=auth.auth_session)
        record_audit(
            db,
            actor_type="app",
            actor_id=auth.device.id,
            action="auth.revoke",
            entity_type="auth_session",
            entity_id=auth.auth_session.id,
        )
        db.commit()

        return success({"revoked": True})

    @app.post("/v1/push/register")
    def register_push(
        payload: PushRegisterRequest,
        auth: AuthContext = Depends(get_auth_context),
        db: Session = Depends(get_db),
        settings: Settings = Depends(get_settings),
    ) -> dict:
        if str(payload.deviceId) != auth.device.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot register push token for another device.")

        registration = upsert_push_registration(
            db,
            device=auth.device,
            apns_token=payload.apnsToken,
            push_environment=settings.apns_environment,  # Override: use relay env, not app-reported
            bundle_id=payload.bundleId,
        )
        record_audit(
            db,
            actor_type="app",
            actor_id=auth.device.id,
            action="push.register",
            entity_type="push_registration",
            entity_id=registration.id,
        )
        db.commit()

        return success(
            {
                "registered": True,
                "updatedAt": registration.updated_at,
            }
        )

    @app.post("/v1/push/deactivate")
    def deactivate_push(
        auth: AuthContext = Depends(get_auth_context),
        db: Session = Depends(get_db),
    ) -> dict:
        """Deactivate push registrations for this device.

        Called when the user disables notifications in-app. Marks all
        active push registrations for the device as inactive so the
        relay stops sending pushes.
        """
        registrations = db.scalars(
            select(PushRegistration).where(
                PushRegistration.device_id == auth.device.id,
                PushRegistration.is_active == True,
            )
        ).all()
        for registration in registrations:
            registration.is_active = False
        if registrations:
            record_audit(
                db,
                actor_type="app",
                actor_id=auth.device.id,
                action="push.deactivate",
                entity_type="push_registration",
                entity_id=registrations[0].id,
            )
            db.commit()

        return success({"deactivated": True, "count": len(registrations)})

    @app.post("/v1/device/app-state")
    def device_app_state(
        payload: DeviceAppStateRequest,
        auth: AuthContext = Depends(get_auth_context),
        db: Session = Depends(get_db),
    ) -> dict:
        device = update_device_app_state(db, device=auth.device, state=payload.state)
        record_audit(
            db,
            actor_type="app",
            actor_id=auth.device.id,
            action="device.app_state",
            entity_type="device",
            entity_id=device.id,
            payload={"state": payload.state},
        )
        db.commit()
        return success(
            {
                "deviceId": device.id,
                "state": device.app_state,
                "updatedAt": device.app_state_updated_at,
            }
        )

    @app.post("/v1/push/send")
    async def send_push(
        request: Request,
        payload: dict = Body(...),
        db: Session = Depends(get_db),
        _internal: None = Depends(require_internal_key),
    ) -> dict:
        """Send a push notification to all active devices for a user.

        Called by the connector (via internal API key) when the agent has
        a proactive message or wants to wake the iOS app.

        Body:
            user_id: str — target user ID
            type: "silent" | "alert" (default: silent)
            title: str (for alert type)
            body: str (for alert type)
        """
        apns_client = request.app.state.apns_client
        if apns_client is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="APNs not configured. Set APNS_KEY_PATH, APNS_KEY_ID, APNS_TEAM_ID.",
            )

        user_id = payload.get("user_id")
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id is required")

        push_type = payload.get("type", "silent")

        from .models import Device
        device_ids = [
            d.id for d in db.scalars(
                select(Device).where(Device.user_id == user_id)
            ).all()
        ]

        registrations = db.scalars(
            select(PushRegistration).where(
                PushRegistration.device_id.in_(device_ids),
                PushRegistration.is_active == True,
            )
        ).all()

        if not registrations:
            return success({"sent": 0, "reason": "no active push registrations"})

        sent = 0
        for reg in registrations:
            if push_type == "alert":
                title = payload.get("title", "Hermes")
                body_text = payload.get("body", "New message from Hermes")
                result = await apns_client.send_alert_push(
                    reg.apns_token,
                    title=title,
                    body=body_text,
                    bundle_id=reg.bundle_id,
                    environment=reg.push_environment,
                )
            else:
                result = await apns_client.send_silent_push(
                    reg.apns_token,
                    bundle_id=reg.bundle_id,
                    environment=reg.push_environment,
                )

            if result == PushResult.SENT:
                sent += 1
            elif result == PushResult.TOKEN_INVALID:
                reg.is_active = False
                db.commit()

        record_audit(
            db,
            actor_type="internal",
            actor_id="relay",
            action="push.send",
            entity_type="user",
            entity_id=user_id,
        )
        db.commit()

        return success({"sent": sent, "total": len(registrations)})

    @app.get("/v1/conversations/current")
    def current_conversation(auth: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db)) -> dict:
        conversation = get_or_create_current_conversation(db, user_id=auth.user.id)
        messages = list_conversation_messages(db, conversation_id=conversation.id)
        jobs = list_message_jobs_for_conversation(db, conversation_id=conversation.id)
        return success({"conversation": serialize_conversation(conversation, messages, jobs=jobs)})

    @app.post("/v1/conversations/current/clear")
    def clear_current_conversation(
        auth: AuthContext = Depends(get_auth_context),
        db: Session = Depends(get_db),
    ) -> dict:
        archive_current_conversation(db, user_id=auth.user.id)
        conversation = get_or_create_current_conversation(db, user_id=auth.user.id)
        messages = list_conversation_messages(db, conversation_id=conversation.id)
        record_audit(
            db,
            actor_type="user",
            actor_id=auth.user.id,
            action="chat.conversation.clear",
            entity_type="conversation",
            entity_id=conversation.id,
        )
        db.commit()
        return success({"conversation": serialize_conversation(conversation, messages)})

    @app.post("/v1/messages")
    async def create_message(
        payload: MessageCreateRequest,
        auth: AuthContext = Depends(get_auth_context),
        db: Session = Depends(get_db),
        request_settings: Settings = Depends(get_settings),
    ) -> JSONResponse:
        client_message_id = str(payload.clientMessageId) if payload.clientMessageId else None
        if client_message_id is not None:
            existing_user_message = get_user_message_by_client_message_id(
                db,
                user_id=auth.user.id,
                client_message_id=client_message_id,
            )
            if existing_user_message is not None:
                existing_job = get_message_job_for_user_message(db, user_message_id=existing_user_message.id)
                if existing_job is not None:
                    payload_data, status_code = build_message_response_payload(
                        db,
                        conversation_id=existing_user_message.conversation_id,
                        job_id=existing_job.id,
                    )
                    record_audit(
                        db,
                        actor_type="user",
                        actor_id=auth.user.id,
                        action="chat.message.create",
                        entity_type="conversation",
                        entity_id=existing_user_message.conversation_id,
                        payload={
                            "jobId": existing_job.id,
                            "replyState": payload_data["replyState"],
                            "deduplicated": True,
                            "clientMessageId": client_message_id,
                        },
                    )
                    db.commit()
                    return success_response(payload_data, status_code=status_code)

        conversation = get_or_create_current_conversation(db, user_id=auth.user.id)
        initial_delivery_status = "pending" if request_settings.hermes_adapter == "connector" else "sent"
        attachments_raw = (
            [att.model_dump() for att in payload.attachments]
            if payload.attachments
            else None
        )
        user_message = append_message(
            db,
            conversation=conversation,
            user_id=auth.user.id,
            role="user",
            text=payload.text,
            client_message_id=client_message_id,
            delivery_status=initial_delivery_status,
            attachments_data=attachments_raw,
        )

        job = create_message_job(
            db,
            user_id=auth.user.id,
            conversation_id=conversation.id,
            user_message_id=user_message.id,
            session_id_snapshot=conversation.hermes_session_id,
        )

        if request_settings.hermes_adapter == "connector":
            # Pre-create the event buffer so streaming events are captured
            # even before the iOS client opens its SSE connection.
            ensure_job_event_buffer(job.id)
            host = current_hermes_host_for_user(db, user_id=auth.user.id)
            if host is not None and hermes_host_is_online(db, host=host, settings=request_settings):
                await wait_for_job_completion(job.id, request_settings.connector_sync_wait_seconds)
        else:
            process_message_job_with_adapter(
                db,
                job_id=job.id,
                adapter=app.state.hermes_adapter,
            )
            db.expire_all()
            completed_job = get_message_job(db, job_id=job.id)
            if completed_job is not None and completed_job.status == "completed" and completed_job.result_message_id:
                result_message = db.get(Message, completed_job.result_message_id)
                if result_message is not None:
                    await maybe_send_message_push(
                        db=db,
                        user_id=auth.user.id,
                        conversation_id=conversation.id,
                        message_id=result_message.id,
                        message_text=result_message.text,
                    )

        db.expire_all()
        payload_data, status_code = build_message_response_payload(db, conversation_id=conversation.id, job_id=job.id)
        # For connector-backed jobs, ALWAYS return "pending" so the iOS client
        # opens an SSE connection for streaming. If we return "delivered" here
        # (because the connector finished before db.expire_all), the client
        # skips SSE and never sees streaming events.
        if request_settings.hermes_adapter == "connector" and payload_data["replyState"] != "failed":
            payload_data["replyState"] = "pending"
            strip_current_job_result_from_pending_payload(payload_data, job_id=job.id)
            status_code = status.HTTP_202_ACCEPTED
        record_audit(
            db,
            actor_type="user",
            actor_id=auth.user.id,
            action="chat.message.create",
            entity_type="conversation",
            entity_id=conversation.id,
            payload={"jobId": job.id, "replyState": payload_data["replyState"]},
        )
        db.commit()
        return success_response(payload_data, status_code=status_code)

    # ------------------------------------------------------------------
    # Job Events SSE
    # ------------------------------------------------------------------

    @app.get("/v1/jobs/{job_id}/events")
    async def job_events_stream(
        job_id: str,
        auth: AuthContext = Depends(get_auth_context),
        db: Session = Depends(get_db),
    ):
        from .models import MessageJob

        job = get_message_job(db, job_id=job_id)
        if job is None or job.user_id != auth.user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

        # Always subscribe first — this drains any buffered events that were
        # published before the SSE connection opened.
        queue = subscribe_job_events(job_id)

        # Build the done event from DB (used if job already completed)
        done_event_data: dict | None = None
        if job.status in ("completed", "failed"):
            done_event_data = {"jobId": job_id, "status": job.status}
            if job.usage_data:
                done_event_data["usage"] = job.usage_data
            if job.diff_data:
                done_event_data["diff"] = job.diff_data
            if job.error_text:
                done_event_data["error"] = job.error_text
            if job.result_message_id:
                result_message = db.get(Message, job.result_message_id)
                if result_message is not None:
                    done_event_data["message"] = serialize_message(result_message, job=job)

        async def event_stream():
            try:
                # First, replay any buffered events (text_delta, tool_activity, etc.)
                while not queue.empty():
                    event = queue.get_nowait()
                    event_type = event.get("event", "progress")
                    data = json.dumps(jsonable_encoder(event.get("data", {})))
                    yield f"event: {event_type}\ndata: {data}\n\n"
                    if event_type == "done":
                        return

                # If job already completed, emit the done event after replaying buffer
                if done_event_data is not None:
                    yield f"event: done\ndata: {json.dumps(jsonable_encoder(done_event_data))}\n\n"
                    return

                # Otherwise, stream live events as they arrive
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=float(settings.sse_keepalive_seconds))
                        event_type = event.get("event", "progress")
                        data = json.dumps(jsonable_encoder(event.get("data", {})))
                        yield f"event: {event_type}\ndata: {data}\n\n"
                        if event_type == "done":
                            break
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                unsubscribe_job_events(job_id, queue)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ------------------------------------------------------------------
    # Inbox
    # ------------------------------------------------------------------

    @app.get("/v1/inbox")
    def inbox(auth: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db)) -> dict:
        items = [serialize_inbox_item(item) for item in list_inbox_items(db, user_id=auth.user.id)]
        return success({"items": items})

    @app.post("/v1/inbox/{item_id}/action")
    def inbox_action(
        item_id: str,
        payload: InboxActionRequest,
        auth: AuthContext = Depends(get_auth_context),
        db: Session = Depends(get_db),
    ) -> dict:
        item = get_inbox_item_for_user(db, item_id=item_id, user_id=auth.user.id)
        action = record_inbox_action(db, item=item, action_id=payload.actionId, actor_type="user")
        record_audit(
            db,
            actor_type="user",
            actor_id=auth.user.id,
            action=f"inbox.{payload.actionId}",
            entity_type="inbox_item",
            entity_id=item.id,
        )
        db.commit()

        return success(
            {
                "itemID": item.id,
                "actionID": action.action_id,
                "status": item.status,
                "completedAt": action.created_at,
            }
        )

    @app.post("/internal/inbox/create", dependencies=[Depends(require_internal_key)])
    def internal_create_inbox(
        payload: InternalInboxCreateRequest,
        db: Session = Depends(get_db),
        request_settings: Settings = Depends(get_settings),
    ) -> dict:
        user = ensure_default_user(db, request_settings)
        target_user_id = str(payload.userId) if payload.userId else user.id
        target_device_id = str(payload.deviceId) if payload.deviceId else None
        item = create_inbox_item(
            db,
            user_id=target_user_id,
            device_id=target_device_id,
            kind=payload.kind,
            title=payload.title,
            body=payload.body,
            priority=payload.priority,
            payload=payload.payload,
            expires_at=payload.expiresAt,
        )
        record_audit(
            db,
            actor_type="hermes",
            action="internal.inbox.create",
            entity_type="inbox_item",
            entity_id=item.id,
        )
        db.commit()

        return success({"item": serialize_inbox_item(item)})

    @app.get("/internal/inbox/{item_id}/actions", dependencies=[Depends(require_internal_key)])
    def internal_inbox_actions(item_id: str, db: Session = Depends(get_db)) -> dict:
        actions = list_inbox_actions(db, item_id=item_id)
        return success(
            {
                "actions": [
                    {
                        "id": action.id,
                        "actionId": action.action_id,
                        "actorType": action.actor_type,
                        "result": action.result,
                        "createdAt": action.created_at,
                    }
                    for action in actions
                ]
            }
        )

    @app.websocket("/v1/hosts/ws")
    async def hosts_websocket(websocket: WebSocket) -> None:
        await websocket.accept()
        authorization = websocket.headers.get("authorization")
        connector_token = parse_bearer_token(authorization)
        if connector_token is None:
            await websocket.close(code=4401)
            return

        try:
            hello_message = await websocket.receive_json()
        except WebSocketDisconnect:
            return

        if hello_message.get("type") != "hello":
            await websocket.close(code=4400)
            return

        connector_info = hello_message.get("connector") or {}
        connection_nonce = str(uuid.uuid4())
        host_id: str | None = None
        user_id: str | None = None

        try:
            with database.session() as db:
                host = authenticate_hermes_host(db, connector_token=connector_token)
                activated_host = activate_hermes_host_connection(
                    db,
                    host=host,
                    connection_nonce=connection_nonce,
                    connector_version=connector_info.get("connectorVersion", "unknown"),
                    platform=connector_info.get("platform", "unknown"),
                    hostname=connector_info.get("hostname", "unknown"),
                    hermes_command=connector_info.get("hermesCommand", "hermes"),
                    hermes_version=connector_info.get("hermesVersion"),
                    hermes_model=connector_info.get("hermesModel"),
                    display_name=connector_info.get("displayName"),
                )
                host_id = activated_host.id
                user_id = activated_host.user_id
                set_connector_session(
                    user_id,
                    ConnectorSession(
                        websocket=websocket,
                        host_id=host_id,
                        connection_nonce=connection_nonce,
                    ),
                )
                ready_payload = {
                    "type": "ready",
                    "version": 1,
                    "host": serialize_hermes_host(db, host=activated_host, settings=settings),
                }

            await websocket.send_json(jsonable_encoder(ready_payload))

            while True:
                with database.session() as db:
                    host = db.get(HermesHost, host_id)
                    if host is None or host.active_connection_nonce != connection_nonce or host.revoked_at is not None:
                        await websocket.close(code=4401)
                        return

                    claimed_job = claim_next_message_job(
                        db,
                        host=host,
                        connection_nonce=connection_nonce,
                        settings=settings,
                    )

                    if claimed_job is not None:
                        job_payload = build_job_execute_payload(db, job_id=claimed_job.id)
                    else:
                        job_payload = None

                if job_payload is not None:
                    session = connector_session_for_user(user_id)
                    if session is not None and session.connection_nonce == connection_nonce:
                        session.busy = True

                    try:
                        await websocket.send_json(job_payload)

                        while True:
                            try:
                                incoming = await asyncio.wait_for(
                                    websocket.receive_json(),
                                    timeout=settings.connector_heartbeat_timeout_seconds,
                                )
                            except asyncio.TimeoutError:
                                await websocket.close(code=1011)
                                return

                            message_type = incoming.get("type")
                            if message_type == "heartbeat":
                                with database.session() as db:
                                    touched = touch_hermes_host_connection(db, host_id=host_id, connection_nonce=connection_nonce)
                                if touched is None:
                                    await websocket.close(code=4401)
                                    return
                                continue

                            if message_type == "sensor.ack":
                                resolve_sensor_delivery(
                                    incoming.get("deliveryId"),
                                    delivered=incoming.get("deliveryState", "delivered") == "delivered",
                                )
                                continue

                            if message_type == "rpc.response":
                                resolve_connector_rpc_response(
                                    incoming.get("requestId"),
                                    success=bool(incoming.get("success", False)),
                                    result=incoming.get("result"),
                                    error=incoming.get("error"),
                                )
                                continue

                            if message_type == "job.progress" and incoming.get("jobId") == claimed_job.id:
                                publish_job_event(claimed_job.id, {
                                    "event": incoming.get("kind", "progress"),
                                    "data": {
                                        "jobId": claimed_job.id,
                                        "kind": incoming.get("kind"),
                                        "delta": incoming.get("delta"),
                                        "label": incoming.get("label"),
                                    },
                                })
                                continue

                            if message_type == "job.result" and incoming.get("jobId") == claimed_job.id:
                                with database.session() as db:
                                    completed = complete_message_job(
                                        db,
                                        job_id=claimed_job.id,
                                        connection_nonce=connection_nonce,
                                        text=incoming.get("text", "").strip(),
                                        session_id=incoming.get("sessionId"),
                                        usage=incoming.get("usage"),
                                        diff=incoming.get("diff"),
                                        attachments=incoming.get("attachments"),
                                    )
                                    if completed is None:
                                        await websocket.close(code=1011)
                                        return
                                    result_message = (
                                        serialize_message(db.get(Message, completed.result_message_id), job=completed)
                                        if completed.result_message_id and db.get(Message, completed.result_message_id) is not None
                                        else None
                                    )
                                    if completed.result_message_id:
                                        completed_message = db.get(Message, completed.result_message_id)
                                        if completed_message is not None:
                                            await maybe_send_message_push(
                                                db=db,
                                                user_id=completed.user_id,
                                                conversation_id=completed.conversation_id,
                                                message_id=completed_message.id,
                                                message_text=completed_message.text,
                                            )
                                done_event_data: dict = {
                                    "jobId": claimed_job.id,
                                    "status": "completed",
                                    "usage": completed.usage_data,
                                    "message": result_message,
                                }
                                if completed.diff_data:
                                    done_event_data["diff"] = completed.diff_data
                                publish_job_event(claimed_job.id, {
                                    "event": "done",
                                    "data": done_event_data,
                                })
                                break

                            if message_type == "job.failed" and incoming.get("jobId") == claimed_job.id:
                                with database.session() as db:
                                    failed = fail_message_job(
                                        db,
                                        job_id=claimed_job.id,
                                        connection_nonce=connection_nonce,
                                        error_text=incoming.get("error", "Hermes connector failed."),
                                        retryable=bool(incoming.get("retryable", False)),
                                    )
                                    if failed is None:
                                        await websocket.close(code=1011)
                                        return
                                    result_message = (
                                        serialize_message(db.get(Message, failed.result_message_id), job=failed)
                                        if failed.result_message_id and db.get(Message, failed.result_message_id) is not None
                                        else None
                                    )
                                if failed.status != "queued":
                                    publish_job_event(claimed_job.id, {
                                        "event": "done",
                                        "data": {
                                            "jobId": claimed_job.id,
                                            "status": "failed",
                                            "error": incoming.get("error"),
                                            "message": result_message,
                                        },
                                    })
                                break

                            await websocket.close(code=4400)
                            return
                    finally:
                        session = connector_session_for_user(user_id)
                        if session is not None and session.connection_nonce == connection_nonce:
                            session.busy = False

                    continue

                try:
                    incoming = await asyncio.wait_for(
                        websocket.receive_json(),
                        timeout=settings.connector_idle_poll_interval_seconds,
                    )
                except asyncio.TimeoutError:
                    continue

                if incoming.get("type") == "heartbeat":
                    with database.session() as db:
                        touched = touch_hermes_host_connection(db, host_id=host_id, connection_nonce=connection_nonce)
                    if touched is None:
                        await websocket.close(code=4401)
                        return
                    continue

                if incoming.get("type") == "sensor.ack":
                    resolve_sensor_delivery(
                        incoming.get("deliveryId"),
                        delivered=incoming.get("deliveryState", "delivered") == "delivered",
                    )
                    with database.session() as db:
                        touched = touch_hermes_host_connection(db, host_id=host_id, connection_nonce=connection_nonce)
                    if touched is None:
                        await websocket.close(code=4401)
                        return
                    continue

                if incoming.get("type") == "rpc.response":
                    resolve_connector_rpc_response(
                        incoming.get("requestId"),
                        success=bool(incoming.get("success", False)),
                        result=incoming.get("result"),
                        error=incoming.get("error"),
                    )
                    with database.session() as db:
                        touched = touch_hermes_host_connection(db, host_id=host_id, connection_nonce=connection_nonce)
                    if touched is None:
                        await websocket.close(code=4401)
                        return
                    continue

                await websocket.close(code=4400)
                return
        except HTTPException:
            await websocket.close(code=4401)
            return
        except WebSocketDisconnect:
            return
        finally:
            if host_id is not None:
                clear_connector_session(user_id, connection_nonce)
                with database.session() as db:
                    deactivate_hermes_host_connection(db, host_id=host_id, connection_nonce=connection_nonce)

    return app


app = create_app()
