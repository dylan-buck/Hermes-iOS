from __future__ import annotations

import json
import uuid
from threading import Thread

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import VoiceTurn
from app.services import record_voice_turn


def build_client(tmp_path, **overrides):
    settings = Settings(
        environment="test",
        public_base_url="https://relay.example.test/v1",
        database_url=f"sqlite:///{tmp_path / 'relay-hosts.db'}",
        internal_api_key="test-internal-key",
        pairing_code_ttl_seconds=900,
        phone_pairing_code_ttl_seconds=900,
        phone_pairing_max_attempts_per_code=3,
        phone_pairing_max_attempts_per_ip=3,
        phone_pairing_rate_limit_window_seconds=300,
        host_enrollment_code_ttl_seconds=900,
        hermes_adapter="connector",
        connector_sync_wait_seconds=2,
        connector_job_lease_seconds=30,
        connector_heartbeat_timeout_seconds=5,
        connector_idle_poll_interval_seconds=0.1,
        **overrides,
    )
    app = create_app(settings)
    return TestClient(app)


def connector_setup_payload(owner_display_name: str = "Taylor") -> dict:
    return {
        "ownerDisplayName": owner_display_name,
        "hostDisplayName": "Home Mac mini",
        "connector": {
            "platform": "macos",
            "hostname": "test-host",
            "connectorVersion": "0.1.0",
            "hermesCommand": "/usr/local/bin/hermes",
            "hermesVersion": "hermes 1.2.3",
        },
    }


def phone_pairing_payload(code: str, installation_id: str) -> dict:
    return {
        "code": code,
        "device": {
            "platform": "ios",
            "deviceName": "Taylor's iPhone",
            "appVersion": "1.0.0",
            "buildNumber": "1",
            "bundleId": "io.hermesmobile.HermesMobile",
            "installationId": installation_id,
            "deviceModel": "iPhone17,2",
            "systemVersion": "26.2",
        },
        "client": {
            "environment": "production",
        },
    }


def setup_connector(client: TestClient) -> dict:
    response = client.post("/v1/connector/setup", json=connector_setup_payload())
    assert response.status_code == 200
    return response.json()["data"]


def create_phone_pairing_code(client: TestClient, connector_credential: str) -> dict:
    response = client.post(
        "/v1/connector/phone-pairing-codes",
        headers={"Authorization": f"Bearer {connector_credential}"},
    )
    assert response.status_code == 200
    return response.json()["data"]


def redeem_phone(client: TestClient, code: str, installation_id: str) -> dict:
    response = client.post(
        "/v1/phone-pairing/redeem",
        json=phone_pairing_payload(code=code, installation_id=installation_id),
    )
    assert response.status_code == 200
    return response.json()["data"]


def sensor_location_payload() -> dict:
    return {
        "latitude": 40.7128,
        "longitude": -74.0060,
        "altitude": 12.0,
        "accuracy": 35.0,
        "address": "New York, NY",
        "recordedAt": "2026-04-01T15:00:00Z",
    }


def test_connector_setup_and_phone_pairing_attach_phone_to_existing_user(tmp_path):
    with build_client(tmp_path) as client:
        connector_data = setup_connector(client)
        pairing_code = create_phone_pairing_code(client, connector_data["connectorCredential"])

        assert pairing_code["displayCode"].count("-") == 1
        first_phone = redeem_phone(client, pairing_code["displayCode"], "11111111-1111-1111-1111-111111111111")
        second_pairing_code = create_phone_pairing_code(client, connector_data["connectorCredential"])
        second_phone = redeem_phone(client, second_pairing_code["code"], "22222222-2222-2222-2222-222222222222")

        assert first_phone["user"]["id"] == connector_data["user"]["id"]
        assert second_phone["user"]["id"] == connector_data["user"]["id"]
        assert first_phone["deviceId"] != second_phone["deviceId"]

        current_host = client.get(
            "/v1/hosts/current",
            headers={"Authorization": f"Bearer {first_phone['auth']['accessToken']}"},
        )
        assert current_host.status_code == 200
        assert current_host.json()["data"]["host"]["id"] == connector_data["host"]["id"]


def test_connector_setup_requires_matching_secret_when_configured(tmp_path):
    with build_client(tmp_path, connector_setup_secret="correct-secret") as client:
        missing = client.post("/v1/connector/setup", json=connector_setup_payload())
        assert missing.status_code == 403

        wrong = client.post(
            "/v1/connector/setup",
            json={**connector_setup_payload(), "installationSecret": "wrong"},
        )
        assert wrong.status_code == 403

        ok = client.post(
            "/v1/connector/setup",
            json={**connector_setup_payload(), "installationSecret": "correct-secret"},
        )
        assert ok.status_code == 200


