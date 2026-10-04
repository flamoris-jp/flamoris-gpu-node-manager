# Architecture

RuntimeManager owns state and transitions. CLI, HTTP and MCP compose that same
implementation through `bootstrap.build_manager`, with systemd, health, process
release and lock ports. Adapters do not implement alternate start/stop sequences.
Separate processes share a file lock and reconstruct from real services/processes;
in-flight progress is process-local, not a durable cross-process event stream.

Mutations validate a registry ID, acquire the lock, inspect, stop conflicting
runtimes, await inactive, await owned-process release, start the target, await
active, then await health. Failure blocks the next step and reports FAILED with
transition context. An already READY target is a safe no-op. Unknown/disabled
activation is rejected. Startup does not choose a runtime or kill unknown processes.

Public states: OFF, STOPPING, WAITING_FOR_GPU, STARTING, READY, FAILED.
Resource classes define conflicts; profiles describe runtimes, not individual models.

Release currently verifies configured process matchers against procfs. This is a
bounded ownership check, **not proof that total VRAM reaches zero**. Incorrect or
incomplete matchers can miss processes; deployment validation is essential.
Inspection failure is conservative. AMDGPU sysfs telemetry is best effort and
never substitutes for ownership/readiness. Other GPU vendors can use lifecycle
control with unavailable telemetry; no vendor-specific monitoring is claimed.

Health types: http (loopback URL), http-json (loopback URL plus one JSON Pointer
scalar equality), tcp (loopback host/port), process, systemd-active, none. HTTP JSON
responses are bounded to 64 KiB; malformed, truncated, duplicate-key, non-finite,
and missing values fail closed. Equality requires the same scalar type (a boolean
is not the number 1). Array pointer indices use canonical ASCII decimal notation. Only
use a probe that truthfully represents readiness for
that runtime; an open TCP port does not prove that model weights have loaded.
HTTP probes bypass environment proxies, reject all redirects and accept only
direct 2xx responses from the configured endpoint.
GPU-heavy profiles cannot disable resource release checks.

NodeIdentity is immutable presentation data loaded independently of profiles.
Web HTML escapes names; JavaScript uses textContent. MCP instructions stay fixed;
configurable names are not executable instructions. Display text does not affect
runtime identifiers, transport paths, registry discovery or lock selection.

Generation services retain generation-job authority. Model installs, idle policy,
sleep/wake, multi-node scheduling and remote authentication are separate work.

