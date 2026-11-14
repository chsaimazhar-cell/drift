# Drift public deployment readiness

This document records the first audit of the `public-deployment` branch. It is a migration plan, not a claim that Drift is production-ready.

## Product requirements

- Preserve the existing embedded HTML/CSS/JavaScript interface.
- Keep core downloads free and do not require visitor accounts.
- Do not buy a domain as part of this work.
- Do not add advertisements before the service is stable.
- Keep the frontend independent from the backend host so infrastructure can change without redesigning the product.
- Enforce explicit resource limits so traffic cannot create unbounded compute, storage, or bandwidth costs.

## Current implementation

The current app is a single-process Python `ThreadingHTTPServer`. It embeds the frontend in `drift.py`, runs yt-dlp jobs in a local `ThreadPoolExecutor`, stores job state in SQLite, writes media to the local filesystem, and serves files directly from that filesystem. The browser calls `/api/analyze`, `/api/downloads`, job status/cancel routes, and the file route.

This is suitable for local development and controlled testing. It is not yet a horizontally scalable public service.

## Audit findings

### Deployment blockers

1. **Process-local work queue and cancellation state.** `ThreadPoolExecutor`, `CANCEL_EVENTS`, and rate-limit buckets exist only in one process. Multiple instances would not share job ownership, queue state, or limits.
2. **Ephemeral local persistence.** SQLite and completed media files live on the local filesystem. A restart, redeploy, or host replacement can lose both job state and downloadable files.
3. **No durable worker lifecycle.** Startup marks interrupted jobs failed. There is no durable queue or separate worker process to resume/retry jobs safely.
4. **Direct media transfer from the app.** Large files pass through the Python web process, consuming its outbound bandwidth and connection capacity.
5. **Abuse controls are not production controls.** Current limits are in-memory and per apparent client IP; there are no durable per-IP daily quotas, per-IP active-job limits, global spending ceilings, or shared limits across instances.
6. **Proxy identity is unresolved.** The app deliberately ignores forwarded-IP headers. Behind a reverse proxy, all clients may appear to have the proxy's address unless a trusted-proxy boundary is explicitly configured.
7. **URL validation has a DNS time-of-check/time-of-use gap.** The hostname is checked before yt-dlp later connects to it. A DNS change or redirect can invalidate the earlier check. This needs defense-in-depth at the egress/network boundary, not just a one-time DNS lookup.
8. **Job access uses an unguessable job ID as a bearer capability.** Status, cancellation, and file routes do not bind a job to a client capability beyond possession of the ID. The replacement API should use high-entropy job access tokens, store only their hashes, and avoid exposing them in logs or analytics.
9. **CORS defaults to wildcard.** CORS is not authentication, but the production deployment should still set the exact frontend origin and reject unexpected browser origins where appropriate.
10. **Analysis requests are expensive too.** yt-dlp metadata extraction needs shared concurrency limits, timeouts, and per-client quotas just like downloads.

### Existing protections worth retaining

- HTTP(S)-only URL parsing, supported-host allowlist, credential rejection, and rejection of non-public DNS results.
- A maximum download size, bounded worker count, basic rate limits, cancellation, job expiry, and temporary-file cleanup.
- Filename sanitization, subprocess argument lists (not shell interpolation), MP4 compatibility processing, health endpoint, and tests for URL validation and byte-range file serving.
- The frontend already uses a configurable `window.API_BASE`, which is a useful seam for separating the website from the API.

## Target architecture

Keep the frontend as a static site and the downloader as a separately deployable Python service.

- **Frontend:** extract the current embedded HTML, CSS, and JavaScript into static files without redesigning them. Configure the API base URL at deployment time.
- **API service:** containerized Python service exposing a versioned HTTP API. It validates requests, issues job capabilities, enforces policy, and reports job state.
- **Worker:** separate long-running worker process for yt-dlp and FFmpeg. A durable queue coordinates API and worker processes.
- **Database:** managed PostgreSQL for jobs, quotas, job ownership-token hashes, and lifecycle state.
- **Media storage:** private object storage with short-lived signed download URLs and automatic lifecycle deletion. Do not use the application container's filesystem as durable storage.
- **Limits:** shared per-IP and global concurrency limits; bounded queue; request/analysis quotas; maximum duration and file size; download expiry; storage TTL; egress controls; monitoring and spend alerts.
- **Provider portability:** keep storage, queue, database, and downloader interfaces behind small adapters and use environment-based configuration. Avoid provider-specific logic in the frontend.

No visitor registration is required. A random, high-entropy job capability can authorize access to a single job without creating an account.

## Spending and usage guardrails

Initial public-beta limits must be conservative and configurable, not hard-coded promises. Before opening public access, configure:

- Maximum file size and maximum media duration.
- Maximum active jobs per client and globally.
- Maximum queued jobs globally.
- Per-client analysis and download quotas over rolling windows and daily windows.
- Worker timeouts, retry ceilings, and a maximum number of concurrent FFmpeg conversions.
- Short retention for completed files and automatic object deletion.
- A maximum total storage allocation and bandwidth/egress budget.
- Provider billing alerts and an operational kill switch that stops accepting new work.
- Request-size limits, strict origin configuration, structured logs with secrets and full media URLs redacted, and basic error/queue/storage monitoring.

A provider billing alert is not always a hard spending cap. The service must also enforce its own quotas and have a documented way to disable new jobs quickly.

## Migration sequence

1. **Audit and preserve:** record existing API behavior and tests; keep the original interface and `main` branch untouched.
2. **Separate the frontend:** extract embedded assets byte-for-byte, add a configurable API endpoint, and verify the visual interface and browser flows are unchanged.
3. **Harden the API contract:** validate all request fields server-side, add job capability tokens, exact CORS/origin policy, shared quotas, timeouts, and tests.
4. **Introduce storage and queue adapters:** retain a local development adapter while adding PostgreSQL, durable queue, and private object storage interfaces.
5. **Move media work into a worker:** make job transitions durable and idempotent; ensure cancellation, retries, cleanup, and worker restarts are tested.
6. **Add deployment configuration:** container image, health/readiness checks, non-root runtime, resource ceilings, secrets configuration, and provider-neutral environment variables.
7. **Load and failure testing:** test concurrent jobs, queue saturation, restart/redeploy, storage expiry, byte-range downloads, abusive request patterns, and cost ceilings.
8. **Controlled launch:** deploy a private test first; open a small public beta only after the safeguards work. Add no ads until stability is demonstrated.

## Not yet done

No production migration, provider provisioning, database creation, object-storage setup, public deployment, domain purchase, or advertisements have been performed. The current branch is the safe work area; `main` remains the source of truth until reviewed changes are ready.

## Verification update

The latest GitHub Actions run for commit `bcaa29580e88fa425dd326e513ee9717a99957b6` completed successfully. The workflow validates the Python test suite, builds the extracted frontend, checks JavaScript syntax, validates the Compose configuration, and builds the container image.

The branch now also includes bounded HTTP handler concurrency, FFmpeg probe and conversion timeouts, an HTTP idle-connection timeout, startup recovery for interrupted jobs, stricter URL port/control-character validation, and tests for these protections.

A passing CI run verifies the tested code and build steps; it does **not** establish production readiness. In particular, the DNS/redirect egress risk remains unresolved until the hosting network enforces outbound restrictions. The current app must not be exposed as an unrestricted public downloader. Keep it to controlled testing until the target backend, durable shared services, trusted proxy boundary, and network egress policy are implemented and verified.
