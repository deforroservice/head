/*
 * DEFORRO TRACES sandbox engine (browser + Node).
 *
 * A JavaScript port of the deforro_traces Python package: loaders, EUDR pre-flight
 * validation, risk gate, SOAP/WS-Security envelope, a mock TRACES service and the
 * bulk uploader. tests/test_web_parity.py runs both implementations over the same
 * inputs and fails if their validation results, digests or SOAP bodies diverge.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.DeforroEngine = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // ------------------------------------------------------------------ constants

  const ACTIVITY_TYPES = new Set(["IMPORT", "EXPORT", "DOMESTIC"]);
  const KNOWN_OPERATOR_ROLES = new Set(["OPERATOR"]);
  const GEOMETRY_TYPES = new Set(["Point", "MultiPoint", "Polygon", "MultiPolygon"]);
  const ANNEX_I_HS_PREFIXES = [
    "0102", "0201", "0202", "0206", "1602", "4101", "4104", "4107",
    "1801", "1802", "1803", "1804", "1805", "1806",
    "0901",
    "1207", "1511", "1513", "2306", "2905", "2915", "3401", "3823",
    "4001", "4005", "4006", "4007", "4008", "4010", "4011", "4012", "4013",
    "4015", "4016", "4017",
    "1201", "1208", "1507", "2304",
    "44", "47", "48", "49", "9401", "9403", "9406",
  ];
  const COUNTRY_RE = /^[A-Z]{2}$/;
  const HS_RE = /^\d{4}(\d{2}){0,2}$/;
  const GROUPED_RE = /^[A-Z0-9]{8,20}$/;
  const EARTH_RADIUS_M = 6371008.8;
  const POINT_MAX_HECTARES = 4.0;
  const MIN_DECIMALS = 6;
  const MAX_GEOJSON_BYTES = 25 * 1024 * 1024;

  const COMMODITY_BY_HS = [
    ["1801", "Cocoa"], ["1802", "Cocoa"], ["1803", "Cocoa"], ["1804", "Cocoa"], ["1805", "Cocoa"], ["1806", "Cocoa"],
    ["0901", "Coffee"],
    ["1207", "Palm oil"], ["1511", "Palm oil"], ["1513", "Palm oil"], ["2306", "Palm oil"], ["2905", "Palm oil"],
    ["2915", "Palm oil"], ["3401", "Palm oil"], ["3823", "Palm oil"],
    ["4001", "Rubber"], ["40", "Rubber"],
    ["1201", "Soya"], ["1208", "Soya"], ["1507", "Soya"], ["2304", "Soya"],
    ["0102", "Cattle"], ["0201", "Cattle"], ["0202", "Cattle"], ["0206", "Cattle"], ["1602", "Cattle"], ["41", "Cattle"],
    ["44", "Timber"], ["47", "Timber"], ["48", "Timber"], ["49", "Timber"], ["94", "Timber"],
  ];

  function commodityName(hs) {
    for (const [prefix, name] of COMMODITY_BY_HS) if (String(hs || "").startsWith(prefix)) return name;
    return "Other";
  }

  // ------------------------------------------------------------------ validation

  class Result {
    constructor() { this.issues = []; }
    error(code, message, path = "") { this.issues.push({ level: "error", code, message, path }); }
    warn(code, message, path = "") { this.issues.push({ level: "warning", code, message, path }); }
    get errors() { return this.issues.filter((i) => i.level === "error"); }
    get warnings() { return this.issues.filter((i) => i.level === "warning"); }
    get ok() { return this.errors.length === 0; }
  }

  function validateStatement(stmt, config = {}) {
    const requireGeo = config.requireGeolocation !== false;
    const maxBytes = config.maxGeojsonBytes || MAX_GEOJSON_BYTES;
    const r = new Result();
    const ref = stmt.internal_reference;
    if (!ref || ref.length > 50) r.error("REF", "internal reference is required (max 50 chars); it is the idempotency key");
    if (!ACTIVITY_TYPES.has(stmt.activity_type))
      r.error("ACTIVITY", `activityType must be IMPORT, EXPORT or DOMESTIC, got '${stmt.activity_type}'`);
    if (!KNOWN_OPERATOR_ROLES.has(stmt.operator_role))
      r.warn("ROLE", `operatorRole '${stmt.operator_role}' is not one the sandbox knows; check the XSD`);
    for (const [label, value] of [["countryOfActivity", stmt.country_of_activity], ["borderCrossCountry", stmt.border_cross_country]]) {
      if (value != null && !COUNTRY_RE.test(value))
        r.error("COUNTRY", `${label} must be an ISO 3166-1 alpha-2 code, got '${value}'`);
    }
    if ((stmt.activity_type === "IMPORT" || stmt.activity_type === "EXPORT") && !stmt.border_cross_country)
      r.error("BORDER", `borderCrossCountry is required for ${stmt.activity_type}`);
    if (!stmt.commodities.length) r.error("COMMODITY", "at least one commodity is required");
    for (const g of stmt.grouped_declarations)
      if (!GROUPED_RE.test(g)) r.error("GROUPED", `grouped declaration '${g}' does not look like a DDS reference number`);

    stmt.commodities.forEach((c, i) => {
      const cp = `commodities[${i + 1}]`;
      if (!c.description) r.error("DESCRIPTION", "descriptionOfGoods is required", cp);
      if (!HS_RE.test(c.hs_heading || "")) r.error("HS", `hsHeading must be 4, 6 or 8 digits, got '${c.hs_heading}'`, cp);
      else if (!ANNEX_I_HS_PREFIXES.some((p) => c.hs_heading.startsWith(p)))
        r.warn("HS_ANNEX", `hsHeading ${c.hs_heading} is not a recognised Annex I heading`, cp);
      if (c.net_weight == null && c.supplementary_unit == null) r.error("MEASURE", "netWeight or supplementaryUnit is required", cp);
      if (c.net_weight != null && c.net_weight <= 0) r.error("MEASURE", `netWeight must be positive, got ${c.net_weight}`, cp);
      if ((c.supplementary_unit == null) !== (c.supplementary_unit_qualifier == null))
        r.error("MEASURE", "supplementaryUnit and supplementaryUnitQualifier go together", cp);
      c.species.forEach((sp, si) => {
        if (!sp.scientific_name) r.error("SPECIES", "scientificName is required", `${cp}.species[${si + 1}]`);
      });
      if (requireGeo && ACTIVITY_TYPES.has(stmt.activity_type) && !c.producers.length)
        r.error("PRODUCER", "at least one producer with geolocation is required", cp);
      c.producers.forEach((p, pi) => {
        const pp = `${cp}.producers[${pi + 1}]`;
        if (!COUNTRY_RE.test(p.country || "")) r.error("COUNTRY", `producer country must be ISO alpha-2, got '${p.country}'`, pp);
        if (p.geojson == null) {
          if (requireGeo) r.error("GEO_MISSING", "producer has no geometry", pp);
          return;
        }
        const size = JSON.stringify(p.geojson).length;
        if (size > maxBytes) r.error("GEO_SIZE", `GeoJSON is ${size} bytes, limit ${maxBytes}`, pp);
        validateGeojson(p.geojson, r, pp);
      });
    });
    return r;
  }

  function isObj(x) { return x !== null && typeof x === "object" && !Array.isArray(x); }

  function validateGeojson(doc, r, path) {
    if (!isObj(doc) || doc.type !== "FeatureCollection") { r.error("GEO_TYPE", "GeoJSON must be a FeatureCollection", path); return; }
    const features = doc.features;
    if (!Array.isArray(features) || !features.length) { r.error("GEO_EMPTY", "FeatureCollection has no features", path); return; }
    let lowPrecision = false;
    features.forEach((feature, fi) => {
      const fp = `${path}.features[${fi + 1}]`;
      if (!isObj(feature) || feature.type !== "Feature") { r.error("GEO_FEATURE", "each entry must be a Feature", fp); return; }
      const geom = feature.geometry;
      if (!isObj(geom) || !GEOMETRY_TYPES.has(geom.type)) {
        r.error("GEO_GEOMETRY", "geometry type must be one of MultiPoint, MultiPolygon, Point, Polygon", fp);
        return;
      }
      const props = isObj(feature.properties) ? feature.properties : {};
      const coords = geom.coordinates;
      let precise = false;
      try {
        if (geom.type === "Point") {
          precise = checkPosition(coords, r, fp) || precise;
          const area = props.Area;
          if (typeof area === "number" && area > POINT_MAX_HECTARES)
            r.error("GEO_POINT_AREA", `plot declares Area=${area} ha; plots over ${POINT_MAX_HECTARES} ha need a polygon`, fp);
        } else if (geom.type === "MultiPoint") {
          if (!Array.isArray(coords)) throw new TypeError("coords");
          for (const pos of coords) precise = checkPosition(pos, r, fp) || precise;
        } else if (geom.type === "Polygon") {
          precise = checkPolygon(coords, r, fp) || precise;
        } else {
          if (!Array.isArray(coords)) throw new TypeError("coords");
          for (const poly of coords) precise = checkPolygon(poly, r, fp) || precise;
        }
      } catch (e) {
        r.error("GEO_COORDS", "malformed coordinates", fp);
        return;
      }
      if (!precise) lowPrecision = true;
    });
    if (lowPrecision) r.warn("GEO_PRECISION", `a feature has no coordinate with ${MIN_DECIMALS}+ decimal places`, path);
  }

  function num(x) {
    if (typeof x === "number") return x;
    if (typeof x === "string" && x.trim() !== "" && !isNaN(Number(x))) return Number(x);
    throw new TypeError("not a number");
  }

  function checkPosition(pos, r, path) {
    if (!Array.isArray(pos) || pos.length < 2) throw new TypeError("position");
    const lon = num(pos[0]), lat = num(pos[1]);
    if (!(lon >= -180 && lon <= 180 && lat >= -90 && lat <= 90))
      r.error("GEO_RANGE", `position [${pos[0]}, ${pos[1]}] out of range (expects [lon, lat])`, path);
    return decimals(pos[0]) >= MIN_DECIMALS || decimals(pos[1]) >= MIN_DECIMALS;
  }

  function checkPolygon(rings, r, path) {
    if (!Array.isArray(rings) || !rings.length) throw new TypeError("polygon");
    let precise = false;
    rings.forEach((ring, ri) => {
      if (!Array.isArray(ring)) throw new TypeError("ring");
      if (ring.length < 4) { r.error("GEO_RING", `ring ${ri} needs at least 4 positions`, path); return; }
      const a = ring[0], b = ring[ring.length - 1];
      if (!Array.isArray(a) || !Array.isArray(b)) throw new TypeError("ring");
      if (a[0] !== b[0] || a[1] !== b[1]) r.error("GEO_RING", `ring ${ri} is not closed (first != last position)`, path);
      for (const pos of ring) precise = checkPosition(pos, r, path) || precise;
    });
    if (rings[0].length >= 4 && ringAreaM2(rings[0]) === 0) r.error("GEO_RING", "outer ring has zero area", path);
    return precise;
  }

  function decimals(value) {
    const text = String(value);
    if (/e/i.test(text)) return MIN_DECIMALS;
    const dot = text.indexOf(".");
    return dot === -1 ? 0 : text.length - dot - 1;
  }

  const rad = (d) => (d * Math.PI) / 180;

  function ringAreaM2(ring) {
    let total = 0;
    for (let i = 0; i < ring.length - 1; i++) {
      const [lon1, lat1] = ring[i].map(num), [lon2, lat2] = ring[i + 1].map(num);
      total += rad(lon2 - lon1) * (2 + Math.sin(rad(lat1)) + Math.sin(rad(lat2)));
    }
    return (Math.abs(total) * EARTH_RADIUS_M ** 2) / 2;
  }

  function geojsonAreaHectares(doc) {
    let total = 0;
    for (const f of (doc && doc.features) || []) {
      const g = (f && f.geometry) || {};
      const polys = g.type === "Polygon" ? [g.coordinates] : g.type === "MultiPolygon" ? g.coordinates : [];
      for (const rings of polys) {
        try { total += ringAreaM2(rings[0]) - rings.slice(1).reduce((s, h) => s + ringAreaM2(h), 0); } catch (e) { /* invalid */ }
      }
    }
    return total / 10000;
  }

  // ------------------------------------------------------------------ loaders

  function req(obj, key) {
    if (!isObj(obj) || !(key in obj)) throw new Error(`missing field '${key}'`);
    return obj[key];
  }

  function dec(value) {
    if (value == null || value === "") return null;
    const n = Number(value);
    if (Number.isNaN(n)) throw new Error(`'${value}' is not a number`);
    return n;
  }

  // files: Map of path -> text for uploaded GeoJSON; resolved by exact relative path, then basename.
  function resolveGeojson(ref, files) {
    const clean = String(ref).replace(/^\.\//, "");
    const base = clean.split(/[\\/]/).pop();
    const text = files.get(clean) ?? files.get(base);
    if (text == null) throw new Error(`GeoJSON file '${ref}' was not uploaded`);
    try { return JSON.parse(text); } catch (e) { throw new Error(`GeoJSON file '${ref}' is not valid JSON`); }
  }

  function statementFromDict(data, files) {
    const risk = data.risk;
    return {
      internal_reference: String(req(data, "internal_reference")),
      activity_type: String(req(data, "activity_type")).toUpperCase(),
      country_of_activity: data.country_of_activity ?? null,
      border_cross_country: data.border_cross_country ?? null,
      operator_role: data.operator_role ?? "OPERATOR",
      geo_location_confidential: Boolean(data.geo_location_confidential),
      grouped_declarations: [...(data.grouped_declarations || [])],
      risk: risk
        ? {
            level: String(req(risk, "level")).toLowerCase(),
            score: risk.score ?? null,
            assessment_id: risk.assessment_id ?? null,
            mitigated: Boolean(risk.mitigated),
          }
        : null,
      commodities: req(data, "commodities").map((c) => ({
        description: req(c, "description"),
        hs_heading: String(req(c, "hs_heading")),
        net_weight: dec(c.net_weight_kg),
        supplementary_unit: dec(c.supplementary_unit),
        supplementary_unit_qualifier: c.supplementary_unit_qualifier ?? null,
        species: (c.species || []).map((s) => ({ scientific_name: req(s, "scientific_name"), common_name: s.common_name ?? null })),
        producers: (c.producers || []).map((p) => {
          let geojson = p.geojson ?? null;
          if (p.geojson_file) geojson = resolveGeojson(p.geojson_file, files);
          return { country: req(p, "country"), name: p.name ?? null, geojson };
        }),
      })),
    };
  }

  function loadJsonl(text, files = new Map()) {
    const out = [];
    text.split(/\r?\n/).forEach((line, idx) => {
      if (!line.trim() || line.trimStart().startsWith("#")) return;
      let key = `line ${idx + 1}`;
      try {
        const data = JSON.parse(line);
        key = (isObj(data) && data.internal_reference) || key;
        out.push({ key: String(key), statement: statementFromDict(data, files), errors: [] });
      } catch (e) {
        out.push({ key: String(key), statement: null, errors: [e.message] });
      }
    });
    return out;
  }

  function parseCsv(text) {
    const rows = [];
    let row = [], field = "", quoted = false;
    text = text.replace(/^﻿/, "");
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      if (quoted) {
        if (ch === '"' && text[i + 1] === '"') { field += '"'; i++; }
        else if (ch === '"') quoted = false;
        else field += ch;
      } else if (ch === '"') quoted = true;
      else if (ch === ",") { row.push(field); field = ""; }
      else if (ch === "\n" || ch === "\r") {
        if (ch === "\r" && text[i + 1] === "\n") i++;
        row.push(field); rows.push(row); row = []; field = "";
      } else field += ch;
    }
    if (field !== "" || row.length) { row.push(field); rows.push(row); }
    return rows.filter((r) => r.some((v) => v.trim() !== ""));
  }

  function loadCsv(text, files = new Map()) {
    const [header, ...body] = parseCsv(text);
    if (!header) return [];
    const cols = header.map((h) => h.trim());
    const groups = new Map();
    body.forEach((cells, i) => {
      const row = {};
      cols.forEach((c, j) => { if (c) row[c] = (cells[j] || "").trim(); });
      const ref = row.internal_reference || `line ${i + 2}`;
      if (!groups.has(ref)) groups.set(ref, []);
      groups.get(ref).push({ line: i + 2, row });
    });
    const out = [];
    for (const [ref, rows] of groups) {
      try {
        out.push({ key: ref, statement: statementFromRows(ref, rows.map((r) => r.row), files), errors: [] });
      } catch (e) {
        out.push({ key: ref, statement: null, errors: [`rows ${rows.map((r) => r.line).join(", ")}: ${e.message}`] });
      }
    }
    return out;
  }

  function statementFromRows(ref, rows, files) {
    const first = rows[0];
    const commodities = new Map();
    for (const row of rows) {
      const ckey = `${req(row, "hs_heading")}\u0000${req(row, "description")}`;
      if (!commodities.has(ckey))
        commodities.set(ckey, {
          description: row.description,
          hs_heading: row.hs_heading,
          net_weight_kg: row.net_weight_kg,
          supplementary_unit: row.supplementary_unit,
          supplementary_unit_qualifier: row.supplementary_unit_qualifier || null,
          species: [],
          producers: [],
        });
      const c = commodities.get(ckey);
      if (row.scientific_name) {
        const sp = { scientific_name: row.scientific_name, common_name: row.common_name || null };
        if (!c.species.some((s) => s.scientific_name === sp.scientific_name && s.common_name === sp.common_name)) c.species.push(sp);
      }
      if (row.producer_country)
        c.producers.push({ country: row.producer_country, name: row.producer_name || null, geojson_file: row.geojson_file || null });
    }
    const data = {
      internal_reference: ref,
      activity_type: req(first, "activity_type"),
      country_of_activity: first.country_of_activity || null,
      border_cross_country: first.border_cross_country || null,
      commodities: [...commodities.values()],
    };
    if (first.risk_level) data.risk = { level: first.risk_level, score: first.risk_score ? Number(first.risk_score) : null };
    return statementFromDict(data, files);
  }

  // ------------------------------------------------------------------ samples

  function mulberry32(seed) {
    let a = seed >>> 0;
    const next = () => {
      a = (a + 0x6d2b79f5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
    return {
      random: next,
      uniform: (lo, hi) => lo + (hi - lo) * next(),
      int: (lo, hi) => lo + Math.floor(next() * (hi - lo + 1)),
      choice: (arr) => arr[Math.floor(next() * arr.length)],
    };
  }

  const SAMPLE_COMMODITIES = [
    ["1801", "Cocoa beans, whole, raw", "Theobroma cacao", "Cacao", ["GH", "CI", "CM"]],
    ["0901", "Coffee, not roasted", "Coffea arabica", "Arabica coffee", ["ET", "CO", "BR"]],
    ["1511", "Crude palm oil", "Elaeis guineensis", "Oil palm", ["ID", "MY"]],
    ["4001", "Natural rubber, smoked sheets", "Hevea brasiliensis", "Rubber tree", ["TH", "CI"]],
    ["4407", "Sawn wood, tropical", "Triplochiton scleroxylon", "Ayous", ["CM", "GH"]],
  ];
  const CENTROIDS = {
    GH: [6.7, -1.6], CI: [6.9, -5.3], CM: [4.0, 11.5], ET: [7.7, 36.8], CO: [4.6, -75.7],
    BR: [-12.5, -55.7], ID: [0.5, 101.4], MY: [3.8, 102.3], TH: [7.9, 98.4],
  };
  const EU_PORTS = ["BE", "NL", "DE", "FR", "IT", "ES"];
  const COOP_WORDS = ["Asante", "Nyame", "Kpong", "Sankofa", "Bia", "Tano", "Abura", "Dorma", "Sefwi", "Juaso", "Yayu", "Sidama", "Huila", "Riau", "Jambi", "Surat"];

  const round6 = (x) => Math.round(x * 1e6) / 1e6;

  function samplePlot(rng, country, hectares) {
    const [lat0, lon0] = CENTROIDS[country];
    const lat = lat0 + rng.uniform(-1, 1), lon = lon0 + rng.uniform(-1, 1);
    const props = { ProducerCountry: country, Area: Math.round(hectares * 1e4) / 1e4 };
    let geometry;
    if (hectares <= 4 && rng.random() < 0.3) {
      geometry = { type: "Point", coordinates: [round6(lon), round6(lat)] };
    } else {
      const radius = Math.sqrt((hectares * 10000) / (1.5 * Math.sqrt(3)));
      const ring = [];
      for (let k = 0; k < 6; k++) {
        const angle = rad(60 * k + rng.uniform(-10, 10));
        const r = radius * rng.uniform(0.85, 1.15);
        ring.push([round6(lon + (r * Math.cos(angle)) / (111320 * Math.cos(rad(lat)))), round6(lat + (r * Math.sin(angle)) / 111320)]);
      }
      ring.push([...ring[0]]);
      geometry = { type: "Polygon", coordinates: [ring] };
    }
    return { type: "FeatureCollection", features: [{ type: "Feature", properties: props, geometry }] };
  }

  function generateSamples(count, { seed = 7, prefix = "DEF", riskyRate = 0.06, invalidRate = 0.08 } = {}) {
    const rng = mulberry32(seed);
    const records = [];
    for (let n = 1; n <= count; n++) {
      const [hs, desc, sci, common, countries] = rng.choice(SAMPLE_COMMODITIES);
      const origin = rng.choice(countries);
      const port = rng.choice(EU_PORTS);
      const producers = [];
      const nProducers = rng.int(1, 4);
      for (let p = 0; p < nProducers; p++)
        producers.push({
          country: origin,
          name: `${rng.choice(COOP_WORDS)} ${rng.choice(["Farmers", "Growers", "Cooperative", "Union", "Estate"])}`,
          geojson: samplePlot(rng, origin, rng.choice([0.8, 1.5, 3.2, 6.0, 12.5, 40.0])),
        });
      const level = rng.random() < riskyRate ? rng.choice(["standard", "high"]) : "negligible";
      const record = {
        internal_reference: `${prefix}-${String(n).padStart(6, "0")}`,
        activity_type: "IMPORT",
        country_of_activity: port,
        border_cross_country: port,
        risk: {
          level,
          score: Math.round((level === "negligible" ? rng.uniform(0, 0.1) : rng.uniform(0.4, 0.95)) * 1000) / 1000,
          assessment_id: `RA-${String(n).padStart(6, "0")}`,
        },
        commodities: [{
          description: desc, hs_heading: hs,
          net_weight_kg: rng.choice([500, 1200, 5000, 12000, 24000]),
          species: [{ scientific_name: sci, common_name: common }],
          producers,
        }],
      };
      if (rng.random() < invalidRate) {
        const breakage = rng.choice(["open_ring", "latlon_swap", "no_border", "point_too_big"]);
        const feature = producers[0].geojson.features[0];
        const geom = feature.geometry;
        if (breakage === "open_ring" && geom.type === "Polygon") geom.coordinates[0].pop();
        else if (breakage === "latlon_swap") geom.coordinates = swap(geom.coordinates);
        else if (breakage === "point_too_big") {
          feature.geometry = { type: "Point", coordinates: geom.type === "Point" ? geom.coordinates : geom.coordinates[0][0] };
          feature.properties.Area = 12.5;
        } else delete record.border_cross_country;
      }
      records.push(record);
    }
    return records;
  }

  function swap(c) {
    return typeof c[0] === "number" ? [c[1], c[0] + 200] : c.map(swap);
  }

  // ------------------------------------------------------------------ crypto + encoding

  const subtle = () => (globalThis.crypto && globalThis.crypto.subtle) || require("crypto").webcrypto.subtle;

  function bytesToB64(bytes) {
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(bin);
  }
  function b64ToBytes(b64) {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
  const utf8 = (s) => new TextEncoder().encode(s);

  async function sha1B64(bytes) {
    return bytesToB64(new Uint8Array(await subtle().digest("SHA-1", bytes)));
  }
  async function sha256Hex(text) {
    const buf = new Uint8Array(await subtle().digest("SHA-256", utf8(text)));
    return [...buf].map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  function concat(...arrays) {
    const out = new Uint8Array(arrays.reduce((n, a) => n + a.length, 0));
    let o = 0;
    for (const a of arrays) { out.set(a, o); o += a.length; }
    return out;
  }

  function formatTimestamp(date) {
    return date.toISOString(); // 2026-05-20T09:55:01.123Z, same shape as the Python client
  }

  async function passwordDigest(nonceBytes, created, password) {
    return sha1B64(concat(nonceBytes, utf8(created), utf8(password)));
  }

  async function makeToken(creds, { ttlSeconds = 60, now = new Date(), nonce = null } = {}) {
    nonce = nonce || globalThis.crypto.getRandomValues(new Uint8Array(16));
    const created = formatTimestamp(now);
    const expires = formatTimestamp(new Date(now.getTime() + ttlSeconds * 1000));
    return {
      username: creds.username,
      passwordDigest: await passwordDigest(nonce, created, creds.authKey),
      nonceB64: bytesToB64(nonce),
      created,
      expires,
    };
  }

  // ------------------------------------------------------------------ SOAP

  const NS = {
    soapenv: "http://schemas.xmlsoap.org/soap/envelope/",
    v4: "http://ec.europa.eu/sanco/tracesnt/base/v4",
    dds: "http://ec.europa.eu/tracesnt/certificate/eudr/due-diligence-statement/v3",
    eudrCommon: "http://ec.europa.eu/tracesnt/certificate/eudr/common/v3",
    wsse: "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd",
    wsu: "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd",
  };
  const PASSWORD_DIGEST_TYPE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest";
  const NONCE_ENCODING = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary";

  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const el = (tag, text, indent) => `${indent}<${tag}>${esc(text)}</${tag}>`;

  function formatNumber(n) {
    // Plain decimal, no exponent, no trailing zeros (mirrors models.format_decimal).
    if (Number.isInteger(n)) return String(n);
    return n.toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
  }

  function encodeGeojson(doc) {
    return bytesToB64(utf8(JSON.stringify(doc)));
  }

  function statementXml(stmt, i0 = "        ", { truncateGeo = false } = {}) {
    const i1 = i0 + "    ", i2 = i1 + "    ", i3 = i2 + "    ", i4 = i3 + "    ";
    const L = [`${i0}<dds:statement>`];
    L.push(el("dds:internalReferenceNumber", stmt.internal_reference, i1));
    L.push(el("dds:activityType", stmt.activity_type, i1));
    if (stmt.country_of_activity) L.push(el("dds:countryOfActivity", stmt.country_of_activity, i1));
    if (stmt.border_cross_country) L.push(el("dds:borderCrossCountry", stmt.border_cross_country, i1));
    stmt.commodities.forEach((c, ci) => {
      L.push(`${i1}<dds:commodities>`);
      L.push(el("dds:position", ci + 1, i2));
      L.push(`${i2}<dds:descriptors>`);
      L.push(el("eudrCommon:descriptionOfGoods", c.description, i3));
      L.push(`${i3}<eudrCommon:goodsMeasure>`);
      if (c.net_weight != null) L.push(el("eudrCommon:netWeight", formatNumber(c.net_weight), i4));
      if (c.supplementary_unit != null) L.push(el("eudrCommon:supplementaryUnit", formatNumber(c.supplementary_unit), i4));
      if (c.supplementary_unit_qualifier) L.push(el("eudrCommon:supplementaryUnitQualifier", c.supplementary_unit_qualifier, i4));
      L.push(`${i3}</eudrCommon:goodsMeasure>`);
      L.push(`${i2}</dds:descriptors>`);
      L.push(el("dds:hsHeading", c.hs_heading, i2));
      for (const sp of c.species) {
        L.push(`${i2}<dds:speciesInfo>`);
        L.push(el("dds:scientificName", sp.scientific_name, i3));
        if (sp.common_name) L.push(el("dds:commonName", sp.common_name, i3));
        L.push(`${i2}</dds:speciesInfo>`);
      }
      c.producers.forEach((p, pi) => {
        L.push(`${i2}<dds:producers>`);
        L.push(el("dds:position", pi + 1, i3));
        L.push(el("dds:country", p.country, i3));
        if (p.name) L.push(el("dds:name", p.name, i3));
        if (p.geojson != null) {
          let geo = encodeGeojson(p.geojson);
          if (truncateGeo && geo.length > 64) geo = `${geo.slice(0, 48)}…(${geo.length} chars)`;
          L.push(el("dds:geometryGeojson", geo, i3));
        }
        L.push(`${i2}</dds:producers>`);
      });
      L.push(`${i1}</dds:commodities>`);
    });
    L.push(el("dds:geoLocationConfidential", stmt.geo_location_confidential ? "true" : "false", i1));
    for (const g of stmt.grouped_declarations) {
      L.push(`${i1}<dds:groupedDeclarations>`);
      L.push(el("eudrCommon:groupedDeclaration", g, i2));
      L.push(`${i1}</dds:groupedDeclarations>`);
    }
    L.push(`${i0}</dds:statement>`);
    return L.join("\n");
  }

  function submitBody(stmt, opts) {
    return [
      "        <dds:SubmitDdsRequest>",
      el("dds:operatorRole", stmt.operator_role, "            "),
      statementXml(stmt, "            ", opts),
      "        </dds:SubmitDdsRequest>",
    ].join("\n");
  }

  function envelope(token, clientId, body, { redact = false } = {}) {
    const pw = redact ? "***" : esc(token.passwordDigest);
    const nonce = redact ? "***" : esc(token.nonceB64);
    return `<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="${NS.soapenv}" xmlns:v4="${NS.v4}" xmlns:dds="${NS.dds}" xmlns:eudrCommon="${NS.eudrCommon}">
    <soapenv:Header>
        <wsse:Security xmlns:wsse="${NS.wsse}" xmlns:wsu="${NS.wsu}">
            <wsu:Timestamp wsu:Id="TS-1">
                <wsu:Created>${token.created}</wsu:Created>
                <wsu:Expires>${token.expires}</wsu:Expires>
            </wsu:Timestamp>
            <wsse:UsernameToken wsu:Id="UT-1">
                <wsse:Username>${esc(token.username)}</wsse:Username>
                <wsse:Password Type="${PASSWORD_DIGEST_TYPE}">${pw}</wsse:Password>
                <wsse:Nonce EncodingType="${NONCE_ENCODING}">${nonce}</wsse:Nonce>
                <wsu:Created>${token.created}</wsu:Created>
            </wsse:UsernameToken>
        </wsse:Security>
        <v4:WebServiceClientId>${esc(clientId)}</v4:WebServiceClientId>
    </soapenv:Header>
    <soapenv:Body>
${body}
    </soapenv:Body>
</soapenv:Envelope>`;
  }

  function getDdsBody(uuids) {
    return ["        <dds:GetDdsRequest>", ...uuids.map((u) => el("dds:uuidList", u, "            ")), "        </dds:GetDdsRequest>"].join("\n");
  }

  // Minimal namespace-agnostic XML reading (DOMParser in browsers).
  function parseXml(text) {
    const doc = new DOMParser().parseFromString(text, "application/xml");
    if (doc.getElementsByTagName("parsererror").length) throw new Error("not well-formed XML");
    return doc;
  }
  const kids = (node, name) => (node ? [...node.children].filter((c) => c.localName === name) : []);
  const kid = (node, name) => kids(node, name)[0] || null;
  const txt = (node, name) => { const k = kid(node, name); return k ? k.textContent.trim() : null; };
  const firstByLocal = (doc, name) => doc.getElementsByTagNameNS("*", name)[0] || null;

  function statementFromXml(st) {
    return {
      internal_reference: txt(st, "internalReferenceNumber") || "",
      activity_type: txt(st, "activityType") || "",
      country_of_activity: txt(st, "countryOfActivity"),
      border_cross_country: txt(st, "borderCrossCountry"),
      operator_role: "OPERATOR",
      geo_location_confidential: txt(st, "geoLocationConfidential") === "true",
      grouped_declarations: kids(st, "groupedDeclarations").map((g) => txt(g, "groupedDeclaration")).filter(Boolean),
      risk: null,
      commodities: kids(st, "commodities").map((com) => {
        const desc = kid(com, "descriptors"), gm = kid(desc, "goodsMeasure");
        const nw = txt(gm, "netWeight"), su = txt(gm, "supplementaryUnit");
        return {
          description: txt(desc, "descriptionOfGoods") || "",
          hs_heading: txt(com, "hsHeading") || "",
          net_weight: nw ? Number(nw) : null,
          supplementary_unit: su ? Number(su) : null,
          supplementary_unit_qualifier: txt(gm, "supplementaryUnitQualifier"),
          species: kids(com, "speciesInfo").map((s) => ({ scientific_name: txt(s, "scientificName") || "", common_name: txt(s, "commonName") })),
          producers: kids(com, "producers").map((p) => {
            const raw = txt(p, "geometryGeojson");
            let geojson = null;
            if (raw) {
              try { geojson = JSON.parse(new TextDecoder().decode(b64ToBytes(raw))); } catch (e) { geojson = { __invalid__: true }; }
            }
            return { country: txt(p, "country") || "", name: txt(p, "name"), geojson };
          }),
        };
      }),
    };
  }

  // ------------------------------------------------------------------ mock TRACES

  const SANDBOX_CREDS = { username: "sandbox", authKey: "sandbox-auth-key", clientId: "eudr-test" };
  const ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";

  class MockTraces {
    constructor({ processingDelayMs = 1500, rejectRate = 0, faultRate = 0, seed = 11 } = {}) {
      Object.assign(this, { processingDelayMs, rejectRate, faultRate });
      this.rng = mulberry32(seed);
      this.store = new Map();
      this.nonces = new Set();
      this.requests = 0;
      this.faultsInjected = 0;
    }
    code(n) { let s = ""; for (let i = 0; i < n; i++) s += this.rng.choice(ALNUM); return s; }
    refresh(d) { if (d.status === "SUBMITTED" && Date.now() - d.submittedAt >= this.processingDelayMs) d.status = d.finalStatus; }

    async handle(xml) {
      this.requests++;
      if (this.rng.random() < this.faultRate) { this.faultsInjected++; return { status: 503, body: "Service Unavailable (injected by sandbox)" }; }
      let doc;
      try { doc = parseXml(xml); } catch (e) { return fault("soapenv:Client", "Malformed XML"); }
      const auth = await this.authenticate(doc);
      if (auth) return auth;
      const body = firstByLocal(doc, "Body");
      const request = body && body.firstElementChild;
      if (!request) return fault("soapenv:Client", "Missing SOAP Body");
      switch (request.localName) {
        case "SubmitDdsRequest": return this.submit(request);
        case "GetDdsRequest": return this.get(request);
        case "WithdrawDdsRequest": return this.withdraw(request);
        default: return fault("soapenv:Client", `Unknown operation ${request.localName}`);
      }
    }

    async authenticate(doc) {
      const token = firstByLocal(doc, "UsernameToken");
      if (!token) return fault("wsse:InvalidSecurity", "Missing WS-Security UsernameToken");
      if (txt(firstByLocal(doc, "Header"), "WebServiceClientId") !== SANDBOX_CREDS.clientId)
        return fault("soapenv:Client", "Invalid or missing WebServiceClientId");
      const created = txt(token, "Created"), nonce = txt(token, "Nonce");
      let expected;
      try { expected = await passwordDigest(b64ToBytes(nonce), created, SANDBOX_CREDS.authKey); } catch (e) {
        return fault("wsse:InvalidSecurity", "Malformed Nonce or Created");
      }
      if (txt(token, "Username") !== SANDBOX_CREDS.username || txt(token, "Password") !== expected)
        return fault("wsse:FailedAuthentication", "The security token could not be authenticated");
      if (Math.abs(Date.now() - Date.parse(created)) > 300000) return fault("wsse:MessageExpired", "The message has expired");
      if (this.nonces.has(nonce)) return fault("wsse:InvalidSecurity", "Nonce has already been used");
      this.nonces.add(nonce);
      return null;
    }

    submit(request) {
      const stmt = statementFromXml(kid(request, "statement"));
      stmt.operator_role = txt(request, "operatorRole") || "OPERATOR";
      const result = validateStatement(stmt);
      if (!result.ok) return businessFault(result.errors.map((i) => [`EUDR-${i.code}`, `${i.path} ${i.message}`.trim()]));
      const country = stmt.country_of_activity || "BE";
      const uuid = globalThis.crypto.randomUUID();
      this.store.set(uuid, {
        uuid, stmt, submittedAt: Date.now(), status: "SUBMITTED", version: 1,
        finalStatus: this.rng.random() < this.rejectRate ? "REJECTED" : "AVAILABLE",
        reference: String(new Date().getUTCFullYear()).slice(2) + country + this.code(10),
        verification: this.code(8),
      });
      return ok(`<dds:SubmitDdsResponse xmlns:dds="${NS.dds}"><dds:uuid>${uuid}</dds:uuid></dds:SubmitDdsResponse>`);
    }

    get(request) {
      const items = kids(request, "uuidList").map((u) => this.store.get(u.textContent.trim())).filter(Boolean);
      const lists = items.map((d) => {
        this.refresh(d);
        const final = d.status !== "SUBMITTED" && d.status !== "REJECTED";
        return `<dds:ddsOverviewList><c:uuid>${d.uuid}</c:uuid><c:internalReferenceNumber>${esc(d.stmt.internal_reference)}</c:internalReferenceNumber>` +
          (final ? `<c:referenceNumber>${d.reference}</c:referenceNumber><c:verificationNumber>${d.verification}</c:verificationNumber>` : "") +
          `<c:status>${d.status}</c:status><c:date>${new Date(d.submittedAt).toISOString()}</c:date><c:updatedBy>sandbox</c:updatedBy><c:version>${d.version}</c:version></dds:ddsOverviewList>`;
      });
      return ok(`<dds:GetDdsResponse xmlns:dds="${NS.dds}" xmlns:c="${NS.eudrCommon}">${lists.join("")}</dds:GetDdsResponse>`);
    }

    withdraw(request) {
      const d = this.store.get(txt(request, "uuid"));
      if (!d) return businessFault([["EUDR-NOT-FOUND", "No statement with this UUID"]]);
      this.refresh(d);
      if (["WITHDRAWN", "REJECTED", "GROUPED"].includes(d.status))
        return businessFault([["EUDR-STATUS", `Statement in status ${d.status} cannot be withdrawn`]]);
      d.status = "WITHDRAWN";
      return ok(`<dds:WithdrawDdsResponse xmlns:dds="${NS.dds}"><dds:uuid>${d.uuid}</dds:uuid><dds:status>WITHDRAWN</dds:status></dds:WithdrawDdsResponse>`);
    }
  }

  function wrap(inner) {
    return `<?xml version="1.0" encoding="UTF-8"?><S:Envelope xmlns:S="${NS.soapenv}"><S:Body>${inner}</S:Body></S:Envelope>`;
  }
  const ok = (inner) => ({ status: 200, body: wrap(inner) });
  function fault(code, message, detail = "") {
    return { status: 500, body: wrap(`<S:Fault><faultcode>${code}</faultcode><faultstring>${esc(message)}</faultstring>${detail ? `<detail>${detail}</detail>` : ""}</S:Fault>`) };
  }
  function businessFault(errors) {
    const items = errors.map(([c, m]) => `<d:error><d:code>${esc(c)}</d:code><d:message>${esc(m)}</d:message></d:error>`).join("");
    return fault("soapenv:Client", "Business rules validation failed", `<d:BusinessRulesValidationException xmlns:d="${NS.dds}">${items}</d:BusinessRulesValidationException>`);
  }

  class SoapFault extends Error {
    constructor(code, message, details) {
      super(`${code}: ${message}${details.length ? ` (${details.join("; ")})` : ""}`);
      Object.assign(this, { code, faultMessage: message, details });
    }
  }
  class TransportError extends Error {}

  function parseResponse({ status, body }) {
    if (!body.startsWith("<?xml") && !body.startsWith("<")) {
      throw Object.assign(new TransportError(`HTTP ${status}`), { retryable: [408, 429, 500, 502, 503, 504].includes(status) });
    }
    const doc = parseXml(body);
    const b = firstByLocal(doc, "Body");
    const first = b && b.firstElementChild;
    if (first && first.localName === "Fault") {
      const detail = kid(first, "detail");
      const details = detail ? [...detail.getElementsByTagNameNS("*", "message")].map((m) => m.textContent.trim()) : [];
      throw new SoapFault(txt(first, "faultcode") || "Server", txt(first, "faultstring") || "", details);
    }
    return first;
  }

  // ------------------------------------------------------------------ uploader

  // Ledger states, as in deforro_traces/ledger.py
  const S = {
    INVALID: "INVALID", BLOCKED_RISK: "BLOCKED_RISK", READY: "READY", SUBMITTING: "SUBMITTING",
    SUBMITTED: "SUBMITTED", DONE: "DONE", REJECTED: "REJECTED", WITHDRAWN: "WITHDRAWN",
  };

  function riskGate(stmt, allowed = new Set(["negligible"]), allowMissing = false) {
    const risk = stmt.risk;
    if (!risk) return allowMissing ? null : "no DEFORRO risk assessment attached";
    if (allowed.has(risk.level) || risk.mitigated) return null;
    return `risk level '${risk.level}' is not cleared for submission (score ${risk.score ?? "n/a"})`;
  }

  function payloadKey(stmt) {
    const { risk, ...rest } = stmt;
    return JSON.stringify(rest);
  }

  class Sandbox {
    constructor({ mock } = {}) {
      this.mock = mock || new MockTraces();
      this.ledger = new Map(); // internal reference -> row
      this.stats = { requests: 0, retries: 0 };
    }

    log(row, event, detail = "") { row.events.push({ at: new Date(), event, detail }); }

    // Pre-flight: load records into the ledger with INVALID / BLOCKED_RISK / READY.
    preflight(records) {
      const seen = new Set();
      for (const rec of records) {
        let row = this.ledger.get(rec.key);
        const fresh = !row;
        if (fresh) {
          row = { ref: rec.key, events: [], attempts: 0 };
          this.ledger.set(rec.key, row);
        }
        const live = row.uuid && (row.state === S.SUBMITTED || row.state === S.DONE);
        row.statement = rec.statement;
        if (!rec.statement) {
          Object.assign(row, { issues: rec.errors.map((m) => ({ level: "error", code: "LOAD", message: m, path: "" })) });
          if (!live) row.state = S.INVALID;
          this.log(row, "load error", rec.errors.join("; "));
          continue;
        }
        if (seen.has(rec.key)) {
          row.issues = [{ level: "error", code: "DUPLICATE", message: "internal reference appears more than once in the file", path: "" }];
          if (!live) row.state = S.INVALID;
          continue;
        }
        seen.add(rec.key);
        const result = validateStatement(rec.statement);
        row.issues = result.issues;
        const key = payloadKey(rec.statement);
        if (live) {
          row.changed = key !== row.payloadKey;
          this.log(row, row.changed ? "content changed since filing" : "unchanged since filing");
          continue;
        }
        row.payloadKey = key;
        if (!result.ok) { row.state = S.INVALID; this.log(row, "pre-flight failed", `${result.errors.length} error(s)`); continue; }
        const gate = riskGate(rec.statement);
        if (gate) { row.state = S.BLOCKED_RISK; row.gate = gate; this.log(row, "held by risk gate", gate); continue; }
        if (row.state === S.REJECTED && row.rejectedKey === key) { this.log(row, "rejected earlier, unchanged"); continue; }
        row.state = S.READY;
        this.log(row, "ready to file");
      }
    }

    async call(body) {
      let lastError;
      for (let attempt = 1; attempt <= 6; attempt++) {
        const token = await makeToken(SANDBOX_CREDS);
        const xml = envelope(token, SANDBOX_CREDS.clientId, body);
        this.stats.requests++;
        try {
          return parseResponse(await this.mock.handle(xml));
        } catch (e) {
          if (!(e instanceof TransportError) || !e.retryable) throw e;
          lastError = e;
          this.stats.retries++;
          await new Promise((r) => setTimeout(r, Math.min(200, 10 * 2 ** attempt * Math.random())));
        }
      }
      throw lastError;
    }

    async submitOne(row) {
      row.state = S.SUBMITTING;
      row.attempts++;
      this.log(row, "signed and sent", "submitDds");
      try {
        const resp = await this.call(submitBody(row.statement));
        row.uuid = txt(resp, "uuid");
        row.state = S.SUBMITTED;
        row.traces = "SUBMITTED";
        this.log(row, "TRACES accepted", `uuid ${row.uuid}`);
        return "submitted";
      } catch (e) {
        if (e instanceof SoapFault) {
          Object.assign(row, { state: S.REJECTED, error: e.message, rejectedKey: row.payloadKey });
          this.log(row, "TRACES refused", e.message);
          return "rejected";
        }
        row.state = S.READY;
        row.error = e.message;
        this.log(row, "transport failed after retries", e.message);
        return "failed";
      }
    }

    async poll() {
      const pending = [...this.ledger.values()].filter((r) => r.state === S.SUBMITTED);
      for (let i = 0; i < pending.length; i += 50) {
        const chunk = pending.slice(i, i + 50);
        let resp;
        try { resp = await this.call(getDdsBody(chunk.map((r) => r.uuid))); } catch (e) { continue; }
        const byUuid = new Map(chunk.map((r) => [r.uuid, r]));
        for (const ov of kids(resp, "ddsOverviewList")) {
          const row = byUuid.get(txt(ov, "uuid"));
          if (!row) continue;
          const status = txt(ov, "status");
          if (status === row.traces) continue;
          row.traces = status;
          row.reference = txt(ov, "referenceNumber");
          row.verification = txt(ov, "verificationNumber");
          row.version = txt(ov, "version");
          if (status === "AVAILABLE" && row.reference) { row.state = S.DONE; this.log(row, "official numbers issued", `${row.reference} / ${row.verification}`); }
          else if (status === "REJECTED") {
            Object.assign(row, { state: S.REJECTED, error: "TRACES status REJECTED", rejectedKey: row.payloadKey });
            this.log(row, "rejected by TRACES after processing");
          }
        }
      }
      return [...this.ledger.values()].filter((r) => r.state === S.SUBMITTED).length;
    }

    async withdraw(ref) {
      const row = this.ledger.get(ref);
      const resp = await this.call(`        <dds:WithdrawDdsRequest>\n${el("dds:uuid", row.uuid, "            ")}\n        </dds:WithdrawDdsRequest>`);
      row.state = S.WITHDRAWN;
      row.traces = txt(resp, "status");
      this.log(row, "withdrawn");
    }

    async previewEnvelope(ref) {
      const row = this.ledger.get(ref);
      const token = await makeToken(SANDBOX_CREDS);
      return envelope(token, "YOUR_CLIENT_ID", submitBody(row.statement, { truncateGeo: true }), { redact: true });
    }

    counts() {
      const c = {};
      for (const r of this.ledger.values()) c[r.state] = (c[r.state] || 0) + 1;
      return c;
    }
  }

  return {
    S, validateStatement, geojsonAreaHectares, loadJsonl, loadCsv, parseCsv, generateSamples, commodityName,
    makeToken, passwordDigest, envelope, submitBody, statementXml, encodeGeojson, sha256Hex, bytesToB64,
    MockTraces, Sandbox, riskGate, SANDBOX_CREDS, SoapFault, TransportError,
  };
});
