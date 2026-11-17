# AirSupply Extended

Monitor CPAP usage and nightly history in Home Assistant. The development
branch supports the ResMed AirMini over Bluetooth Classic and the AirSense 11
over Bluetooth LE.

Each machine becomes a device belonging to one of your people: therapy hours,
last used, state, therapy mode, pressures. It reads; it never writes.

The host's Bluetooth adapter must be within range of each machine.

## Multi-machine development

`feature/multi-machine` adds an AirSense 11 driver using the MIT-licensed
protocol client and summary parser from
[maroliar/hass-resmed-airsense](https://github.com/maroliar/hass-resmed-airsense),
pinned to commit `01dae572e9fe0732905e11427fed826266f91354`. Its copyright
and license are included in `airsupply/airsense/protocol/LICENSE` inside the
container.

Both drivers use AirSupply's existing MQTT discovery, owner assignment,
per-machine topics and 14-night card. Existing machine records default to
the AirMini driver and retain their entity identifiers and pairing keys.

The experimental ARM64 image is
`ghcr.io/rnodern/airsupply-extended:multi-machine-arm64`. It uses the same
port, D-Bus mount, persistent data volume and MQTT environment as the AirMini
development image. Historical summaries are enabled with
`AIRSUPPLY_HISTORY_ENABLED=1`; the timezone comes from
`AIRSUPPLY_HISTORY_TIMEZONE`.

In AirSupply, choose Look for machines, add the AirSense, then enter the
code from the machine's Bluetooth pairing screen. This creates an application
pairing and replaces the phone app's Bluetooth pairing. myAir's cellular
nightly reports continue. Each machine has its own stored credentials,
reconnect loop, Disconnect action and owner.

The AirSense pushes therapy state changes. AirSupply waits 60 seconds after
Therapy → Standby to read the completed daily summary, retrying failed
downloads after two minutes. The AirMini refreshes history on its first
regular standby reading after therapy and retries on the next read.
The configured historical sync interval is also retained as a fallback.

AirSense daily records provide usage and reported AHI. The card computes a
usage-weighted average from the available values and marks unreported event
counts as unavailable. AirMini records can also supply their measured event
counts. Each machine's history stays under its own MQTT topic.

Pairing, encrypted reads, pushed standby updates, history publication,
disconnect/reconnect and legacy AirMini compatibility have synthetic tests.
AirSense 11 operation on a physical machine remains to be verified.

## Install

[![Add repository to your Home Assistant instance](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fananthb%2Fhass-addons)

Add `https://github.com/ananthb/hass-addons` as an add-on repository, install
**airsupply**, open it from the sidebar. Install the Mosquitto broker add-on
too; that is how the sensors get out. [Add-on docs][docs].

[docs]: https://github.com/ananthb/hass-addons/blob/main/airsupply/DOCS.md

## The card

Fourteen nights of therapy as bars against the four-hour mark. Install through
HACS as a Dashboard repository pointing here, or copy
`card/airsupply-card.js` into `config/www/` and add it as a resource.

```yaml
type: custom:airsupply-card
entity: sensor.airsupply_<machine>_therapy_hours
```

Options: `nights` (14), `goal` (4), `title`, `only_owner`. With
`only_owner: true` the card shows only to the user the machine is assigned to.

`card/preview.html` opens in any browser with invented data.

## AirSupply Extended dashboard (development)

The fork's `airsupply-extended-card` now displays a responsive **14-night history
dashboard** when its new Home Assistant `Therapy history` MQTT sensor is
available. It shows:

- Latest AHI, usage and a usage-weighted average AHI for the available nights.
- Nightly AHI and usage charts, with the configured four-hour goal.
- Stacked obstructive/central apnoea and hypopnoea event counts.
- A table of nightly usage, AHI, event counts and RERAs.

The extended card uses a separate custom element to avoid a conflict with upstream `airsupply-card`. The original therapy-hours-only view remains available using
`view: usage`, and is the automatic fallback before history is present.

### Install the card in Home Assistant

This GitHub fork is still on a **development branch**. For staging,
download `card/airsupply-card.js` from
`feature/airmini-history` and put it into Home Assistant's
`/config/www/airsupply-extended/airsupply-card.js`. Register the
resource URL `/local/airsupply-extended/airsupply-card.js` as a
**JavaScript module** in Home Assistant's Dashboard resources.

Then add a Manual card (replace the entity ID with the actual
`Therapy hours` entity from your MQTT device):

```yaml
type: custom:airsupply-extended-card
entity: sensor.resmed_871866_therapy_hours
history_entity: sensor.resmed_871866_therapy_history
view: dashboard
nights: 14
goal: 4
title: AirMini therapy
```

The example uses Home Assistant's actual entity names from an AirMini installed
in the local test environment. If your `Therapy history` sensor has another
entity ID, set `history_entity:` explicitly to that sensor. The card also
attempts to discover history alongside `entity:`, but no longer displays
misleading cumulative-recorder usage when the history sensor is missing.

After installing a changed JS card, update its Home Assistant resource URL
query string (for example `?v=2`) and refresh the browser. The running
Pi's AirSupply container does not need restarting for card-only fixes.

The history sensor is discovered through MQTT from the same AirSupply instance;
no separate broker, API key or Home Assistant REST access is required. Your
browser reads only Home Assistant's existing entity attributes.

**Privacy:** The history sensor contains nightly medical summaries and Home
Assistant retains them. Restrict dashboard and Home Assistant access to people
you authorise. These are calculations from machine events, not medical advice.
Pressure and leak units have not been validated and are not charted.

The original upstream add-on installation instructions above do **not** install
this development fork or its changed card.

## Layout

| | |
|---|---|
| `airsupply/` | the add-on image |
| `card/` | the Lovelace card |
| `docs/protocol.md` | the transport, and what is known versus assumed |
| `docs/verify.md` | what is confirmed against a real machine |

## Licence

GPL-3.0-only. [libairmini][lib] (BSD-2-Clause) owns the protocol and is built
from the commit in [`airsupply/libairmini.pin`](airsupply/libairmini.pin),
not vendored.

[lib]: https://github.com/psychoticbeef/libairmini
