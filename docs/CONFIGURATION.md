# Configuration and Deployment

This repo is designed to be **public-safe** and **self-hosted first**.

Tracked files should keep generic defaults. Real deployment values belong in local env files, deployment secrets, or untracked local build settings.

## Recommended setup order

1. Run the relay
2. Run `hermes-mobile setup` on the Hermes host
3. Pair the phone
4. Add optional APNs or CarPlay later if you need them

> [!TIP]
> You do not need APNs or CarPlay to get started. The base app, relay, and connector flow works without them.

## Relay environment variables

### Required in real deployments

| Variable | Required | Notes |
| --- | --- | --- |
| `PUBLIC_BASE_URL` | Yes | Public API base URL ending in `/v1` |
| `DATABASE_URL` | Yes | PostgreSQL in production; SQLite is fine for local dev |
| `INTERNAL_API_KEY` | Yes | Must be a strong random value outside development/test |
| `RELAY_ENVIRONMENT` | Recommended | `development`, `test`, or `production` |

### Connector mode

| Variable | Required | Notes |
| --- | --- | --- |
| `HERMES_ADAPTER=connector` | Yes | Production/self-hosted mode |
| `CONNECTOR_SYNC_WAIT_SECONDS` | Optional | Inline wait window before returning `pending` |
| `CONNECTOR_JOB_LEASE_SECONDS` | Optional | Job lease duration |
| `CONNECTOR_HEARTBEAT_TIMEOUT_SECONDS` | Optional | Host online/offline timeout |
| `CONNECTOR_IDLE_POLL_INTERVAL_SECONDS` | Optional | Connector idle polling interval |
| `CONNECTOR_SETUP_SECRET` | Yes outside development/test | Bootstrap gate for new connectors; the relay refuses to start in production without it |
| `ALLOW_OPEN_DEVICE_REGISTRATION` | Dev only | Enables unauthenticated `POST /v1/device/register` for local testing. Off by default; the relay refuses to start with it on outside development/test |

### Pairing and rate limits

| Variable | Required | Notes |
| --- | --- | --- |
| `PHONE_PAIRING_CODE_TTL_SECONDS` | Optional | Phone pairing code expiry |
| `PHONE_PAIRING_MAX_ATTEMPTS_PER_CODE` | Optional | Retry limit per code |
| `PHONE_PAIRING_MAX_ATTEMPTS_PER_IP` | Optional | Retry limit per IP |
| `PHONE_PAIRING_RATE_LIMIT_WINDOW_SECONDS` | Optional | Window for IP-based throttling |
| `HOST_ENROLLMENT_CODE_TTL_SECONDS` | Optional | Legacy host enrollment expiry |

### APNs

| Variable | Required | Notes |
| --- | --- | --- |
| `APNS_KEY_PATH` or `APNS_KEY_CONTENTS` | Optional | `.p8` key path or raw key contents |
| `APNS_KEY_ID` | Optional | Apple Key ID |
| `APNS_TEAM_ID` | Optional | Apple Team ID |
| `APNS_BUNDLE_ID` | Optional | Default app bundle ID for push delivery |
| `APNS_ENVIRONMENT` | Optional | `development` or `production` |
| `APP_PRESENCE_STALE_SECONDS` | Optional | Foreground suppression window |

## Connector environment variables

### Required for real use

| Variable | Required | Notes |
| --- | --- | --- |
| `HERMES_MOBILE_RELAY_URL` | Usually | Required unless you pass `--relay-url` or use the setup wizard |
| `HERMES_COMMAND` | Yes | Absolute path or resolvable `hermes` binary |

### Hermes runtime context

| Variable | Required | Notes |
| --- | --- | --- |
| `HERMES_WORKDIR` | Optional | Git workdir / project root for Hermes |
| `HERMES_PROVIDER` | Optional | Provider override |
| `HERMES_MODEL` | Optional | Model override |
| `HERMES_TOOLSETS` | Optional | Toolset override |
| `HERMES_SOURCE` | Optional | Defaults to `tool` |
| `HERMES_HISTORY_LIMIT` | Optional | Defaults to `20` |
| `HERMES_HOME` | Optional | Hermes config/skills home |

