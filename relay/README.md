# Hermes iOS Relay

The relay is the public control plane for Hermes iOS. It handles pairing, auth, jobs, SSE, push registration, and the connector WebSocket, but it does **not** run Hermes itself in connector mode.

## What the relay does

- device registration, auth, refresh, and session lifecycle
- connector-first pairing and host management
- durable message jobs with SSE progress streaming
- talk readiness and voice-session bootstrap
- inbox APIs and APNs registration/delivery

In production, Hermes execution stays on the user-owned host through the connector.

## Requirements

- Python 3.11+
- PostgreSQL for production
- HTTPS base URL for public deployments
- a strong `INTERNAL_API_KEY`

## Quick start (local development)

```bash
cd relay
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
cp .env.example .env
uvicorn app.main:app --reload
```

You can also use Docker Compose from the same directory if you want a local Postgres-backed stack.

For same-network device testing, see [docs/local-dev.md](docs/local-dev.md).

## Production checklist

> [!IMPORTANT]
> The relay must not run in production with `INTERNAL_API_KEY=replace-me`. Current code treats that as a startup error outside development/test.

Minimum production configuration:

```bash
export RELAY_ENVIRONMENT=production
export PUBLIC_BASE_URL=https://your-relay.example.com/v1
export DATABASE_URL=postgresql+psycopg://...
export INTERNAL_API_KEY=replace-with-a-real-secret
export HERMES_ADAPTER=connector
```

Required outside development/test:

```bash
export CONNECTOR_SETUP_SECRET=replace-with-a-bootstrap-secret
```

Every connector must provide the same value during `hermes-mobile setup`. The relay refuses to start in production without it.

The full variable matrix lives in [../docs/CONFIGURATION.md](../docs/CONFIGURATION.md).

## Deploy to Fly.io

The tracked [fly.toml](fly.toml) contains placeholders only. Replace the app name and `PUBLIC_BASE_URL`, then deploy.

Two supported paths:

- **Guided path**: run `hermes-mobile setup` on the connector host and choose the Fly deployment option
- **Manual path**: follow [docs/fly-io.md](docs/fly-io.md)

The relay docs assume Fly Managed Postgres for new deployments.

## Local Hermes execution modes

The relay supports three modes:

- `HERMES_ADAPTER=mock`
  - deterministic demo behavior
- `HERMES_ADAPTER=cli`
  - runs Hermes locally from the relay process
- `HERMES_ADAPTER=connector`
  - production-oriented path where a connected host claims and executes jobs

For public/self-hosted users, `connector` mode is the intended deployment model.

## API overview

### Core and auth

- `GET /v1/health`
- `GET /v1/version`
- `GET /v1/session`
- `POST /v1/auth/refresh`
- `POST /v1/auth/revoke`
- `POST /v1/device/register` (disabled unless `ALLOW_OPEN_DEVICE_REGISTRATION=true` in development/test; phones onboard via phone pairing)

### Pairing and hosts

- `POST /v1/connector/setup`
- `POST /v1/connector/phone-pairing-codes`
- `POST /v1/phone-pairing/redeem`
- `GET /v1/hosts/current`
- `POST /v1/hosts/current/revoke`
- `GET /v1/hosts/ws` (WebSocket)

### Chat and jobs

- `GET /v1/conversations/current`
- `POST /v1/messages`
- `GET /v1/jobs/{job_id}/events`

### Talk mode

- `GET /v1/talk/readiness`
- `POST /v1/talk/session`
- `POST /v1/talk/session/{voice_session_id}/end`
- `POST /v1/talk/session/{voice_session_id}/turns`

### Inbox and push

- `GET /v1/inbox`
- `POST /v1/inbox/{id}/action`
- `POST /v1/push/register`
- `POST /v1/push/deactivate`
- `POST /v1/push/send` *(internal)*
- `POST /internal/inbox/create`
- `GET /internal/inbox/{id}/actions`

## Security notes

- Use HTTPS in any deployment the phone will reach over the internet.
- Set a strong `INTERNAL_API_KEY`.
- Set `CONNECTOR_SETUP_SECRET` (required in production) so arbitrary connectors cannot bootstrap accounts on your relay.
- Leave `ALLOW_OPEN_DEVICE_REGISTRATION` unset on any relay reachable from the internet.
- Keep APNs secrets on the relay only, never on the connector.

## APNs and CarPlay

APNs and CarPlay are optional platform features. They are documented in [../docs/CONFIGURATION.md](../docs/CONFIGURATION.md).

- APNs is fully optional for base setup
- CarPlay requires Apple approval for the entitlement and is inert when not configured

## Troubleshooting

### Relay won’t start

- confirm `DATABASE_URL`
- confirm `PUBLIC_BASE_URL`
- confirm `INTERNAL_API_KEY` is not the default in production

### Connector can’t claim jobs

- verify the host is connected through `/v1/hosts/ws`
- confirm the relay and connector are using the same base URL
- confirm `CONNECTOR_SETUP_SECRET` matches on both sides when enabled

### Push registration works but no alerts arrive

- check APNs config on the relay
- verify bundle ID and environment match the device registration
- confirm the app is not foregrounded when testing reply-triggered alerts

## Related docs

- [../README.md](../README.md)
- [../docs/CONFIGURATION.md](../docs/CONFIGURATION.md)
- [docs/fly-io.md](docs/fly-io.md)
- [docs/local-dev.md](docs/local-dev.md)
