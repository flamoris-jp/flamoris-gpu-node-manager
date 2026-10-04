# Runtime evidence invalidation boundary

This implements the managed-transition invalidation prerequisite for issue #3.
It does **not** complete that issue or establish `exclusive-mutation-lock-v1`
continuity. Do not enable Generation qualification using a guessed, hand-authored
or health/stat-derived manifest.

The separate [protected content measurement primitive](CONTENT_MEASUREMENT.md)
measures actual bytes and rejects unsafe roots/ancestors. It is also a prerequisite,
not a publisher or proof of complete runtime/lifetime coverage.

## Optional configuration

A runtime profile may add one field:

```yaml
evidence_record: /var/lib/example-runtime-evidence/runtime.json
```

The path is absolute/normalized with a bounded JSON filename. Omission preserves
existing behavior and API payloads. Presentation identity cannot choose this path.
All CLI/HTTP/MCP entry points must load the same profiles and existing host-wide
transition lock. Evidence slots must not overlap each other or that transition
lock. Multiple slots are acquired in deterministic order, with one shared timeout,
before any record is removed or supervisor mutation starts.

## Stable storage and cold provisioning

The overlay owns a persistent directory writable only by the trusted mutation
identity, protected ancestors without symlinks, and group read/traverse access for
Generation. The runtime user and other untrusted principals must not own this
directory or its writable ancestors. The implementation rejects group/world write
access, foreign owners, symlinks and hard links on slot files. A root-owned sticky
ancestor is accepted for temporary offline tests, but not as the slot directory.

While **all participants are stopped**, overlay code may invoke
`provision_evidence_slot(record_path, reader_gid=...)`. This creates a stable regular
`<record>.lock`, `<record>.identity.json` and `<record>.epoch.json`, never a manifest.
The caller owns the files. The optional reader group gets mode 0640 on the lock;
the identity and epoch records remain mode 0600. Group directory access remains
the overlay's responsibility. The identity anchors directory/lock device and inode
plus a random installation authority ID. A slot is loaded from that existing
identity; construction never creates missing files or resets identity.

Any existing slot component causes provisioning to fail. Interrupted provisioning
is deliberately not repaired automatically. Inspect it offline while every reader
and writer is stopped; never remove/recreate the stable lock under live clients.
Preserve the entire directory together. Restoring old epoch counters/anchors from
backup under live clients or silently changing backing storage is unsupported.

## Managed transition behavior

1. RuntimeManager acquires the existing host-wide transition lock and inspects
   runtime state. An already READY activation is still a safe no-op.
2. Before actual managed mutation, acquire every affected slot's exclusive flock
   using the same `<record>.lock` that Generation opens for its shared guard.
3. Check current directory/lock identities against the persistent anchor. Remove
   any existing regular, owned, write-protected qualification record and fsync the
   directory before advancing epochs or calling systemd.
4. Durably allocate the next epoch using the installation ID plus a monotonic
   64-bit counter. The epoch JSON is replaced atomically and the directory fsynced.
   Missing, malformed or exhausted state fails closed; it never falls back to zero.
5. Perform the normal stop/inactive/release/start/active/health sequence while
   holding those exclusive locks. Release them afterwards. Leave the evidence
   record absent after both success and failure.

A reader that already holds the shared flock can finish its guarded admission;
managed mutation waits. A timed-out wait changes no evidence or services. Once all
locks are acquired, a partial withdrawal/persistence failure can leave evidence
absent for some affected runtimes but prevents **every** supervisor mutation.
After persisted withdrawal, no code path restores or publishes the old record;
supervisor calls begin only after that removal is persisted. Host reboot/provider
crash coverage still belongs to the remaining continuity prerequisites below.
Health `READY` reports runtime availability; it does not grant Workflow readiness.

The epoch JSON records allocation only. It is not a manifest or a second source
of runtime/job state. A future publisher must follow the same anchored lock and
epoch protocol; do not create a valid Generation record from this ledger alone.

## Remaining continuity prerequisites

Keep qualification evidence unavailable until all are implemented and accepted:

- protect effective core, dependency, config, custom-node and model content roots
  from runtime/unmanaged writers, including writable parents and resolved targets;
- serialize every approved content update and every systemd start/stop/restart
  path, including external callers/automatic restarts, before provider mutation;
- establish exact provider lifetime and fail closed for crashes or unknown lifetime;
- measure actual content plus installed node interfaces/implementations and models,
  and publish the exact Generation schema/URL with bounded expiry and automatic
  refresh; manifest publication must remain under the same exclusive lock;
- review/pin the overlay after CI and accept real mutation/restart, bounded
  `workflows.verify`, reference-image execution and terminal copy release.

Intercepting Manager activate/stop alone cannot cover these paths. Native/systemd
supervision remains unchanged. Generation never activates runtimes, and no shell,
arbitrary-service, model-download or per-Workflow approval API is introduced.

Offline tests cover shared-reader contention, all-slot locking, inode replacement,
missing/corrupt epoch state, persistence failure and restart epoch allocation.
They do not certify actual runtime writer coverage, content measurement or inference.