### Connector-local state

| Variable | Required | Notes |
| --- | --- | --- |
| `HERMES_MOBILE_CONNECTOR_HOME` | Optional | Defaults to `~/.hermes-mobile` |
| `CONNECTOR_SETUP_SECRET` | Optional | Must match relay when bootstrap gate is enabled |

## iOS app build and runtime config

The app reads these keys from `Info.plist` or local build settings.

| Key | Required | Notes |
| --- | --- | --- |
| `APP_HOSTED_RELAY_ENABLED` | Optional | Enables a hosted-relay option in the app UI |
| `APP_HOSTED_RELAY_URL` | Optional | Hosted relay base URL ending in `/v1` |
| `APP_SUPPORT_URL` | Optional | Support link shown in Settings |
| `APP_TERMS_URL` | Optional | Terms of Service link |
| `APP_PRIVACY_URL` | Optional | Privacy Policy link |
| `APP_GROUP_ID` | Recommended if customizing IDs | App Group for widget/shared state |

> [!IMPORTANT]
> If you change the bundle ID or App Group locally, update `APP_GROUP_ID` to match your real App Group. Otherwise widgets and shared snapshot data will silently stop working.

## Private override strategy

Keep these values out of tracked source:

- hosted relay URL
- Fly app name
- `CONNECTOR_SETUP_SECRET`
- Apple signing team and custom bundle IDs
- APNs `.p8` keys and team secrets

Recommended approach:

- **relay**: local `.env`, Fly secrets, or another deployment secret store
- **connector**: shell env, launchd env, systemd env, or Scheduled Task env
- **iOS app**: local `.xcconfig` or local build settings

## APNs setup

APNs is optional. Without it, the app still works and refreshes when opened.

### What APNs enables

- alert pushes for Hermes replies when the app is backgrounded
- relay-side push delivery keyed to the device’s registered bundle ID and environment
- future proactive notifications

### Setup

1. Create an APNs key in the Apple Developer portal.
2. Keep the `.p8` file private.
3. Set these on the relay:

```bash
APNS_KEY_PATH=/path/to/AuthKey_XXXXXXXXXX.p8
APNS_KEY_ID=XXXXXXXXXX
APNS_TEAM_ID=YYYYYYYYYY
APNS_BUNDLE_ID=io.hermesmobile.HermesMobile
APNS_ENVIRONMENT=development
```

4. Build and run the app on a real device.
5. Allow notifications in iOS.

The app will register its token automatically and report foreground/background presence so the relay can suppress alerts while the app is active.

## CarPlay setup

CarPlay is optional and requires Apple approval.

### What you need

1. Request the **Voice-Based Conversational** entitlement from Apple.
2. Wait for approval.
3. Add the entitlement in your local signing/capabilities setup.
4. Rebuild the app.

Without approval:

- the app still builds and runs
- the CarPlay scene stays inert
- all non-CarPlay features are unaffected

## Same-network local development

If you run the relay on your Mac and test with a physical iPhone:

- use your Mac’s LAN IP, for example `http://192.168.1.10:8000/v1`
- do **not** use `127.0.0.1` or `localhost`

If you use the simulator on the same Mac, `127.0.0.1` is fine.

## Personal deployment checklist

1. Confirm `PUBLIC_BASE_URL` is correct.
2. Confirm `INTERNAL_API_KEY` is set and not the default.
3. Confirm `HERMES_MOBILE_RELAY_URL` is present in the connector environment.
4. Confirm `CONNECTOR_SETUP_SECRET` matches on relay and connector when enabled.
5. Confirm `APP_GROUP_ID` matches your local App Group if you changed bundle IDs.
6. Add APNs only when you are ready to test push on a real device.
