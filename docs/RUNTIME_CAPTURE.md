# Runtime-owned ComfyUI binding capture

`infrastructure.comfyui_capture.capture_comfyui_bindings()` reads existing
registrations **inside the selected ComfyUI process**. This is the observation
prerequisite for evidence issue #3, following protected content measurement.
It does not install a startup hook, publish evidence or grant Workflow readiness.
There are no new profiles, commands, HTTP/MCP tools or RuntimeManager transitions.
Package version and production pins are unchanged.

## Invocation and observed data

A future reviewed runtime adapter must invoke capture after asynchronous node
initialization has completed, using the actual `nodes` and `folder_paths` modules
already in `sys.modules`. A nonempty registry alone cannot prove that initialization
is complete. The collector takes no inventory or caller-supplied roots/mapping;
Manager, Generation and remote clients must not manufacture its inputs.

The immutable result contains:

- current process PID/UID/GID, working directory, Python executable spelling and
  ordered Python search paths; an empty search-path entry denotes that working
  directory, not an omitted root;
- every observed `sys.modules` entry, including aliases, builtin/frozen modules,
  ordinary files, namespace entries, blocked entries and unknown origins;
- every registered node's class module, qualified name and observed module file;
- every actual model-folder category, its ordered loader roots and sorted
  extension filters, including categories outside an extra-model-path YAML.

No runtime/provider package is imported by the capture call. No node INPUT_TYPES,
GET_SCHEMA, loader, interface, model scan, hash, GPU operation or provider request
runs. Builtin descriptors read module/class dictionaries and class names without
invoking custom module/metaclass attribute hooks. Nonstandard module specs are
unknown; custom/lazy namespace path iterators are not executed and remain
unresolved. Explicit list/tuple namespace paths are observed as given. Ordinary
container subclasses are rejected rather than calling custom iterators.
Non-module compatibility entries (including Python's `typing.io`/`typing.re`
class aliases) are preserved as unknown without accessing their attributes.
They cannot be used as registered-node file origins or complete closure evidence.

Paths are converted to absolute lexical paths relative to the observed working
directory, preserving `..` components, including those following symlinks.
Neither `abspath`/`normpath` nor filesystem resolution is used: collapsing a
symlink followed by `..` can record a different origin than the OS actually reads.
Symlinks are not resolved or silently accepted as protected content.
All captured file/root paths still require descriptor-anchored protection and
actual byte measurement by the trusted authority. The current protected content
measurer rejects `..` paths and symlinks; callers must not normalize the captured
path to bypass that rejection or treat this observation as qualification.

## Consistency, limits and failure

Capture twice with one five-second cooperative deadline and one work budget:
at most 20,000 entries per mapping/sequence, 100,000 aggregate entries across both
passes, eight MiB of observed UTF-8 text and 4,096 characters per text value.
Controls/DEL, invalid UTF-8, missing/empty registrations and unsupported bindings
fail with the fixed `RuntimeCaptureError` message. Unknown registered-node file
origins reject capture; unresolved unrelated modules remain explicitly listed.
Expected/other errors never include paths or metadata in the public message;
chained exceptions are for trusted debugging, not future presentation adapters.

Compare the two value snapshots plus module/registry/class object identities.
Even replacement classes/modules with identical names/paths, registry replacement,
model-root reorder or search-path changes reject the entire observation.
Private references retain those objects through the recheck; identity reuse does
not make a replacement appear unchanged. Returned mappings and nested records
are immutable and do not share mutable registry/root lists with the runtime.

This is a binding observation, **not an atomic snapshot or mutation lock**.
It does not inspect executable method bodies, dynamic monkeypatches, class
interface semantics, all native library mappings, configuration reads, future
imports or every possible dependency origin. PID/UID alone is not a lifetime
fence. A future integration must establish the coordinated mutation boundary,
actual initialization timing, boot/start identity and complete effective closure;
unknown/dynamic origins cannot be treated as measured or covered.
The cooperative deadline does not bound a stalled OS call; integration still
needs an external watchdog for a hard wall-clock bound.

## Remaining qualification boundary

The result intentionally has no interface/implementation digest, manifest,
provider URL/epoch, continuity assertion or expiry. It cannot satisfy Generation's
evidence schema and must never be serialized into a qualification record as-is.
Module names and observed file attributes are not evidence of protected content
or honest runtime behavior. Root/trusted writer control, initialization/loader
binding, measured node semantics and complete dependencies/models/configuration,
all systemd/maintenance/restart paths, lifetime fencing, atomic publication,
refresh and actual host/Workflow acceptance remain necessary.

Keep evidence unavailable while these are incomplete. The collector is not wired
into bootstrap and no live runtime is changed by importing this source.
See [runtime evidence](RUNTIME_EVIDENCE.md) and
[protected byte measurement](CONTENT_MEASUREMENT.md).
