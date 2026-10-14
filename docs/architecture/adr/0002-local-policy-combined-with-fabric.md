# ADR 0002: Local policy always evaluated; fabric can only tighten

Status: accepted (2026-10-14)

## Context

Agents in the portfolio receive credentials from agent-identity-fabric, whose
`/v1/authorize` evaluates the enterprise agent policy. Laboratories also need
agents registered directly as OIDC clients, and some deployments run without
a fabric. Domain invariants (ADR 0001) must hold in every configuration.

## Decision

- Bearer tokens whose unverified `iss` equals the configured fabric issuer
  are introspected at the fabric (RFC 7662); all others are verified locally
  against the enterprise OIDC JWKS. The unverified issuer only selects the
  verifier and is never trusted.
- Every action is evaluated by the local agentic-core PolicyEngine. For fabric
  principals the fabric's decision is also obtained and the most restrictive
  of the two wins (deny > require_approval > allow).
- Without a fabric URL the local policy alone decides; there is no allow-all
  fallback. Fabric outages fail closed (503).

## Consequences

- Two policies to maintain for fabric agents; documented in the agent
  specification. The local policy expresses laboratory rules; the fabric
  policy expresses enterprise agent governance.
- One extra network call per fabric-authenticated action. Acceptable at
  laboratory request rates; cacheable later by decision TTL if needed.