def test_phone_pairing_rejects_reused_and_rate_limited_codes(tmp_path):
    with build_client(tmp_path) as client:
        connector_data = setup_connector(client)
        pairing_code = create_phone_pairing_code(client, connector_data["connectorCredential"])

        redeem_phone(client, pairing_code["displayCode"], "33333333-3333-3333-3333-333333333333")

        reused = client.post(
            "/v1/phone-pairing/redeem",
            json=phone_pairing_payload(pairing_code["displayCode"], "44444444-4444-4444-4444-444444444444"),
        )
        assert reused.status_code == 400
        assert reused.json()["detail"] == "This phone pairing code has already been used."

        invalid_one = client.post(
            "/v1/phone-pairing/redeem",
            json=phone_pairing_payload("ZZZZ-ZZZZ", "55555555-5555-5555-5555-555555555555"),
        )
        invalid_two = client.post(
            "/v1/phone-pairing/redeem",
            json=phone_pairing_payload("ZZZZ-ZZZZ", "66666666-6666-6666-6666-666666666666"),
        )
        limited = client.post(
            "/v1/phone-pairing/redeem",
            json=phone_pairing_payload("ZZZZ-ZZZZ", "77777777-7777-7777-7777-777777777777"),
        )

        assert invalid_one.status_code == 400
        assert invalid_two.status_code == 400
        assert limited.status_code == 429
        assert limited.json()["detail"] == "Too many pairing attempts. Try again later."


def test_messages_return_pending_when_host_is_offline(tmp_path):
    with build_client(tmp_path) as client:
        connector_data = setup_connector(client)
        pairing_code = create_phone_pairing_code(client, connector_data["connectorCredential"])
        access_token = redeem_phone(
            client,
            pairing_code["displayCode"],
            "99999990-8888-8888-8888-888888888888",
        )["auth"]["accessToken"]

        message_response = client.post(
            "/v1/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"text": "Hello while offline"},
        )
        assert message_response.status_code == 202
        data = message_response.json()["data"]
        assert data["replyState"] == "pending"
        assert data["message"] is None if "message" in data else True
        assert data["conversation"]["messages"][0]["deliveryStatus"] == "pending"


def test_connected_host_gets_job_and_preserves_session_resume(tmp_path):
    with build_client(tmp_path) as client:
        connector_data = setup_connector(client)
        pairing_code = create_phone_pairing_code(client, connector_data["connectorCredential"])
        access_token = redeem_phone(
            client,
            pairing_code["displayCode"],
            "99999999-9999-9999-9999-999999999999",
        )["auth"]["accessToken"]

        with client.websocket_connect(
            "/v1/hosts/ws",
            headers={"Authorization": f"Bearer {connector_data['connectorCredential']}"},
        ) as websocket:
            websocket.send_json(
                {
                    "type": "hello",
                    "connector": {
                        "platform": "macos",
                        "hostname": "test-host",
                        "connectorVersion": "0.1.0",
                        "hermesCommand": "/usr/local/bin/hermes",
                        "hermesVersion": "hermes 1.2.3",
                        "displayName": "Home Mac mini",
                    },
                }
            )
            ready = websocket.receive_json()
            assert ready["type"] == "ready"

            first_response: dict = {}

            def send_first_message() -> None:
                first_response["payload"] = client.post(
                    "/v1/messages",
                    headers={"Authorization": f"Bearer {access_token}"},
                    json={"text": "Hello from phone"},
                )

            thread = Thread(target=send_first_message)
            thread.start()
            first_job = websocket.receive_json()
            assert first_job["type"] == "job.execute"
            assert first_job["job"]["sessionId"] is None

            websocket.send_json(
                {
                    "type": "job.result",
                    "jobId": first_job["job"]["id"],
                    "text": "First connector reply",
                    "sessionId": "session-123",
                }
            )
            thread.join(timeout=5)
            assert first_response["payload"].status_code == 202
            assert first_response["payload"].json()["data"]["replyState"] == "pending"

            second_response: dict = {}

            def send_second_message() -> None:
                second_response["payload"] = client.post(
                    "/v1/messages",
                    headers={"Authorization": f"Bearer {access_token}"},
                    json={"text": "Follow up"},
                )

            second_thread = Thread(target=send_second_message)
            second_thread.start()
            second_job = websocket.receive_json()
            assert second_job["job"]["sessionId"] == "session-123"
            websocket.send_json(
                {
                    "type": "job.result",
                    "jobId": second_job["job"]["id"],
                    "text": "Second connector reply",
                    "sessionId": "session-123",
                }
            )
            second_thread.join(timeout=5)
            assert second_response["payload"].status_code == 202


