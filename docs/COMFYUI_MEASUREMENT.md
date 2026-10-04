# Initialized ComfyUI measurement source

`infrastructure.comfyui_measurement.ComfyUIManifestSource` connects the reviewed
binding capture and protected byte measurement primitives to the publisher's
`ManifestSource.measure(deadline=...)` port. It returns the typed `MeasuredManifest`
and does not publish a record, grant readiness, install a runtime hook, or change
any profile, command, service, production pin or provider process.

## Trust and completeness

An overlay must bind `InitializedRuntimePort.observe(deadline=...)` to reviewed
**local, runtime-owned instrumentation**. Portable
[runtime implementations](RUNTIME_INSTRUMENTATION.md) supply an explicit local
port, structural legacy reader and PID/UID-bound UNIX client. The local port invokes
`capture_comfyui_bindings()` inside the selected ComfyUI process after actual
asynchronous node initialization. A manager-side Python process, a remote
`object_info` response, client-authored inventory or a nonempty node registry
cannot establish this timing or supply an authoritative observation.

The resulting `InitializedObservation` explicitly certifies:

- `initialized=True`: actual initialization has completed;
- `closure.complete=True`: the reviewed plan contains the entire effective
  core/dependency/config closure, including custom-node source, lazy import
  search locations, native libraries, interpreter content, and configuration
  reads relevant to execution;
- `closure.mutation_coverage_complete=True`: every permitted runtime/content
  mutation, including privileged maintenance, automatic/external restart and
  dynamic class/module changes, is covered by the publisher's mutation protocol;
- `schema_contract="comfyui-structural-schema-v1"`: the initialized node schemas
  follow the reviewed normalization contract described below.

These are certifications of the trusted port, **not user switches or facts proved
by the adapter from a list of roots**. Return false or raise when any prerequisite
is unknown. A port implementation and its host writer/lifecycle coverage must be
reviewed before production qualification. This repository deliberately supplies
no default port that makes these certifications for a real ComfyUI installation.

`snapshot_token` is an opaque retained-object binding revision. The port retains
the actual captured registry, class and module objects through the measurement
interval; replacing any of them changes this token even when names, paths and
value snapshots remain identical. A PID or filename is not such a token. The
publisher separately owns exact boot/start lifetime fencing and the anchored
exclusive evidence lock. Snapshot comparison cannot replace either requirement.

## Structural node schema contract

`node_interfaces` must contain exactly every registered node, with a nonempty
JSON object for each. The reviewed runtime port obtains the schema from initialized
node definitions and normalizes it consistently for qualification and refresh.
The source validates bounded JSON values and exact registry coverage, then hashes
the normalized object with Generation's canonical JSON SHA-256:

`json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()`

This includes Python's default `ensure_ascii=True`. A schema object is never
converted to a string before hashing. No provider package is imported and no
node interface/loader method is invoked by this source adapter.

The v1 port normalization must preserve structural execution-relevant information:
input names and required/optional/hidden roles, type bindings, list semantics,
declared defaults/constraints, output type/order/list semantics, and output-node
role. Fixed categorical enum values remain part of the schema. For explicitly
reviewed inventory inputs such as current upload-file or checkpoint choices,
replace the changing inventory with a stable typed inventory marker bound to its
loader category; retain the input's structural semantics. Excluding another
field requires evidence that it does not affect execution. Unknown dynamic
schema semantics fail closed. The port implementation owns and tests this
normalization; the adapter cannot infer it from arbitrary JSON.

In particular, hashing raw `object_info` blindly would treat a new managed upload
filename in LoadImage choices as an interface change. Conversely, stripping every
enum/default would miss real behavior changes. Neither is a supported v1 port.
The offline fixtures use a synthetic already-normalized schema; they do not
certify normalization for installed third-party nodes.
The portable legacy port/normalizer is implemented separately from this source.
Its installed API compatibility and actual initialization binding remain
unaccepted prerequisites. V3 and unknown dynamic semantics fail closed.

## Origin and content binding

Exactly `core`, `dependencies`, and `config` groups must contain nonempty named
file/tree roots. The source copies the plan and checks that every observed
executable, file module, namespace root, ordered import search root, registered
node source, and explicitly observed native library is covered by those roots.
Observed file origins must exist as regular files; namespace and search roots
must exist as directories. Registered node module/file bindings must agree with
the captured module registry. Unknown origins, unresolved namespace iterators,
missing files and incomplete schemas reject the whole result. Builtin/frozen
modules are bound through the protected interpreter/effective closure; their
optional observed file spelling is also checked when present.

