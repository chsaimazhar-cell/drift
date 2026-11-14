# Drift backend provider research

**Research date:** 2026-11-14  
**Status:** Research and preparation only. No provider selected, account connected, service created, payment method entered, or deployment performed.  
**Branch:** `public-deployment`

## Decision summary

Do not launch Drift's downloader on an all-free stack. Free tiers are useful for adapter development and integration tests, but the currently documented limits introduce unacceptable reliability risks for public downloads.

Keep the existing Vercel static frontend separate. Keep the backend portable behind the repository, queue, media-storage, and abuse-control interfaces in [backend-architecture.md](backend-architecture.md).

## Provider findings

### Render — candidate Python API/worker host; not approved or selected

- Render supports Python services, Docker containers, and continuously running background workers.
- Its free web services sleep after 15 minutes without inbound traffic, may restart, and have ephemeral filesystems. Free services cannot attach persistent disks.
- Free Render Postgres expires after 30 days and has no backups. Free Key Value is in-memory and loses data on restart.
- Render's pricing page lists paid compute beginning at $7/month for a small 512 MB instance; the listed 2 GB / 1 CPU tier is $25/month. A separate API and worker at that size would therefore be roughly $50/month before database, storage, bandwidth, and taxes.
- Persistent disks are paid, single-instance attachments; they prevent scaling that service to multiple instances. Prefer object storage for completed media rather than relying on a local disk.
- Render documents background workers as continuously running queue consumers, suitable for media processing.

Sources:
- https://render.com/pricing
- https://render.com/docs/free
- https://render.com/docs/background-workers
- https://render.com/docs/disks

Assessment: useful operationally simple candidate for a controlled pilot, but not a zero-cost production host. Before choosing it, verify the actual memory requirement of yt-dlp/FFmpeg and design a defensible outbound network boundary. Do not assume the platform provides an outbound destination allowlist.

### Neon — candidate managed PostgreSQL

- Current Free plan documentation reports 1 GB storage per project and 100 compute-hours per project per month, with compute scaling to zero after inactivity.
- PostgreSQL can hold both job records and a durable queue using transactional row claiming (for example, `FOR UPDATE SKIP LOCKED`), leases, retries, and recovery. This avoids making an ephemeral Redis free tier the source of truth.
- Free-tier quotas and scale-to-zero behavior make it suitable for development or a low-volume trial, not an availability guarantee.

Sources:
- https://neon.com/docs/introduction/pro-plan
- https://neon.com/blog/neon-free-plan-1-gb-per-project

Assessment: preferred database candidate for prototyping the PostgreSQL repository and durable queue adapter. Re-check current plan limits and backup/restore guarantees before any production decision.

### Cloudflare R2 — candidate private media object storage

- Standard storage includes 10 GB-month, 1 million Class A operations, and 10 million Class B operations per month on the free tier.
- Internet egress is listed as free.
- Buckets must remain private. Downloads should use short-lived signed URLs or an authenticated API stream; never publish a public bucket URL.
- Add object lifecycle deletion, per-job size ceilings, storage quotas, and a global pause control. Free allowance is a quota, not a hard spending cap.

Source:
- https://developers.cloudflare.com/r2/pricing/

Assessment: strong candidate for completed media objects because egress is free and the S3-compatible interface is portable. Validate signed URL behavior, lifecycle rules, and account billing controls before selecting it.

### Upstash Redis — do not use as the only durable queue

- The current free Redis tier lists 256 MB, 10 GB monthly bandwidth, and 500,000 commands.
- The pricing comparison does not list persistence for the free tier; the production pack adds durability/availability features at substantial additional cost.
- A queue whose entries can disappear on restart is not acceptable as Drift's sole job ledger.

Source:
- https://upstash.com/pricing/redis

Assessment: optional cache or non-authoritative coordination only. Prefer PostgreSQL-backed durable job state initially.

### Fly.io — alternative Docker host to evaluate

- Fly Machines are billed by the second; the published pricing page lists approximately $8.78/month for a continuously running shared-CPU 1 GB machine and $17.55/month for 2 GB, before storage and network transfer.
- It is not a free production option.
- The team would need to operate and secure the worker/API deployment and separately choose managed database and object storage.
- Egress filtering, persistent volumes, backup strategy, and regional data transfer costs require further verification before recommendation.

Source:
- https://fly.io/pricing/

Assessment: potentially lower compute cost than two similarly sized Render services, but higher operational responsibility. Not selected.

## Provisional architecture to prototype (not a provider commitment)

1. **Frontend:** retain the existing static Vercel site and configurable API base URL.
2. **API and worker:** Dockerized Python service(s) on a host that can run a continuously available worker with adequate memory, CPU, timeouts, and a controllable network boundary.
3. **Database and durable queue:** PostgreSQL. Implement atomic job claims, leases, retry/backoff, idempotency, and orphan reconciliation in the database adapter. The database remains the source of truth.
4. **Media:** private S3-compatible object storage, with short-lived authorized downloads and automatic expiry.
5. **Abuse controls:** shared database-backed/global limits, per-client quotas, maximum queue depth, maximum active jobs, file-size and duration ceilings, strict retention, and an operational kill switch.
6. **Network security:** downloader traffic must pass through an enforceable outbound policy that blocks loopback, private, link-local, reserved, and cloud-metadata IP ranges at connection time, including after redirects and DNS changes. Application URL validation alone is insufficient.

## Cost and reliability guardrails

- Do not assume a provider's billing alert is a hard spending cap.
- Do not enable automatic scale-out or uncapped retries.
- Start with one worker and one active conversion; use strict queue limits.
- Cap download size, media duration, wall-clock time, temporary storage, retained objects, and outbound traffic.
- Alert before free quotas are exhausted; disable new jobs automatically or operationally when thresholds are reached.
- Keep object retention short and verify deletion.
- Calculate an explicit monthly maximum before selecting paid compute or enabling billing.
- Do not collect visitor registration data; use high-entropy job capabilities and strict per-job access.

## Remaining research and engineering gates

- [ ] Verify provider network egress controls or design a separately enforceable egress proxy.
- [ ] Benchmark yt-dlp and FFmpeg memory/CPU needs with representative media.
- [ ] Confirm region availability, latency from Pakistan and major visitor regions, and data-transfer pricing.
- [ ] Confirm PostgreSQL backup, restore, connection pooling, and quota behavior.
- [ ] Confirm object-storage lifecycle deletion, signed URL expiry, and abuse/cost controls.
- [ ] Implement PostgreSQL migrations and migration tests.
- [ ] Implement durable queue claim/lease/ack/retry/recovery contract and tests.
- [ ] Implement object-storage adapter and contract tests.
- [ ] Implement shared abuse controls, kill switch, and usage accounting.
- [ ] Run failure, restart, duplicate-job, load, security, and cost-ceiling tests.
- [ ] Obtain a separate explicit approval before creating/connecting any external service, entering billing details, provisioning paid resources, or deploying production.

## Approval boundary

This document records research only. It does not authorize account creation, service connection, billing activation, purchasing, subscriptions, payment entry, domain registration, or production deployment. Those actions require separate explicit approval from the user.
