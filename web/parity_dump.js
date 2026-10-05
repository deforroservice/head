// Used by tests/test_web_parity.py: runs the browser engine under Node and prints
// what it concluded about each input record, plus a digest and a SOAP body.
const fs = require("fs");
const path = require("path");
const E = require("./engine.js");

function walk(dir, base = dir, out = new Map()) {
  for (const name of fs.readdirSync(dir)) {
    const full = path.join(dir, name);
    if (fs.statSync(full).isDirectory()) walk(full, base, out);
    else if (/\.(geo)?json$/i.test(name)) out.set(path.relative(base, full).split(path.sep).join("/"), fs.readFileSync(full, "utf8"));
  }
  return out;
}

(async () => {
  const [input, nonceHex, created, password] = process.argv.slice(2);
  const text = fs.readFileSync(input, "utf8");
  const files = walk(path.dirname(input));
  const records = input.endsWith(".csv") ? E.loadCsv(text, files) : E.loadJsonl(text, files);
  const out = { records: {}, digest: null, bodies: {} };
  for (const rec of records) {
    if (!rec.statement) { out.records[rec.key] = { loaded: false }; continue; }
    const r = E.validateStatement(rec.statement);
    out.records[rec.key] = {
      loaded: true,
      errors: [...new Set(r.errors.map((i) => i.code))].sort(),
      warnings: [...new Set(r.warnings.map((i) => i.code))].sort(),
      gate: E.riskGate(rec.statement),
    };
    out.bodies[rec.key] = E.submitBody(rec.statement);
  }
  if (nonceHex) out.digest = await E.passwordDigest(Buffer.from(nonceHex, "hex"), created, password);
  process.stdout.write(JSON.stringify(out));
})();
