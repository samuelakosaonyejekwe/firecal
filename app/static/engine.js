/* FireCal analysis engine for the browser (static edition).
 *
 * A faithful port of app/analysis.py (Store.harmonize, Store._analyze, Store.nowcast) so that any
 * drawn box can be analyzed without a server. tests/test_engine_parity.py runs this file under
 * Node and checks its output against the Python implementation on the same data.
 */
(function (root) {
  "use strict";

  const DAY = 864e5;
  const MODIS_START = Date.UTC(2000, 10, 1);
  const AQUA_START = Date.UTC(2002, 6, 4);
  const VIIRS_START = Date.UTC(2012, 0, 20);
  const TERRA_DRIFT = Date.UTC(2022, 0, 1); // as app/constants.py
  const LAMBDA = 20, LAMBDA_AREA = 50;
  // reliability flags and map size: the same values as app/constants.py (tests/test_engine_parity.py)
  const MIN_OVERLAP_CELL_DAYS = 3000, MIN_R2 = 0.5, MAX_CV_ERROR = 15, MAX_MAP_CELLS = 20000, CELL = 10;
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  // ---------- small numeric helpers (numpy/pandas semantics) ----------
  const isNum = (v) => v != null && Number.isFinite(v);
  const roundEven = (x) => { const f = Math.floor(x), d = x - f; return d > 0.5 ? f + 1 : d < 0.5 ? f : (f % 2 === 0 ? f : f + 1); };
  // Python's round(x, nd): the nearest value with nd decimals, ties (x·2^(nd+1) an odd integer) to the even digit
  const r = (x, nd = 2) => {
    if (!isNum(x)) return null;
    const y = x * 2 ** (nd + 1);
    if (!(Number.isInteger(y) && y % 2 !== 0)) return Number(x.toFixed(nd));
    const lo = Math.floor(x * 10 ** nd);
    return (lo % 2 === 0 ? lo : lo + 1) / 10 ** nd;
  };
  const rl = (a, nd = 2) => Array.from(a, (v) => r(v, nd));
  const sum = (a) => a.reduce((s, v) => s + v, 0);
  const nanmean = (a) => { const b = a.filter(isNum); return b.length ? sum(b) / b.length : NaN; };
  const nanstd1 = (a) => { const b = a.filter(isNum); if (b.length < 2) return NaN; const m = sum(b) / b.length; return Math.sqrt(sum(b.map((v) => (v - m) ** 2)) / (b.length - 1)); };
  function quantile(a, q) { // numpy default (linear)
    const b = a.filter(isNum).sort((x, y) => x - y);
    if (!b.length) return NaN;
    const pos = (b.length - 1) * q, lo = Math.floor(pos), hi = Math.ceil(pos);
    return b[lo] + (b[hi] - b[lo]) * (pos - lo);
  }
  const median = (a) => quantile(a, 0.5);
  const iso = (t) => new Date(t).toISOString().slice(0, 10);
  const ymd = (t) => { const d = new Date(t); return [d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()]; };
  const fmtDM = (t) => { const [, m, d] = ymd(t); return `${String(d).padStart(2, "0")} ${MONTHS[m]}`; };

  // ---------- harmonization ----------
  function monthlySums(days, cols) {
    // one row per calendar month: {ts, y, mon, <col>: sum} (pandas resample('MS').sum(min_count=1))
    const rows = [], idx = new Map();
    for (let i = 0; i < days.length; i++) {
      const [y, mo] = ymd(days[i]);
      const key = y * 12 + mo;
      let row = idx.get(key);
      if (!row) { row = { ts: Date.UTC(y, mo, 1), y, mon: mo + 1 }; for (const name in cols) row[name] = NaN; idx.set(key, row); rows.push(row); }
      for (const name in cols) { const v = cols[name][i]; if (isNum(v)) row[name] = (isNum(row[name]) ? row[name] : 0) + v; }
    }
    return rows;
  }

  function fitK(monthly, src, prior, excludeYear) {
    const ov = monthly.filter((r) => r.overlap && (src !== "t" || r.ts < TERRA_DRIFT) && (excludeYear == null || r.y !== excludeYear));
    const kArea = (sum(ov.map((r) => r.v)) + LAMBDA_AREA * prior) / (sum(ov.map((r) => r[src])) + LAMBDA_AREA);
    const k = new Array(12);
    for (let mon = 1; mon <= 12; mon++) {
      const g = ov.filter((r) => r.mon === mon);
      k[mon - 1] = (sum(g.map((r) => r.v)) + LAMBDA * kArea) / (sum(g.map((r) => r[src])) + LAMBDA);
    }
    return [kArea, k];
  }

  function crossValidate(monthly, src, prior) {
    const ov = monthly.filter((r) => r.overlap && (src !== "t" || r.ts < TERRA_DRIFT));
    const out = [];
    for (const y of [...new Set(ov.map((r) => r.y))].sort((a, b) => a - b)) {
      const g = ov.filter((r) => r.y === y);
      if (g.length < 12) continue;
      const [, ky] = fitK(monthly, src, prior, y);
      const pred = sum(g.map((r) => r[src] * ky[r.mon - 1])), obs = sum(g.map((r) => r.v));
      out.push({ year: y, obs: r(obs, 1), pred: r(pred, 1), raw: r(sum(g.map((r) => r[src])), 1),
                 ape: obs > 0 ? r((Math.abs(pred - obs) / obs) * 100, 1) : null });
    }
    const apes = out.map((c) => c.ape).filter(isNum);
    return [out, apes.length ? r(median(apes), 1) : null];
  }

  function harmonize(S, prior, end) {
    const { days, m, t, v } = S; // v: NaN before VIIRS and on its outage days (see analyze)
    const mg = S.mg || new Float64Array(days.length); // MODIS cells where VIIRS lost an orbit (data.js)
    // sensor outages (prior, from pipeline/prior.py), read as analysis.py does: from the sensor that recorded the
    // day, and left out of every comparison of the sensors
    const modisOut = new Set(prior.modis_gaps || []), aquaOut = new Set(prior.aqua_gaps || []);
    const gap = Array.from(days, (d, i) => d >= VIIRS_START && !isNum(v[i]));
    const strip = Array.from(days, (d, i) => d >= VIIRS_START && !gap[i] && mg[i] > 0);
    const aqua = Array.from(days, (d) => aquaOut.has(iso(d)) && !modisOut.has(iso(d)));
    const mout = Array.from(days, (d) => modisOut.has(iso(d)));
    const unknown = Array.from(days, (d, i) => mout[i] && (d < VIIRS_START || gap[i]));
    const out = Array.from(days, (d, i) => gap[i] || strip[i] || aqua[i] || mout[i]);
    const mf = m.map((x, i) => (out[i] ? NaN : x)), tf = t.map((x, i) => (out[i] ? NaN : x)), vf = v.map((x, i) => (out[i] ? NaN : x));
    const monthly = monthlySums(days, { m: mf, t: tf, v: vf });
    const ovStart = Date.UTC(2012, 1, 1); // VIIRS_START rolled forward to the next month start
    for (const row of monthly) row.overlap = row.ts >= ovStart && isNum(row.v);
    const [kAll, k] = fitK(monthly, "m", prior.k_world, null);
    const [ktAll, kt] = fitK(monthly, "t", prior.k_terra_world, null);
    const h = new Float64Array(days.length);
    for (let i = 0; i < days.length; i++) {
      const mo = ymd(days[i])[1], modis = aqua[i] ? t[i] * kt[mo] : m[i] * k[mo];
      h[i] = days[i] < AQUA_START ? t[i] * kt[mo] : days[i] < VIIRS_START || gap[i] ? modis : v[i] + mg[i] * k[mo];
    }
    // no usable record: the area's usual fire on that calendar day, scaled to the level of the month's recorded
    // days (as analysis.py)
    const scale = {};
    if (unknown.some(Boolean)) {
      const key = (i) => { const [, mo, d] = ymd(days[i]); return mo * 32 + (mo === 1 && d === 29 ? 28 : d); };
      const ymk = (i) => { const [y, mo] = ymd(days[i]); return y * 12 + mo; };
      const cl = new Map(), rec = new Map(), usual = new Map(), tot = new Map();
      for (let i = 0; i < days.length; i++) {
        if (unknown[i]) continue;
        const a = cl.get(key(i)) || [0, 0]; a[0] += h[i]; a[1]++; cl.set(key(i), a);
      }
      const clim = (i) => { const a = cl.get(key(i)); return a ? a[0] / a[1] : 0; };
      for (let i = 0; i < days.length; i++) {
        if (unknown[i]) continue;
        rec.set(ymk(i), (rec.get(ymk(i)) || 0) + h[i]); usual.set(ymk(i), (usual.get(ymk(i)) || 0) + clim(i));
      }
      for (let i = 0; i < days.length; i++) {
        if (!unknown[i]) continue;
        const r0 = (rec.get(ymk(i)) || 0) / (usual.get(ymk(i)) || 0);
        h[i] = clim(i) * (Number.isFinite(r0) ? r0 : 1);
      }
      for (let i = 0; i < days.length; i++) tot.set(ymk(i), (tot.get(ymk(i)) || 0) + h[i]);
      for (let i = 0; i < days.length; i++) {
        if (!unknown[i]) continue;
        const [y, mo] = ymd(days[i]), recd = rec.get(ymk(i)) || 0;
        scale[`${y}-${String(mo + 1).padStart(2, "0")}`] = recd > 0 ? r(tot.get(ymk(i)) / recd, 4) : 1.0;
      }
    }
    const ov = monthly.filter((r) => r.overlap);
    const preds = ov.map((row) => row.m * k[row.mon - 1]);
    const vbar = ov.length ? sum(ov.map((r) => r.v)) / ov.length : NaN;
    const ssRes = sum(ov.map((row, i) => (row.v - preds[i]) ** 2)), ssTot = sum(ov.map((row) => (row.v - vbar) ** 2));
    const r2 = ssTot > 0 ? 1 - ssRes / ssTot : null;
    const [cv, cvApe] = crossValidate(monthly, "m", prior.k_world);
    const [, cvApeT] = crossValidate(monthly, "t", prior.k_terra_world);
    // VIIRS-independent check: Terra-only vs Terra+Aqua reconstructions, 2003-2011
    const yA = new Map(), yB = new Map();
    for (let i = 0; i < days.length; i++) {
      const [y, mo] = ymd(days[i]);
      if (y < 2003 || y >= 2012) continue;
      if (out[i]) continue;
      yA.set(y, (yA.get(y) || 0) + m[i] * k[mo]); yB.set(y, (yB.get(y) || 0) + t[i] * kt[mo]);
    }
    const errs = [...yA.keys()].filter((y) => yA.get(y) > 0).map((y) => (Math.abs(yB.get(y) - yA.get(y)) / yA.get(y)) * 100);
    const nOv = sum(ov.map((r) => r.v));
    return {
      h,
      cal: {
        k_all: r(kAll, 3), k_month: rl(k, 3), k_terra_all: r(ktAll, 3), k_terra_month: rl(kt, 3),
        k_world: r(prior.k_world, 3), k_terra_world: r(prior.k_terra_world, 3), r2_monthly: r(r2, 3),
        cv, cv_median_ape: cvApe, cv_median_ape_terra: cvApeT,
        terra_check_ape: errs.length ? r(median(errs), 1) : null,
        // outage days, by how they were read (as analysis.py)
        viirs_gaps: Array.from(days).filter((d, i) => gap[i] && !unknown[i]).map(iso),
        viirs_strips: Array.from(days).filter((d, i) => strip[i]).map(iso),
        terra_only: Array.from(days).filter((d, i) => aqua[i] && (d < VIIRS_START || gap[i]) && !unknown[i]).map(iso),
        no_record: Array.from(days).filter((d, i) => unknown[i]).map(iso),
        no_record_scale: scale,
        overlap_viirs_cell_days: r(nOv, 0), low_counts: nOv < MIN_OVERLAP_CELL_DAYS || r2 == null || r2 < MIN_R2 || cvApe == null || cvApe > MAX_CV_ERROR,
        scatter: ov.map((row) => [r(row.m, 1), r(row.v, 1), row.mon]),
        periods: [
          { from: iso(MODIS_START), to: iso(AQUA_START - DAY), source: "MODIS Terra × k_terra" },
          { from: iso(AQUA_START), to: iso(VIIRS_START - DAY), source: "MODIS Terra+Aqua × k" },
          { from: iso(VIIRS_START), to: iso(end), source: "VIIRS S-NPP (reference)" },
        ],
      },
    };
  }

  // ---------- calendar analytics ----------
  function analyze(S, prior, meta) {
    const end = Date.UTC(S.endYear, 11, 31);
    // VIIRS outage days (prior.viirs_gaps, from pipeline/prior.py): no record, not "no fire"
    const gaps = new Set(prior.viirs_gaps || []);
    if (gaps.size) S = { ...S, v: S.v.map((x, i) => (gaps.has(iso(S.days[i])) ? NaN : x)) };
    const { days } = S;
    const N = days.length;
    const { h, cal } = harmonize(S, prior, end);
    const h7 = new Float64Array(N);
    for (let i = 0; i < N; i++) { let s = 0, c = 0; for (let j = Math.max(0, i - 3); j <= Math.min(N - 1, i + 3); j++) { s += h[j]; c++; } h7[i] = s / c; }
    const startYear = 2000, firstFull = 2001, lastFull = S.endYear;
    const years = []; for (let y = startYear; y <= S.endYear; y++) years.push(y);

    // month x year matrix + anomalies
    const mat = years.map(() => new Array(12).fill(NaN));
    for (let i = 0; i < N; i++) { const [y, mo] = ymd(days[i]); const row = mat[y - startYear]; row[mo] = (isNum(row[mo]) ? row[mo] : 0) + h[i]; }
    const base = years.map((y, i) => [y, mat[i]]).filter(([y]) => y >= firstFull && y <= lastFull).map(([, row]) => row);
    const mu = [], sd = [];
    for (let mo = 0; mo < 12; mo++) { const col = base.map((row) => row[mo]); mu.push(nanmean(col)); sd.push(nanstd1(col)); }
    const z = mat.map((row) => row.map((v, mo) => (sd[mo] > 0 && isNum(v) ? (v - mu[mo]) / sd[mo] : NaN)));
    const total = sum(mu.filter(isNum));
    const busy = mu.map((v) => v >= Math.max(1, 0.02 * total));
    let unusual = [];
    years.forEach((y, yi) => { for (let mo = 0; mo < 12; mo++) { const zz = z[yi][mo];
      if (isNum(zz) && Math.abs(zz) >= 2 && busy[mo]) unusual.push({ year: y, month: mo + 1, z: r(zz), value: r(mat[yi][mo], 1), normal: r(mu[mo], 1) }); } });
    unusual.sort((a, b) => Math.abs(b.z) - Math.abs(a.z));

    // day-of-year climatology of the 7-day mean (Feb 29 folded into Feb 28)
    const byKey = new Map(); // key -> Map(year -> [sum, n])
    for (let i = 0; i < N; i++) {
      const [y, mo, d] = ymd(days[i]);
      if (y < firstFull || y > lastFull) continue;
      const key = `${String(mo + 1).padStart(2, "0")}-${String(mo === 1 && d === 29 ? 28 : d).padStart(2, "0")}`;
      let ym = byKey.get(key); if (!ym) { ym = new Map(); byKey.set(key, ym); }
      const acc = ym.get(y) || [0, 0]; acc[0] += h7[i]; acc[1]++; ym.set(y, acc);
    }
    const keys = [...byKey.keys()].sort();
    const clim = { keys, mean: [], p10: [], p50: [], p90: [] };
    for (const k of keys) {
      const vals = [...byKey.get(k).values()].map(([s, n]) => s / n);
      clim.mean.push(nanmean(vals)); clim.p10.push(quantile(vals, 0.1)); clim.p50.push(quantile(vals, 0.5)); clim.p90.push(quantile(vals, 0.9));
    }

    // fire seasons: start the month after the quietest month
    let argmin = 0; mu.forEach((v, i) => { if (isNum(v) && (!isNum(mu[argmin]) || v < mu[argmin])) argmin = i; });
    const s0 = mu.some(isNum) ? ((argmin + 1) % 12) + 1 : 1;
    const dayIndex = (t) => Math.round((t - MODIS_START) / DAY);
    const seasons = [];
    for (let y = startYear - 1; ; y++) {
      const a = Date.UTC(y, s0 - 1, 1), b = Date.UTC(y + 1, s0 - 1, 1) - DAY;
      if (a < MODIS_START) continue;
      if (b > end) break;
      const ia = dayIndex(a), ib = dayIndex(b);
      let tot = 0; for (let i = ia; i <= ib; i++) tot += h[i];
      const label = s0 === 1 ? String(y) : `${y}–${String(y + 1).slice(2)}`;
      const row = { label, start_year: y, from: iso(a), total: r(tot, 1) };
      if (tot >= 5) {
        // same tolerances as analysis.py so dates don't depend on floating-point noise
        let cum = 0, st = null, en = null, pk = ia;
        const r9 = (x) => Math.round(x * 1e9) / 1e9;
        for (let i = ia; i <= ib; i++) {
          cum += h[i];
          const cs = cum / tot;
          if (st == null && cs >= 0.1 - 1e-9) st = i;
          if (en == null && cs >= 0.9 - 1e-9) en = i;
          if (r9(h7[i]) > r9(h7[pk])) pk = i;
        }
        en = en ?? ib; st = st ?? ia;
        row.start = iso(days[st]); row.peak = iso(days[pk]); row.end = iso(days[en]);
        row.start_off = st - ia; row.peak_off = pk - ia; row.end_off = en - ia;
        row.length = en - st + 1; row.from_modis = a < VIIRS_START;
      }
      seasons.push(row);
    }
    const tots = seasons.map((s) => s.total), smu = tots.length ? sum(tots) / tots.length : NaN, ssd = nanstd1(tots);
    for (const s of seasons) s.z = ssd > 0 ? r((s.total - smu) / ssd) : null;
    const timed = seasons.filter((s) => s.start_off != null);
    const offLabel = (off) => fmtDM(Date.UTC(2001, s0 - 1, 1) + roundEven(off) * DAY);
    let critical = null;
    if (timed.length >= 3) {
      const med = {}; for (const k of ["start_off", "peak_off", "end_off"]) med[k] = median(timed.map((s) => s[k]));
      critical = { start: offLabel(med.start_off), peak: offLabel(med.peak_off), end: offLabel(med.end_off),
                   length: roundEven(med.end_off) - roundEven(med.start_off) + 1, start_off: roundEven(med.start_off), peak_off: roundEven(med.peak_off), end_off: roundEven(med.end_off) };
    }
    // four busiest weeks, at least 3 weeks apart (as analysis.py): 7-day sums centred on each day of the mean
    // year, wrapping around New Year; ties go to the earlier date; weeks with fire only
    const n = clim.mean.length;
    const wk = clim.mean.map((_, i) => { let s = 0; for (let j = -3; j <= 3; j++) s += clim.mean[(i + j + n) % n]; return s; });
    const doy = (k) => Math.round((Date.UTC(2001, +k.slice(0, 2) - 1, +k.slice(3)) - Date.UTC(2001, 0, 1)) / DAY) + 1;
    const top = [];
    for (const i of wk.map((v, i) => i).sort((a, b) => wk[b] - wk[a] || a - b)) {
      if (wk[i] <= 0 || top.length === 4) break;
      if (top.every((j) => { const d = Math.abs(doy(keys[i]) - doy(keys[j])); return Math.min(d, 365 - d) >= 21; })) top.push(i);
    }
    const topWeeks = top.map((i) => keys[i]);
    // annual totals
    const ys = years.filter((y) => y >= firstFull && y <= lastFull);
    const ym = new Map(), yv = new Map(), yh = new Map();
    for (let i = 0; i < N; i++) { const y = ymd(days[i])[0]; ym.set(y, (ym.get(y) || 0) + S.m[i]); yh.set(y, (yh.get(y) || 0) + h[i]); if (isNum(S.v[i])) yv.set(y, (yv.get(y) || 0) + S.v[i]); }

    return {
      aoi: meta.aoi, label: meta.label, countries: meta.countries || [], missing: meta.missing || [], view: meta.view,
      total_cell_days: r(sum(h), 0),
      range: { start: iso(MODIS_START), end: iso(end), viirs_start: iso(VIIRS_START), first_full: firstFull, last_full: lastFull },
      harmonization: cal,
      daily: { start: iso(MODIS_START), m: rl(S.m, 1), v: rl(S.v, 1), h: rl(h, 2), h7: rl(h7, 2), frp: S.frp ? rl(S.frp, 0) : Array(N).fill(null) },
      monthly: { years, values: mat.map((row) => rl(row, 1)), z: z.map((row) => rl(row)), normal: rl(mu, 1), sd: rl(sd, 1) },
      unusual: unusual.slice(0, 12),
      climatology: { keys, mean: rl(clim.mean), p10: rl(clim.p10), p50: rl(clim.p50), p90: rl(clim.p90) },
      season_start_month: s0, seasons, critical,
      top_weeks: topWeeks.map((k) => fmtDM(Date.UTC(2001, +k.slice(0, 2) - 1, +k.slice(3)))),
      yearly: { years: ys, m: ys.map((y) => r(ym.get(y) || 0, 0)), v: ys.map((y) => (Date.UTC(y, 0, 1) < VIIRS_START ? null : r(yv.get(y) || 0, 0))), h: ys.map((y) => r(yh.get(y) || 0, 0)) },
    };
  }

  // ---------- daily series from cell-day rows ----------
  function series(endYear) {
    const N = Math.round((Date.UTC(endYear, 11, 31) - MODIS_START) / DAY) + 1;
    const days = new Float64Array(N);
    for (let i = 0; i < N; i++) days[i] = MODIS_START + i * DAY;
    const v = new Float64Array(N);
    for (let i = 0; i < N; i++) v[i] = days[i] < VIIRS_START ? NaN : 0;
    return { days, m: new Float64Array(N), t: new Float64Array(N), v, mg: new Float64Array(N), endYear };
  }

  // ---------- early warning from live counts + harmonized history ----------
  function nowcast(liveDays, liveCells, h, endYear) {
    // liveDays: ISO dates of complete days; liveCells: fire cell-days per day in the area
    const total = sum(liveCells);
    const out = { available: true, days: liveDays, cells: liveCells, total };
    if (!h) return { ...out, history: false };
    const end = Date.UTC(endYear, 11, 31);
    const d0 = new Date(liveDays[0] + "T00:00:00Z"), L = liveDays.length;
    const hist = [];
    for (let y = 2001; y <= endYear; y++) { // same start date (28 Feb for 29 Feb), same length (as analysis.py)
      const mo = d0.getUTCMonth(), dd = d0.getUTCDate(), leap = (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0;
      const a = Date.UTC(y, mo, mo === 1 && dd === 29 && !leap ? 28 : dd), b = a + (L - 1) * DAY;
      if (b > end) continue;
      let s = 0; for (let i = Math.round((a - MODIS_START) / DAY); i <= Math.round((b - MODIS_START) / DAY); i++) s += h[i] || 0;
      hist.push(s);
    }
    if (!hist.length) return { ...out, history: false };
    const lt = hist.filter((x) => x < total).length / hist.length, eq = hist.filter((x) => x === total).length / hist.length;
    return { ...out, history: true, p10: r(quantile(hist, 0.1), 1), p50: r(median(hist), 1), p90: r(quantile(hist, 0.9), 1),
             max: r(Math.max(...hist), 1), percentile: r(lt * 100 + eq * 50, 0), n_years: hist.length, record: total > Math.max(...hist) };
  }

  const api = { analyze, series, nowcast, harmonize, DAY,
                constants: { MODIS_START, AQUA_START, VIIRS_START, TERRA_DRIFT, LAMBDA, LAMBDA_AREA, MIN_OVERLAP_CELL_DAYS, MIN_R2,
                             MAX_CV_ERROR, MAX_MAP_CELLS, CELL } };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.FireEngine = api;
})(typeof self !== "undefined" ? self : this);
