# Drift public deployment readiness

This document is a migration and verification plan, not a claim that Drift is production-ready.

## Product requirements

- Preserve the existing interface and frontend behavior.
- Keep core downloads free and do not require visitor accounts.
- Do not buy a domain or add advertisements before the service is stable.
- Keep the frontend independent from the backend host.
- Enforce explicit usage, resource, and spending safeguards.
- Keep `main` untouched while preparing the `public-deployment` branch.

## Current implementation

The app is a single-process Python `ThreadingHTTPServer`. The static frontend is separated under `public/` and uses a configurable API origin. The backend runs yt-dlp jobs through a local `ThreadPoolExecutor`, stores job state in SQLite, and writes media to local disk. It exposes analysis, download creation, job status/cancel, file, and health routes.

This is suitable for local development and controlled testing. It is not a horizontally scalable public service.

## Current queue behavior

The local SQLite-backed queue now polls for due queued jobs and dispatches them while the process is running. Startup recovery runs before the dispatcher starts:

- Existing queued jobs are preserved, including their delayed `available_at` schedule.
- Stale lease metadata is cleared from queued jobs.
- Interrupted downloading/processing jobs have partial workspace output removed and are requeued immediately if retry attempts remain.
- Interrupted jobs that have exhausted the retry limit become terminal failures.

The latest GitHub Actions run for commit `08883937` passed all configured checks: Python unit tests, Vercel frontend build, JavaScript syntax, Compose validation, and backend Docker image build. Workflow: https://github.com/chsaimazhar-cell/drift/actions/runs/43633396296

**Important limitation:** this is still a single-process local queue backed by SQLite. It cannot coordinate multiple instances and cannot recover job state or files after ephemeral disk loss, host replacement, or storage deletion. It is not the production durable queue.

## Audit findings and deployment blockers

1. **Shared queue and job state:** the executor, cancellation events, and several controls remain process-local. PostgreSQL transactional claims, shared leases, and a separate worker lifecycle are not implemented.
2. **Persistence:** local SQLite and media files are not suitable as production shared durable storage. Host replacement or ephemeral disk loss can remove them.
3. **Private object storage:** the adapter contract exists, but no real provider adapter, private bucket, signed-download implementation, lifecycle policy, or provider integration test is configured.
4. **Media transfer:** large files currently pass through the Python service; production should use private object storage and short-lived authorized downloads where appropriate.
5. **Abuse controls:** local controls and a shared-store wrapper contract exist, but no real shared atomic provider adapter, durable daily quotas, or enforced global spending ceiling is connected.
6. **Trusted proxy identity:** forwarded client-IP headers must only be accepted from a specifically trusted proxy boundary; spoofable headers must never be trusted.
7. **Outbound network security:** application URL validation cannot constrain every yt-dlp redirect, extractor request, CDN hop, or DNS change. Deny-by-default infrastructure egress filtering remains mandatory.
8. **Job capabilities:** job IDs authorize access to status, cancellation, and files. Keep them high-entropy and out of logs/analytics; a separate token-hash design remains a hardening option.
9. **CORS:** a separately hosted frontend must configure the exact allowed origin; wildcard CORS is not recommended.
10. **Analysis abuse:** metadata extraction is expensive and needs shared concurrency limits, timeouts, and per-client quotas just like downloads.

## Existing protections to retain

- HTTP(S)-only URL parsing, supported-host allowlist, credential rejection, and rejection of non-public DNS answers at initial validation.
- Bounded workers/handlers, maximum download size, local storage reservations, basic rate limits, cancellation, job expiry, and temporary-file cleanup.
- Filename sanitization, safe subprocess argument lists, FFmpeg inspection/conversion timeouts, socket idle timeout, health endpoint, and byte-range file serving tests.
- Non-root Docker runtime, read-only root filesystem, dropped Linux capabilities, `no-new-privileges`, and bounded non-executable temporary storage.
- Closed-by-default CORS and configurable frontend API origin.

These are code-level protections, not proof that production network, storage, or provider controls are active.

## Target architecture

- **Frontend:** static Vercel site preserving the current UI, configured with a backend API origin.
- **API:** containerized Python service for validation, job creation, authorization, policy, and status.
- **Worker:** separate long-running process for yt-dlp and FFmpeg, coordinated by a durable shared queue.
- **Database:** PostgreSQL for job state, leases, quotas, capability-token hashes, and lifecycle metadata.
- **Media storage:** private object storage with short-lived download authorization, byte-range support, retention, and automatic deletion.
- **Abuse controls:** shared atomic per-client/global quotas, active-job limits, bounded queue, global pause, usage accounting, and operational kill switch.
- **Network:** deny-by-default worker egress controls that block private/reserved/link-local/metadata destinations and prevent bypass.
- **Portability:** small provider-neutral adapters and environment-based configuration; no provider-specific frontend logic.

