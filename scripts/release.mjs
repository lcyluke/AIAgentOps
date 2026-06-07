#!/usr/bin/env node
/**
 * APEX Release Script — version bump, changelog, git tag, GitHub release.
 *
 * Usage:
 *   node scripts/release.mjs patch    # 1.0.0 → 1.0.1
 *   node scripts/release.mjs minor    # 1.0.0 → 1.1.0
 *   node scripts/release.mjs major    # 1.0.0 → 2.0.0
 */

import { execSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, "..");
const PKG_PATH = join(ROOT, "pyproject.toml");

// ── Parse current version ──────────────────────────────────

const pkg = readFileSync(PKG_PATH, "utf-8");
const currentMatch = pkg.match(/version\s*=\s*"(\d+\.\d+\.\d+)"/);
if (!currentMatch) {
  console.error("❌ Cannot parse version from pyproject.toml");
  process.exit(1);
}
const current = currentMatch[1];
const [major, minor, patch] = current.split(".").map(Number);

// ── Bump version ───────────────────────────────────────────

const bump = process.argv[2] || "patch";
let next;
switch (bump) {
  case "major": next = `${major + 1}.0.0`; break;
  case "minor": next = `${major}.${minor + 1}.0`; break;
  case "patch": default: next = `${major}.${minor}.${patch + 1}`; break;
}

console.log(`📦 ${current} → ${next}`);

// Update pyproject.toml
const updated = pkg.replace(
  /version\s*=\s*"\d+\.\d+\.\d+"/,
  `version = "${next}"`
);
writeFileSync(PKG_PATH, updated);

// ── Changelog entry ────────────────────────────────────────

const today = new Date().toISOString().split("T")[0];
const tag = `v${next}`;

// Get commits since last tag
let commits;
try {
  commits = execSync(`git log --oneline $(git describe --tags --abbrev=0 2>/dev/null || echo "HEAD~5")..HEAD`, { encoding: "utf-8" });
} catch {
  commits = execSync("git log --oneline -10", { encoding: "utf-8" });
}

const changelog = `## ${tag} (${today})\n\n${commits.split("\n").filter(Boolean).map(c => `- ${c.slice(9)}`).join("\n")}\n`;

// Prepend to CHANGELOG.md or create it
const clPath = join(ROOT, "CHANGELOG.md");
let existing = "";
try { existing = readFileSync(clPath, "utf-8"); } catch {}
writeFileSync(clPath, changelog + "\n" + existing);

// ── Git operations ─────────────────────────────────────────

console.log("📝 Committing version bump...");
execSync("git add pyproject.toml CHANGELOG.md", { cwd: ROOT });
execSync(`git commit -m "🔖 Release ${tag}"`, { cwd: ROOT });
console.log(`🏷  Tagging ${tag}...`);
execSync(`git tag -a ${tag} -m "${tag}"`, { cwd: ROOT });
console.log("🚀 Pushing...");
execSync("git push --follow-tags", { cwd: ROOT });

// ── GitHub release hint ────────────────────────────────────

console.log(`\n✅ Release ${tag} ready!`);
console.log(`   Create release at:`);
console.log(`   https://github.com/lcyluke/AIAgentOps/releases/new?tag=${tag}`);
console.log(`\n   Or run: gh release create ${tag} --notes "See CHANGELOG.md"`);
