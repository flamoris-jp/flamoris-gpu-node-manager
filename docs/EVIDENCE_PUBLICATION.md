# Anchored runtime evidence publication

`infrastructure.evidence_publication.FileEvidencePublisher` composes the existing
protected slot, lock and epoch primitives with **trusted Python measurement and
lifetime ports**. It implements atomic schema-v1 publication and expiry refresh;
it does not discover a closure, supervise a provider, configure systemd, install a
hook, grant Workflow readiness, or establish that every writer follows the lock.
No profile, CLI, HTTP/MCP endpoint, shell command or arbitrary manifest import is
added. A missing, partial or unknown trusted port must fail closed.

The actual ComfyUI binding adapter is described in
[COMFYUI_MEASUREMENT.md](COMFYUI_MEASUREMENT.md). It supplies the measured manifest;
binding capture and protected byte measurement remain separate prerequisites.
All live deployment claims require the private overlay and real-host acceptance.

## Trusted contracts

`ManifestSource.measure(*, deadline)` receives an absolute `time.monotonic()`
deadline and returns a `MeasuredManifest`, containing actual core, dependency and
configuration content identities, immutable `MeasuredNode(interface,
implementation)` values, and model content identities. Every identity is an exact
lowercase SHA-256 digest. The manifest is copied and bounded before serialization;
source dictionaries, JSON records and health-derived inventories are rejected.

The port must establish complete effective closure, initialized node semantics,
protected content, quiescent mutations and truthful provenance. Type checking
cannot prove those properties. Python composition is a trust boundary; an ordinary
client must never choose the source, roots, node identities or manifest. Synthetic
test sources deliberately exercise serialization/locking, not honest measurement.

Generation's model keys use `checkpoint:<exact loader name>`. Protected byte
measurement uses `kind/name` internally, so the trusted binding adapter explicitly
maps the first kind separator to a colon. The publisher requires `kind:name` and
does not silently interpret an arbitrary slash-form identity. Loader subdirectory
separators within the exact model name are retained.

`LifetimeFence.observe()` returns a nonempty immutable token for a **known, alive,
exact provider lifetime**, or raises. PID alone is insufficient: a reviewed port
must establish the boot/start identity, initialization timing, crash observation
and writer/lifecycle coverage. The token is checked before and after measurement,
after atomic publication and before releasing a mutation lease. Drift, unknown
lifetime and port errors withdraw evidence. An expiry refresher is not a crash
monitor; the overlay must invalidate every external/automatic lifecycle path.

The opt-in [Linux fence and pidfd exit invalidator](RUNTIME_INSTRUMENTATION.md)
implement lifetime observations and post-exit withdrawal with explicit limits.
They do not replace coordinated supervisor restart and writer bindings.

## Slot and mutation ownership

Provision the slot offline using the existing
`provision_evidence_slot(record, reader_gid=...)` contract. Publication anchors the
same directory and stable lock inode as `FileEvidenceInvalidator` and Generation's
shared guard. Construction never creates, replaces or repairs a missing lock,
identity marker or epoch ledger. Files remain singly linked, trusted-owned and
protected from group/world write. The manifest inherits group read access from
the pre-provisioned lock, otherwise remains mode 0600.

`with publisher.mutation(timeout=...) as lease` obtains the exclusive flock,
withdraws and fsyncs any record, then durably allocates a never-reused epoch before
yielding. The caller may perform its approved mutation, then call `lease.publish()`
once while the same exclusive flock is still held. Publication must be the final
content/lifecycle mutation within that lease: do not change the measured runtime
after publishing. The final lifetime/record recheck detects observed lifetime or
record drift; it does not replace the trusted writer protocol or rehash all bytes
after arbitrary Python code. An exception after publication removes that result
before releasing the guard. An unpublished normal exit leaves the record absent.

`publisher.issue(timeout=...)` composes a mutation lease and publication. A failed
attempt never restores the old record or reuses its epoch. Missing, malformed or
exhausted epoch state is unavailable, with no reset-to-zero fallback.

