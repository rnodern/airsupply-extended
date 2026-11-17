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


const haNames=make(records);
haNames._config.entity = "sensor.resmed_871866_therapy_hours";
haNames._hass.states[haNames._config.entity] = { state:"162.83", attributes: {} };
haNames._hass.states["sensor.resmed_871866_therapy_history"] = {
  state:"3", attributes:{ nights: records },
};
check("Home Assistant named sensor resolves correctly",
  haNames._history()?.length === 3);
check("no guessing cumulative usage in dashboard mode",
  (() => { const c=make(null); c._config.view="dashboard"; return c._history() === null; })());
const customNames=make(records);
customNames._config.entity = "sensor.resmed_871866_therapy_hours";
customNames._config.history_entity = "sensor.my_custom_nights";
customNames._hass.states[customNames._config.entity] = { state:"162.83", attributes:{} };
customNames._hass.states["sensor.my_custom_nights"] = {
  state:"3", attributes:{ nights: records },
};
check("explicit history_entity works despite rename",
  customNames._history()?.length === 3);
const invalidFallback=make(null);
invalidFallback._config.view="usage";
const currentNight = new Date(2026,10,16,12);
const huge = invalidFallback._fold([{start:new Date(2026,10,16,1).getTime(),change:29.183}], currentNight);
check("29-hour usage is suppressed rather than presented",
  huge.find(n => n.at === invalidFallback._nightStart(new Date(2026,10,16,1)))?.hours === null);

const deviceSummaries = make([
  {night:"2026-10-11", usage_hours:4, ahi:2.5, source:"airsense11_summary"},
  {night:"2026-10-12", usage_hours:8, ahi:5.0, source:"airsense11_summary"},
]);
check("AirSense summary AHI is weighted by usage",
  deviceSummaries._dashboard().includes(">4.17</strong>"));
check("unreported counts are marked unavailable on the graph",
  deviceSummaries._eventChart(deviceSummaries._history()).includes("Event counts unavailable"));
check("summary records do not acquire artificial event counts",
  !deviceSummaries._history().some(n => n.ahi_events !== undefined));
const missingAHI = make([{night:"2026-10-13",usage_hours:7}]);
check("unknown AHI stays unavailable in the average",
  /Average AHI<\/span>\s*<strong>—<\/strong>/.test(missingAHI._dashboard()));
const mixedSources = make([
  {night:"2026-10-11", usage_hours:4, ahi:2.5, ahi_events:10},
  {night:"2026-10-12", usage_hours:8, ahi:5.0},
  {night:"2026-10-13", usage_hours:5},
]);
check("weighted AHI handles mixed sources and unknown nights",
  mixedSources._dashboard().includes(">4.17</strong>") &&
  mixedSources._dashboard().includes("weighted by usage · 2 nights"));