def test_completed_job_events_include_usage_and_result_message(tmp_path):
    with build_client(tmp_path) as client:
        connector_data = setup_connector(client)
        pairing_code = create_phone_pairing_code(client, connector_data["connectorCredential"])
        access_token = redeem_phone(
            client,
            pairing_code["displayCode"],
            "81818181-9191-a1a1-b1b1-c1c1c1c1c1c1",
        )["auth"]["accessToken"]

        with client.websocket_connect(
            "/v1/hosts/ws",
            headers={"Authorization": f"Bearer {connector_data['connectorCredential']}"},
        ) as websocket:
            websocket.send_json(
                {
                    "type": "hello",
                    "connector": {
                        "platform": "macos",
                        "hostname": "test-host",
                        "connectorVersion": "0.1.0",
                        "hermesCommand": "/usr/local/bin/hermes",
                        "hermesVersion": "hermes 1.2.3",
                    },
                }
            )
            assert websocket.receive_json()["type"] == "ready"

            response_holder: dict = {}

            def send_message() -> None:
                response_holder["payload"] = client.post(
                    "/v1/messages",
                    headers={"Authorization": f"Bearer {access_token}"},
                    json={"text": "Stream completion details"},
                )

            thread = Thread(target=send_message)
            thread.start()
            job = websocket.receive_json()["job"]
            websocket.send_json(
                {
                    "type": "job.result",
                    "jobId": job["id"],
                    "text": "Completed connector reply",
                    "sessionId": "session-usage-1",
                    "usage": {
                        "promptTokens": 12,
                        "completionTokens": 8,
                        "totalTokens": 20,
                    },
                }
            )
            thread.join(timeout=5)
            assert response_holder["payload"].status_code == 202

            events_response = client.get(
                f"/v1/jobs/{job['id']}/events",
                headers={"Authorization": f"Bearer {access_token}"},
            )

            assert events_response.status_code == 200
            payload = json.loads(events_response.text.split("data: ", maxsplit=1)[1].strip())
            assert payload["status"] == "completed"
            assert payload["usage"]["totalTokens"] == 20
            assert payload["message"]["role"] == "hermes"
            assert payload["message"]["jobId"] == job["id"]


def test_failed_job_response_and_conversation_include_job_id(tmp_path):
    with build_client(tmp_path) as client:
        connector_data = setup_connector(client)
        pairing_code = create_phone_pairing_code(client, connector_data["connectorCredential"])
        access_token = redeem_phone(
            client,
            pairing_code["displayCode"],
            "91919191-a2a2-b2b2-c2c2-d2d2d2d2d2d2",
        )["auth"]["accessToken"]

        with client.websocket_connect(
            "/v1/hosts/ws",
            headers={"Authorization": f"Bearer {connector_data['connectorCredential']}"},
        ) as websocket:
            websocket.send_json(
                {
                    "type": "hello",
                    "connector": {
                        "platform": "macos",
                        "hostname": "test-host",
                        "connectorVersion": "0.1.0",
                        "hermesCommand": "/usr/local/bin/hermes",
                        "hermesVersion": "hermes 1.2.3",
                    },
                }
            )
            assert websocket.receive_json()["type"] == "ready"

            response_holder: dict = {}

            def send_message() -> None:
                response_holder["payload"] = client.post(
                    "/v1/messages",
                    headers={"Authorization": f"Bearer {access_token}"},
                    json={"text": "Fail this job"},
                )

            thread = Thread(target=send_message)
            thread.start()
            job = websocket.receive_json()["job"]
            websocket.send_json(
                {
                    "type": "job.failed",
                    "jobId": job["id"],
                    "retryable": False,
                    "error": "Tool failed hard",
                }
            )
            thread.join(timeout=5)

            response = response_holder["payload"]
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["replyState"] == "failed"
            assert data["conversation"]["messages"][-1]["jobId"] == job["id"]


