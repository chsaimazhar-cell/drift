# Drift backend implementation audit

**Reviewed:** 2026-11-14  
**Branch:** `public-deployment`  
**Scope:** Static code review of backend lifecycle, queue recovery, storage integration, container configuration, and deployment architecture. No provider accounts, credentials, or production services were accessed.

## Executive result

The repository has a useful provider-neutral foundation, but the current runtime remains a single-process development/smoke-test backend. It is not ready for public production traffic. The most important remaining work is completing and verifying durable backend adapters and their security boundaries.

## Findings

### 1. Local retry dispatch and restart recovery

**Severity: High — correctness and availability**

The worker failure-state bug has been corrected: ordinary download exceptions propagate to `LocalThreadPoolJobQueue`, which schedules a retry or marks the job failed after the attempt limit. A local dispatcher polls SQLite for due queued jobs and submits them while the process is running.

Startup recovery now preserves already-queued jobs, including their existing delayed `available_at` schedule, and clears stale lease metadata. Jobs interrupted while downloading or processing have partial workspace output removed and are requeued immediately when attempts remain. Jobs that have exhausted the retry limit become terminal failures. Recovery runs before the local dispatcher starts.

CI on commit `08883937` passed the Python unit tests, frontend build, JavaScript syntax check, Compose validation, and backend container build.

**Still incomplete:** this is a single-process SQLite-backed development queue. It cannot coordinate multiple API/worker instances, guarantee recovery after ephemeral disk loss or host replacement, or provide a production-grade distributed lease. Typed transient-versus-terminal error classification, shared transactional claims, lease renewal, idempotency, and real database crash/concurrency integration tests remain required.

### 2. Queue, rate limits, cancellation, and capacity are process-local

**Severity: Critical before horizontal scaling**

The active queue uses a local `ThreadPoolExecutor`; rate-limit maps, cancellation events, and emergency pause state are in memory or local environment configuration. Multiple API instances do not share these controls.

**Required resolution:** use a shared PostgreSQL-backed job repository with atomic claim and lease operations, durable queue state, and a real shared atomic abuse-control store. Until then, run no more than one backend instance and do not claim distributed protection.

### 3. Private object storage is an interface, not an active service

**Severity: Critical before ephemeral hosting**

`PRIVATE_MEDIA_STORE` is currently `None`. The application therefore writes finished media to local disk. The private object-store contract and optional request path exist, but no real provider adapter, credentials, signed URL implementation, lifecycle policy, or provider integration test is configured.

**Required resolution:** implement and test a provider adapter against a private bucket, including upload consistency, authorization, signed URL expiry, byte ranges, deletion, retention, upload failure, and orphan cleanup. Do not host downloads on an ephemeral filesystem.

### 4. Outbound network filtering remains an infrastructure blocker

**Severity: Critical security**

Application URL validation cannot enforce the destination of every connection made by yt-dlp after redirects, extractor requests, DNS changes, or CDN hops. No infrastructure-level egress filter is active in the current code.

**Required resolution:** deploy the worker behind deny-by-default egress controls that validate destinations at connection time, block private/reserved/link-local/metadata ranges, prevent direct bypass, and are tested against redirects and DNS rebinding. A port-only outbound rule is insufficient.

### 5. Current container configuration is a single-instance baseline

**Severity: High — deployment architecture**

The Compose file binds one backend to localhost, uses a persistent local volume, and applies resource limits and container hardening. It is useful for a controlled single-host test, but it does not supply managed database durability, a separate worker, private object storage, shared abuse controls, or network egress enforcement.

**Required resolution:** treat Compose as a local/single-instance baseline only. Production topology must separate API and worker responsibilities where appropriate and connect approved durable services.

## Safe implementation order

1. Complete PostgreSQL repository and transactional queue implementation with migrations and concurrency tests.
2. Add a durable worker dispatcher, lease heartbeat, crash recovery, idempotency, and orphan reconciliation.
3. Implement a real private object-store adapter and retention/authorization tests.
4. Implement shared atomic abuse controls, global pause, quotas, and usage accounting.
5. Enforce infrastructure-level egress restrictions and prove redirect/DNS-rebinding resistance.
6. Add integration, restart, failure-injection, load, cost-ceiling, monitoring, and restore tests.
7. Run a private beta and verify the deployed frontend against the real backend.

## Approval and safety boundary

This audit is repository preparation only. It does not authorize provider account creation, service connections, credentials, purchases, subscriptions, payment entry, or production deployment. Those actions require separate explicit approval and an agreed maximum spend. The frontend, free downloads, and no-required-login requirement remain unchanged.

## Launch decision

**Not ready for production deployment.** Passing unit tests and building the frontend/container are useful quality gates, but they do not prove durable jobs, safe media fetching, persistent files, shared abuse controls, or production recovery.
