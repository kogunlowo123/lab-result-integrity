# ADR 0001: Agents never release, verify or autoverify results

Status: accepted (2026-10-14)

## Context

Autoverification is a validated LIS function owned by the laboratory
director. Letting an agent release results would make a non-deterministic,
continuously changing component part of the release path and would move
accountability away from the people the regulations name. The market signal
for this domain is explicit: lab agents never release diagnostic outcomes on
model confidence.

## Decision

- The service has exactly two screening outcomes, `screened_no_findings` and
  `held_for_review`. Neither releases. `released_by_agent` is always `false`.
- No REST route, MCP tool or A2A skill exists to release, verify or claim and
  decide on behalf of an agent. Agents may only attach proposals, which never
  change case state.
- The policy denies `result:release`, `result:autoverify`, `result:verify` to
  everyone and `review:claim|decide|override` to agents. At start-up the
  service evaluates its loaded policy against a fully scoped synthetic agent
  and refuses to start if any of these would not be denied.

## Consequences

- A policy edit cannot silently grant release authority; it fails start-up.
- `screened_no_findings` is input to the LIS's own autoverification rules,
  not a substitute for them. Laboratories that want fewer manual reviews
  tune those rules, not this service.
- Pathologists see more cases than an autoverifying agent would produce;
  hold-rate monitoring and catalogue tuning manage that load.
