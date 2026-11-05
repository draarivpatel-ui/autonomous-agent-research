/* Default-deny structured action boundary. No command is ever executed here. */
(function (root) {
  "use strict";
  const fields = {
    read: ["path"], test: ["argv", "cwd"], delete: ["path", "dry_run"],
    iam: ["actions", "resources", "effect"],
    compute: ["replicas", "hourly_cost_cents", "duration_minutes"],
    network: ["host", "method", "data_class"],
  };
  const object = x => x !== null && typeof x === "object" && !Array.isArray(x);
  const keys = (x, expected) => object(x) &&
    JSON.stringify(Object.keys(x).sort()) === JSON.stringify([...expected].sort());
  const integer = (x, lo, hi) => Number.isSafeInteger(x) && x >= lo && x <= hi;
  function scopedPath(path, scope, includeRoot = true) {
    if (typeof path !== "string" || path.length > 1024 || !path.startsWith("/") ||
        /[\\\x00$*?~]/.test(path) || path.split("/").slice(1).some(p => ["", ".", ".."].includes(p))) return false;
    return (includeRoot && path === scope) || path.startsWith(scope + "/");
  }
  function decide(m) {
    const reasons = [];
    if (!keys(m, ["version", "kind", "parameters"]) || m.version !== 1 ||
        typeof m.kind !== "string" || !Object.hasOwn(fields, m.kind) ||
        !keys(m.parameters, fields[m.kind])) return { allowed: false, rules: ["SCHEMA"] };
    const p = m.parameters;
    switch (m.kind) {
      case "read":
        if (!scopedPath(p.path, "/workspace")) reasons.push("PATH_SCOPE");
        break;
      case "test":
        if (!["[\"python\",\"-m\",\"pytest\"]", "[\"python\",\"-m\",\"pytest\",\"-q\"]",
              "[\"pytest\",\"-q\"]", "[\"npm\",\"test\"]"].includes(JSON.stringify(p.argv)) ||
            !scopedPath(p.cwd, "/workspace")) reasons.push("TEST_ALLOWLIST");
        break;
      case "delete":
        if (p.dry_run !== true) reasons.push("DELETION_APPROVAL");
        if (!scopedPath(p.path, "/workspace/build", false)) reasons.push("PATH_SCOPE");
        break;
      case "iam":
        if (p.effect !== "Allow" || !Array.isArray(p.actions) || !p.actions.length ||
            p.actions.some(a => !["s3:GetObject", "s3:ListBucket"].includes(a)) ||
            !Array.isArray(p.resources) || !p.resources.length ||
            p.resources.some(r => !["arn:aws:s3:::contest-demo", "arn:aws:s3:::contest-demo/*"].includes(r)))
          reasons.push("LEAST_PRIVILEGE");
        break;
      case "compute":
        if (!integer(p.replicas, 1, 1000) || !integer(p.hourly_cost_cents, 0, 1000000) ||
            !integer(p.duration_minutes, 1, 60)) reasons.push("COST_SCHEMA");
        else if (p.replicas * p.hourly_cost_cents > 5000 ||
                 p.replicas * p.hourly_cost_cents * p.duration_minutes > 10000 * 60) reasons.push("COST_CAP");
        break;
      case "network": reasons.push("OFFLINE_EGRESS"); break;
    }
    return { allowed: reasons.length === 0, rules: [...new Set(reasons)].sort() };
  }
  root.BlastManifest = { decide, scopedPath };
  if (typeof document !== "undefined") document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("manifest-check")?.addEventListener("click", () => {
      const output = document.getElementById("manifest-result");
      try { output.textContent = JSON.stringify(decide(JSON.parse(document.getElementById("manifest-input").value)), null, 2); }
      catch (_) { output.textContent = JSON.stringify({ allowed: false, rules: ["SCHEMA"] }); }
    });
  });
  if (typeof module !== "undefined") module.exports = root.BlastManifest;
})(typeof window === "undefined" ? globalThis : window);
