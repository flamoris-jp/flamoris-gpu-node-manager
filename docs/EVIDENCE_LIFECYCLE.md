# Trusted lifecycle composition

`RuntimeEvidenceLifecycle` is an optional implementation of the existing
`EvidenceInvalidationPort`. It joins the portable publisher and measurement source
to RuntimeManager's existing managed stop/release/start/health sequence. It adds no
runtime selection, GPU reservation, remote tool, profile field or per-Workflow
approval. Default CLI/HTTP/MCP composition remains invalidation-only.

The deployment's trusted Python composition can create one
`EvidencePublicationBinding(profile, publisher, eligible)` per configured evidence
profile and pass `RuntimeEvidenceLifecycle(bindings)` as `build_manager(...,
evidence_lifecycle=...)`. Profiles must match the loaded registry exactly; slot
paths must match the publishers. Existing slot validation remains in bootstrap.
An unknown or mismatched evidence profile fails before any supervisor mutation.

`eligible()` must return an actual boolean from trusted current service,
initialization and health observations. `False` denotes a known stopped or
ineligible runtime; unknown observation must raise. It is not a client-supplied
coverage assertion. The source and lifetime ports still establish complete
protected runtime binding. A health-ready runtime alone cannot issue evidence.

## Managed transition protocol

1. RuntimeManager acquires its existing host-wide transition lock and performs its
   ordinary inspection. An already READY activation remains a no-op.
2. Acquire every affected publisher's anchored exclusive slot lock in sorted path
   order, within one shared lock-wait budget. If any lock cannot be acquired,
   neither records, epoch ledgers nor services change.
3. Call `begin_mutation()` on every lease. This persists withdrawal and allocates
   epochs before the caller performs any service operation.
4. Run the normal Manager sequence while retaining those same locks. A separate
   startup hook must not acquire the locks again while Manager awaits health.
5. After successful completion, measure and publish each actually eligible
   runtime through its current lease. Known stopped runtimes remain absent.
   Publisher/source/eligibility failure leaves all affected evidence absent,
   including any result already published earlier in this batch.
6. Release the evidence locks, then return through the existing Manager authority.
   Publication failure is a failed transition; success is still lifecycle READY,
   not proof of a completed Generation Workflow attestation.

The same publisher exposes `locked()` and `mutation()` leases for a reviewed
trusted lifecycle or content-maintenance adapter. Such an adapter must first use
the same host-wide transition authority/lock when selecting or changing services.
Do not hold an evidence lock and then wait for the host transition lock: reversing
this order can deadlock a Manager transition. Never issue evidence after an
uncoordinated restart merely because health is green.

## Long-lived renewal and deployment boundary

Issuance is separate from `publisher.automatic_refresh(...)`. A short-lived CLI
call cannot own continual renewal. The deployment must place renewal in its
reviewed long-lived trusted owner and stop/revoke it on lifetime or writer-boundary
loss. Refresh does not create a new runtime epoch, activate a runtime, recover an
unknown provider, or repair incomplete source closure.

The source-only adapters do not install a ComfyUI startup hook, replace a systemd
unit, discover native restart/update paths, or fence spontaneous crashes. A
trusted lifetime observation alone is not continuous crash coverage: a provider
can die after the last observation, and the schema-v1 Generation reader cannot
independently inspect provider liveness. These bindings must be resolved by the
reviewed runtime/deployment protocol before claiming
`exclusive-mutation-lock-v1`. Keep qualification unavailable while they are unknown.

Offline tests verify real flock interoperability and Manager ordering using
synthetic services/lifetimes. They do not certify a production runtime. See
[publication](EVIDENCE_PUBLICATION.md),
[ComfyUI measurement](COMFYUI_MEASUREMENT.md), and
[the qualification requirements](RUNTIME_EVIDENCE.md).
