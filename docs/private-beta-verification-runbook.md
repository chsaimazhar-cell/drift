# Drift private-beta verification runbook

**Status:** Test plan only. No backend provider or external service is connected.
**Branch:** `public-deployment`
**Release rule:** Do not open public access until every required test has evidence and all launch blockers are closed.

## 1. Preconditions

- [ ] A backend host and region have been explicitly selected and approved.
- [ ] The API and worker run separately; the worker is not an in-process thread pool.
- [ ] A shared managed database is configured, migrated, backed up, and restore-tested.
- [ ] A durable queue supports atomic claims, leases, bounded retries, and recovery.
- [ ] Completed media uses private object storage with signed, short-lived authorization.
- [ ] Shared quotas, global capacity limits, and the emergency pause control are active.
- [ ] A trusted reverse proxy overwrites client-IP headers; direct API access is blocked.
- [ ] The outbound network policy is enforced outside the application and passes the tests in [outbound-network-security-policy.md](outbound-network-security-policy.md).
- [ ] CPU, RAM, worker time, file size, duration, storage, bandwidth, and spending ceilings are enforced.
- [ ] Secrets are stored in the host's secret manager, never in Git or frontend build output.
- [ ] The frontend has the exact backend API origin configured and strict CORS is enabled.

## 2. Functional smoke tests

Use a small, known public test asset that the owner is authorized to download. Do not use private, restricted, or copyrighted material without permission.

| Test | Expected result |
|---|---|
| Open frontend | Original Drift interface loads without redesign |
| Analyze supported URL | Metadata and available qualities display |
| Submit download | One job is created and a status is returned |
| Poll job | Status progresses and reaches ready or a safe failure |
| Download ready file | Correct MP4 is delivered |
| Cancel active job | Job stops and partial files are removed |
| Expired job | File access is denied and stored object is deleted |
| Unsupported URL | Request is rejected without contacting destination |
| Invalid JSON / oversized request | Safe 4xx response; no worker is spawned |

## 3. Queue, database, and worker failure tests

- [ ] Two API instances observe the same job state and capacity limits.
- [ ] Two workers cannot claim the same job concurrently.
- [ ] Duplicate queue delivery does not create duplicate output or double-charge quota.
- [ ] Worker termination during download releases or expires its lease and follows bounded retry policy.
- [ ] Worker termination after upload but before job publication is reconciled without leaving a permanent orphan.
- [ ] Database outage fails closed; no phantom queued job is reported as accepted.
- [ ] Queue outage fails closed and does not leave a job permanently queued.
- [ ] Database migration is tested against a production-like copy and has a rollback/restore procedure.
- [ ] Backup restore recovers job metadata and does not expose deleted or expired media.

## 4. Storage and download authorization tests

- [ ] Object bucket/container is private and has no public listing or public-read policy.
- [ ] Signed download authorization is short-lived and scoped to one object.
- [ ] A user cannot change an object key to read another user's file.
- [ ] Range requests return correct byte ranges and content headers.
- [ ] Expired, deleted, failed, and cancelled jobs cannot retrieve media.
- [ ] Object deletion and lifecycle expiry are verified in the actual provider.
- [ ] Failed publication cleans up uploaded orphan objects.
- [ ] Storage quotas account for source, conversion, and completed artifacts.

## 5. Abuse, security, and cost tests

- [ ] Per-client analysis and download quotas work across API instances.
- [ ] Global active-job and queue ceilings cannot be exceeded by concurrent requests.
- [ ] The shared emergency pause rejects new work on every API instance.
- [ ] Existing job status and file access behave according to the documented pause policy.
- [ ] Spoofed forwarding headers cannot change the effective client identity.
- [ ] URL redirects and DNS rebinding are blocked by the infrastructure egress policy.
- [ ] IPv4 and IPv6 private, loopback, link-local, reserved, and metadata destinations are blocked.
- [ ] Request floods do not exhaust memory, threads, database connections, or storage.
- [ ] CPU, RAM, duration, file-size, storage, and egress limits are observed under load.
- [ ] A simulated quota threshold triggers alerting and the documented pause action.
- [ ] A monthly maximum cost is calculated from the actual configured resources; billing alerts are not treated as hard caps.

## 6. Private-beta procedure

1. Deploy only to a non-public test environment after separate owner approval.
2. Restrict access to a small tester group; do not advertise or index the site.
3. Run the smoke tests and retain sanitized results.
4. Run queue, restart, security, storage, and cost tests.
5. Monitor error rate, queue age, worker restarts, CPU/RAM, storage, egress, and spend.
6. Keep a tested pause procedure and rollback target ready.
7. Fix all critical/high-severity failures and rerun affected tests.
8. Present the evidence and remaining risks to the owner.
9. Request a separate explicit approval before public production deployment.

## 7. Stop conditions

Immediately pause new jobs and do not widen access if any of these occurs:

- An outbound request reaches a non-public or metadata destination.
- A job or file can be accessed without its valid capability.
- The same job is processed concurrently or quota can be bypassed.
- A private object becomes publicly readable.
- Storage deletion, queue recovery, or database restore fails.
- Resource or spending ceilings cannot be enforced.
- Logs contain signed URLs, secrets, source URL credentials, or job capabilities.
- Monitoring cannot detect worker failure, queue growth, storage pressure, or spend anomalies.

## 8. Owner approval boundaries

Research, local code, tests, and documentation do not authorize external account creation, service connection, payment entry, paid provisioning, domain purchase, or production deployment. Each of those requires separate explicit approval. Preserve the original interface, free downloads, no mandatory visitor registration, no domain purchase now, and no advertisements until stability is demonstrated.
