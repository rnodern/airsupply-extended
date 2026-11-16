# airsupply

Monitor a ResMed AirMini CPAP in Home Assistant, over Bluetooth Classic.

Each machine becomes a device belonging to one of your people: therapy hours,
last used, state, therapy mode, pressures. It reads; it never writes.

Bluetooth Classic, not BLE, so the host must be within about ten metres of the
machine.

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
entity: sensor.airsupply_58_26_3a_e8_c9_7d_therapy_hours
view: dashboard
nights: 14
goal: 4
title: AirMini therapy
```

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
