# Drift backend architecture and migration plan

**Status:** Design only — no external services have been selected or provisioned.  
**Applies to:** `public-deployment` branch.  
**Goal:** Keep the existing Drift interface and free, no-login downloads while allowing the backend to move between hosting providers without rewriting the downloader.

## Current implementation (do not mistake this for production readiness)

The current backend in `drift.py` is a single Python process:
- `ThreadingHTTPServer` serves the API and static files.
- `ThreadPoolExecutor` runs downloads in the same process.
- SQLite stores job records.
- Local disk stores source and converted media.
- Queue limits, rate limits, cancellation events, and storage accounting are process-local.

This is suitable for local development and controlled single-instance testing. It is not safe to scale to multiple replicas: different processes would have different queues, rate-limit maps, cancellation events, and potentially different local files. A process restart also interrupts active work.

## Target logical boundaries

Keep the downloader and HTTP API independent from infrastructure by introducing four replaceable interfaces:

1. **JobRepository** — create a job, fetch a job, make conditional status updates, claim work atomically, list expired jobs, and record completion/failure. Production implementation must use a shared managed database and migrations.
2. **JobQueue** — enqueue a job ID, claim it with a lease, acknowledge completion, retry transient failures, and recover expired leases. Production implementation must be durable; an in-memory queue is development-only.
3. **MediaStorage** — create a private per-job workspace, store the finished media, stream or issue a short-lived authorized download, and delete expired objects. Production implementation must use private object storage; never expose public bucket URLs.
4. **AbuseControls** — enforce shared per-client and global limits, queue capacity, concurrency ceilings, and an operator kill switch. Limits must be shared across all API instances.

The HTTP layer should validate requests and call application services. The downloader worker should receive a job ID, load its job, download into a private workspace, finalize the MP4, store the result, and update job state. It should not know which database, queue, object store, or hosting vendor is in use.

## Required production request flow

1. Browser calls the API to analyze or create a download.
2. API validates the URL and applies shared abuse/queue limits.
3. API creates a job record and durably enqueues its ID. If enqueueing fails, the API must safely mark/reconcile the job rather than leave a phantom queued job.
4. A worker claims the job using a renewable lease and a bounded concurrency slot.
5. The worker downloads and converts media in an isolated, resource-limited environment.
6. The worker writes the completed file to private object storage and atomically marks the job ready.
7. Browser polls job status. The download endpoint authorizes access to that job and streams the object or issues a short-lived signed URL.
8. A scheduled cleanup process deletes expired objects and corresponding job records. Failed/abandoned jobs and partial workspaces also have retention limits.

Job IDs must remain high entropy and be treated as bearer capabilities unless a separate authorization design is introduced. Do not log job IDs, signed URLs, source URLs containing credentials, or provider tokens.

## Security requirements before public use

- Enforce outbound network policy outside the application process. yt-dlp follows redirects and may resolve hosts again; URL parsing and preflight DNS checks alone do not prevent redirect-based SSRF or DNS rebinding.
- Permit outbound connections only to approved public destinations and required media/CDN hosts, while blocking loopback, private, link-local, reserved, and cloud metadata ranges at connection time.
- Keep backend origin private where possible. Configure exact `DRIFT_ALLOWED_ORIGIN`; do not use wildcard CORS for production.
- Enable `DRIFT_TRUST_PROXY` only if the trusted proxy overwrites forwarded headers and direct backend access is blocked.
- Use TLS, secret management, least-privilege service identities, and private object storage.
- Apply hard CPU, memory, wall-clock, file-size, disk, egress, queue, and per-client limits at the hosting/infrastructure layer as well as in application code.
- Provide a global pause/kill switch for new jobs and worker execution.
- Define and test deletion/retention, backups, restore, incident response, and abuse reporting.
- Do not require visitor accounts; use unguessable job capabilities and strict access checks for status and file access.

## Provider-neutral migration sequence

### Phase A — safe refactor in this repository
- Extract current SQLite job operations behind a repository interface without changing API response shapes.
- Extract local filesystem operations behind a storage interface.
- Extract enqueue/claim operations behind a queue interface; keep the current single-process implementation explicitly marked development-only.
- Add contract tests that run against the local adapters.
- Preserve the current frontend and all public endpoints.
- Do not add a second API server or worker until the interfaces and lifecycle tests are in place.

### Phase B — production adapters (requires provider and budget approval)
- Select managed SQL, durable queue, private object storage, and a worker-capable host.
- Implement provider adapters and database migrations.
- Add atomic job claiming, lease renewal, retry policy, idempotency, and orphan reconciliation.
- Add signed-download authorization and scheduled object cleanup.
- Configure network egress restrictions and trusted proxy behavior.
- Add shared rate limits, global capacity controls, metrics, alerts, and spend ceilings.

### Phase C — verification before launch
- Test API/worker restarts, duplicate queue delivery, worker crashes, lease expiry, database outage, object-store outage, cancellation, cleanup, and restore.
- Run concurrency, load, storage, and bandwidth tests under the configured hard limits.
- Verify the frontend against the actual backend origin end-to-end.
- Run a private beta with monitoring and a documented rollback procedure.
- Only then request explicit approval for production deployment.

## Decisions intentionally left open

No backend provider, managed database, queue, object-storage vendor, paid plan, or budget has been selected. Do not create accounts, provision resources, enter payment details, or deploy production infrastructure without explicit approval. Free tiers may sleep, expire, impose bandwidth limits, or use ephemeral disks; they must not be assumed suitable for durable public downloads.

## Current launch status

**Not ready to deploy.** The current Vercel configuration builds the frontend only. A working download service requires the separate backend architecture above. The readiness checklist in `docs/public-deployment-readiness.md` remains the launch gate.
