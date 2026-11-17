/*
 * airsupply-card -- a night's sleep, fourteen of them at a time.
 *
 * The interesting number about a CPAP is not what it is doing now; it is how
 * many hours it ran last night, and the night before. Home Assistant already
 * has that: therapy hours is a total_increasing sensor, so the recorder keeps
 * the increase per hour, and a night is the increase between one midday and
 * the next.
 *
 * Midday to midday rather than midnight to midnight, because a night spans
 * midnight and calendar days would cut every one of them in half.
 *
 * No build step and no dependencies: one ES module, which is what Home
 * Assistant loads. Colours are Home Assistant's own custom properties -- a
 * card that ships its own palette fights whatever theme it lands in.
 */

const GOAL_HOURS = 4;
const NIGHTS = 14;
const NOON = 12;

// Four hours a night is the number everything clinical is measured against.
const DAY = 86400000;

class AirsupplyCard extends HTMLElement {
  static getStubConfig(hass) {
    const guess = Object.keys(hass?.states || {}).find(
      (id) => id.startsWith("sensor.") && id.endsWith("_therapy_hours") &&
        (id.includes("resmed") || id.includes("airsupply")),
    );
    return { entity: guess || "sensor.airsupply_therapy_hours" };
  }

  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("airsupply-card needs an entity: the therapy hours sensor.");
    }
    this._config = {
      nights: NIGHTS,
      goal: GOAL_HOURS,
      only_owner: false,
      view: "dashboard",
      history_entity: null,
      ...config,
    };
    this._nights = null;
    this._fetchedFor = null;
    this._problem = null;
  }

  getCardSize() {
    return 4;
  }

  set hass(hass) {
    this._hass = hass;
    if (this._hidden()) {
      this.style.display = "none";
      return;
    }
    this.style.display = "";
    if (this._config.view === "usage") this._load();
    this._render();
  }

  /* Hidden unless the viewer is the machine's owner.
   *
   * The owner's user id rides on every entity, so the card follows the
   * assignment rather than hard-coded ids. Unassigned stays visible.
   */
  _hidden() {
    if (!this._config?.only_owner) return false;
    const owner = this._hass?.states?.[this._config.entity]?.attributes?.person_user_id;
    // A machine nobody owns is not somebody else's, so it stays visible.
    if (!owner) return false;
    return this._hass?.user?.id !== owner;
  }

  /* --- the nights ------------------------------------------------------- */

  async _load() {
    const key = `${this._config.entity}|${this._config.nights}|${this._bucketOfNow()}`;
    if (this._fetchedFor === key) return;
    this._fetchedFor = key;

    const start = new Date(this._nightStart(new Date()));
    start.setTime(start.getTime() - (this._config.nights - 1) * DAY);

    try {
      const answer = await this._hass.callWS({
        type: "recorder/statistics_during_period",
        start_time: start.toISOString(),
        statistic_ids: [this._config.entity],
        period: "hour",
        types: ["change", "sum"],
      });
      this._nights = this._fold(answer?.[this._config.entity] || []);
      this._problem = null;
    } catch (err) {
      this._problem = err?.message || "No history for that sensor.";
      this._nights = null;
    }
    this._render();
  }

  /* Statistics are refetched once an hour, not on every state change. */
  _bucketOfNow() {
    return Math.floor(Date.now() / 3600000);
  }

  /* The midday a night belongs to: a sleep at 01:00 belongs to yesterday. */
  _nightStart(when) {
    const noon = new Date(when);
    noon.setHours(NOON, 0, 0, 0);
    if (when.getTime() < noon.getTime()) noon.setTime(noon.getTime() - DAY);
    return noon.getTime();
  }

  _fold(rows, now = new Date()) {
    const buckets = new Map();
    let previous = null;

    for (const row of rows) {
      const at = new Date(typeof row.start === "number" ? row.start : Date.parse(row.start));
      // `change` is what we want and what modern Home Assistant sends. Older
      // recorders answer with a running `sum` only, so difference it.
      let hours = typeof row.change === "number" ? row.change : null;
      if (hours === null && typeof row.sum === "number") {
        hours = previous === null ? 0 : row.sum - previous;
        previous = row.sum;
      }
      if (hours === null || hours < 0) continue;

      const night = this._nightStart(at);
      buckets.set(night, (buckets.get(night) || 0) + hours);
    }

    const latest = this._nightStart(now);
    const out = [];
    for (let i = this._config.nights - 1; i >= 0; i -= 1) {
      const at = latest - i * DAY;
      const hours = buckets.get(at) || 0;
      // A 29-hour therapy night is a broken recorder delta, not therapy.
      out.push({ at, hours: hours > 25 ? null : hours });
    }
    return out;
  }

  /* --- the machine's other entities ------------------------------------- */

  _sibling(suffix) {
    const id = this._config.entity;
    const stem = id.slice(id.indexOf(".") + 1).replace(/_therapy_hours$/, "");
    for (const domain of ["sensor", "binary_sensor"]) {
      const found = this._hass.states[`${domain}.${stem}_${suffix}`];
      if (found) return found;
    }
    return null;
  }

  _title() {
    if (this._config.title) return this._config.title;
    const own = this._hass.states[this._config.entity];
    const name = own?.attributes?.friendly_name || "";
    return name.replace(/\s*therapy hours$/i, "") || "Therapy";
  }

  /* --- drawing ---------------------------------------------------------- */

  _render() {
    if (!this._hass || !this._config) return;
    if (!this._root) {
      this._root = this.attachShadow({ mode: "open" });
      this._root.innerHTML = `<ha-card><div class="wrap"></div></ha-card><style>${STYLE}</style>`;
    }
    const wrap = this._root.querySelector(".wrap");
    const missing = !this._hass.states[this._config.entity];

    if (missing) {
      wrap.innerHTML = `<p class="problem">${escape(this._config.entity)} does not exist.</p>`;
      return;
    }

    const usageOnly = this._config.view === "usage";
    const history = usageOnly ? null : this._history();
    wrap.innerHTML = [
      this._head(),
      usageOnly
        ? (this._problem ? `<p class="problem">${escape(this._problem)}</p>` : this._chart())
        : history
          ? this._dashboard()
          : `<p class="problem">Nightly history is not available.
             Check the MQTT “Therapy history” entity, then add
             <code>history_entity: sensor.your_actual_therapy_history_id</code>
             to this card. The card will not guess usage from the cumulative meter.</p>`,
      usageOnly ? this._foot() : "",
    ].join("");
  }

  _head() {
    const status = this._sibling("status");
    const therapy = this._sibling("in_therapy");
    const connected = this._sibling("connected");

    const running = therapy?.state === "on";
    const live = connected ? connected.state === "on" : true;
    const lamp = running ? "running" : live ? "idle" : "gone";
    const words = running
      ? "In therapy"
      : status
        ? status.state
        : live
          ? "Idle"
          : "Disconnected";

    return `<header>
      <h2>${escape(this._title())}</h2>
      <span class="state"><i class="lamp ${lamp}"></i>${escape(words)}</span>
    </header>`;
  }

  _chart() {
    const nights = this._nights;
    if (!nights) return `<p class="problem">Reading the last fortnight&hellip;</p>`;

    const goal = this._config.goal;
    const top = Math.max(goal + 1, Math.ceil(Math.max(...nights.map((n) => n.hours))));
    const W = 100;
    const H = 46;
    const step = W / nights.length;
    // A 2px gap between bars, in the same units as everything else.
    const gap = Math.min(step * 0.25, 1.6);
    const width = step - gap;

    const y = (hours) => H - (Math.min(hours, top) / top) * H;
    const bars = nights
      .map((night, i) => {
        const x = i * step + gap / 2;
        if (night.hours === null) {
          return `<line class="empty" x1="${x + width / 2}" y1="${H}"
            x2="${x + width / 2}" y2="${H - 3}"><title>Invalid recorder total</title></line>`;
        }
        const height = H - y(night.hours);
        const short = night.hours + 0.0001 < goal;
        const label = `${dayName(night.at)} ${clock(night.hours)}`;
        if (height <= 0) {
          return `<line class="empty" x1="${x + width / 2}" y1="${H}" x2="${x + width / 2}" y2="${H - 0.6}"><title>${label}</title></line>`;
        }
        return `<path class="bar ${short ? "short" : ""}" d="${capped(x, y(night.hours), width, height)}"><title>${label}</title></path>`;
      })
      .join("");

    const line = y(goal);
    return `<div class="chart">
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img"
           aria-label="Hours of therapy on each of the last ${nights.length} nights">
        <line class="goal" x1="0" y1="${line}" x2="${W}" y2="${line}"></line>
        ${bars}
      </svg>
      <span class="goal-label" style="bottom: calc(${(1 - line / H) * 100}% - 0.55em)">${goal} h</span>
    </div>
    <div class="scale">${nights.map((n, i) => tick(n, i, nights.length)).join("")}</div>`;
  }

  /* --- AirSupply Extended nightly dashboard ----------------------------- */

  _historySensor() {
    const states = this._hass?.states || {};
    const entity = this._config.entity || "";
    const stem = entity.replace(/^sensor\./, "").replace(/_therapy_hours$/, "");
    const explicit = this._config.history_entity;
    const ids = explicit ? [explicit] : [
      `sensor.${stem}_therapy_history`,
      `sensor.${stem}_history_nights`,
    ];
    for (const id of ids) {
      const item = states[id];
      if (Array.isArray(item?.attributes?.nights)) return item;
    }
    if (explicit) return null;

    // A device's MQTT unique_id is not necessarily its Home Assistant entity_id.
    // Only select a history sensor attributable to this machine.
    const address = states[entity]?.attributes?.address;
    const candidates = Object.entries(states).filter(([id, item]) =>
      id.startsWith("sensor.") && Array.isArray(item?.attributes?.nights) &&
      (id.startsWith(`sensor.${stem}_`) ||
       (address && item.attributes.address === address)));
    return candidates.length === 1 ? candidates[0][1] : null;
  }

  _history() {
    const items = this._historySensor()?.attributes?.nights;
    if (!Array.isArray(items) || !items.length) return null;
    const byDate = new Map();
    for (const item of items) {
      if (!item || typeof item.night !== "string" ||
          !/^\d{4}-\d{2}-\d{2}$/.test(item.night)) continue;
      byDate.set(item.night, item);
    }
    const rows = [...byDate.values()].sort((a, b) => a.night.localeCompare(b.night))
      .slice(-Math.max(1, Math.min(31, Number(this._config.nights) || 14)));
    return rows.length ? rows : null;
  }

  _dashboard() {
    const nights = this._history();
    if (!nights) return '<p class="problem">No historical data received yet.</p>';

    const valid = nights.filter((n) => Number.isFinite(n.usage_hours) && n.usage_hours > 0);
    const latest = nights[nights.length - 1];
    const rates = valid.filter((n) =>
      (Number.isInteger(n.ahi_events) && n.ahi_events >= 0) ||
      (Number.isFinite(n.ahi) && n.ahi >= 0));
    const hours = rates.reduce((total, n) => total + n.usage_hours, 0);
    const events = rates.reduce((total, n) => total +
      (Number.isInteger(n.ahi_events) && n.ahi_events >= 0 ? n.ahi_events : n.ahi * n.usage_hours), 0);
    const averageAHI = hours > 0 ? events / hours : null;
    const overGoal = valid.filter((n) => n.usage_hours >= this._config.goal).length;

    return `<div class="dashboard">
      <div class="stats">
        <div class="stat"><span class="stat-label">Latest AHI</span>
          <strong>${this._number(latest.ahi)}</strong>
          <small>events per hour</small></div>
        <div class="stat"><span class="stat-label">Latest usage</span>
          <strong>${this._hours(latest.usage_hours)}</strong>
          <small>${escape(latest.night)}</small></div>
        <div class="stat"><span class="stat-label">Average AHI</span>
          <strong>${this._number(averageAHI)}</strong>
          <small>weighted by usage · ${rates.length} nights</small></div>
        <div class="stat"><span class="stat-label">Usage goal</span>
          <strong>${overGoal}<span class="stat-sub"> / ${valid.length}</span></strong>
          <small>nights ≥ ${escape(this._config.goal)} h</small></div>
      </div>
      ${this._metricChart(nights, "ahi", "AHI trend", "events/h", "ahi")}
      ${this._metricChart(nights, "usage_hours", "Therapy usage", "hours", "usage")}
      ${this._eventChart(nights)}
      <div class="history-table">
        <div class="history-heading">Nightly breakdown</div>
        <div class="history-scroll"><table><thead><tr>
          <th scope="col">Night</th><th scope="col">Usage</th>
          <th scope="col">AHI</th><th scope="col">OA</th>
          <th scope="col">CA</th><th scope="col">H</th>
          <th scope="col">RERA</th>
        </tr></thead><tbody>${nights.slice().reverse().map((n) => `<tr>
          <td>${escape(n.night)}</td><td>${this._hours(n.usage_hours)}</td>
          <td>${this._number(n.ahi)}</td>
          <td>${this._count(n.obstructive_apnoeas)}</td>
          <td>${this._count(n.central_apnoeas)}</td>
          <td>${this._count(n.hypopnoeas)}</td>
          <td>${this._count(n.rera)}</td>
        </tr>`).join("")}</tbody></table></div>
      </div>
      <p class="fineprint">AHI uses the machine's summary or logged events and therapy time.
      OA = obstructive apnoea, CA = central apnoea,
      H = hypopnoea. RERA is excluded from AHI. Unreported event counts appear as a dash. Leak and pressure
      are withheld pending unit validation.</p>
    </div>`;
  }

  _number(value) {
    return typeof value === "number" && Number.isFinite(value)
      ? escape(value.toFixed(2)) : "—";
  }

  _count(value) {
    return Number.isInteger(value) && value >= 0 ? String(value) : "—";
  }

  _hours(value) {
    if (!Number.isFinite(value) || value < 0) return "—";
    const minutes = Math.round(value * 60);
    return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
  }

  _metricChart(nights, key, title, units, kind) {
    const width = 720, height = 145, margin = 18;
    const values = nights.map((n) => Number.isFinite(n[key]) && n[key] >= 0 ? n[key] : null);
    const max = Math.max(1, kind === "usage" ? this._config.goal + 1 : 5,
      ...values.filter((v) => v !== null));
    const scale = (v) => margin + (height - 2 * margin) * (1 - v / max);
    const step = width / nights.length, bw = Math.max(2, step * .57);
    const colours = kind === "usage" ? "usage" : "ahi";
    const marks = nights.map((n, i) => {
      const value = values[i], cx = (i + .5) * step;
      if (value === null) return `<line class="missing-mark" x1="${cx}" y1="${height-margin-4}"
        x2="${cx}" y2="${height-margin}"/>`;
      const vHeight = Math.max(value === 0 ? 2 : 3, height - margin - scale(value));
      const css = kind === "usage" && value < this._config.goal ? " below-goal" : "";
      const label = `${n.night}: ${value.toFixed(2)} ${units}`;
      return `<rect class="plot-bar ${colours}${css}" x="${cx-bw/2}" y="${height-margin-vHeight}"
        width="${bw}" height="${vHeight}" rx="2"><title>${escape(label)}</title></rect>`;
    }).join("");
    const goal = kind === "usage" ? `<line class="target-line" x1="0" x2="${width}"
      y1="${scale(this._config.goal)}" y2="${scale(this._config.goal)}"/>` : "";
    return `<section class="plot">
      <div class="plot-title"><b>${escape(title)}</b>
        <span>${kind === "usage" ? escape(this._config.goal + "h goal") : "lower is generally better"}</span></div>
      <svg class="plot-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none"
        role="img" aria-label="${escape(title + " for " + nights.length + " nights")}">
        <line class="axis" x1="0" x2="${width}" y1="${height-margin}" y2="${height-margin}"/>
        ${goal}${marks}
      </svg>
      <div class="date-axis"><span>${escape(nights[0].night.slice(5))}</span>
        <span>${escape(nights[nights.length-1].night.slice(5))}</span></div>
    </section>`;
  }

  _eventChart(nights) {
    const width = 720, height = 98, bottom = 12;
    const categories = [
      ["obstructive_apnoeas", "oa"],
      ["central_apnoeas", "ca"],
      ["hypopnoeas", "hyp"],
    ];
    const totals = nights.map((n) => categories.reduce(
      (t, [key]) => t + (Number.isInteger(n[key]) && n[key] > 0 ? n[key] : 0), 0));
    const max = Math.max(1, ...totals);
    const step = width / nights.length, bw = step * .6, available = height - bottom;
    const bars = nights.map((n, i) => {
      if (categories.some(([key]) => !Number.isInteger(n[key]) || n[key] < 0)) {
        return `<line class="missing-mark" x1="${(i+.5)*step}" x2="${(i+.5)*step}"
          y1="${height-bottom-4}" y2="${height-bottom+1}">
          <title>${escape(n.night + ": Event counts unavailable")}</title></line>`;
      }
      let top = height - bottom;
      let shapes = "";
      for (const [key, cls] of categories) {
        const count = Number.isInteger(n[key]) ? Math.max(0, n[key]) : 0;
        if (!count) continue;
        const part = (count / max) * available;
        top -= part;
        shapes += `<rect class="event-segment ${cls}" x="${(i+.5)*step-bw/2}"
          y="${top}" width="${bw}" height="${part}">
          <title>${escape(n.night + ": " + count + " " + key)}</title></rect>`;
      }
      return shapes;
    }).join("");
    return `<section class="plot"><div class="plot-title"><b>Respiratory events</b>
      <span>counts per therapy night</span></div>
      <div class="legend"><span class="oa">Obstructive</span><span class="ca">Central</span>
        <span class="hyp">Hypopnoea</span></div>
      <svg class="plot-svg event-svg" viewBox="0 0 ${width} ${height}"
        preserveAspectRatio="none" role="img" aria-label="Nightly respiratory event counts">
        <line class="axis" x1="0" x2="${width}" y1="${height-bottom}" y2="${height-bottom}"/>
        ${bars}
      </svg>
      <div class="date-axis"><span>${escape(nights[0].night.slice(5))}</span>
        <span>${escape(nights[nights.length-1].night.slice(5))}</span></div>
    </section>`;
  }

  _foot() {
    const nights = this._nights;
    if (!nights) return "";
    const goal = this._config.goal;
    const met = nights.filter((n) => Number.isFinite(n.hours) && n.hours + 0.0001 >= goal).length;
    const last = nights[nights.length - 1];
    const used = this._sibling("last_used");

    return `<footer>
      <span class="big">${last.hours === null ? "—" : clock(last.hours)}</span>
      <span class="since">${last.hours === null ? "invalid recorder data" : last.hours > 0 ? "last night" : "nothing last night"}</span>
      <span class="tally">${met} of ${nights.length} nights over ${goal} h</span>
      ${used && used.state !== "unknown" ? `<span class="tally">last used ${escape(shortDate(used.state))}</span>` : ""}
    </footer>`;
  }
}

