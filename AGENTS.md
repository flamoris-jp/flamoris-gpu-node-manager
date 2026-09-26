# AGENTS.md

This repository is part of the FLAMORIS ecosystem.

AI agents and human contributors should inspect the current repository before making substantial changes. Do not assume that setup, build, deployment, service names, paths, configuration, or architecture match another FLAMORIS repository.

## Core principles

1. **Current implementation is authoritative**
   - Read the repository documentation, configuration, tests, and relevant source before changing behavior.
   - Do not invent repository-specific commands, paths, services, or configuration.

2. **Keep responsibility clear**
   - Keep this repository focused on its documented purpose.
   - Preserve application and service ownership boundaries.
   - Do not create a second source of truth for state owned elsewhere.

3. **Reuse deliberately**
   - Check existing FLAMORIS shared packages and repositories before duplicating common infrastructure.
   - Reuse code only when the dependency direction and ownership boundary remain clear.
   - Avoid speculative abstractions for requirements that do not yet exist.

4. **Security and privacy are architectural requirements**
   - Never commit or log secrets, credentials, tokens, private keys, or sensitive user data.
   - Prefer least-privilege access and bounded resource use.
   - Treat external input and remote responses as untrusted.

5. **Stable behavior over cleverness**
   - Prefer explicit, testable contracts and straightforward implementations.
   - Preserve existing public behavior unless a change intentionally modifies it.
   - Document externally visible behavior and compatibility impact.

6. **AI-native, human-authoritative**
   - AI-assisted development is welcome.
   - Humans remain responsible for reviewing behavior, security, licensing, and compatibility.

## Before implementing a substantial change

- read this file and README.md;
- read relevant docs, Issues, and Pull Requests;
- inspect current implementation and tests;
- identify the source of truth and dependency direction;
- check whether reusable FLAMORIS infrastructure already exists;
- verify repository-specific setup and deployment details instead of guessing.

## Testing

Add or update tests where practical.

Prefer deterministic tests and explicit contracts. When behavior differs by platform, runtime, provider, or environment, document the supported boundary and test the relevant cases.

## Licensing

Unless stated otherwise, code in this repository is licensed under Apache License 2.0.

Do not add third-party code, models, model weights, datasets, fonts, media, or generated assets unless their licenses are compatible and clearly documented.

## Support

FLAMORIS does not provide guaranteed individual support.

Use the repository documentation, Issues, tests, logs, and source code as primary references when diagnosing problems.

## GPU Node Manager invariants

- Read README, docs/ARCHITECTURE.md and docs/DEPLOYMENT.md before changing adapters.
- RuntimeManager is the sole state/transition implementation. All adapters use it.
- Serialize mutations with one host-wide lock; presentation identity cannot select a lock.
- Stop → inactive → bounded owned-process release → start → active → health → READY.
- Never report READY or successful release when inspection fails.
- systemd remains the supervisor. No shell API, arbitrary service API or runtime-ID branches.
- This repository contains synthetic examples and host-independent tests only.
  Deployment overlays own real profiles, host paths, services, credentials and topology.
- The shared .NET logging/MCP packages do not apply to this Python implementation.
  Keep standard logging and the Python MCP SDK; do not vendor shared source.
- Preserve thin CLI/HTTP/MCP adapters, explicit failure states and telemetry degradation.
- Run pytest, ruff check/format and mypy src; do not auto-merge or deploy from a migration PR.
