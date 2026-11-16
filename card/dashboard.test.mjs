// Synthetic dashboard tests. No Home Assistant server, patient data or broker.
let Card;
globalThis.HTMLElement = class { constructor() { this.style = {}; } };
globalThis.customElements = { define: (_name, cls) => { Card = cls; } };
globalThis.window = globalThis;
const quiet = console.info; console.info = () => {};
await import("./airsupply-card.js");
console.info = quiet;

const check = (name, predicate) => {
  if (!predicate) { console.error("FAIL:", name); process.exitCode = 1; }
  else console.log("ok:", name);
};

const records = [
  {
    night: "2026-11-05", usage_hours: 4.325, ahi: 9.48, ahi_events: 41,
    obstructive_apnoeas: 39, central_apnoeas: 0, hypopnoeas: 2, rera: 0,
  },
  {
    night: "2026-11-11", usage_hours: 4.852, ahi: 5.56, ahi_events: 27,
    obstructive_apnoeas: 16, central_apnoeas: 2, hypopnoeas: 9, rera: 4,
  },
  {
    night: "2026-11-15", usage_hours: 4.65, ahi: 0.21, ahi_events: 1,
    obstructive_apnoeas: 0, central_apnoeas: 1, hypopnoeas: 0, rera: 0,
  },
];

const make = (history=records) => {
  const card = Object.create(Card.prototype);
  card._config = {
    entity: "sensor.airsupply_58_26_3a_e8_c9_7d_therapy_hours",
    nights: 14, goal: 4, only_owner: false, view: "dashboard",
  };
  card._hass = { states: {
    [card._config.entity]: {state: "162.83", attributes:{}},
    "sensor.airsupply_58_26_3a_e8_c9_7d_history_nights": {
      state: history?.length ?? "unknown",
      attributes: history ? { nights: history } : {},
    },
  }};
  return card;
};

const card=make([records[2], records[0], records[1]]);
check("history sorts chronologically", card._history().map(n=>n.night).join(",") === "2026-11-05,2026-11-11,2026-11-15");
check("latest night shows actual AHI", card._dashboard().includes(">0.21</strong>"));
check("latest usage shown in hour and minute format", card._dashboard().includes("4h 39m"));
check("weighted AHI uses total events divided by total hours", card._dashboard().includes(">4.99</strong>"));
check("event chart includes three types", card._dashboard().includes("Hypopnoea") && card._dashboard().includes("Central"));
check("not rendering pressure/leak units as validated", card._dashboard().includes("withheld pending unit validation"));
check("RERA in table", card._dashboard().includes("<th scope=\"col\">RERA</th>"));
check("owner visibility defaults preserved", !card._hidden());
check("missing history falls back to old usage chart", make(null)._history() === null);
const malformed=make([null,{night:"2026-11-15",ahi:0.21,usage_hours:2},{night:"bad<script>",ahi:99}]);
check("bad history rows rejected", malformed._history().length===1);
check("missing data displayed as dash", malformed._dashboard().includes("—"));
check("history limited to requested count", (()=>{const c=make(records);c._config.nights=2;return c._history().length===2;})());