Every covered group then goes through `ProtectedContentMeasurer`: all bytes and
directory topology are measured, and ownership, ancestor protection, symlinks,
hard links, ACL support and content stability are checked. Lexical coverage alone
never substitutes for this protection/measurement. Paths are not resolved and
`..` components are rejected rather than normalized away. Unsupported linked venv
layouts, zip import paths, absent search directories, and runtime-owned/writable
installations therefore remain unavailable until explicitly supported and reviewed.

The node implementation digest binds the three measured group identities, the
fingerprint of the complete observed runtime binding document, and the exact node
name/module/qualified-name/file binding under `comfyui-measured-node-v1`. The
binding document includes ordered search paths, module and node mappings,
model-folder mappings, retained-object revision, native libraries, and labeled absolute
roots. Its canonical JSON is hashed once, then reused as an identity while
computing each node digest. No source/model/config bytes or actual paths appear
in the returned manifest.

## Effective checkpoint lookup

The v1 adapter supports exact checkpoint identities. The trusted port's
`closure.checkpoints` maps each exact loader name to the runtime's actually
selected effective path. Names may contain canonical nested paths such as
`sub/example.bin`; absolute paths, empty/dot/dotdot segments, backslashes and
unsupported extensions are rejected.

The source uses captured `model_folders["checkpoints"]`, preserving its path order
and extension filters. It compares the port's effective selection with the first
existing regular-file candidate in that order. Every consulted lookup root,
including earlier roots with no current matching file, must be a directory under
a measured group. Those directories are fully measured and protected. This
conservative requirement binds potential same-name shadowing and rejects a
writable earlier directory even if the selected file is protected. It can hash
additional model files in those directories; byte/entry budgets count that work.

Every other captured model-folder category also requires nonempty directory
roots under the measured groups. For example, embedding content read during text
encoding can influence an image even though it is not the selected checkpoint.
Changing those bytes changes measured content and node implementation identities.
Unmeasured, nonexistent or writable roots in any category reject qualification.
This conservative subset can measure many installed model files and must fit
the configured budgets; partial coverage is never inferred from a true flag.

The protected measurer receives `checkpoints/<exact-name>` and hashes the selected
model's actual bytes. The adapter explicitly converts that reviewed loader kind
to Generation's consumer key `checkpoint:<exact-name>`. It does not globally
replace path separators or accept arbitrary consumer digests. Missing/shadowed
selection, symlink targets, zero-byte models, or unsupported lookup behavior leave
evidence unavailable. Alternate/custom checkpoint loader behavior needs its own
reviewed adapter contract before use.

## Consistency, deadlines and failure

The trusted port is observed before and after hashing, under one absolute
monotonic deadline. Compare actual capture values, retained-object token, copied
binding/closure document, and normalized schemas. Any difference rejects all
measurements. Permission/content changes during filesystem traversal are rejected
by the existing protected measurer. Unmanaged writers remain excluded by the
trusted mutation boundary rather than by snapshot comparison alone.

The source limits observation work to 100,000 entries, eight MiB of UTF-8 text,
4,096 characters per text value and 64 nested JSON levels. Byte measurement uses
`MeasurementLimits` with its timeout capped by the remaining source deadline.
Checks occur throughout observation processing and each node digest. There is no
digest cache. The opt-in spawned authority watchdog bounds worker operations;
its limits and required overlay binding are documented in
[runtime instrumentation](RUNTIME_INSTRUMENTATION.md).

Every failure raises `ComfyUIMeasurementError` with the fixed public message
`initialized runtime measurement unavailable`. Paths, configuration bytes and
schema metadata are absent from that message. Chained errors are for trusted
debugging, not presentation. The publisher withdraws evidence before measuring
and leaves it absent on failure; this source never restores evidence or fabricates
a partially usable manifest.

## Offline validation and production boundary

Real temporary-file fixtures exercise actual collector output, protected byte
hashing, same-name/size/mtime-restored content changes, native and module origin
coverage/existence, lookup precedence and exact consumer keys, writable earlier
lookup roots, unresolved modules, symlink/dotdot rejection, interface/registry
coverage, before/after binding changes and deadline/error behavior. They require
no GPU or provider inference.

These tests do not complete issue #3 or certify a host. A reviewed initialized
runtime port, all effective dependency/config/native origins, normalized installed
node interfaces, writer/lifecycle coverage, watchdog, boot/start lifetime fence,
protected storage and actual Workflow/refresh/restart acceptance remain required
before evidence may enable production qualification. See
[runtime capture](RUNTIME_CAPTURE.md), [content measurement](CONTENT_MEASUREMENT.md),
and [runtime evidence](RUNTIME_EVIDENCE.md).
