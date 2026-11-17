# AirSupply Extended — historical AirMini data (development)

This fork builds on [ananthb/airsupply](https://github.com/ananthb/airsupply)
and retains its **read-only** medical-device policy. Work is isolated from
the main branch and is not yet deployed on the Raspberry Pi.

## Implemented on feature/airmini-history

- `history.py`: deduplicates LoggedData notifications and matches MaskOn /
  MaskOff pairs, respiratory events, one-minute leak and pressure samples.
  Multiple sessions are combined into a local noon-to-noon therapy night.
  AHI excludes RERA; unmatched sessions are excluded.
- `Session.collect_logged_data()`: collects all four requested signals on
  the **existing held RFCOMM session**. It requires completion markers for
  each signal and fails on timeout/connection loss.
- `history_sync.py`: optional 16-day read-only pull, with a 12-hour fallback
  interval by default. AirSupply also pulls history on its first normal read
  that sees standby after therapy, or a standby reading with therapy use newer
  than the last successful history pull. A failed download is retried on the
  next eligible read.
- `history_store.py`: private SQLite storage of **derived summaries only**,
  with per-device records and upserts; no raw therapy event logs, PINs or keys.
- Home Assistant MQTT Discovery: sensors for the latest night/date, usage,
  AHI, obstructive/central apnoeas and hypopnoeas; 14-night summary array
  retained on `airsupply/<device-id>/nights`.
- Synthetic tests for parsing, storage, collection completeness and entities.

## Safe default

Historical synchronisation is **disabled** unless expressly enabled:

```text
AIRSUPPLY_HISTORY_ENABLED=1
AIRSUPPLY_HISTORY_TIMEZONE=Australia/Melbourne
AIRSUPPLY_HISTORY_CLOCK_OFFSET_SECONDS=<measured seconds>
AIRSUPPLY_HISTORY_SYNC_HOURS=12
AIRSUPPLY_HISTORY_TIMEOUT_SECONDS=300
AIRSUPPLY_HISTORY_DB=/data/airsupply-history.sqlite3
```

The offset is *real UTC minus AirMini device UTC*, measured from a fresh
GetDateTime response, not copied from somebody else's machine. **Do not
enable** until this is verified; otherwise therapy-night dates may be wrong.

AirSupply's existing MQTT variables can target an external broker:

```text
AIRSUPPLY_MQTT_HOST=<LAN hostname or address>
AIRSUPPLY_MQTT_PORT=1883
AIRSUPPLY_MQTT_USERNAME=<dedicated account>
AIRSUPPLY_MQTT_PASSWORD=<secret, never committed>
```

Only validated nightly usage and AHI-related fields are published. Leak and
pressure remain `*_raw` inside the private summary store until their units
are cross-checked. Official ResMed score is not reconstructed.

## Before deploying

1. Run upstream CI and the new synthetic tests; verify add-on container builds.
2. Compare nightly calculation and clock offset against the official AirMini app.
3. Confirm the external broker's MQTT Discovery integration is active.
4. Prove AirSupply running on the Pi can use the existing pairing key without
   disturbing the official phone app. **One active RFCOMM connection at a time.**
5. Validate historical notification completion, response sizes and reconnection
   behaviour on hardware. Do not rely on unattended access until tested.

No patient readings, raw history, pairing key or broker credentials belong in
GitHub commits, issues, PR descriptions or CI output.

## CI verification

GitHub Actions runs the existing `CI` workflow on pull-request updates. The
`tests` job runs the Python unit suite, while the `image` job builds the
AirSupply container (including its bundled libairmini).
Do not merge this draft PR or deploy to the AirMini until both jobs pass and
hardware behaviour has been checked independently.

## Bluetooth reconnect development (not yet deployed)

AirSupply Extended now retries paired-device serial transport failures with
up to three bounded attempts. After a transient BlueZ page timeout,
already-connected/in-progress or refused error, it performs a brief BR/EDR
inquiry, allows the adapter to settle and retries the *existing* saved
application key. An inactive machine might still refuse attempts until its
Bluetooth button is pressed: this is not guaranteed to bypass firmware
limits. Never reset pairing, erase the saved key, force-disconnect an active
phone connection or write device settings.

Relevant environment settings (optional):

```text
AIRSUPPLY_RECONNECT_ATTEMPTS=3
AIRSUPPLY_RECONNECT_SCAN_SECONDS=8
AIRSUPPLY_RECONNECT_RETRY_PAUSE=8
```

The BlueZ serial profile now bounds each connection stage and cleans up failed
pending file descriptors. For validation, test three scenarios independently
on the Pi: standby reconnect after idle; Bluetooth recovery after restarting
AirSupply; and phone-app connection/disconnection. Collect **only sanitized**
log messages and timings. The fix must not interfere with therapy.
