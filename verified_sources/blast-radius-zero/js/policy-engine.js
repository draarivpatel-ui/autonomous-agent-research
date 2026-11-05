/**
 * BlastRadius Zero - Deterministic Policy Engine
 * Heuristic text scanner for shell / SQL / Terraform / JSON.
 * No LLM calls - pure deterministic analysis.
 * Exposes: window.BlastPolicy.analyze(text) -> { score, violations, affectedResources, findings }
 */

(function () {
  "use strict";

  // ── Policy definitions ───────────────────────────────────────────────
  const POLICIES = [
    { id: "POL-01", name: "No Unbounded Deletion", short: "No Unbounded Delete" },
    { id: "POL-02", name: "Least Privilege IAM", short: "Least Privilege" },
    { id: "POL-03", name: "Cost Cap ≤ $50/hr", short: "Cost Cap" },
    { id: "POL-04", name: "Zero Secret Exfiltration", short: "No Exfiltration" },
  ];

  // ── Resource catalogue ──────────────────────────────────────────────
  const ALL_RESOURCES = [
    { id: "lb", label: "Load Balancer", kind: "network" },
    { id: "gpu", label: "GPU Cluster", kind: "compute" },
    { id: "compute", label: "Vultr Compute", kind: "compute" },
    { id: "postgres", label: "Primary Postgres", kind: "database" },
    { id: "redis", label: "Redis Cluster", kind: "cache" },
    { id: "iam", label: "IAM Roles", kind: "security" },
    { id: "secrets", label: "Secrets / Env", kind: "security" },
    { id: "storage", label: "Object Storage", kind: "storage" },
  ];

  // ── Helpers ──────────────────────────────────────────────────────────
  function lineNo(text, idx) {
    return text.slice(0, idx).split("\n").length;
  }

  // ── Rule implementations ───────────────────────────────────────────
  // Each rule: { id, policy, severity(1-10), test(text)->finding[] }
  // finding: { rule, policy, severity, message, line, remediation }

  const RULES = [
    // ── POL-01 : No Unbounded Deletion ──────────────────────────────
    {
      id: "R01-rm-rf",
      policy: "POL-01",
      severity: 10,
      label: "Recursive filesystem deletion",
      test(text) {
        const re = /rm\s+.*-rf\s+\/|rm\s+-rf\s+\//gi;
        return matchRule(text, re, this,
          "Unbounded filesystem deletion - would destroy host filesystem");
      }
    },
    {
      id: "R01-rm-rf-any",
      policy: "POL-01",
      severity: 9,
      label: "Recursive rm -rf",
      test(text) {
        // rm -rf on non-root but still dangerous (avoid double-counting the / case)
        const re = /rm\s+(-[a-z]*r[a-z]*f[a-z]*\b|--recursive\b)[^\n]*/gi;
        const hits = [];
        let m;
        while ((m = re.exec(text))) {
          // skip if already caught by R01-rm-rf (contains " /")
          if (/rm\s+.*-rf\s+\//i.test(m[0])) continue;
          hits.push(hit(this, text, m,
            `Recursive deletion: \`${m[0].trim().slice(0, 80)}\` - requires explicit allowlist`));
        }
        return hits;
      }
    },
    {
      id: "R01-drop-table",
      policy: "POL-01",
      severity: 10,
      label: "DROP TABLE",
      test(text) {
        // Matches SQL DROP TABLE with or without IF EXISTS
        const re = /DROP\s+TABLE\s+(IF\s+EXISTS\s+)?["'`\w.]+/gi;
        return matchRule(text, re, this, "Irreversible DROP TABLE - use expand-contract migration");
      }
    },
    {
      id: "R01-drop-column",
      policy: "POL-01",
      severity: 9,
      label: "DROP COLUMN",
      test(text) {
        const re = /ALTER\s+TABLE\s+[^;]+\bDROP\s+COLUMN\b[^;]*/gi;
        return matchRule(text, re, this, "DROP COLUMN is non-reversible - use phased column deprecation");
      }
    },
    {
      id: "R01-drop-database",
      policy: "POL-01",
      severity: 10,
      label: "DROP DATABASE",
      test(text) {
        const re = /DROP\s+DATABASE\s+["'`\w-]+/gi;
        return matchRule(text, re, this, "DROP DATABASE - catastrophic data loss");
      }
    },
    {
      id: "R01-truncate",
      policy: "POL-01",
      severity: 9,
      label: "TRUNCATE TABLE",
      test(text) {
        const re = /TRUNCATE\s+TABLE\s+["'`\w.]+/gi;
        return matchRule(text, re, this, "TRUNCATE TABLE - unbounded row deletion without WHERE clause");
      }
    },
    {
      id: "R01-delete-no-where",
      policy: "POL-01",
      severity: 8,
      label: "DELETE without WHERE",
      test(text) {
        // DELETE FROM tbl;  or DELETE FROM tbl\n  (no WHERE before ; or end)
        const re = /DELETE\s+FROM\s+["'`\w.]+\s*(;|\n|$)/gi;
        const hits = [];
        let m;
        while ((m = re.exec(text))) {
          const snippet = m[0];
          // if WHERE appears between DELETE and ; it's safe
          const segment = text.slice(m.index, m.index + m[0].length + 200);
          // simple: the matched snippet itself has no WHERE
          if (!/WHERE/i.test(snippet)) {
            hits.push(hit(this, text, m,
              `DELETE without WHERE clause - would remove all rows: \`${snippet.trim().slice(0, 70)}\``));
          }
        }
        return hits;
      }
    },
    {
      id: "R01-flushall",
      policy: "POL-01",
      severity: 8,
      label: "Redis FLUSHALL",
      test(text) {
        const re = /\bFLUSHALL\b|\bFLUSHDB\b/gi;
        return matchRule(text, re, this, "Redis FLUSHALL/FLUSHDB - wipes entire cache, causes thundering-herd stampede");
      }
    },
    {
      id: "R01-kubectl-delete-ns",
      policy: "POL-01",
      severity: 9,
      label: "kubectl delete namespace",
      test(text) {
        const re = /kubectl\s+delete\s+(ns|namespace)\b[^\n]*/gi;
        return matchRule(text, re, this, "kubectl delete namespace - destroys all workloads in namespace");
      }
    },
    {
      id: "R01-kubectl-delete-all",
      policy: "POL-01",
      severity: 9,
      label: "kubectl delete --all",
      test(text) {
        const re = /kubectl\s+delete[^\n]*--all\b[^\n]*/gi;
        return matchRule(text, re, this, "kubectl delete --all - unbounded resource deletion");
      }
    },
    {
      id: "R01-terraform-destroy",
      policy: "POL-01",
      severity: 9,
      label: "terraform destroy",
      test(text) {
        const re = /terraform\s+destroy\b[^\n]*/gi;
        return matchRule(text, re, this, "terraform destroy - deletes all managed infrastructure");
      }
    },

    // ── POL-02 : Least Privilege IAM ────────────────────────────────
    {
      id: "R02-iam-wildcard-action",
      policy: "POL-02",
      severity: 10,
      label: "IAM wildcard action (iam:*)",
      test(text) {
        const re = /["']iam:\*["']|action\s*=\s*["']iam:\*["']|actions\s*=\s*\[[^\]]*iam:\*[^\]]*\]/gi;
        // Also match bare iam:* in policy JSON / HCL
        const re2 = /\biam:\*/gi;
        // Use re2 but filter to IAM/policy context or aws cli
        const hits = [];
        let m;
        // Prefer precise match first
        const seen = new Set();
        while ((m = re.exec(text))) { hits.push(hit(this, text, m, "IAM wildcard action `iam:*` - grants full IAM control")); seen.add(m.index); }
        // Bare iam:* in aws cli or terraform
        const bareRe = /aws\s+iam\s+attach[^\n]*AdministratorAccess|policy_arn\s*=\s*["'][^"']*AdministratorAccess["']/gi;
        while ((m = bareRe.exec(text))) {
          if (!seen.has(m.index)) hits.push(hit(this, text, m, "AdministratorAccess policy attachment - full account takeover"));
        }
        // Also bare iam:* in non-already-matched context
        const bare2 = /(^|[^a-z_])iam:\*/gim;
        while ((m = bare2.exec(text))) {
          if (!seen.has(m.index)) {
            // Check surrounding context is IAM-ish
            const ctx = text.slice(Math.max(0, m.index - 120), m.index + 120).toLowerCase();
            if (ctx.includes("action") || ctx.includes("policy") || ctx.includes("permission") || ctx.includes("allow") || ctx.includes("statement")) {
              hits.push(hit(this, text, m, "IAM wildcard action `iam:*` in policy statement"));
              seen.add(m.index);
            }
          }
        }
        return hits;
      }
    },
    {
      id: "R02-admin-access",
      policy: "POL-02",
      severity: 10,
      label: "AdministratorAccess",
      test(text) {
        const re = /AdministratorAccess/gi;
        return matchRule(text, re, this, "AdministratorAccess grant - violates least-privilege principle");
      }
    },
    {
      id: "R02-wildcard-resource",
      policy: "POL-02",
      severity: 8,
      label: 'IAM Resource "*"',
      test(text) {
        // resource = "*"  or "Resource": "*"
        const re = /resource\s*=\s*["']\*["']|"Resource"\s*:\s*["']\*["']/gi;
        return matchRule(text, re, this, 'IAM Resource "*" - policy applies to every resource in the account');
      }
    },
    {
      id: "R02-assume-role-wildcard",
      policy: "POL-02",
      severity: 9,
      label: "sts:AssumeRole wildcard",
      test(text) {
        const re = /sts:AssumeRole[^"']*["'][^"']*\*[^"']*["']/gi;
        const re2 = /Action[^\n]*sts:\*/gi;
        let hits = matchRule(text, re, this, "sts:AssumeRole with wildcard - privilege escalation vector");
        hits = hits.concat(matchRule(text, re2, { ...this, severity: 8 }, "sts:* wildcard - overly broad STS permission"));
        return hits;
      }
    },

    // ── POL-03 : Cost Cap ───────────────────────────────────────────
    {
      id: "R03-gpu-scale",
      policy: "POL-03",
      severity: 9,
      label: "GPU autoscale / spinup",
      test(text) {
        const hits = [];
        // vultr gpu / gpu node patterns
        const patterns = [
          { re: /replicas\s*=\s*(\d+)/gi, threshold: 5, msg: n => `Replica count ${n} exceeds cost-safe limit` },
          { re: /count\s*=\s*(\d+)[^\n]*gpu/gi, threshold: 4, msg: n => `GPU count ${n} - estimated cost breach` },
          { re: /gpus?\s*[:=]\s*(\d+)/gi, threshold: 4, msg: n => `GPU allocation ${n} exceeds budget` },
        ];
        for (const p of patterns) {
          let m;
          while ((m = p.re.exec(text))) {
            const n = parseInt(m[1], 10);
            if (n > p.threshold) hits.push(hit(this, text, m, p.msg(n)));
          }
        }
        // Explicit vultr gpu spinup
        const gpuRe = /vultr\s+.*gpu|gpu.*cluster.*spinup|spinup.*gpu/gi;
        let m;
        while ((m = gpuRe.exec(text))) {
          hits.push(hit(this, text, m, "GPU cluster spinup detected - verify cost cap before approval"));
        }
        // $ cost annotation
        const costRe = /\$\s*[\d,]+\s*\/\s*hr/gi;
        while ((m = costRe.exec(text))) {
          const val = parseFloat(m[0].replace(/[^0-9.]/g, ""));
          if (val > 50) hits.push(hit(this, text, m, `Estimated cost ${m[0].trim()} exceeds $50/hr cap`));
        }
        // Terraform large count
        const tfCount = /resource\s+"[^"]+"\s+"[^"]+"\s*\{[^}]*count\s*=\s*(\d+)/gi;
        while ((m = tfCount.exec(text))) {
          const n = parseInt(m[1], 10);
          if (n > 10) hits.push(hit(this, text, m, `Terraform count = ${n} - potential cost explosion`));
        }
        return hits;
      }
    },
    {
      id: "R03-unbounded-autoscale",
      policy: "POL-03",
      severity: 7,
      label: "Unbounded autoscale",
      test(text) {
        const re = /maxReplicas\s*:\s*(\d+)|max_replicas\s*=\s*(\d+)|autoscal\w+[^\n]*max[^\n]*(\d+)/gi;
        const hits = [];
        let m;
        while ((m = re.exec(text))) {
          const n = parseInt(m[1] || m[2] || m[3], 10);
          if (n > 20) hits.push(hit(this, text, m, `Autoscale maxReplicas=${n} - unbounded scaling risk`));
        }
        return hits;
      }
    },

    // ── POL-04 : Zero Secret Exfiltration ───────────────────────────
    {
      id: "R04-curl-exfil",
      policy: "POL-04",
      severity: 10,
      label: "curl exfiltration",
      test(text) {
        const hits = [];
        // curl with env / secret in URL or body, or piping to bash
        const patterns = [
          /curl[^\n]*\$\{?[_A-Z]+(?:TOKEN|SECRET|KEY|PASSWORD|CREDENTIAL)[^\n]*/gi,
          /curl[^\n]*env[^\n]*\|\s*curl/gi,
          /curl[^\n]*\$\(env\)[^\n]*/gi,
          /curl[^\n]*--data[^\n]*\$[A-Z_]+/gi,
          /curl[^\n]*https?:\/\/[^\s"']+[^\n]*/gi, // any curl to external URL is suspicious in agent context
        ];
        // Only flag curl-to-external as exfil if combined with env/secret context
        let m;
        const curlRe = /curl[^\n]*/gi;
        while ((m = curlRe.exec(text))) {
          const line = m[0];
          const lower = line.toLowerCase();
          const hasSecretRef = /\$(?:\{)?[A-Z_]*(?:SECRET|TOKEN|KEY|PASSWORD|ENV|CREDENTIAL|AWS_|GITHUB_)/.test(line)
            || /\benv\b/i.test(line) || /\$\(/.test(line);
          const hasExternal = /https?:\/\//.test(line);
          const hasPipe = /\|\s*(?:bash|sh|python|node)\b/.test(line);
          if ((hasExternal && hasSecretRef) || hasPipe) {
            hits.push(hit(this, text, m,
              `Potential secret exfiltration via curl: \`${line.trim().slice(0, 90)}\``));
          } else if (hasExternal && /exfil|attacker|ngrok|oast|burpcollaborator|webhook/i.test(text)) {
            hits.push(hit(this, text, m, "curl to external host in untrusted context - possible exfiltration"));
          }
        }
        // Generic: markdown image exfil pattern ![...](https://attacker...${SECRET})
        const mdExfil = /!\[[^\]]*\]\([^)]*\$\{?[A-Z_]+[^)]*\)/gi;
        while ((m = mdExfil.exec(text))) {
          hits.push(hit(this, text, m, "Markdown image with secret interpolation - covert exfiltration channel"));
        }
        return hits;
      }
    },
    {
      id: "R04-env-dump",
      policy: "POL-04",
      severity: 9,
      label: "Environment dump",
      test(text) {
        const re = /\benv\b[^\n]*\|\s*base64|\bprintenv\b|\bexport\b[^\n]*\|\s*curl|\bcat\s+\/proc\/self\/environ/gi;
        return matchRule(text, re, this, "Environment variable dump - would expose secrets");
      }
    },
    {
      id: "R04-secret-in-url",
      policy: "POL-04",
      severity: 9,
      label: "Secret in URL",
      test(text) {
        // token=, key=, secret= in URL
        const re = /https?:\/\/[^\s"'`]*[?&](?:token|key|secret|password|api_key)=[^\s"'`&]+/gi;
        return matchRule(text, re, this, "Secret material embedded in URL query string");
      }
    },
    {
      id: "R04-wget-exfil",
      policy: "POL-04",
      severity: 10,
      label: "wget exfiltration",
      test(text) {
        const re = /wget[^\n]*\$\{?[A-Z_]+[^\n]*/gi;
        const hits = [];
        let m;
        while ((m = re.exec(text))) {
          if (/\$(?:\{)?[A-Z_]*(?:SECRET|TOKEN|KEY|PASSWORD)/.test(m[0]) || /https?:\/\//.test(m[0])) {
            hits.push(hit(this, text, m, `wget with secret/external URL: \`${m[0].trim().slice(0, 80)}\``));
          }
        }
        return hits;
      }
    },
  ];

  // ── Shared helpers ─────────────────────────────────────────────────
  function hit(rule, text, match, message) {
    return {
      rule: rule.id,
      policy: rule.policy,
      severity: rule.severity,
      label: rule.label,
      message: message || match[0].trim().slice(0, 120),
      snippet: match[0].trim().slice(0, 140),
      line: lineNo(text, match.index),
      remediation: remediationFor(rule.id),
    };
  }

  function matchRule(text, re, rule, message) {
    const hits = [];
    let m;
    while ((m = re.exec(text))) {
      hits.push(hit(rule, text, m, message));
    }
    return hits;
  }

  function remediationFor(ruleId) {
    const map = {
      "R01-rm-rf": "Replace with scoped delete targeting an explicit path allowlist; require --dry-run preview.",
      "R01-rm-rf-any": "Scope deletion to an allowlisted directory; add pre-flight `ls` dry-run.",
      "R01-drop-table": "Use expand-contract: create new table → dual-write → backfill → cutover → drop (phased).",
      "R01-drop-column": "Add new column nullable → backfill → switch reads → drop old column in next release.",
      "R01-drop-database": "Never auto-drop databases; require manual approval + snapshot restore point.",
      "R01-truncate": "Use batched DELETE with LIMIT + WHERE predicate; or create replacement table.",
      "R01-delete-no-where": "Add explicit WHERE clause or use LIMIT 1; require row-count preview.",
      "R01-flushall": "Use token-bucket coalescing + read-replica autoscale; never flush cache under load.",
      "R01-kubectl-delete-ns": "Cordon + drain; delete workloads individually with PDBs respected.",
      "R01-kubectl-delete-all": "Target specific label selectors; require namespace + resource-type scope.",
      "R01-terraform-destroy": "Use targeted `terraform destroy -target=...` after state backup.",
      "R02-iam-wildcard-action": 'Replace `iam:*` with explicit action list (e.g., `iam:GetRole`, `iam:ListRoles`).',
      "R02-admin-access": "Grant minimal managed policy (e.g., PowerUser without IAM write); use permission boundary.",
      "R02-wildcard-resource": 'Scope Resource to specific ARNs instead of "*".',
      "R02-assume-role-wildcard": "Restrict `sts:AssumeRole` to specific role ARNs.",
      "R03-gpu-scale": "Cap replicas at budget limit; require cost-approval gate for GPU scale > $50/hr.",
      "R03-unbounded-autoscale": "Set maxReplicas ceiling aligned to cost cap; add HPA with cost webhook.",
      "R04-curl-exfil": "Block outbound curl/wget in agent sandbox; redact secrets before any network call.",
      "R04-env-dump": "Never dump env; use scoped secret accessor with audit log.",
      "R04-secret-in-url": "Move secrets to Authorization header or env; never embed in URL.",
      "R04-wget-exfil": "Block outbound wget in sandbox; strip secrets before network egress.",
    };
    return map[ruleId] || "Review and apply least-privilege / safe-default remediation.";
  }

  // ── Blast radius scoring ───────────────────────────────────────────
  function scoreFromFindings(findings) {
    if (findings.length === 0) return 0;
    // Weighted by severity
    let raw = 0;
    for (const f of findings) raw += f.severity * 3.2;
    // Diminishing for multiple findings of same policy
    const policyCount = new Set(findings.map(f => f.policy)).size;
    raw += (policyCount - 1) * 6;
    // Clamp
    const floor = findings.some(f => f.severity >= 8) ? 35 : 12;
    return Math.min(100, Math.max(floor, Math.round(raw)));
  }

  function affectedFromFindings(findings) {
    if (findings.length === 0) return [];
    const policyToResources = {
      "POL-01": ["postgres", "compute", "storage", "redis"],
      "POL-02": ["iam", "compute", "secrets"],
      "POL-03": ["gpu", "compute", "lb"],
      "POL-04": ["secrets", "iam", "compute"],
    };
    const ruleToResources = {
      "R01-flushall": ["redis"],
      "R01-rm-rf": ["compute", "storage"],
      "R01-drop-table": ["postgres"],
      "R01-drop-column": ["postgres"],
      "R01-drop-database": ["postgres"],
      "R01-truncate": ["postgres"],
      "R01-delete-no-where": ["postgres"],
      "R02-iam-wildcard-action": ["iam", "secrets"],
      "R02-admin-access": ["iam", "secrets", "compute"],
      "R03-gpu-scale": ["gpu", "compute"],
      "R04-curl-exfil": ["secrets", "iam"],
      "R04-env-dump": ["secrets"],
    };
    const ids = new Set();
    for (const f of findings) {
      const specific = ruleToResources[f.rule];
      if (specific) specific.forEach(id => ids.add(id));
      else {
        const fallback = policyToResources[f.policy] || [];
        fallback.forEach(id => ids.add(id));
      }
    }
    return [...ids];
  }

  // ── Public API ─────────────────────────────────────────────────────
  function analyze(text) {
    const input = (text || "").trim();
    if (!input) {
      return { score: 0, violations: [], findings: [], affected: [], clean: true, policyStates: emptyPolicyStates() };
    }
    const findings = [];
    for (const rule of RULES) {
      try {
        const hits = rule.test(input);
        if (hits && hits.length) findings.push(...hits);
      } catch (_) {
        findings.push({
          rule: "ENGINE-ERROR", policy: rule.policy, severity: 10,
          label: "Analysis failed", message: "Rule evaluation failed; approval denied",
          snippet: "", line: 0, remediation: "Repair the scanner before approving this plan",
        });
      }
    }
    // Deduplicate by (rule + line)
    const seen = new Set();
    const deduped = findings.filter(f => {
      const k = f.rule + ":" + f.line + ":" + f.snippet.slice(0, 40);
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    });

    const score = scoreFromFindings(deduped);
    const affected = affectedFromFindings(deduped);
    const policyStates = buildPolicyStates(deduped);
    const violations = [...new Set(deduped.map(f => f.policy))];

    return {
      score,
      violations,
      findings: deduped,
      affected,
      clean: deduped.length === 0,
      policyStates,
    };
  }

  function emptyPolicyStates() {
    return POLICIES.map(p => ({ ...p, status: "pass", count: 0 }));
  }

  function buildPolicyStates(findings) {
    const byPolicy = {};
    for (const f of findings) {
      byPolicy[f.policy] = byPolicy[f.policy] || [];
      byPolicy[f.policy].push(f);
    }
    return POLICIES.map(p => {
      const hits = byPolicy[p.id] || [];
      if (hits.length === 0) return { ...p, status: "pass", count: 0 };
      const maxSev = Math.max(...hits.map(h => h.severity));
      return { ...p, status: maxSev >= 8 ? "block" : "warn", count: hits.length, maxSeverity: maxSev };
    });
  }

  // Expose
  window.BlastPolicy = { analyze, POLICIES, ALL_RESOURCES, RULES };

})();