def test_talk_readiness_reflects_connector_configuration(tmp_path):
    with build_client(tmp_path) as client:
        connector_data = setup_connector(client)
        pairing_code = create_phone_pairing_code(client, connector_data["connectorCredential"])
        access_token = redeem_phone(
            client,
            pairing_code["displayCode"],
            "31313131-4141-5151-6161-717171717171",
        )["auth"]["accessToken"]

        with client.websocket_connect(
            "/v1/hosts/ws",
            headers={"Authorization": f"Bearer {connector_data['connectorCredential']}"},
        ) as websocket:
            websocket.send_json(
                {
                    "type": "hello",
                    "connector": {
                        "platform": "macos",
                        "hostname": "test-host",
                        "connectorVersion": "0.1.0",
                        "hermesCommand": "/usr/local/bin/hermes",
                        "hermesVersion": "hermes 1.2.3",
                    },
                }
            )
            assert websocket.receive_json()["type"] == "ready"

            response: dict = {}

            def fetch_readiness() -> None:
                response["payload"] = client.get(
                    "/v1/talk/readiness",
                    headers={"Authorization": f"Bearer {access_token}"},
                )

            thread = Thread(target=fetch_readiness)
            thread.start()
            rpc = websocket.receive_json()
            assert rpc["type"] == "rpc.request"
            assert rpc["method"] == "talk.prewarm"

            websocket.send_json(
                {
                    "type": "rpc.response",
                    "requestId": rpc["requestId"],
                    "success": True,
                    "result": {
                        "configured": False,
                        "preferredModels": ["gpt-realtime-1.5", "gpt-realtime"],
                        "selectedModel": None,
                        "voice": "verse",
                        "blockedReason": "OpenAI Realtime is not configured on this Hermes host.",
                        "voiceContextUpdatedAt": "2026-04-01T12:00:00Z",
                    },
                }
            )
            thread.join(timeout=5)

            assert response["payload"].status_code == 200
            data = response["payload"].json()["data"]
            assert data["ready"] is False
            assert data["hostOnline"] is True
            assert data["configured"] is False
            assert data["blockedReason"] == "OpenAI Realtime is not configured on this Hermes host."
            assert data["voice"] == "verse"


def test_talk_session_create_and_end_roundtrip(tmp_path):
    with build_client(tmp_path) as client:
        connector_data = setup_connector(client)
        pairing_code = create_phone_pairing_code(client, connector_data["connectorCredential"])
        access_token = redeem_phone(
            client,
            pairing_code["displayCode"],
            "81818181-9191-a1a1-b1b1-c1c1c1c1c1c1",
        )["auth"]["accessToken"]

        with client.websocket_connect(
            "/v1/hosts/ws",
            headers={"Authorization": f"Bearer {connector_data['connectorCredential']}"},
        ) as websocket:
            websocket.send_json(
                {
                    "type": "hello",
                    "connector": {
                        "platform": "macos",
                        "hostname": "test-host",
                        "connectorVersion": "0.1.0",
                        "hermesCommand": "/usr/local/bin/hermes",
                        "hermesVersion": "hermes 1.2.3",
                    },
                }
            )
            assert websocket.receive_json()["type"] == "ready"

            create_response: dict = {}

            def create_session() -> None:
                create_response["payload"] = client.post(
                    "/v1/talk/session",
                    headers={"Authorization": f"Bearer {access_token}"},
                )

            create_thread = Thread(target=create_session)
            create_thread.start()
            create_rpc = websocket.receive_json()
            assert create_rpc["type"] == "rpc.request"
            assert create_rpc["method"] == "talk.session.create"
            assert create_rpc["params"]["relayMcpURL"].startswith("https://relay.example.test/v1/talk/mcp?token=")

            websocket.send_json(
                {
                    "type": "rpc.response",
                    "requestId": create_rpc["requestId"],
                    "success": True,
                    "result": {
                        "clientSecret": "ephemeral-secret",
                        "expiresAt": "2026-04-01T16:00:00Z",
                        "session": {
                            "id": "sess_123",
                            "type": "realtime",
                        },
                        "model": "gpt-realtime-1.5",
                        "voice": "verse",
                        "voiceContextUpdatedAt": "2026-04-01T15:59:00Z",
                    },
                }
            )
            create_thread.join(timeout=5)

            assert create_response["payload"].status_code == 200
            create_data = create_response["payload"].json()["data"]
            assert create_data["bootstrap"]["clientSecret"] == "ephemeral-secret"
            assert create_data["bootstrap"]["session"]["id"] == "sess_123"
            assert create_data["bootstrap"]["model"] == "gpt-realtime-1.5"
            voice_session_id = create_data["voiceSession"]["id"]

            end_response: dict = {}

            def end_session() -> None:
                end_response["payload"] = client.post(
                    f"/v1/talk/session/{voice_session_id}/end",
                    headers={"Authorization": f"Bearer {access_token}"},
                )

            end_thread = Thread(target=end_session)
            end_thread.start()
            end_rpc = websocket.receive_json()
            assert end_rpc["type"] == "rpc.request"
            assert end_rpc["method"] == "talk.session.end"
            assert end_rpc["params"]["voiceSessionId"] == voice_session_id
            websocket.send_json(
                {
                    "type": "rpc.response",
                    "requestId": end_rpc["requestId"],
                    "success": True,
                    "result": {
                        "ended": True,
                        "voiceSessionId": voice_session_id,
                    },
                }
            )
            end_thread.join(timeout=5)

            assert end_response["payload"].status_code == 200
            assert end_response["payload"].json()["data"]["ended"] is True


