/* Taxi Weather dashboard. Loads the committed per-day table (data/daily.csv) and the
   Python results (data/results.json), then recomputes every effect and interval in the
   browser. The like-for-like estimator and the seeded bootstrap mirror
   analysis/weather_effect.py exactly, so the default view can be checked against
   results.json on load. */
(() => {
  "use strict";

  // ---------- Definitions shared with analysis/weather_effect.py ----------
  const BOOT_SEED = 405;
  const BOOT_REPS = 2000;
  const ZONES = { jfk: ["JFK"], cp: ["Central Park"], both: ["JFK", "Central Park"] };
  const ZONE_LABEL = { jfk: "JFK", cp: "Central Park", both: "JFK + Central Park" };
  const CONDITIONS = [
    { key: "any_rain", label: "Any rain", hint: "Any measurable rain and no snow, against fully dry days." },
    { key: "light_rain", label: "Light rain", hint: "Up to 0.10 in of rain, against dry days." },
    { key: "moderate_rain", label: "Moderate rain", hint: "0.10 to 0.30 in of rain, against dry days." },
    { key: "heavy_rain", label: "Heavy rain", hint: "More than 0.30 in of rain, against dry days." },
    { key: "snow", label: "Snow", hint: "Any snowfall, against dry days. Few days: read with care." },
    { key: "freezing", label: "Freezing", hint: "Dry days averaging below 32°F, against dry days at 32°F or above." },
    { key: "hot", label: "Hot", hint: "Dry days averaging 80°F or more, against cooler dry days." },
  ];
  const COND = Object.fromEntries(CONDITIONS.map((c) => [c.key, c]));
  const METRICS = {
    trips: { label: "Trips per day", title: "Trips per Day", short: "trips per day", fmt: (v) => fmtInt(v) },
    avg_fare: { label: "Average fare", title: "Average Fare", short: "average fare", fmt: (v) => "$" + v.toFixed(2) },
    tip_pct: { label: "Tip % (card trips)", title: "Tip % on Card Trips", short: "tip %", fmt: (v) => v.toFixed(1) + "%" },
  };
  const SEASONS = { all: null, winter: [12, 1, 2], spring: [3, 4, 5], summer: [6, 7, 8], fall: [9, 10, 11] };
  const RAIN_BUCKETS = [
    { key: "dry", label: "Dry", test: (p) => p === 0 },
    { key: "light", label: "Light", test: (p) => p > 0 && p <= 0.1 },
    { key: "moderate", label: "Moderate", test: (p) => p > 0.1 && p <= 0.3 },
    { key: "heavy", label: "Heavy", test: (p) => p > 0.3 },
  ];

  function isTreated(d, cond) {
    const p = d.prcp, s = d.snow, t = d.tavg;
    const dry = p === 0 && s === 0;
    switch (cond) {
      case "any_rain": return p > 0 && s === 0;
      case "light_rain": return p > 0 && p <= 0.1 && s === 0;
      case "moderate_rain": return p > 0.1 && p <= 0.3 && s === 0;
      case "heavy_rain": return p > 0.3 && s === 0;
      case "snow": return s > 0;
      case "freezing": return dry && t < 32;
      case "hot": return dry && t >= 80;
    }
    return false;
  }
  function isControl(d, cond) {
    const dry = d.prcp === 0 && d.snow === 0;
    if (cond === "freezing") return dry && d.tavg >= 32;
    if (cond === "hot") return dry && d.tavg < 80;
    return dry;
  }

  // mulberry32: bit-for-bit the same as weather_effect.Mulberry32
  function mulberry32(a) {
    return function () {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  function quantile(sorted, p) {
    const pos = p * (sorted.length - 1);
    const lo = Math.floor(pos);
    const hi = Math.min(lo + 1, sorted.length - 1);
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
  }

  // ---------- Formatting ----------
  const nf0 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
  function fmtInt(v) { return nf0.format(Math.round(v)); }
  function fmtPct(v, digits = 1) {
    if (v === null || v === undefined || Number.isNaN(v)) return "n/a";
    const s = Math.abs(v).toFixed(digits);
    if (Number(s) === 0) return (digits ? "0." + "0".repeat(digits) : "0") + "%";
    return (v > 0 ? "+" : "−") + s + "%";
  }
  function fmtCompact(v) {
    if (v >= 1e6) return (v / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
    if (v >= 1e3) return (v / 1e3).toFixed(v >= 1e4 ? 0 : 1).replace(/\.0$/, "") + "k";
    return fmtInt(v);
  }
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function fmtDate(ms) {
    const d = new Date(ms);
    return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}, ${d.getUTCFullYear()}`;
  }

  // ---------- Tokens ----------
  const css = getComputedStyle(document.documentElement);
  const tok = (name) => css.getPropertyValue(name).trim();
  const C = {
    ink: tok("--ink"), ink2: tok("--ink-2"), muted: tok("--muted"), faint: tok("--faint"),
    line: tok("--line"), lineStrong: tok("--line-strong"), surface: tok("--surface"), surface2: tok("--surface-2"),
    accent: tok("--accent"), accentInk: tok("--accent-ink"), highlight: tok("--highlight"),
    jfk: tok("--series-jfk"), cp: tok("--series-cp"), rain: tok("--rain"), rainSoft: tok("--rain-soft"),
    mono: tok("--mono"), display: tok("--display"),
  };
  const ZONE_COLOR = { JFK: C.jfk, "Central Park": C.cp };

  // ---------- State ----------
  const DEFAULTS = { zone: "jfk", condition: "any_rain", metric: "trips", season: "all", years: [2023, 2024, 2025], station: "cp" };
  const state = { ...DEFAULTS, years: [...DEFAULTS.years] };
  let DAYS = [];     // one object per date, both zones nested
  let RESULTS = null;
  const charts = {};

  // ---------- Data ----------
  function parseCSV(text) {
    const lines = text.trim().split(/\r?\n/);
    const head = lines[0].split(",");
    return lines.slice(1).map((line) => {
      const cells = line.split(",");
      const o = {};
      head.forEach((h, i) => { o[h] = cells[i]; });
      return o;
    });
  }

  function buildDays(rows) {
    const byDate = new Map();
    for (const r of rows) {
      let d = byDate.get(r.date);
      if (!d) {
        const [y, m] = r.date.split("-").map(Number);
        d = {
          date: r.date, ms: Date.parse(r.date + "T00:00:00Z"), year: y, month: m, ym: r.date.slice(0, 7),
          dow: Number(r.dow), holiday: r.holiday === "1",
          prcp: Number(r.prcp_in), prcp4: Number(r.prcp_max4_in), snow: Number(r.snow_in), tavg: Number(r.tavg_f),
          zones: {},
        };
        byDate.set(r.date, d);
      }
      d.zones[r.zone] = {
        trips: Number(r.trips), card_trips: Number(r.card_trips), fare_sum: Number(r.fare_sum),
        card_fare_sum: Number(r.card_fare_sum), card_tip_sum: Number(r.card_tip_sum), distance_sum: Number(r.distance_sum),
      };
    }
    return [...byDate.values()].sort((a, b) => a.ms - b.ms);
  }

  function zoneValue(d, zones, metric) {
    let trips = 0, fare = 0, cfare = 0, ctip = 0;
    for (const z of zones) {
      const v = d.zones[z];
      trips += v.trips; fare += v.fare_sum; cfare += v.card_fare_sum; ctip += v.card_tip_sum;
    }
    if (metric === "trips") return trips;
    if (metric === "avg_fare") return fare / trips;
    return (100 * ctip) / cfare;
  }

  function inFilter(d, f) {
    if (!f.years.includes(d.year)) return false;
    const months = SEASONS[f.season];
    return !months || months.includes(d.month);
  }
  function filteredDays(f = state) { return DAYS.filter((d) => inFilter(d, f)); }

  // The like-for-like estimator. Mirrors matched_effect() in analysis/weather_effect.py.
  function matchedEffect(days, zoneKey, cond, metric, reps = BOOT_REPS) {
    const zones = ZONES[zoneKey];
    const pool = days.filter((d) => !d.holiday);
    const ctrl = new Map();
    for (const d of pool) {
      if (!isControl(d, cond)) continue;
      const k = d.ym + "|" + d.dow;
      const c = ctrl.get(k) || { sum: 0, n: 0 };
      c.sum += zoneValue(d, zones, metric); c.n += 1;
      ctrl.set(k, c);
    }
    const clusters = new Map();
    let nTreated = 0, nUnmatched = 0, ctrlUsed = 0, ctrlLevel = 0;
    const usedCells = new Set();
    for (const d of pool) {
      if (!isTreated(d, cond)) continue;
      nTreated += 1;
      const k = d.ym + "|" + d.dow;
      const c = ctrl.get(k);
      if (!c) { nUnmatched += 1; continue; }
      const mean = c.sum / c.n;
      if (!clusters.has(d.ym)) clusters.set(d.ym, []);
      clusters.get(d.ym).push(zoneValue(d, zones, metric) / mean - 1);
      if (!usedCells.has(k)) { usedCells.add(k); ctrlUsed += c.n; ctrlLevel += c.sum; }
    }
    const keys = [...clusters.keys()].sort();
    const sums = keys.map((k) => clusters.get(k).reduce((a, b) => a + b, 0));
    const lens = keys.map((k) => clusters.get(k).length);
    const n = lens.reduce((a, b) => a + b, 0);
    const out = { n_treated: nTreated, n_matched: n, n_unmatched: nUnmatched, n_months: keys.length,
      n_controls: ctrlUsed, control_level: ctrlUsed ? ctrlLevel / ctrlUsed : null };
    if (n === 0) return { ...out, estimate: null, ci_low: null, ci_high: null };
    let tot = 0;
    for (const s of sums) tot += s;
    const est = tot / n;
    const rng = mulberry32(BOOT_SEED);
    const boots = new Float64Array(reps);
    const K = keys.length;
    for (let r = 0; r < reps; r++) {
      let t = 0, c = 0;
      for (let j = 0; j < K; j++) {
        const i = Math.floor(rng() * K);
        t += sums[i]; c += lens[i];
      }
      boots[r] = t / c;
    }
    const sorted = Array.from(boots).sort((a, b) => a - b);
    return { ...out, estimate: 100 * est, ci_low: 100 * quantile(sorted, 0.025), ci_high: 100 * quantile(sorted, 0.975) };
  }

  // ---------- Parity with results.json ----------
  function checkParity() {
    const all = DAYS;
    let ok = 0, total = 0, worst = 0;
    for (const z of Object.keys(ZONES)) {
      for (const c of CONDITIONS) {
        for (const m of Object.keys(METRICS)) {
          const want = RESULTS.effects[z][c.key][m];
          const got = matchedEffect(all, z, c.key, m);
          total += 1;
          const diffs = ["estimate", "ci_low", "ci_high"].map((k) => Math.abs(got[k] - want[k]));
          const d = Math.max(...diffs);
          worst = Math.max(worst, d);
          if (d < 0.005 && got.n_matched === want.n_matched) ok += 1;
        }
      }
    }
    const text = document.getElementById("status-text");
    const pill = document.getElementById("status-pill");
    const note = document.getElementById("parity-note");
    if (ok === total) {
      text.textContent = `Live · ${ok}/${total} match Python`;
      pill.title = `All ${total} effects recomputed here match results.json (largest gap ${worst.toExponential(1)} percentage points).`;
      note.textContent = `Right now all ${total} effects match, to within ${worst < 1e-9 ? "rounding" : worst.toExponential(1) + " points"}.`;
    } else {
      document.body.classList.add("is-error");
      text.textContent = `${ok}/${total} match Python`;
      note.textContent = `${total - ok} of ${total} effects differ from results.json: the data and results files are out of sync.`;
      console.warn("parity", { ok, total, worst });
    }
  }

  // ---------- SVG interval chart (forest plot) ----------
  const SVGNS = "http://www.w3.org/2000/svg";
  function el(name, attrs = {}, parent) {
    const e = document.createElementNS(SVGNS, name);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (parent) parent.appendChild(e);
    return e;
  }
  function niceStep(span, target) {
    const raw = span / target;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const n = raw / mag;
    return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * mag;
  }

  const tooltip = document.getElementById("tooltip");
  function showTip(html, evt) {
    tooltip.innerHTML = html;
    tooltip.hidden = false;
    const pad = 14;
    const w = tooltip.offsetWidth, h = tooltip.offsetHeight;
    let x = evt.clientX + pad, y = evt.clientY + pad;
    if (x + w > window.innerWidth - 8) x = evt.clientX - w - pad;
    if (y + h > window.innerHeight - 8) y = evt.clientY - h - pad;
    tooltip.style.left = Math.max(8, x) + "px";
    tooltip.style.top = Math.max(8, y) + "px";
  }
  function hideTip() { tooltip.hidden = true; }

  /* rows: [{label, selected, series: [{name, color, e}]}] where e is a matchedEffect result */
  function renderIntervals(container, rows, opts = {}) {
    const width = Math.max(280, container.clientWidth);
    const narrow = width < 520;
    const labelW = opts.labelW || (narrow ? 118 : 168);
    const perSeries = rows[0].series.length;
    const rowH = perSeries > 1 ? (narrow ? 50 : 46) : (narrow ? 44 : 40);
    const top = 8, bottom = 30, right = 14;
    const height = top + rows.length * rowH + bottom;
    const plotL = labelW, plotR = width - right;

    let lo = 0, hi = 0;
    for (const r of rows) for (const s of r.series) {
      if (s.e.estimate === null) continue;
      lo = Math.min(lo, s.e.ci_low); hi = Math.max(hi, s.e.ci_high);
    }
    const span0 = Math.max(hi - lo, 2);
    const step = niceStep(span0, narrow ? 4 : 6);
    lo = Math.floor((lo - span0 * 0.04) / step) * step;
    hi = Math.ceil((hi + span0 * 0.04) / step) * step;
    const x = (v) => plotL + ((v - lo) / (hi - lo)) * (plotR - plotL);

    container.innerHTML = "";
    const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, width, height, "aria-hidden": "true", focusable: "false" }, container);

    // grid: label every tick if there is room, else every other one (always keep 0%)
    const every = (x(step) - x(0)) < 42 ? 2 : 1;
    for (let v = lo, i = 0; v <= hi + 1e-9; v += step, i++) {
      const xx = x(v);
      const zero = Math.abs(v) < 1e-9;
      el("line", { x1: xx, x2: xx, y1: top, y2: height - bottom, stroke: zero ? C.lineStrong : C.line, "stroke-width": zero ? 1.5 : 1 }, svg);
      if (!zero && Math.round(v / step) % every !== 0) continue;
      const t = el("text", { x: xx, y: height - bottom + 18, "text-anchor": "middle", "font-family": C.mono, "font-size": 11, fill: C.muted }, svg);
      t.textContent = Math.abs(v) < 1e-9 ? "0%" : fmtPct(v, step < 1 ? 1 : 0);
    }

    rows.forEach((r, i) => {
      const y0 = top + i * rowH;
      if (r.selected) el("rect", { x: 0, y: y0 + 2, width, height: rowH - 4, rx: 8, fill: "rgba(15, 143, 98, 0.07)" }, svg);
      const name = el("text", { x: 0, y: y0 + rowH / 2 - (perSeries > 1 ? 4 : 3), "font-family": C.display, "font-size": narrow ? 12.5 : 13.5, "font-weight": r.selected ? 700 : 500, fill: C.ink }, svg);
      name.textContent = r.label;
      const sub = el("text", { x: 0, y: y0 + rowH / 2 + 12, "font-family": C.mono, "font-size": 10.5, fill: C.muted }, svg);
      const s0 = r.series[0].e;
      sub.textContent = perSeries > 1 ? `${s0.n_matched} days` : (s0.estimate === null ? "no days" : `${fmtPct(s0.estimate)} · ${s0.n_matched} days`);

      r.series.forEach((s, j) => {
        const e = s.e;
        if (e.estimate === null) return;
        const yy = perSeries > 1 ? y0 + rowH / 2 + (j - (perSeries - 1) / 2) * 14 : y0 + rowH / 2;
        const color = s.color || (r.selected ? C.accent : C.ink);
        el("line", { x1: x(e.ci_low), x2: x(e.ci_high), y1: yy, y2: yy, stroke: color, "stroke-width": 2, "stroke-linecap": "round", opacity: 0.9 }, svg);
        for (const v of [e.ci_low, e.ci_high]) el("line", { x1: x(v), x2: x(v), y1: yy - 4, y2: yy + 4, stroke: color, "stroke-width": 2, "stroke-linecap": "round" }, svg);
        el("circle", { cx: x(e.estimate), cy: yy, r: 5.5, fill: color, stroke: C.surface, "stroke-width": 2 }, svg);
      });

      const hit = el("rect", { x: 0, y: y0, width, height: rowH, class: "row-hit" }, svg);
      const tipHtml = r.series.map((s) => {
        const e = s.e;
        const head = perSeries > 1 ? `<b>${s.name}</b>: ` : "";
        if (e.estimate === null) return `${head}no matched days`;
        return `${head}${fmtPct(e.estimate)} (95% interval ${fmtPct(e.ci_low)} to ${fmtPct(e.ci_high)})<br>${e.n_matched} days in ${e.n_months} months` + (e.n_unmatched ? `, ${e.n_unmatched} with no dry match` : "");
      }).join("<br>");
      hit.addEventListener("mousemove", (evt) => showTip(`<b>${r.label}</b><br>${tipHtml}`, evt));
      hit.addEventListener("mouseleave", hideTip);
      if (opts.onSelect) {
        hit.style.cursor = "pointer";
        hit.addEventListener("click", () => opts.onSelect(r.key));
      }
    });
  }

  // ---------- Chart.js helpers ----------
  function baseChartOptions() {
    Chart.defaults.font.family = C.mono;
    Chart.defaults.font.size = 11;
    Chart.defaults.color = C.muted;
    Chart.defaults.borderColor = C.line;
    Chart.defaults.animation = { duration: 350 };
    Chart.defaults.plugins.tooltip.backgroundColor = C.ink;
    Chart.defaults.plugins.tooltip.titleFont = { family: C.mono, size: 12, weight: "600" };
    Chart.defaults.plugins.tooltip.bodyFont = { family: C.mono, size: 12 };
    Chart.defaults.plugins.tooltip.padding = 9;
    Chart.defaults.plugins.tooltip.cornerRadius = 9;
    Chart.defaults.plugins.tooltip.displayColors = false;
  }

  function monthTicks(min, max, maxTicks) {
    const a = new Date(min), b = new Date(max);
    const months = (b.getUTCFullYear() - a.getUTCFullYear()) * 12 + b.getUTCMonth() - a.getUTCMonth() + 1;
    const every = [1, 2, 3, 6, 12].find((k) => months / k <= maxTicks) || 12;
    const ticks = [];
    let y = a.getUTCFullYear(), m = a.getUTCMonth();
    while (m % every !== 0) { m += 1; if (m > 11) { m = 0; y += 1; } }
    for (;;) {
      const t = Date.UTC(y, m, 1);
      if (t > max) break;
      if (t >= min) ticks.push({ value: t });
      m += every; while (m > 11) { m -= 12; y += 1; }
    }
    return ticks;
  }
  function monthLabel(ms, span) {
    const d = new Date(ms);
    return span > 400 * 864e5 ? `${MONTHS[d.getUTCMonth()]} ’${String(d.getUTCFullYear()).slice(2)}` : `${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
  }

  function linePoints(days, zones, metric) {
    const pts = [];
    let prev = null;
    for (const d of days) {
      if (prev && d.ms - prev.ms > 864e5 * 1.5) pts.push({ x: prev.ms + 864e5, y: null });
      pts.push({ x: d.ms, y: zoneValue(d, zones, metric) });
      prev = d;
    }
    return pts;
  }

  function renderTimeSeries(days) {
    const metric = state.metric;
    const zoneList = state.zone === "both" ? ["JFK", "Central Park"] : ZONES[state.zone];
    const datasets = [];
    for (const z of zoneList) {
      datasets.push({
        type: "line", label: z, data: linePoints(days, [z], metric), borderColor: ZONE_COLOR[z],
        borderWidth: 1.25, pointRadius: 0, pointHoverRadius: 4, spanGaps: false, tension: 0, order: 2,
      });
      datasets.push({
        type: "scatter", label: `${z}: ${COND[state.condition].label.toLowerCase()} day`,
        data: days.filter((d) => isTreated(d, state.condition)).map((d) => ({ x: d.ms, y: zoneValue(d, [z], metric) })),
        pointRadius: days.length > 400 ? 2.2 : 3.2, pointHoverRadius: 5, pointBackgroundColor: C.rain, pointBorderWidth: 0, order: 1,
      });
    }
    const min = days.length ? days[0].ms : 0, max = days.length ? days[days.length - 1].ms : 1;
    const narrow = document.getElementById("ts-chart").parentElement.clientWidth < 520;
    const xScale = {
      type: "linear", min, max, grid: { display: false },
      afterBuildTicks: (s) => { s.ticks = monthTicks(min, max, narrow ? 4 : 9); },
      ticks: { callback: (v) => monthLabel(v, max - min), maxRotation: 0, autoSkip: false },
    };
    const fmt = METRICS[metric].fmt;
    const tsTooltip = {
      callbacks: {
        title: (items) => fmtDate(items[0].raw.x),
        label: (item) => {
          const d = DAYS.find((x) => x.ms === item.raw.x);
          const z = item.dataset.label.split(":")[0];
          const w = d ? ` · ${d.prcp.toFixed(2)} in rain${d.snow > 0 ? `, ${d.snow.toFixed(1)} in snow` : ""}, ${Math.round(d.tavg)}°F` : "";
          return `${z}: ${fmt(item.raw.y)}${w}`;
        },
      },
    };
    const cfg = {
      data: { datasets },
      options: {
        responsive: true, maintainAspectRatio: false, parsing: false, normalized: true,
        interaction: { mode: "nearest", axis: "x", intersect: false },
        plugins: { legend: { display: false }, tooltip: tsTooltip },
        scales: {
          x: { ...xScale, ticks: { ...xScale.ticks, display: false } },
          y: { afterFit: (sc) => { sc.width = 52; }, grid: { color: C.line }, border: { display: false }, ticks: { callback: (v) => (metric === "trips" ? fmtCompact(v) : fmt(v)), maxTicksLimit: 6 } },
        },
      },
    };
    if (charts.ts) { charts.ts.destroy(); }
    charts.ts = new Chart(document.getElementById("ts-chart"), cfg);

    const prcpData = days.map((d) => ({ x: d.ms, y: d.prcp }));
    const pcfg = {
      type: "bar",
      data: { datasets: [{ label: "Precipitation (in)", data: prcpData, backgroundColor: C.rain, borderWidth: 0, barThickness: narrow ? 1 : 1.6, grouped: false }] },
      options: {
        responsive: true, maintainAspectRatio: false, parsing: false,
        plugins: { legend: { display: false }, tooltip: { callbacks: { title: (it) => fmtDate(it[0].raw.x), label: (it) => `${it.raw.y.toFixed(2)} in of precipitation` } } },
        scales: {
          x: xScale,
          y: { afterFit: (sc) => { sc.width = 52; }, min: 0, grid: { color: C.line }, border: { display: false }, ticks: { maxTicksLimit: 3, callback: (v) => v + " in" } },
        },
      },
    };
    if (charts.prcp) charts.prcp.destroy();
    charts.prcp = new Chart(document.getElementById("prcp-chart"), pcfg);

    document.getElementById("ts-title").textContent = METRICS[metric].title;
    document.getElementById("ts-legend").innerHTML = zoneList.map((z) => `<span class="key"><span class="sw" style="background:${ZONE_COLOR[z]}"></span>${z}</span>`).join("") +
      `<span class="key"><span class="sw-mark"></span>${COND[state.condition].label} day</span>`;
  }

  function barChart(id, labels, values, fmt, color) {
    const cfg = {
      type: "bar",
      data: { labels, datasets: [{ data: values, backgroundColor: color, borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: "bottom", maxBarThickness: 46 }] },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { callbacks: { title: (it) => it[0].label.replace(",", " · "), label: (it) => fmt(it.raw) } } },
        scales: {
          x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkip: false, font: { size: 10.5 } } },
          y: { beginAtZero: true, grid: { color: C.line }, border: { display: false }, ticks: { maxTicksLimit: 5, callback: (v) => fmtCompact(v) } },
        },
      },
    };
    if (charts[id]) charts[id].destroy();
    charts[id] = new Chart(document.getElementById(id), cfg);
  }

  // ---------- Render ----------
  function renderAll() {
    const days = filteredDays();
    const zone = state.zone, metric = state.metric, cond = state.condition;
    const filterLabel = describeFilter();

    // Effects for every condition in the current zone, metric and filter
    const effects = Object.fromEntries(CONDITIONS.map((c) => [c.key, matchedEffect(days, zone, c.key, metric)]));
    const e = effects[cond];

    // KPI 1
    document.getElementById("k1-label").textContent = `${COND[cond].label} · ${ZONE_LABEL[zone]} · ${METRICS[metric].short}`;
    document.getElementById("k1-value").textContent = fmtPct(e.estimate);
    document.getElementById("k1-foot").textContent = e.estimate === null
      ? "No matched days in this view"
      : `95% interval ${fmtPct(e.ci_low)} to ${fmtPct(e.ci_high)} · ${e.n_matched} days in ${e.n_months} months`;

    // KPI 2: verdict
    const v2 = document.getElementById("k2-value");
    const f2 = document.getElementById("k2-foot");
    v2.className = "kpi-value kpi-value-sm";
    if (e.estimate === null) { v2.textContent = "No data"; f2.textContent = "Widen the filters to include matching days."; }
    else if (e.ci_low > 0) { v2.textContent = "Clearly higher"; v2.classList.add("verdict-up"); f2.textContent = "The whole interval sits above zero."; }
    else if (e.ci_high < 0) { v2.textContent = "Clearly lower"; v2.classList.add("verdict-down"); f2.textContent = "The whole interval sits below zero."; }
    else { v2.textContent = "No clear effect"; v2.classList.add("verdict-flat"); f2.textContent = "The interval spans zero: within ordinary day-to-day noise."; }
    if (e.estimate !== null && e.n_matched < 30) f2.textContent += ` Only ${e.n_matched} days, so read with care.`;

    // KPI 3: typical comparison day
    document.getElementById("k3-label").textContent = cond === "freezing" ? "Typical dry, non-freezing day" : cond === "hot" ? "Typical dry, cooler day" : "Typical dry day";
    document.getElementById("k3-value").textContent = e.control_level === null ? "n/a" : METRICS[metric].fmt(e.control_level);
    document.getElementById("k3-foot").textContent = e.control_level === null ? "" :
      `${METRICS[metric].short} across ${fmtInt(e.n_controls)} comparison days in the same months and weekdays`;

    // KPI 4: the naive totals reading of the same condition
    const zones = ZONES[zone];
    let tTot = 0, cTot = 0, tDays = 0, cDays = 0;
    for (const d of days) {
      if (isTreated(d, cond)) { tTot += zoneValue(d, zones, "trips"); tDays += 1; }
      else if (isControl(d, cond)) { cTot += zoneValue(d, zones, "trips"); cDays += 1; }
    }
    const naive = cTot ? (tTot / cTot - 1) * 100 : null;
    document.getElementById("k4-value").textContent = fmtPct(naive, 0);
    document.getElementById("k4-foot").textContent = cTot
      ? `in total trips, ${COND[cond].label.toLowerCase()} vs comparison days. It mostly counts days: ${fmtInt(tDays)} against ${fmtInt(cDays)}.`
      : "";

    // Forest
    renderIntervals(document.getElementById("forest-chart"),
      CONDITIONS.map((c) => ({ key: c.key, label: c.label, selected: c.key === cond, series: [{ e: effects[c.key] }] })),
      { onSelect: (k) => { state.condition = k; document.getElementById("condition").value = k; update(); } });
    document.getElementById("forest-sub").textContent = `${ZONE_LABEL[zone]} · ${METRICS[metric].label.toLowerCase()} · ${filterLabel}`;
    const clear = CONDITIONS.filter((c) => effects[c.key].estimate !== null && (effects[c.key].ci_low > 0 || effects[c.key].ci_high < 0));
    document.getElementById("forest-foot").textContent = clear.length
      ? `Intervals that exclude zero here: ${clear.map((c) => `${c.label.toLowerCase()} (${fmtPct(effects[c.key].estimate)})`).join(", ")}. With 7 conditions tested, expect the odd one to clear zero by chance. Click a row to select it.`
      : "Every interval spans zero in this view. Click a row to select it.";

    renderTimeSeries(days);
    renderNaive(days);
    renderZones(days);
    renderTable(effects);
  }

  function renderNaive(days) {
    const zones = ZONES[state.zone];
    const key = state.station === "max4" ? "prcp4" : "prcp";
    const tot = RAIN_BUCKETS.map(() => 0), cnt = RAIN_BUCKETS.map(() => 0);
    for (const d of days) {
      const i = RAIN_BUCKETS.findIndex((b) => b.test(d[key]));
      tot[i] += zoneValue(d, zones, "trips"); cnt[i] += 1;
    }
    const labels = RAIN_BUCKETS.map((b, i) => [b.label, `${cnt[i]} days`]);
    barChart("naive-total", labels, tot, (v) => `${fmtInt(v)} trips in total`, C.faint);
    barChart("naive-perday", labels, tot.map((t, i) => (cnt[i] ? t / cnt[i] : 0)), (v) => `${fmtInt(v)} trips per day`, C.ink2);
    const rainKeys = ["light_rain", "moderate_rain", "heavy_rain"];
    renderIntervals(document.getElementById("naive-matched"),
      rainKeys.map((k) => ({ key: k, label: COND[k].label.replace(" rain", ""), selected: false, series: [{ e: matchedEffect(days, state.zone, k, "trips"), color: C.accent }] })),
      { labelW: 92 });
    const dryShare = tot[0] / tot.reduce((a, b) => a + b, 0);
    const dayShare = cnt[0] / cnt.reduce((a, b) => a + b, 0);
    const heavyPerDay = cnt[3] && cnt[0] ? ((tot[3] / cnt[3]) / (tot[0] / cnt[0]) - 1) * 100 : null;
    document.getElementById("naive-foot").textContent =
      `${ZONE_LABEL[state.zone]}, trips. Dry days hold ${(dryShare * 100).toFixed(0)}% of all trips because ${(dayShare * 100).toFixed(0)}% of days were dry. ` +
      `Per day, heavy rain runs ${heavyPerDay === null ? "(no heavy days) vs" : Math.abs(heavyPerDay) < 0.05 ? "level with" : fmtPct(heavyPerDay) + " against"} dry days before any controls, and step 3 compares each wet day only with dry days in its own month and weekday ` +
      `(Central Park buckets). ${state.station === "max4" ? "The wettest of JFK, LaGuardia, Newark and Central Park moves days out of Dry and into Heavy; the original took the max over every station in its NOAA export." : "Switch to “Max of 4 stations” to see how taking the wettest gauge reshuffles the buckets."}`;
  }

  function renderZones(days) {
    const metric = state.metric;
    renderIntervals(document.getElementById("zones-chart"),
      CONDITIONS.map((c) => ({
        key: c.key, label: c.label, selected: c.key === state.condition,
        series: [
          { name: "JFK", color: C.jfk, e: matchedEffect(days, "jfk", c.key, metric) },
          { name: "Central Park", color: C.cp, e: matchedEffect(days, "cp", c.key, metric) },
        ],
      })));
    document.getElementById("zones-chart-title").textContent = `${METRICS[metric].title}: Effect by Condition`;
    document.getElementById("zones-legend").innerHTML =
      `<span class="key"><span class="sw-dot" style="background:${C.jfk}"></span>JFK</span><span class="key"><span class="sw-dot" style="background:${C.cp}"></span>Central Park</span>`;

    const prof = {};
    for (const z of ["JFK", "Central Park"]) {
      let trips = 0, fare = 0, cf = 0, ct = 0, dist = 0, card = 0;
      for (const d of days) { const v = d.zones[z]; trips += v.trips; fare += v.fare_sum; cf += v.card_fare_sum; ct += v.card_tip_sum; dist += v.distance_sum; card += v.card_trips; }
      prof[z] = { perDay: trips / days.length, fare: fare / trips, tip: (100 * ct) / cf, miles: dist / trips, card: (100 * card) / trips, trips };
    }
    const rows = [
      ["Trips per day", (p) => fmtInt(p.perDay)],
      ["Average fare", (p) => "$" + p.fare.toFixed(2)],
      ["Tip % (card)", (p) => p.tip.toFixed(1) + "%"],
      ["Average miles", (p) => p.miles.toFixed(1)],
      ["Paid by card", (p) => p.card.toFixed(0) + "%"],
      ["Trips in view", (p) => fmtCompact(p.trips)],
    ];
    document.querySelector("#profile tbody").innerHTML = rows.map(([label, f]) =>
      `<tr><td>${label}</td><td>${f(prof.JFK)}</td><td>${f(prof["Central Park"])}</td></tr>`).join("");
    document.querySelector("#profile thead").innerHTML =
      `<tr><th scope="col"><span class="sr-only">Measure</span></th><th scope="col"><span class="zone-key" style="background:${C.jfk}"></span>JFK</th><th scope="col"><span class="zone-key" style="background:${C.cp}"></span>Central Park</th></tr>`;
    document.getElementById("profile-tag").textContent = `${days.length} days`;
  }

  function renderTable(effects) {
    document.querySelector("#effects-table tbody").innerHTML = CONDITIONS.map((c) => {
      const e = effects[c.key];
      if (e.estimate === null) return `<tr><td>${c.label}</td><td>n/a</td><td>n/a</td><td>0</td></tr>`;
      return `<tr><td>${c.label}</td><td>${fmtPct(e.estimate)}</td><td>${fmtPct(e.ci_low)} to ${fmtPct(e.ci_high)}</td><td>${e.n_matched}</td></tr>`;
    }).join("");
  }

  function describeFilter() {
    const seasonTxt = { all: "all year", winter: "winters", spring: "springs", summer: "summers", fall: "falls" }[state.season];
    const ys = [...state.years].sort();
    const yearTxt = ys.length === 3 ? "2023 to 2025" : ys.join(", ");
    return `${seasonTxt}, ${yearTxt}`;
  }

  function renderStatic() {
    const totalTrips = DAYS.reduce((a, d) => a + d.zones.JFK.trips + d.zones["Central Park"].trips, 0);
    document.getElementById("chip-trips").textContent = `${(totalTrips / 1e6).toFixed(1)}M trips`;
    document.getElementById("chip-days").textContent = `${fmtInt(DAYS.length)} days × 2 zones`;
    const dq = RESULTS.data_quality;
    const lostCP = dq.spark_date_dedupe_share_lost["Central Park"] * 100;
    const lostJFK = dq.spark_date_dedupe_share_lost.JFK * 100;
    const gaps = dq.data_gap_dates.map((g) => g.date);
    const facts = [
      `${fmtInt(totalTrips)} yellow taxi pickups at JFK (TLC zone 132) and Central Park (zone 43), after the Spark job's row filters.`,
      `Weather from one NOAA station, NY City Central Park (USW00094728): precipitation, snowfall, and the average of the daily high and low.`,
      `${gaps.length} days left out (${gaps[0]} to ${gaps[gaps.length - 1]}): the raw TLC files are nearly empty then, citywide.`,
      `Only exact duplicate records are dropped. The original Spark dedupe keyed on the pickup date and fare, which removed ${lostCP.toFixed(0)}% of Central Park trips and ${lostJFK.toFixed(0)}% of JFK trips.`,
      `Tip % uses card trips only, since cash tips are not recorded.`,
    ];
    document.getElementById("facts").innerHTML = facts.map((f) => `<li>${f}</li>`).join("");
  }

  // ---------- Controls ----------
  function readControls() {
    const form = document.getElementById("controls");
    state.zone = form.querySelector('input[name="zone"]:checked').value;
    state.metric = form.querySelector('input[name="metric"]:checked').value;
    state.condition = document.getElementById("condition").value;
    state.season = document.getElementById("season").value;
    const ys = [...document.querySelectorAll("#years input:checked")].map((i) => Number(i.value));
    if (!ys.length) { // keep at least one year selected
      const first = document.querySelector("#years input");
      first.checked = true; ys.push(Number(first.value));
    }
    state.years = ys;
    state.station = document.querySelector('input[name="station"]:checked').value;
  }
  function writeControls() {
    document.querySelector(`input[name="zone"][value="${state.zone}"]`).checked = true;
    document.querySelector(`input[name="metric"][value="${state.metric}"]`).checked = true;
    document.querySelector(`input[name="station"][value="${state.station}"]`).checked = true;
    document.getElementById("condition").value = state.condition;
    document.getElementById("season").value = state.season;
    document.querySelectorAll("#years input").forEach((i) => { i.checked = state.years.includes(Number(i.value)); });
  }
  let raf = 0;
  function update() {
    document.getElementById("condition-hint").textContent = COND[state.condition].hint;
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(renderAll);
  }

  function initControls() {
    const sel = document.getElementById("condition");
    sel.innerHTML = CONDITIONS.map((c) => `<option value="${c.key}">${c.label}</option>`).join("");
    sel.value = state.condition;
    document.getElementById("controls").addEventListener("change", () => { readControls(); update(); });
    document.querySelectorAll('input[name="station"]').forEach((i) => i.addEventListener("change", () => { readControls(); renderNaive(filteredDays()); }));
    document.getElementById("reset").addEventListener("click", () => {
      Object.assign(state, { ...DEFAULTS, years: [...DEFAULTS.years] });
      writeControls(); update();
    });

    // Re-render the SVG charts when their width changes
    let lastW = 0, t = 0;
    new ResizeObserver((entries) => {
      const w = Math.round(entries[0].contentRect.width);
      if (Math.abs(w - lastW) < 4) return;
      lastW = w;
      clearTimeout(t);
      t = setTimeout(renderAll, 120);
    }).observe(document.querySelector(".main-col"));

    // Highlight the section in view
    const links = [...document.querySelectorAll(".topnav a")];
    const io = new IntersectionObserver((entries) => {
      for (const en of entries) if (en.isIntersecting) {
        links.forEach((a) => a.classList.toggle("is-active", a.getAttribute("href") === "#" + en.target.id));
      }
    }, { rootMargin: "-40% 0px -55% 0px" });
    ["effect", "daily", "naive", "zones", "method"].forEach((id) => io.observe(document.getElementById(id)));
  }

  async function boot() {
    try {
      const [csv, res] = await Promise.all([
        fetch("data/daily.csv").then((r) => { if (!r.ok) throw new Error("daily.csv " + r.status); return r.text(); }),
        fetch("data/results.json").then((r) => { if (!r.ok) throw new Error("results.json " + r.status); return r.json(); }),
      ]);
      DAYS = buildDays(parseCSV(csv));
      RESULTS = res;
      baseChartOptions();
      initControls();
      renderStatic();
      document.getElementById("condition-hint").textContent = COND[state.condition].hint;
      renderAll(); // first render synchronously: background tabs pause animation frames
      document.body.classList.remove("is-loading");
      document.getElementById("status-text").textContent = `${fmtInt(DAYS.length)} days loaded`;
      setTimeout(checkParity, 50);
    } catch (err) {
      console.error(err);
      document.body.classList.add("is-error");
      document.body.classList.remove("is-loading");
      document.getElementById("status-text").textContent = "Data failed to load";
    }
  }

  // Exposed for checking in the console
  window.taxiWeather = { matchedEffect, mulberry32, get days() { return DAYS; } };
  boot();
})();
