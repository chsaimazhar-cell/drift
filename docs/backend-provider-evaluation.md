# Drift backend provider evaluation

**Status:** research and preparation only — no provider has been connected or provisioned.
**Reviewed:** 2026-11-14
**Scope:** public-deployment branch only. No production deployment, account changes, purchases, subscriptions, or payment details.

## Executive decision

There is no verified, reliable, always-on, end-to-end free hosting combination for Drift's public downloader workload. Free tiers are useful for development and limited previews, but their sleep, ephemeral storage, short retention, compute quotas, and lack of worker capacity make them unsuitable for promising reliable public downloads.

Keep the frontend on Vercel if desired. Treat the Python API and media worker as a separate service. Do not select or connect a production provider until the egress-security design, total cost ceiling, and failure/recovery tests are approved.

## Requirements that drive provider choice

- Run the existing Python API and a separate long-running yt-dlp/FFmpeg worker.
- Persist job state in shared PostgreSQL; do not depend on process-local SQLite or in-memory queues.
- Store generated media in private object storage, not an ephemeral application filesystem.
- Enforce a real outbound network policy for media fetching. Application URL validation alone is not enough to stop redirects, DNS rebinding, or access to internal/private addresses.
- Bound CPU, memory, job duration, download size, storage, queue depth, egress, retries, and concurrent jobs.
- Support cleanup, retention, signed/authorized downloads, restart recovery, monitoring, and a global kill switch.
- Preserve the current frontend and public API contract. Visitors must not need accounts and downloads remain free.

## Provider comparison

| Component | Candidate | Current free allowance / entry cost | Fit and limitations |
|---|---|---|---|
| Python API + worker | Fly.io Machines | Shared CPU 256 MB listed at about $2.19/month; 2 GB shared CPU listed at about $17.55/month, before storage and network | Strong candidate for a small always-on container and separate API/worker Machines. Fly network policies support ingress/egress rules and default-deny for a direction once rules are configured. However, documented rules are port/protocol based, not hostname allowlists; a controlled outbound proxy or additional enforcement is still required. |
| Python API + worker | Render | Free web service exists; paid compute starts at $7/month for less than 1 CPU / 512 MB; 1 CPU / 2 GB listed at $25/month | Easy Python/Docker deployment and explicit background-worker service type, but background workers are not free. Free web services sleep, lose local files, and free Postgres expires after 30 days. Render's documented outbound IP feature is for external allowlisting, not a destination egress firewall. |
| PostgreSQL | Neon | Free: 100 CU-hours/project/month, 1 GB storage/project, 5 GB public transfer/project; scales to zero after 5 minutes | Good development/integration candidate. Free compute can suspend after quotas; not a durable always-on production database. Paid Launch is usage-based with no monthly minimum, but costs require budget controls and approval. |
| PostgreSQL | Supabase | Free: 500 MB database/project; pauses after one week of inactivity; no automatic backups on Free | Simple managed Postgres alternative for development. Free tier is not a production durability guarantee. Paid Pro currently starts at $25/month before extra compute; spend cap is enabled by default on Pro. |
| Private object storage | Cloudflare R2 | Free: 10 GB-month Standard storage, 1 million Class A and 10 million Class B operations/month; internet egress free | Strong candidate for temporary output files because download egress is a major cost risk. Must still implement private buckets, short-lived authorization, lifecycle deletion, per-job size limits, and usage alarms. |
| Durable queue | PostgreSQL job table (preferred first) | Included in selected managed Postgres usage | Avoids an extra queue vendor and keeps job lifecycle transactional. Requires atomic claim, lease expiry, ack, retry/backoff, dead-letter state, and recovery tests before separate workers are enabled. |
| Managed queue (alternative) | Cloudflare Queues | Workers Free includes 10,000 operations/day; Free retention is 24 hours | Interesting low-cost alternative, but retention and consumer runtime constraints need validation. It does not replace the Python media worker. |
| Managed queue (alternative) | Upstash QStash | Free: 1,000 messages/day, 1 MB max message, 15-minute HTTP response duration | Useful for webhook-style dispatch, but delivery attempts/retries count as messages and the free limits are not a production guarantee. Avoid placing large media payloads in queue messages. |