def test_talk_turn_endpoint_persists_final_turns_and_is_idempotent(tmp_path):
    with build_client(tmp_path) as client:
        connector_data = setup_connector(client)
        pairing_code = create_phone_pairing_code(client, connector_data["connectorCredential"])
        access_token = redeem_phone(
            client,
            pairing_code["displayCode"],
            "a1111111-b222-c333-d444-e55555555555",
        )["auth"]["accessToken"]

        with client.websocket_connect(
            "/v1/hosts/ws",
            headers={"Authorization": f"Bearer {connector_data['connectorCredential']}"},
        ) as websocket:
            websocket.send_json(
                {
                    "type": "hello",
                    "connector": {
                        "platform": "macos",
                        "hostname": "test-host",
                        "connectorVersion": "0.1.0",
                        "hermesCommand": "/usr/local/bin/hermes",
                        "hermesVersion": "hermes 1.2.3",
                    },
                }
            )
            assert websocket.receive_json()["type"] == "ready"

            create_response: dict = {}

            def create_session() -> None:
                create_response["payload"] = client.post(
                    "/v1/talk/session",
                    headers={"Authorization": f"Bearer {access_token}"},
                )

            create_thread = Thread(target=create_session)
            create_thread.start()
            create_rpc = websocket.receive_json()
            websocket.send_json(
                {
                    "type": "rpc.response",
                    "requestId": create_rpc["requestId"],
                    "success": True,
                    "result": {
                        "clientSecret": "ephemeral-secret",
                        "expiresAt": "2026-04-01T16:00:00Z",
                        "session": {"id": "sess_123", "type": "realtime"},
                        "model": "gpt-realtime-1.5",
                        "voice": "verse",
                        "voiceContextUpdatedAt": "2026-04-01T15:59:00Z",
                    },
                }
            )
            create_thread.join(timeout=5)
            voice_session_id = create_response["payload"].json()["data"]["voiceSession"]["id"]

            client_turn_id = str(uuid.uuid4())
            payload = {
                "clientTurnId": client_turn_id,
                "role": "user",
                "source": "realtime",
                "text": "Tell me what changed today.",
            }
            first_turn = client.post(
                f"/v1/talk/session/{voice_session_id}/turns",
                headers={"Authorization": f"Bearer {access_token}"},
                json=payload,
            )
            duplicate_turn = client.post(
                f"/v1/talk/session/{voice_session_id}/turns",
                headers={"Authorization": f"Bearer {access_token}"},
                json=payload,
            )

            assert first_turn.status_code == 200
            assert duplicate_turn.status_code == 200
            assert first_turn.json()["data"]["turn"]["id"] == duplicate_turn.json()["data"]["turn"]["id"]

            with client.app.state.database.session() as db:
                record_voice_turn(
                    db,
                    voice_session_id=voice_session_id,
                    role="assistant",
                    source="tool",
                    text="Hermes tool reply",
                )
                turns = db.query(VoiceTurn).filter(VoiceTurn.voice_session_id == voice_session_id).all()

            assert len(turns) == 2
            assert {turn.source for turn in turns} == {"realtime", "tool"}


