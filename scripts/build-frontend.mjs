import { writeFile } from "node:fs/promises";

const raw = (process.env.DRIFT_API_BASE_URL || "").trim();
if (!raw) {
  throw new Error("DRIFT_API_BASE_URL is required. Set it to the HTTPS URL of the Drift backend before deploying.");
}

let parsed;
try {
  parsed = new URL(raw);
} catch {
  throw new Error("DRIFT_API_BASE_URL must be a valid absolute URL.");
}
if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.search || parsed.hash) {
  throw new Error("DRIFT_API_BASE_URL must be an HTTPS origin or base path without credentials, query, or fragment.");
}

const apiBase = parsed.toString().replace(/\/$/, "");
await writeFile("public/runtime-config.js", `window.API_BASE = ${JSON.stringify(apiBase)};\n`, "utf8");
console.log("Generated frontend runtime config for:", parsed.origin);