/* A bar with its data end rounded and its base square on the axis. */
function capped(x, y, w, h) {
  const r = Math.min(1.2, w / 2, h);
  return [
    `M${x} ${y + h}`,
    `V${y + r}`,
    `A${r} ${r} 0 0 1 ${x + r} ${y}`,
    `H${x + w - r}`,
    `A${r} ${r} 0 0 1 ${x + w} ${y + r}`,
    `V${y + h}`,
    "Z",
  ].join(" ");
}

function tick(night, i, total) {
  // A label under every bar is a smear at this width; the ends and the middle
  // are enough to read the axis by.
  const show = i === 0 || i === total - 1 || i === Math.floor(total / 2);
  return `<span>${show ? dayName(night.at) : ""}</span>`;
}

function dayName(at) {
  return new Date(at).toLocaleDateString(undefined, { weekday: "short" });
}

function shortDate(iso) {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  return at.toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function clock(hours) {
  if (!hours) return "0 h";
  const whole = Math.floor(hours);
  const minutes = Math.round((hours - whole) * 60);
  if (minutes === 60) return `${whole + 1} h`;
  return minutes ? `${whole} h ${minutes} m` : `${whole} h`;
}

function escape(text) {
  return String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}

const STYLE = `
  ha-card { padding: 16px; }
  .wrap { display: flex; flex-direction: column; gap: 10px; }
  header { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
  h2 { margin: 0; font-size: 16px; font-weight: 500; color: var(--primary-text-color);
       overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .state { display: inline-flex; align-items: center; gap: 6px; flex: none;
           font-size: 13px; color: var(--secondary-text-color); }
  .lamp { width: 7px; height: 7px; border-radius: 50%; background: var(--disabled-text-color, #999); }
  .lamp.running { background: var(--success-color, var(--primary-color)); }
  .lamp.idle { background: var(--primary-color); opacity: .5; }

  .chart { position: relative; }
  svg { display: block; width: 100%; height: 108px; overflow: visible; }
  .bar { fill: var(--primary-color); }
  .bar.short { fill: var(--warning-color, var(--primary-color)); opacity: .55; }
  .empty { stroke: var(--divider-color, #ccc); stroke-width: .6; stroke-linecap: round; }
  .goal { stroke: var(--secondary-text-color); stroke-width: .4; stroke-dasharray: 1.5 1.5; opacity: .6; }
  .goal-label { position: absolute; right: 0; font-size: 11px; color: var(--secondary-text-color);
                background: var(--card-background-color, var(--ha-card-background)); padding: 0 3px; }

  .scale { display: flex; margin-top: 2px; }
  .scale span { flex: 1; text-align: center; font-size: 11px; color: var(--secondary-text-color); }

  footer { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 10px; }
  .big { font-size: 24px; font-weight: 500; color: var(--primary-text-color); font-variant-numeric: tabular-nums; }
  .since, .tally { font-size: 12.5px; color: var(--secondary-text-color); }
  .tally { margin-left: auto; }
  .dashboard { display: flex; flex-direction: column; gap: 16px; }
  .stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
  .stat { display: flex; flex-direction: column; gap: 4px; border-radius: 9px;
          padding: 12px; min-width: 0; background: var(--secondary-background-color, rgba(128,128,128,.07)); }
  .stat-label, .stat small { font-size: 11px; color: var(--secondary-text-color); }
  .stat strong { font-size: 23px; font-weight: 600; font-variant-numeric: tabular-nums;
                 line-height: 1.2; color: var(--primary-text-color); }
  .stat-sub { font-size: 14px; color: var(--secondary-text-color); font-weight: 400; }
  .plot { padding: 12px; border: 1px solid var(--divider-color, #ddd); border-radius: 9px; }
  .plot-title { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .plot-title b { font-size: 13px; font-weight: 600; }
  .plot-title span, .date-axis, .fineprint { font-size: 11px; color: var(--secondary-text-color); }
  .plot-svg { height: 120px; margin-top: 8px; }
  .plot-svg.event-svg { height: 95px; }
  .plot-bar.ahi { fill: var(--primary-color, #03a9f4); }
  .plot-bar.usage { fill: var(--success-color, #4caf50); }
  .plot-bar.below-goal { fill: var(--warning-color, #ffa726); }
  .axis { stroke: var(--divider-color, #aaa); stroke-width: 1; }
  .target-line { stroke: var(--secondary-text-color, #888); stroke-width: 1;
                 stroke-dasharray: 6 6; }
  .missing-mark { stroke: var(--divider-color, #aaa); stroke-width: 3; }
  .date-axis { display: flex; justify-content: space-between; }
  .event-segment.oa { fill: var(--primary-color, #03a9f4); }
  .event-segment.ca { fill: var(--warning-color, #ff9800); }
  .event-segment.hyp { fill: var(--success-color, #4caf50); }
  .legend { display: flex; gap: 14px; flex-wrap: wrap; margin-top: 8px;
            font-size: 11px; color: var(--secondary-text-color); }
  .legend span::before { display: inline-block; width: 8px; height: 8px; margin-right: 5px;
                          content: ""; border-radius: 2px; }
  .legend .oa::before { background: var(--primary-color, #03a9f4); }
  .legend .ca::before { background: var(--warning-color, #ff9800); }
  .legend .hyp::before { background: var(--success-color, #4caf50); }
  .history-heading { margin-bottom: 8px; font-weight: 600; font-size: 13px; }
  .history-scroll { max-height: 280px; overflow: auto; }
  .history-scroll table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .history-scroll th, .history-scroll td { padding: 7px 5px; white-space: nowrap;
                                           border-bottom: 1px solid var(--divider-color, #ddd); }
  .history-scroll th { font-weight: 600; color: var(--secondary-text-color);
                       text-align: right; position: sticky; top: 0;
                       background: var(--card-background-color, var(--ha-card-background, #fff)); }
  .history-scroll th:first-child { text-align: left; }
  .history-scroll td { text-align: right; font-variant-numeric: tabular-nums; }
  .history-scroll td:first-child { text-align: left; }
  .fineprint { margin: 0; line-height: 1.55; }
  @media (max-width: 520px) {
    .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .stat strong { font-size: 20px; }
  }
  .problem { margin: 0; font-size: 13px; color: var(--error-color, var(--secondary-text-color)); }
`;

customElements.define("airsupply-extended-card", AirsupplyCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "airsupply-extended-card",
  name: "AirSupply Extended",
  description: "CPAP therapy history: AHI, usage and reported respiratory events.",
  preview: true,
  documentationURL: "https://github.com/rnodern/airsupply-extended",
});

console.info("%c airsupply-card ", "background:#0d7f74;color:#fff;border-radius:3px", "loaded");