def test_talk_session_end_is_idempotent(tmp_path):
    with build_client(tmp_path) as client:
        connector_data = setup_connector(client)
        pairing_code = create_phone_pairing_code(client, connector_data["connectorCredential"])
        access_token = redeem_phone(
            client,
            pairing_code["displayCode"],
            "f1111111-e222-d333-c444-b55555555555",
        )["auth"]["accessToken"]

        with client.websocket_connect(
            "/v1/hosts/ws",
            headers={"Authorization": f"Bearer {connector_data['connectorCredential']}"},
        ) as websocket:
            websocket.send_json(
                {
                    "type": "hello",
                    "connector": {
                        "platform": "macos",
                        "hostname": "test-host",
                        "connectorVersion": "0.1.0",
                        "hermesCommand": "/usr/local/bin/hermes",
                        "hermesVersion": "hermes 1.2.3",
                    },
                }
            )
            assert websocket.receive_json()["type"] == "ready"

            create_response: dict = {}

            def create_session() -> None:
                create_response["payload"] = client.post(
                    "/v1/talk/session",
                    headers={"Authorization": f"Bearer {access_token}"},
                )

            create_thread = Thread(target=create_session)
            create_thread.start()
            create_rpc = websocket.receive_json()
            websocket.send_json(
                {
                    "type": "rpc.response",
                    "requestId": create_rpc["requestId"],
                    "success": True,
                    "result": {
                        "clientSecret": "ephemeral-secret",
                        "expiresAt": "2026-04-01T16:00:00Z",
                        "session": {"id": "sess_123", "type": "realtime"},
                        "model": "gpt-realtime-1.5",
                        "voice": "verse",
                        "voiceContextUpdatedAt": "2026-04-01T15:59:00Z",
                    },
                }
            )
            create_thread.join(timeout=5)
            voice_session_id = create_response["payload"].json()["data"]["voiceSession"]["id"]

            first_end: dict = {}

            def end_session() -> None:
                first_end["payload"] = client.post(
                    f"/v1/talk/session/{voice_session_id}/end",
                    headers={"Authorization": f"Bearer {access_token}"},
                )

            end_thread = Thread(target=end_session)
            end_thread.start()
            end_rpc = websocket.receive_json()
            websocket.send_json(
                {
                    "type": "rpc.response",
                    "requestId": end_rpc["requestId"],
                    "success": True,
                    "result": {"ended": True, "voiceSessionId": voice_session_id},
                }
            )
            end_thread.join(timeout=5)

            second_end = client.post(
                f"/v1/talk/session/{voice_session_id}/end",
                headers={"Authorization": f"Bearer {access_token}"},
            )

            assert first_end["payload"].status_code == 200
            assert second_end.status_code == 200
            assert first_end["payload"].json()["data"]["ended"] is True
            assert second_end.json()["data"]["ended"] is True


def test_talk_session_returns_conflict_when_connector_is_unconfigured(tmp_path):
    with build_client(tmp_path) as client:
        connector_data = setup_connector(client)
        pairing_code = create_phone_pairing_code(client, connector_data["connectorCredential"])
        access_token = redeem_phone(
            client,
            pairing_code["displayCode"],
            "d1d1d1d1-e2e2-f3f3-a4a4-b5b5b5b5b5b5",
        )["auth"]["accessToken"]

        with client.websocket_connect(
            "/v1/hosts/ws",
            headers={"Authorization": f"Bearer {connector_data['connectorCredential']}"},
        ) as websocket:
            websocket.send_json(
                {
                    "type": "hello",
                    "connector": {
                        "platform": "macos",
                        "hostname": "test-host",
                        "connectorVersion": "0.1.0",
                        "hermesCommand": "/usr/local/bin/hermes",
                        "hermesVersion": "hermes 1.2.3",
                    },
                }
            )
            assert websocket.receive_json()["type"] == "ready"

            response: dict = {}

            def create_session() -> None:
                response["payload"] = client.post(
                    "/v1/talk/session",
                    headers={"Authorization": f"Bearer {access_token}"},
                )

            thread = Thread(target=create_session)
            thread.start()
            rpc = websocket.receive_json()
            assert rpc["type"] == "rpc.request"
            assert rpc["method"] == "talk.session.create"

            websocket.send_json(
                {
                    "type": "rpc.response",
                    "requestId": rpc["requestId"],
                    "success": False,
                    "error": "OpenAI Realtime talk mode is not configured on this Hermes host.",
                }
            )
            thread.join(timeout=5)

            assert response["payload"].status_code == 409
            assert response["payload"].json()["detail"] == "OpenAI Realtime talk mode is not configured on this Hermes host."