## Recommended direction (not yet selected or connected)

1. **Frontend:** keep the existing Vercel static frontend and runtime API-base configuration.
2. **Backend compute candidate:** evaluate Fly.io Machines first because the documented network-policy feature is relevant to Drift's egress threat model. Render remains a simpler operational alternative, but its documented outbound IPs do not solve outbound destination filtering.
3. **Database:** use PostgreSQL as the durable source of truth. Start with a provider-neutral repository contract and a PostgreSQL implementation. Neon is a development candidate; production plan must have adequate availability, backups, and budget controls.
4. **Queue:** initially implement a PostgreSQL-backed durable queue (atomic claim + lease + retry + ack) instead of committing to Redis/QStash. This reduces moving parts and vendor lock-in.
5. **Files:** evaluate Cloudflare R2 private object storage for generated files. The API issues narrowly scoped, short-lived download authorization; lifecycle rules delete expired outputs.
6. **Egress:** before public use, enforce deny-by-default outbound networking at the infrastructure layer. A port-443-only rule is insufficient. Use a controlled egress proxy/firewall with destination and resolved-IP validation, block loopback/private/link-local/reserved ranges, revalidate redirects, and prevent direct bypass from both API and worker. Test DNS rebinding and redirect attacks from the deployed environment.
7. **Cost protection:** start with conservative global concurrency and daily quotas, hard maximum download/converted size, hard job timeouts, queue caps, object lifecycle TTL, per-IP limits, provider usage alerts, and a kill switch. Prefer no payment method / hard suspension where feasible until a separate spend approval is made.

## Important caveats

- Fly network policies apply to traffic directly to/from Machines and do not affect traffic routed through Fly Proxy. Policies are based on ports and protocols; do not treat them as hostname-level SSRF protection.
- Render Free web services sleep after 15 minutes without inbound traffic and use ephemeral filesystems. Free Render Postgres expires 30 days after creation and has no backups.
- Neon Free compute scales to zero after 5 minutes and suspends when monthly compute or transfer allowances are exhausted.
- R2's free allowance is not a blanket guarantee of zero cost if storage or operations exceed it. Keep retention and maximum output size enforced by Drift.
- Provider prices and free allowances can change. Recheck official pricing immediately before any account setup or deployment.
- Pakistan region availability, latency, payment-method requirements, tax, and account eligibility have not been verified. Do not assume a provider will accept a particular payment method or provide a nearby region.

## Decision gates before implementation or deployment

- [ ] User approves a specific provider combination and a maximum monthly spend in PKR and USD.
- [ ] Confirm regions, data residency, account eligibility, and payment requirements without entering payment details.
- [ ] Prove egress enforcement against redirects, DNS rebinding, private IPs, metadata endpoints, and proxy bypass.
- [ ] Finish PostgreSQL migrations and repository contract tests.
- [ ] Finish durable queue claim/lease/ack/retry/recovery and worker crash tests.
- [ ] Finish object storage contract tests, signed-download authorization, TTL deletion, and orphan cleanup.
- [ ] Add global rate limits, usage accounting, hard resource ceilings, alerts, and emergency shutdown.
- [ ] Run integration, load, restart, failover, abuse, and cost-ceiling tests.
- [ ] Only after a separate explicit approval: connect accounts/services, provision paid resources, or deploy production.

## Official references

- Fly.io pricing: https://fly.io/pricing/
- Fly.io network policies: https://fly.io/docs/machines/guides-examples/network-policies/
- Render free limits: https://render.com/docs/free
- Render pricing: https://render.com/pricing
- Render outbound IPs: https://render.com/docs/outbound-ip-addresses
- Neon free limits: https://neon.com/docs/introduction/free-tier
- Neon plans: https://neon.com/docs/introduction/plans
- Supabase pricing: https://supabase.com/pricing
- Cloudflare R2 pricing: https://developers.cloudflare.com/r2/pricing/
- Cloudflare Queues pricing: https://developers.cloudflare.com/queues/platform/pricing/
- Upstash QStash pricing: https://upstash.com/pricing/qstash
