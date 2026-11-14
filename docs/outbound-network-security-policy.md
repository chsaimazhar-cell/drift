# Drift outbound network security policy

**Status:** Required infrastructure design; not enforced by the current application or Docker Compose file.
**Branch:** `public-deployment`
**Launch gate:** Must be implemented and verified by the selected backend host before any public downloader traffic is accepted.

## Why this control is mandatory

Drift accepts a URL and passes it to yt-dlp. A preflight DNS lookup in Python is not a connection-time security boundary. yt-dlp may follow redirects, resolve a hostname again, or request media/CDN hosts discovered from metadata. A hostname that passed validation can later resolve to a private address. Therefore, URL validation alone cannot prevent server-side request forgery (SSRF), DNS rebinding, or access to cloud metadata services.

The current code has useful defense-in-depth checks, but **the infrastructure egress policy described here is not implemented**. Do not expose the current downloader as an unrestricted public service.

## Required network topology

Use a dedicated outbound enforcement point between the downloader worker and the public internet:

```text
Browser
  |
  v
HTTPS reverse proxy / API
  |
  +---- private database
  |
  +---- durable queue
  |
  v
Isolated downloader worker
  |
  | only permitted outbound route
  v
Egress firewall / filtering proxy
  |
  v
Approved public media hosts and CDNs
```

The worker must not have a second route to the internet that bypasses the enforcement point. The API process should not need arbitrary outbound internet access. Database, queue, object storage, DNS, and observability destinations should be explicitly allowed as separate service dependencies.

## Mandatory deny policy

At connection time, deny IPv4 and IPv6 destinations in all non-public, special-purpose, or infrastructure ranges, including:

- Loopback.
- RFC 1918 private IPv4.
- IPv4 and IPv6 link-local.
- IPv6 unique-local.
- Carrier-grade NAT.
- Multicast, unspecified, reserved, benchmarking, documentation, and other non-global ranges.
- Cloud-provider metadata endpoints and platform control-plane addresses.
- Any destination that is not globally routable according to the maintained IANA special-purpose registries.

Apply the policy after DNS resolution and for every new connection, not only to the initial URL. Re-check every redirect and every hostname discovered by the media extractor. Handle IPv4-mapped IPv6 addresses consistently. Deny direct UDP and QUIC unless explicitly required and controlled; do not let a client bypass the proxy with alternate protocols.

A simple hostname allowlist is not sufficient by itself: media platforms use changing CDN hostnames. Combine maintained destination policy, connection-time IP classification, DNS-rebinding resistance, and explicit service egress controls. Do not silently allow a destination merely because yt-dlp requested it.

## DNS and proxy requirements

- Use a controlled resolver for worker traffic.
- Prevent the worker from reaching arbitrary DNS resolvers directly.
- Resolve names through the enforcement layer or pin the validated address for the actual connection while preserving correct TLS SNI and hostname verification.
- Revalidate each redirect and each new connection.
- Reject mixed DNS answers if any answer is non-global.
- Prevent DNS rebinding between validation and connection.
- Ensure proxy configuration cannot be overridden by a submitted URL or downloader metadata.
- Do not log full source URLs, signed URLs, authorization headers, or job capability IDs.

## Worker isolation and resource ceilings

The selected host must enforce controls outside Python as well as inside the application:

- No inbound public traffic to the worker.
- No privileged container, no host networking, no Docker socket, no added Linux capabilities.
- Read-only root filesystem; bounded writable scratch and data mounts only.
- CPU and memory ceilings.
- Maximum wall-clock duration and process count.
- Maximum temporary disk use and download size.
- Maximum outbound bandwidth and monthly egress budget.
- No automatic unbounded scaling or retries.
- One worker and one active conversion for the initial private beta unless measured capacity and cost justify a higher ceiling.

The repository's hardened Compose settings are useful local defaults, but they do not create an egress firewall. Compose `read_only`, `cap_drop`, `no-new-privileges`, and resource limits must not be described as SSRF protection.

## Required verification before selecting a host

Obtain provider documentation or run a controlled proof that demonstrates all of the following:

1. The worker cannot connect to loopback, private, link-local, reserved, or metadata IP ranges over IPv4 or IPv6.
2. A public hostname that redirects to a private address is blocked.
3. A hostname whose DNS answer changes from public to private is blocked at connection time.
4. A hostname returning both public and private answers is rejected.
5. Redirect chains cannot escape the policy.
6. Direct DNS, UDP, QUIC, alternate ports, and proxy bypass attempts are blocked.
7. Required media/CDN hosts can still be fetched without weakening the deny policy.
8. Database, queue, object storage, and monitoring work only through their explicitly permitted destinations.
9. Egress counters and alerts are available, and the worker can be paused immediately.
10. A failed policy test prevents release.

Use a test-only controlled DNS zone and HTTP redirect server for these tests. Never probe cloud metadata endpoints on a real production account as a test.

## Release evidence to retain

Before private beta, record:

- Provider and region.
- Network topology and firewall/proxy ruleset version.
- Allowed service destinations and rationale.
- IPv4 and IPv6 deny-test results.
- Redirect and DNS-rebinding test results.
- Worker CPU, memory, disk, duration, and egress ceilings.
- Kill-switch test result.
- Monitoring and alert destinations.
- Date and reviewer for the security test.

## Current Drift status

- [x] Application-level URL scheme, host, credential, port, control-character, and initial DNS checks.
- [x] Non-root Docker user and hardened local Compose container settings.
- [ ] A provider-enforced egress firewall or filtering proxy.
- [ ] Connection-time IP enforcement for redirects and subsequent DNS resolutions.
- [ ] Automated integration tests proving the provider network policy.
- [ ] Egress budget, alert, and operational pause procedure verified on the selected host.

**Do not mark this gate complete based on URL validation tests or a successful Docker image build.**