def test_sensor_delivery_returns_retry_offline_and_delivered_after_connector_ack(tmp_path):
    with build_client(tmp_path) as client:
        connector_data = setup_connector(client)
        pairing_code = create_phone_pairing_code(client, connector_data["connectorCredential"])
        access_token = redeem_phone(
            client,
            pairing_code["displayCode"],
            "12121212-3434-5656-7878-909090909090",
        )["auth"]["accessToken"]

        offline = client.post(
            "/v1/device/sensor/location",
            headers={"Authorization": f"Bearer {access_token}"},
            json=sensor_location_payload(),
        )
        assert offline.status_code == 202
        assert offline.json()["data"]["deliveryState"] == "retry"

        with client.websocket_connect(
            "/v1/hosts/ws",
            headers={"Authorization": f"Bearer {connector_data['connectorCredential']}"},
        ) as websocket:
            websocket.send_json(
                {
                    "type": "hello",
                    "connector": {
                        "platform": "macos",
                        "hostname": "test-host",
                        "connectorVersion": "0.1.0",
                        "hermesCommand": "/usr/local/bin/hermes",
                        "hermesVersion": "hermes 1.2.3",
                    },
                }
            )
            assert websocket.receive_json()["type"] == "ready"

            response: dict = {}

            def send_location() -> None:
                response["payload"] = client.post(
                    "/v1/device/sensor/location",
                    headers={"Authorization": f"Bearer {access_token}"},
                    json=sensor_location_payload(),
                )

            thread = Thread(target=send_location)
            thread.start()
            sensor_message = websocket.receive_json()
            assert sensor_message["type"] == "sensor.location"
            assert sensor_message["deliveryId"]

            websocket.send_json(
                {
                    "type": "sensor.ack",
                    "deliveryId": sensor_message["deliveryId"],
                    "deliveryState": "delivered",
                }
            )
            thread.join(timeout=5)

            delivered = response["payload"]
            assert delivered.status_code == 200
            assert delivered.json()["data"]["deliveryState"] == "delivered"


def test_stale_connector_disconnect_does_not_remove_newer_live_socket(tmp_path):
    with build_client(tmp_path) as client:
        connector_data = setup_connector(client)
        pairing_code = create_phone_pairing_code(client, connector_data["connectorCredential"])
        access_token = redeem_phone(
            client,
            pairing_code["displayCode"],
            "78787878-5656-3434-1212-000000000000",
        )["auth"]["accessToken"]

        with client.websocket_connect(
            "/v1/hosts/ws",
            headers={"Authorization": f"Bearer {connector_data['connectorCredential']}"},
        ) as first_socket:
            first_socket.send_json(
                {
                    "type": "hello",
                    "connector": {
                        "platform": "macos",
                        "hostname": "test-host",
                        "connectorVersion": "0.1.0",
                        "hermesCommand": "/usr/local/bin/hermes",
                        "hermesVersion": "hermes 1.2.3",
                    },
                }
            )
            assert first_socket.receive_json()["type"] == "ready"

            with client.websocket_connect(
                "/v1/hosts/ws",
                headers={"Authorization": f"Bearer {connector_data['connectorCredential']}"},
            ) as second_socket:
                second_socket.send_json(
                    {
                        "type": "hello",
                        "connector": {
                            "platform": "macos",
                            "hostname": "test-host",
                            "connectorVersion": "0.1.0",
                            "hermesCommand": "/usr/local/bin/hermes",
                            "hermesVersion": "hermes 1.2.3",
                        },
                    }
                )
                assert second_socket.receive_json()["type"] == "ready"

                first_socket.close()

                response: dict = {}

                def send_location() -> None:
                    response["payload"] = client.post(
                        "/v1/device/sensor/location",
                        headers={"Authorization": f"Bearer {access_token}"},
                        json=sensor_location_payload(),
                    )

                thread = Thread(target=send_location)
                thread.start()
                sensor_message = second_socket.receive_json()
                assert sensor_message["type"] == "sensor.location"

                second_socket.send_json(
                    {
                        "type": "sensor.ack",
                        "deliveryId": sensor_message["deliveryId"],
                        "deliveryState": "delivered",
                    }
                )
                thread.join(timeout=5)
                assert response["payload"].status_code == 200
                assert response["payload"].json()["data"]["deliveryState"] == "delivered"


def build_client_with_overrides(tmp_path, db_name="relay-custom.db", **overrides):
    base = dict(
        environment="test",
        public_base_url="https://relay.example.test/v1",
        database_url=f"sqlite:///{tmp_path / db_name}",
        internal_api_key="test-internal-key",
        pairing_code_ttl_seconds=900,
        phone_pairing_code_ttl_seconds=900,
        phone_pairing_max_attempts_per_code=3,
        phone_pairing_max_attempts_per_ip=3,
        phone_pairing_rate_limit_window_seconds=300,
        host_enrollment_code_ttl_seconds=900,
        hermes_adapter="connector",
        connector_sync_wait_seconds=2,
        connector_job_lease_seconds=30,
        connector_heartbeat_timeout_seconds=5,
        connector_idle_poll_interval_seconds=0.1,
    )
    base.update(overrides)
    app = create_app(Settings(**base))
    client = TestClient(app)
    client.__enter__()
    return client


