# Drift backend implementation audit

**Reviewed:** 2026-11-14  
**Branch:** `public-deployment`  
**Scope:** Static code review of the current backend, queue lifecycle, storage integration, container configuration, and deployment architecture. No provider accounts, credentials, or production services were accessed.

## Executive result

The repository has a useful provider-neutral foundation, but the current runtime is still a single-process development/smoke-test backend. It is not ready for public production traffic. The most important remaining work is not frontend deployment; it is completing and verifying the durable backend adapters and their security boundaries.

## Findings

### 1. Retry lifecycle is not end-to-end

**Severity: High — correctness**

The initial worker failure-state bug has been corrected on the working branch: ordinary download exceptions now propagate to `LocalThreadPoolJobQueue`, which can move a leased job back to `queued` (or to terminal `failed` after the attempt limit). A regression test covers propagation from `run_download()`, and another covers the queue's retry transition.

**Still incomplete:** the local in-memory queue does not automatically redispatch a job when its `available_at` retry time arrives. The retry state transition is now testable, but automatic retry execution and restart-safe dispatch require a due-job dispatcher backed by durable shared state. Typed transient-versus-terminal error classification, duplicate delivery, exhausted attempts, and process restart still need coverage. Do not describe retries as operational until these tests pass.

### 2. Startup recovery fails queued work instead of resuming it

**Severity: High — availability**

The SQLite initialization and `recover_interrupted_jobs()` paths mark every queued, downloading, or processing job as failed after restart. This is acceptable only as an explicitly documented single-process development behavior. It does not satisfy durable queue recovery.

**Required resolution:** Production startup must recover expired leases and re-dispatch eligible queued jobs from shared persistent state. A worker restart must not silently discard queued jobs. Add crash/restart integration tests before launch.

### 3. Queue, rate limits, cancellation, and capacity are process-local

**Severity: Critical before horizontal scaling**

The active queue is a `ThreadPoolExecutor`; rate-limit maps, cancellation events, and emergency pause state are in memory or local environment configuration. Multiple API instances would not share these controls.

**Required resolution:** Use a shared PostgreSQL-backed job state/queue with atomic claim and lease operations, and a real shared atomic abuse-control store. Until then, run no more than one backend instance and do not claim distributed protection.

### 4. Private object storage is an interface, not an active service

**Severity: Critical before ephemeral hosting**

`PRIVATE_MEDIA_STORE` is currently `None`. The application therefore writes finished media to local disk. The private object-store contract and optional request path exist, but no real provider adapter, credentials, signed URL implementation, lifecycle policy, or integration test is configured.

**Required resolution:** Implement and test a provider adapter against a private bucket, including upload consistency, authorization, signed URL expiry, byte ranges, deletion, retention, upload failure, and orphan cleanup. Do not host downloads on an ephemeral filesystem.

### 5. Outbound network filtering remains an infrastructure blocker

**Severity: Critical security**

Application URL validation cannot enforce the destination of every connection made by yt-dlp after redirects, extractor requests, DNS changes, or CDN hops. The repository correctly documents this limitation; no infrastructure-level egress filter is active in the current code.

**Required resolution:** Deploy the worker behind a deny-by-default egress control that validates destinations at connection time, blocks private/reserved/link-local/metadata ranges, prevents direct bypass, and is tested against redirects and DNS rebinding. A port-only outbound rule is insufficient.

### 6. Current container configuration is a single-instance baseline

**Severity: High — deployment architecture**

The Compose file binds one backend to localhost, uses a persistent local volume, and applies resource limits and container hardening. It is useful for a controlled single-host test, but it does not supply managed database durability, a separate worker, private object storage, shared abuse controls, or network egress enforcement.

**Required resolution:** Treat Compose as a local/single-instance baseline only. Production topology must separate API and worker responsibilities where appropriate and connect the approved durable services.

## Safe implementation order

1. Correct and test worker failure classification and retry state transitions.
2. Complete a PostgreSQL repository and transactional queue implementation with migrations and concurrency tests.
3. Add a durable worker dispatcher, lease heartbeat, crash recovery, idempotency, and orphan reconciliation.
4. Implement a real private object-store adapter and retention/authorization tests.
5. Implement shared atomic abuse controls, global pause, quotas, and usage accounting.
6. Enforce infrastructure-level egress restrictions and prove redirect/DNS-rebinding resistance.
7. Add integration, restart, failure-injection, load, cost-ceiling, monitoring, and restore tests.
8. Run a private beta and verify the deployed frontend against the real backend.

## Approval and safety boundary

This audit is repository preparation only. It does not authorize provider account creation, service connections, credentials, purchases, subscriptions, payment entry, or production deployment. Those actions require separate explicit approval and an agreed maximum spend. The frontend, free downloads, and no-required-login requirement remain unchanged.

## Launch decision

**Not ready for production deployment.** Passing unit tests and building the frontend/container are useful quality gates, but they do not prove durable jobs, safe media fetching, persistent files, shared abuse controls, or production recovery.
