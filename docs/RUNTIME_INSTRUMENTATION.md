# Runtime instrumentation and bounded measurement

These opt-in Python implementations fill portable observation, schema, transport,
lifetime and watchdog ports. They are not installed hooks or production acceptance.
Default CLI/HTTP/MCP remains invalidation-only. No profile, service, version or
production pin changes automatically.

## Provider-side observation

`LocalInitializedRuntimePort` runs in the actual ComfyUI process. A reviewed
completion hook calls `mark_initialized()` after asynchronous node loading.
A nonempty registry never enables it. `invalidate()` disables observations before
reinitialization; explicit completion is required again. Capture occurs before
and after schema acquisition. Retained module/registry/class objects, module
attributes and inherited class attributes prevent identity reuse from hiding
replacement. An opaque revision changes on replacement even when names, paths
and schemas match. The revision now binds node implementation identity, so
renewal rejects changes between separate measurements too.

This revision is not a hash of object internals. In-place changes to containers,
functions or closures can preserve identity. Every change must still obey the
mutation protocol; snapshots do not prove absence of unmanaged writers.

`read_legacy_node_schema` invokes actual local class `INPUT_TYPES` once, without
construction, node execution or provider imports. It preserves roles/order,
types, fixed enum values/order, all JSON options/defaults/constraints, function
name, input/output list semantics, output names/order and output/intermediate roles.
String-valued IO enums bypass string hooks. Unsupported containers, nonfinite,
deep/oversized schemas, inconsistent outputs and unknown dynamic COMBOs fail closed.

Only exact `nodes` class bindings have reviewed current-inventory exclusions:

| Class and input | Stable marker |
| --- | --- |
| `LoadImage.image` | `managed-image` |
| `LoadImageMask.image` | `managed-image` |
| `LoadImageOutput.image` | `output-image` |
| `CheckpointLoaderSimple.ckpt_name` | `checkpoint` |

Options/defaults remain identity-bearing. Other classes/modules retain exact
choices. Output-image remote COMBO preserves its options and route. The reviewed
reference is upstream ComfyUI
[`f1072eb0350638a3390ddb6afbcaa8c6b237c6fd`](https://github.com/Comfy-Org/ComfyUI/blob/f1072eb0350638a3390ddb6afbcaa8c6b237c6fd/nodes.py),
not the installed revision. These are independently written adapters, not vendored
provider code. V3 `GET_NODE_INFO_V1` classes are unsupported by the default reader;
they need a reviewed local reader. One unsupported node disables the whole manifest.
Never skip nodes or substitute object_info JSON. Schema methods are executable
code: review safety/thread compatibility or schedule the reader in the actual
event loop. A stalled observation thread produces no partial evidence.

## Effective closure and local transport

`ReviewedRuntimeClosure` copies trusted roots/checkpoint names. Defaults
`complete=False` and `mutation_coverage_complete=False` disable qualification.
Reviewed local policy, not client parameters, must establish the full closure
and writer contract. Actual already-loaded `folder_paths.get_full_path` selects
checkpoints; `/proc/self/maps` supplies executable file mappings. Anonymous/deleted
code and escaped paths fail closed; vdso/vsyscall belong to the boot lifetime.
This cannot discover all data/config reads or future imports. The measurement
source independently checks captured coverage and protected actual bytes.

`UnixRuntimeObservationServer` exposes read-only observations in an explicitly
provisioned protected parent. It never replaces existing sockets or accepts
inventory/schema/closure requests. Mode 0600 supports a separate root authority;
same-UID transport tests do not qualify protected measurement. Other UID access
needs reviewed permissions. Authority UID is checked by kernel `SO_PEERCRED`.
Shutdown unlinks only its own socket inode. The client checks exact provider
PID/UID from credentials and observation. Frames are limited to 16 MiB. Every
read shares one monotonic deadline. Truncation, partial/oversized frames,
duplicates, nonfinite values, invalid paths and type substitutions fail closed.
Private error text never crosses the socket. Peer identity does not prove honest
instrumentation or complete code/writer protection.

## Authority watchdog and lifetime

Compose `SpawnedManifestSource(ComfyUIManifestSource(client))` in the non-GPU
authority and pass it to the publisher. A fresh spawn worker connects for before/
after observations and measures bytes under the authority UID while the parent
retains the exclusive lease. No GPU fork or digest cache. Trusted parent inputs
use spawn serialization; worker responses are bounded JSON, never pickle. The
deadline covers partial pipe bodies. Failure kills/reaps only the owned worker,
leaves evidence absent and permits lock reuse. Use the inert UNIX client, not
the in-process port with locks/callbacks. Review composition and the main guard.
There is a one-second cleanup budget; unreaped workers cause failure. Parent
creation/serialization and wedged kernel calls are not interruptible by this
watchdog; it does not universally bound arbitrary parent hooks or the OS.

`LinuxProcessLifetimeFence` owns a pidfd and binds boot ID, PID/start ticks, four
UID credentials and executable path/device/inode/metadata. It checks exit around
procfs reads, rejecting exited/zombie processes, unknown inspection and identity
drift. Closing disables observations; PID reuse cannot revive the handle.
Same-binary exec can preserve observations and needs the mutation protocol.

`ProcessExitEvidenceInvalidator` duplicates the pidfd and observes exit in an
owned thread. It withdraws the publisher's owned epoch, retries bounded lock
failures, withdraws on startup failure, and stops/joins/withdraws on context exit.
Shared Generation guards can delay exclusive withdrawal. It cannot synchronize
before a crash or cover replacement starts: every restart must enter the mutation
protocol before starting. Expiry remains the fallback on storage/lock failures.
This monitor plus consumer schema v1 alone is not continuous all-path coverage.

## Validation and deployment boundary

Synthetic tests cover schema identity, initialization refusal, binding replacement,
lookup/native maps, strict wire data, partial frames, spawned success/failure/hang,
withdrawal/lock reuse, monitor/shared-guard ordering and Linux peer/pidfd behavior.
Restricted environments may skip socket/procfs tests; Linux CI runs them. No GPU
inference or live service changes occur.

Before installation, inspect actual provider/node APIs, startup and thread/event-
loop requirements, effective roots, protection, reader/writer identities and every
maintenance/restart/crash path. Bind through the existing Manager lifecycle port.
First-boot stale-record refusal and external/automatic starts need the same lease.
Keep qualification disabled until binding review and real Workflow/renewal/restart
acceptance. Source implementation cannot certify host facts.