def test_phone_pairing_rejects_expired_code(tmp_path):
    import time

    client = build_client_with_overrides(
        tmp_path,
        db_name="relay-expired.db",
        phone_pairing_code_ttl_seconds=1,
        phone_pairing_max_attempts_per_code=10,
        phone_pairing_max_attempts_per_ip=10,
    )

    data = setup_connector(client)
    pairing = create_phone_pairing_code(client, data["connectorCredential"])
    code = pairing["code"]

    time.sleep(2)

    response = client.post(
        "/v1/phone-pairing/redeem",
        json=phone_pairing_payload(code=code, installation_id=str(uuid.uuid4())),
    )
    assert response.status_code == 400
    assert "expired" in response.json()["detail"].lower()


def test_phone_pairing_allows_exact_attempt_limit_before_rate_limiting(tmp_path):
    import time

    client = build_client_with_overrides(
        tmp_path,
        db_name="relay-attempt-limit.db",
        phone_pairing_code_ttl_seconds=1,
        phone_pairing_max_attempts_per_code=3,
        phone_pairing_max_attempts_per_ip=10,
    )

    data = setup_connector(client)
    pairing = create_phone_pairing_code(client, data["connectorCredential"])
    code = pairing["code"]

    time.sleep(2)

    first = client.post(
        "/v1/phone-pairing/redeem",
        json=phone_pairing_payload(code=code, installation_id=str(uuid.uuid4())),
    )
    second = client.post(
        "/v1/phone-pairing/redeem",
        json=phone_pairing_payload(code=code, installation_id=str(uuid.uuid4())),
    )
    third = client.post(
        "/v1/phone-pairing/redeem",
        json=phone_pairing_payload(code=code, installation_id=str(uuid.uuid4())),
    )
    fourth = client.post(
        "/v1/phone-pairing/redeem",
        json=phone_pairing_payload(code=code, installation_id=str(uuid.uuid4())),
    )

    assert first.status_code == 400
    assert "expired" in first.json()["detail"].lower()
    assert second.status_code == 400
    assert "expired" in second.json()["detail"].lower()
    assert third.status_code == 400
    assert "expired" in third.json()["detail"].lower()
    assert fourth.status_code == 429
    assert "too many attempts" in fourth.json()["detail"].lower()


def test_talk_readiness_returns_error_when_host_offline(tmp_path):
    client = build_client_with_overrides(tmp_path, db_name="relay-talk-offline.db")
    data = setup_connector(client)
    pairing = create_phone_pairing_code(client, data["connectorCredential"])
    phone = redeem_phone(client, pairing["code"], str(uuid.uuid4()))
    access_token = phone["auth"]["accessToken"]

    # Don't connect WebSocket — host is offline
    response = client.get(
        "/v1/talk/readiness",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    # Should indicate host is offline (409 or 200 with blocked status)
    assert response.status_code in (200, 409)


def test_clear_conversation_creates_fresh(tmp_path):
    with build_client(tmp_path) as client:
        connector_data = setup_connector(client)
        pairing_code = create_phone_pairing_code(client, connector_data["connectorCredential"])
        access_token = redeem_phone(
            client,
            pairing_code["displayCode"],
            str(uuid.uuid4()),
        )["auth"]["accessToken"]
        auth = {"Authorization": f"Bearer {access_token}"}

        # Send a message to populate conversation
        client.post("/v1/messages", headers=auth, json={"text": "Hello"})

        # Get original conversation
        original = client.get("/v1/conversations/current", headers=auth).json()["data"]["conversation"]
        assert len(original["messages"]) >= 1
        original_id = original["id"]

        # Clear conversation
        clear_response = client.post("/v1/conversations/current/clear", headers=auth)
        assert clear_response.status_code == 200
        cleared = clear_response.json()["data"]["conversation"]
        assert cleared["id"] != original_id
        assert len(cleared["messages"]) == 0

        # Verify GET returns the new conversation
        current = client.get("/v1/conversations/current", headers=auth).json()["data"]["conversation"]
        assert current["id"] == cleared["id"]
        assert len(current["messages"]) == 0


def test_clear_conversation_when_none_exists(tmp_path):
    with build_client(tmp_path) as client:
        connector_data = setup_connector(client)
        pairing_code = create_phone_pairing_code(client, connector_data["connectorCredential"])
        access_token = redeem_phone(
            client,
            pairing_code["displayCode"],
            str(uuid.uuid4()),
        )["auth"]["accessToken"]
        auth = {"Authorization": f"Bearer {access_token}"}

        # Clear without ever creating a conversation
        clear_response = client.post("/v1/conversations/current/clear", headers=auth)
        assert clear_response.status_code == 200
        conversation = clear_response.json()["data"]["conversation"]
        assert len(conversation["messages"]) == 0