For multi-runtime transitions, a trusted coordinator can enter every
`publisher.locked(timeout=...)` lease in deterministic order before invoking any
`lease.begin_mutation()`. `locked()` acquires/revalidates the anchor only; it does
not withdraw records. This preserves the existing all-slots-before-mutation
invariant when a later slot is contended. Once all are held, begin all mutations,
perform the existing RuntimeManager transition, and publish only eligible active,
initialized and healthy providers. No second GPU state authority is introduced.

Do not invoke a nested systemd startup hook that tries to reacquire the same slot
flock while RuntimeManager holds it across `start`: that deadlocks. Lock handoff
and all external/automatic systemd paths require a separately reviewed deployment
integration. This module does not install one or claim that it exists.

## Schema and identity-preserving refresh

Records contain exactly `schema_version: 1`,
`continuity: exclusive-mutation-lock-v1`, the exact configured `provider_url`, the
durable `provider_epoch`, `expires_at`, and the measured `manifest`. Provider URLs
must already match Generation's trailing-slash-free spelling; credentials,
queries, fragments, malformed ports, backslashes and malformed percent escapes
are rejected. No URL rewriting or guessed endpoint fallback occurs.

The manifest contains `core`, `dependencies`, `config`, `nodes` and `models`.
Each node has `interface` and `implementation` SHA-256 identities. Whole-manifest
fingerprinting matches Generation: SHA-256 over UTF-8
`json.dumps(manifest, sort_keys=True, separators=(',', ':'), allow_nan=False)`.
The whole record is at most one MiB. Atomic replacement writes a new protected
temporary file, fsyncs it, replaces the record and fsyncs the directory under the
exclusive anchored lock. Temporary files are removed after success/failure.

TTL is finite, positive and at most 300 seconds (default 120). `refresh()` acquires
the same exclusive lock, checks the existing exact record/epoch and unexpired
wall/monotonic lifetime, remeasures through the trusted source, and accepts only an
identical canonical manifest and lifetime token. It changes **only `expires_at`**.
Changed content requires a new mutation/epoch. Expiry during remeasurement cannot
be extended as the old epoch; it withdraws and requires a new issue operation.
A stale publisher never withdraws a later owner's epoch.

`with publisher.automatic_refresh(interval=..., timeout=...)` renews previously
issued evidence on an owned daemon worker. The interval must be below TTL and
only one automatic owner can exist on that publisher. Its epoch is fixed at
creation: closing/failing an old worker cannot refresh or withdraw a new issue
on the same publisher. Context close cancels further publication and withdraws
only its own still-current result. Any source/lifetime/storage failure is reported
with a fixed `EvidencePublicationError` message and terminates the worker.

Lock contention never erases a record without owning the exclusive flock. If a
reader keeps the shared guard beyond the withdrawal timeout, the error is surfaced
and the bounded expiry still makes the record unavailable to new admissions. Do
not claim synchronous withdrawal completed in that case.

## Bounds and remaining acceptance

Mutex/flock acquisition uses one finite deadline, including setup/revalidation.
Measurement has a cooperative deadline (default 900 seconds, configurable up to
3600), additionally capped by the prior expiry during refresh. Cancellation is
checked before/after publication so a blocked worker that later returns cannot
publish after close. Worker join/withdrawal waits are bounded by their configured
lock timeout; close reports failure if a worker remains blocked.

OS filesystem calls and arbitrary trusted callbacks can still stall. The source
and lifetime instrumentation need an **external watchdog/process boundary** for a
hard wall-clock limit. A stuck source holds the exclusive guard, blocking new
Generation admissions and managed mutation; this is conservative but not a
production availability guarantee. Threads are not a substitute for that watchdog.
This implementation does not enforce every privileged writer or provider crash.

Offline tests cover exact schema/fingerprint, reader contention, all-slot
acquisition, epoch allocation/restart, source/lifetime/record drift, expiry,
post-publication mutation failure, malformed URLs, atomic storage failure, reader
permissions, bounded records, automatic cancellation and stale worker ownership.
They run without inference, services or production filesystem changes. Keep live
qualification unavailable until protected closure, full writer/lifecycle coverage,
watchdog/lifetime integration and actual Generation/Workflow acceptance are proven.
