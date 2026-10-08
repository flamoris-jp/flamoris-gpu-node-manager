# Progress

## Updater adoption — 2026-10-08

Entry release target: 1.2.0. Source adds durable admission and an independent
mTLS Owner with application-owned schema/resource inspection. Review/testing
and matched dependency wiring are in progress. No release or live update is
claimed. See [Updater contract](docs/UPDATER.md).


## Updater entry review checkpoint

The independent Owner and durable admission are implemented. Local full suite:
**499 passed, 7 skipped**. Owner tests validate configuration/retained state without
provider execution or state reset. SDK pinned to
d9f010a92ff6e8a1e7a3b7fad8817850bdfb72cd (Updater PR #6).
Final application CI and matched integration remain under review.
No release, trust/profile provisioning, enrollment or real-host change occurred.
