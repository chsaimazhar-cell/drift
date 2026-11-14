# PostgreSQL API Migration Plan

## Status

Preparation only. The HTTP API still runs on the SQLite repository and the local in-process queue. This document records the verified integration surface and the safe migration order; it does not enable PostgreSQL or connect to an external service.

## Current runtime boundary

The current API runtime is initialized in `drift.py`:

- `JOB_REPOSITORY` is constructed as `SQLiteJobRepository()` at module import.
- `JOB_QUEUE` is a `LocalThreadPoolJobQueue` backed by a process-local `ThreadPoolExecutor`.
- `main()` calls `init_db()`, recovers interrupted jobs, starts the local dispatcher and cleanup loop, then starts `LimitedThreadingHTTPServer`.
- `DB_PATH` and `DOWNLOAD_ROOT` point to local filesystem storage.
- `CANCEL_EVENTS`, `RATE_WINDOWS`, and the local queue are process-local state.

The PostgreSQL repository and worker modules are currently integration building blocks, not active API runtime adapters.

## HTTP route audit

| Route | Current repository / runtime dependency | Required production behavior |
|---|---|---|
| `GET /api/health` | Reads `JOB_REPOSITORY.active_count()` and local dependency state | Read shared repository health and active count; expose readiness separately from liveness |
| `GET /api/downloads/{id}` | `get_job()` via repository context | Read from the same shared database used by API and workers |
| `GET /api/downloads/{id}/file` | `get_job()`, then local file or optional private media store | Stream private object ranges or issue a short-lived authorized download URL |
| `POST /api/analyze` | No job write; process-local rate limiting and outbound media inspection | Shared abuse controls and hardened outbound request boundary |
| `POST /api/downloads` | `insert_job_if_capacity()`, then `JOB_QUEUE.enqueue()`; creates process-local cancellation event | Atomic shared capacity admission and durable queue dispatch; do not return success until enqueue/outbox state is durable |
| `POST /api/downloads/{id}/cancel` | Reads job, sets process-local `CANCEL_EVENTS`, then calls global `JOB_REPOSITORY.cancel_active()` | Atomically cancel in shared repository; worker heartbeat observes lease loss/cancellation and stops media work |

## Verified integration gaps

1. Repository context currently covers downloader reads and updates only. The API creation helpers `insert_job()` and `insert_job_if_capacity()` write directly to the global repository.
2. Health, cancellation, and queue operations also use global objects directly.
3. `create_download()` enqueues through the local thread-pool queue. A PostgreSQL worker cannot discover work submitted only to that queue.
4. Cancellation currently sets `CANCEL_EVENTS`, a process-local event. A separate worker process cannot observe that event. The HTTP handler also calls the global repository rather than the active repository abstraction.
5. The worker's lease heartbeat detects lease loss, but the API must atomically revoke the active lease in the shared database and the worker processor must cooperate with the resulting signal.
6. The current downloader's default media store is local disk. A separate worker's local filesystem is not necessarily available to the API process.
7. Startup currently initializes and recovers SQLite state and starts the local dispatcher unconditionally. Production startup must select one explicit runtime mode and must not accidentally start the SQLite dispatcher alongside a shared worker.
8. Local rate windows, emergency pause configuration, and local capacity limits are not global across replicas.

## Safe implementation sequence

### Phase A — Repository contract and runtime selection

- Define and test one repository protocol for every API and worker operation: initialize/check schema, get, list, insert, atomic insert-if-capacity, active count, cancel, update, mark-ready, recovery, and worker lease operations.
- Inject the selected repository into the HTTP handler/runtime instead of relying on module-global SQLite calls.
- Preserve SQLite as the default local-development adapter.
- Add explicit configuration validation for runtime mode. A PostgreSQL mode must fail closed if its driver, connection configuration, or migration version is missing; it must never silently fall back to SQLite.
- Keep schema migration an explicit operator action; application startup must not auto-migrate production data.

### Phase B — Durable queue and cancellation

- Select a durable queue strategy. The initial PostgreSQL queue may use the existing atomic claim/lease table only if enqueue, capacity admission, claim, retry, recovery, and cancellation are all backed by the same database.
- Make job insertion and queue visibility one atomic transaction (or use a transactional outbox).
- Make cancellation a shared state transition that clears/revokes the lease and is idempotent.
- Ensure the processor checks cancellation/lease loss during extraction, download, conversion, and artifact publication.
- Do not acknowledge or retry a job after lease ownership has been lost.

### Phase C — Private artifact storage

- Implement and test a private object-store adapter.
- Store only opaque object keys and verified metadata in the job row.
- Keep buckets private; issue short-lived signed URLs only after checking job ownership/status and expiry, or stream authenticated byte ranges through the API.
- Add cleanup for expired objects and database metadata, including failure/retry cleanup.
- Never expose local filesystem paths or permanent public object URLs to visitors.

### Phase D — Shared safeguards and network security

- Implement atomic, shared rate limits, capacity quotas, usage accounting, an operational pause, and bounded retention.
- Add outbound egress enforcement for media extraction, including DNS rebinding, redirect revalidation, private/reserved IP blocking, and connection-time destination checks.
- Ensure proxy-derived client IPs are trusted only behind a proxy that overwrites untrusted forwarding headers.
- Keep visitor registration optional; safeguards must not require visitor accounts.

### Phase E — Verification and controlled rollout

- Add API integration tests using PostgreSQL for every route and repository operation.
- Test concurrent capacity admission, duplicate cancellation, cancellation during each download phase, worker crash/restart, lease expiry, duplicate delivery, and storage failure.
- Run load, restore/backup, retention, SSRF, and cost-ceiling tests.
- Complete a private beta with monitoring and a tested emergency stop before public traffic.
- Obtain separate explicit approval before connecting any paid/external service or deploying production.

## Acceptance gate

Do not switch the API to PostgreSQL or declare production readiness until all phases A–D have passing automated tests and the phase E operational checks are documented. No service purchase, account creation, payment entry, external connection, or production deployment is authorized by this preparation work.
