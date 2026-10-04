# Protected content measurement

`infrastructure.content_measurement.ProtectedContentMeasurer` is the byte
measurement/protection prerequisite for runtime evidence issue #3. It does not
publish an evidence record, discover effective loader paths, bind node interfaces,
refresh expiry, certify lifetime or make a Workflow ready. There are no new
profile fields, CLI/HTTP/MCP commands, transition behavior or production pins.

## Identity and writer boundary

The caller is the trusted mutation identity. `runtime_uid` must be a different,
non-root identity, obtained from the inspected runtime, not an end-user assertion.
Files/directories must belong to root or the caller, with no group/world write
access. Every ancestor is checked through descriptor-relative `O_NOFOLLOW`
opens. A root-owned sticky ancestor is supported for temporary fixtures; a
selected content directory itself never gets that exception.

Extended POSIX access/default ACLs are rejected, including apparently read-only
ones. ACL inspection errors, including unsupported querying, are unavailable;
absence is accepted only on ENODATA. This conservative implementation has no ACL
permission evaluator. All symlinks, hard-linked regular files, sockets, FIFOs and
devices are rejected rather than skipped. Ordinary venv installations with links
therefore need an explicitly supported installation layout before measurement.
No permissions are changed and no file is repaired, created or removed.

Root and processes sharing the trusted mutation identity remain privileged
writers. They must honor the same evidence lock protocol. Permission validation
does not prove they do so, disable their access, or cover external unit restarts.
Likewise this primitive cannot establish that a runtime lacks privilege escalation
or that every dependency/model origin has been supplied. These are integration and
deployment prerequisites; do not publish a valid manifest while they are unknown.

## Measurement contract

Call `measure(groups, models)` while the existing anchored exclusive evidence
guard is held and content mutation is quiesced. The caller must already have
established complete runtime-owned inputs:

- exactly `core`, `dependencies`, `config`, each with nonempty named file/tree
  roots; names belong to a reviewed binding plan;
- nonempty model mapping with exact `kind/name` keys and regular nonempty files.

The result contains only these content group/model SHA-256 identities and work
counters. It deliberately has no nodes, provider URL, epoch, continuity assertion
or expiry and cannot satisfy Generation's evidence schema. Supplying a partial
tree list is not a proof of complete runtime coverage. A publisher must obtain
actual installed node interfaces/implementation origins and effective loader
closure through trusted runtime-owned instrumentation before composing a manifest.

Tree identities include labels, sorted relative entry names, empty directories,
and SHA-256 of **all** file bytes; there are no filename exclusions/imports or stat
hash shortcuts. Frames are length-delimited canonical JSON with a versioned
domain marker. Model identities are ordinary SHA-256 of the model bytes, matching
Generation's content identity contract. Names, size and mtime never replace hashing;
same-name/same-size/mtime-restored replacements still change identity.

All input mappings are copied before traversal. Every file read checks protection,
size and full inode/owner/mode/link/mtime/ctime identity before and after streaming.
Directories are checked after traversal; all measured paths are reopened through
their protected ancestors and checked again at the end. Ancestor topology/mode/
ownership is anchored separately from unrelated directory entries/mtime. The caller's
exclusive mutation protocol is still necessary for coherent measurement; these
checks do not implement an atomic filesystem snapshot or make unlocked writes safe.

## Bounds and failure

Defaults: 100,000 visited entries, 64 GiB streamed bytes, 900-second cooperative
deadline. Traversal depth is at most 64; constructor bounds cap configured limits.
Repeated roots across groups/models count again; there is no hash cache. Reads use
at most 1 MiB chunks and reject unexpected growth/truncation. Exact byte/entry
budgets may succeed; overflow, unknown ownership/ACL, missing roots, unsupported
file types, changed identity or exceeded deadline rejects the **entire** result.

The deadline is checked during opening/scanning/streaming/rechecking; an OS storage
call may still stall. A future publisher must run measurement with a subprocess
watchdog for a hard wall-clock bound. It must withdraw evidence before starting
this work and leave it unavailable after any failure or unknown lifetime.

Expected failures surface as `ContentMeasurementError` with a fixed public message;
source/config/model bytes and filesystem paths are not part of that message. The
chained internal exception is for trusted debugging only and must not be returned
through future presentation adapters. No record is restored or synthesized.

## Validation and remaining work

Offline fixtures exercise real hashing, same-name changes with restored mtime,
directory topology/hidden files, immutable results, writable content/ancestors,
ACL presence/query failure, foreign owners, links/special files, concurrent stream
changes, late inode/root/permission changes, exact budgets, deadline and depth,
missing inputs and separate runtime/authority identities. They make no GPU,
provider, database or production filesystem mutations.

This prerequisite does not complete #3. Coordinated systemd/maintenance guards
(including automatic/external restart), crash/lifetime fencing, runtime-owned
closure/interface collection, atomic publishing/refresh and real host/Workflow
acceptance remain required. Do not configure a publisher from this module alone.