No visitor registration is required. A random high-entropy job capability can authorize access to a single job without creating an account.

## Spending and usage guardrails

Before public beta, configure and test:
- Maximum file size and media duration.
- Maximum active jobs per client and globally; maximum queued jobs.
- Per-client analysis/download quotas over rolling and daily windows.
- Worker timeouts, retry ceilings, and bounded FFmpeg concurrency.
- Short file retention, object lifecycle deletion, and total storage allocation.
- Bandwidth/egress budget, provider billing alerts, and a kill switch that stops new work.
- Request-size limits, exact CORS origin, redacted logs, and queue/storage/error monitoring.

Billing alerts are not necessarily hard spending caps. Application-enforced quotas and a fast operational shutdown path are also required. Do not assume any global spending safeguard is active until it is implemented and verified against real services.

## Migration sequence

1. Preserve the original interface and existing API contract; keep `main` untouched.
2. Complete PostgreSQL repository, migrations, transactional job claims, queue limits, and concurrency tests.
3. Add independent worker process, lease heartbeat, crash recovery, idempotency, cancellation, and orphan reconciliation.
4. Implement private object storage, signed authorization, expiry, deletion, and failure/retention tests.
5. Connect shared atomic abuse controls, global pause, usage accounting, and operational quotas.
6. Enforce worker egress restrictions at the infrastructure/network layer and test redirects and DNS rebinding.
7. Add real-provider integration, restart, failure-injection, load, restore, and cost-ceiling tests.
8. Configure deployment and trusted proxy boundaries; verify the full browser download flow.
9. Run a private beta with monitoring before considering public access.

## Current checklist (2026-11-14)

### Completed in repository (code-level only)

- [x] Preserve original frontend interface and separate static assets.
- [x] Configure frontend API origin independently from backend host.
- [x] CI runs Python tests, frontend build/syntax checks, Compose validation, and backend image build.
- [x] Harden single-instance container defaults and resource limits.
- [x] Bound HTTP handler concurrency, socket idle time, FFmpeg probe/conversion time, file size, and local storage reservations.
- [x] Validate supported source hosts and reject credentials, unsafe ports/control characters, and non-public DNS answers at initial validation.
- [x] Redact media URL query strings and job IDs from logs.
- [x] Keep CORS closed by default and provide a local emergency pause switch.
- [x] Define/test local abuse-control and shared-store wrapper contracts with a fake shared store; no real provider is connected.
- [x] Add automatic due-job dispatch in the local SQLite queue.
- [x] Preserve queued jobs and delayed retry schedules across a same-disk process restart; requeue interrupted work within retry limits and fail exhausted jobs.
- [x] Latest CI run `43633396296` passed all five configured checks, including Docker image build.

### Remaining deployment blockers

- [ ] PostgreSQL production repository, migrations, atomic claims, and concurrency integration tests.
- [ ] Independent durable worker, lease heartbeat, restart/crash recovery, idempotency, and orphan reconciliation.
- [ ] Real private object storage adapter, signed download authorization, retention, and deletion.
- [ ] Real shared abuse-control adapter, global quotas, daily limits, usage accounting, and kill switch.
- [ ] Trusted reverse-proxy boundary and safe client-IP handling.
- [ ] Infrastructure-level deny-by-default egress filtering, including redirect and DNS-rebinding tests.
- [ ] Hard worker CPU/memory/time, storage, bandwidth/egress, and spending safeguards.
- [ ] Integration/load/failure/restore tests against real configured services.
- [ ] Backend host and services selected/configured only after separate owner approval.
- [ ] Frontend connected to verified backend origin; complete browser download flow tested.
- [ ] Private beta, monitoring, incident response, and restore procedure verified.

See [outbound-network-security-policy.md](outbound-network-security-policy.md), [private-beta-verification-runbook.md](private-beta-verification-runbook.md), and [backend-implementation-audit.md](backend-implementation-audit.md). These are plans and test criteria, not evidence that provider controls are active.

## Explicit approval boundary

Research and repository preparation are approved. Provider account creation, external service connections, credential entry, purchases, subscriptions, payment details, and production deployment are **not** approved. Each requires separate explicit approval; paid provisioning also requires an agreed maximum spend.

## Launch decision

**Not ready for production deployment.** Do not ask the owner to click Deploy yet. The static Vercel frontend alone is not a working downloader. Keep downloads free, visitor registration optional/non-required, the domain unpurchased, and advertisements deferred until the backend and safeguards are implemented and verified.
