# Security Policy

## Reporting Vulnerabilities

If you discover a security vulnerability in Hermes iOS, please report it responsibly:

1. **Do not** open a public GitHub issue for security vulnerabilities
2. Report privately through GitHub: [Security → Report a vulnerability](https://github.com/dylan-buck/Hermes-iOS/security/advisories/new)
3. Include a description of the vulnerability, steps to reproduce, and potential impact

We will acknowledge receipt within 48 hours and work with you on a fix.

## Security Architecture

### Relay

The relay is the only internet-facing component. It handles:

- **Authentication:** Bearer token auth for iOS clients, connector credential for WebSocket
- **CONNECTOR_SETUP_SECRET:** Shared secret that gates new connector registration. The connector must provide the same value during `hermes-mobile setup`. Required outside development/test; the relay refuses to start without it.
- **Device registration:** Phones onboard through single-use phone pairing codes. The legacy unauthenticated `POST /v1/device/register` endpoint is disabled unless `ALLOW_OPEN_DEVICE_REGISTRATION=true`, which is only accepted in development/test.
- **INTERNAL_API_KEY:** Gates internal admin endpoints. Must be changed from the default `"replace-me"` in production — the relay logs a security warning if the default is used outside development.
- **Token lifecycle:** Access tokens (1h default), refresh tokens (30d default), phone pairing codes (10min default) are all configurable via env vars.

### Connector

The connector runs on the same machine as the Hermes Agent:

- **WebSocket auth:** Authenticates to the relay using a credential obtained during setup
- **Sensor data:** Stored locally in SQLite at `~/.hermes-mobile/state/sensors.db`
- **MCP tools:** The `query_sensor_data` tool opens a read-only SQLite connection, preventing write-based SQL injection even if the LLM crafts a malicious query
- **OpenAI API key:** Stored in `~/.hermes-mobile/secrets.json` (not in state.json), used only for Realtime voice sessions

### iOS App

- **Relay URL:** Configured during onboarding, persisted locally. Not hardcoded.
- **Credentials:** Stored in the iOS Keychain (service name: `io.hermesmobile.HermesMobile.session`)
- **Health data:** Read-only HealthKit access, uploaded to the relay only when the connector is connected and acknowledges receipt
- **Camera/mic:** Requested just-in-time, not at launch. Camera frames for voice mode are sent directly to OpenAI via WebRTC, not through the relay.

### Known Limitations

- **MCP tool token in URL:** The voice mode MCP tool token is passed as a query parameter (`?token=...`). This is a constraint of the MCP Streamable HTTP protocol. The token is short-lived (valid only during the active voice session), server-to-server (OpenAI → relay, never in a browser), and invalidated when the session ends.
- **Sensor data retention:** Health and location data is retained for 90 days locally on the connector host. Users should be aware of this when granting access to the machine.

## Supported Versions

Security updates are applied to the latest version on the `master` branch. There are no backported security patches for older commits.
