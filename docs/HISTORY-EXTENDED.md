# Historical therapy data (development)

This fork extends [ananthb/airsupply](https://github.com/ananthb/airsupply)
without changing its read-only medical-device policy. Initial work is isolated
in \`airsupply/src/airsupply/history.py\`: a pure parser for \`LoggedData\`
notifications from libairmini's existing \`Session.get_logged_data\` API.

## Current scope

- Recognises \`MaskOn\` / \`MaskOff\` pairs, respiratory events, and 60-second
  periodic leak/pressure samples (interval comes from the payload).
- Deduplicates overlapping notifications, combines multiple sessions into
  a noon-to-noon therapy night, and excludes RERA from AHI.
- Reports AHI, usage, event breakdown and preliminary raw leak/pressure
  statistics. Incomplete mask-on/off pairs are excluded.
- Does **not** guess the AirMini's clock drift: callers must measure and
  pass \`clock_offset_seconds\` (host UTC minus device UTC) and select timezone.
- Does **not** assume units for raw leak/pressure data. Do not publish these
  numbers as clinical measurements before cross-checking against ResMed.
- Is purely local parsing: no Bluetooth calls, MQTT publication, credentials,
  device state changes or background scheduling.

## Next integration milestones

1. Collect \`LoggedData\` through AirSupply's existing held session and
   notification hook; do not start an independent RFCOMM client.
2. Persist notifications and sync cursors in a local SQLite database with
   deduplication, atomic updates and retention controls.
3. Validate clock offset and leak/pressure units with the AirMini application.
4. Extend existing MQTT Discovery and AirSupply card with per-night sensors;
   configure the Pi's existing external broker via environment variables.
5. Test unattended reconnection and the phone app's single-connection behaviour.

Never commit real therapy logs, machine keys, passwords or patient-identifying
details. The tests use invented records only.
