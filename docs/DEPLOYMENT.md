# Deployment contract

Install a reviewed wheel or an immutable source revision into the node's virtual
environment. The distribution is `flamoris-gpu-node-manager`, module
`flamoris_gpu_node_manager`, console script `gpu-node-manager`.
No old package/CLI alias is bundled. An overlay may provide a thin invocation
wrapper for its existing operational command, without copying core modules.

An overlay owns runtime YAML, systemd units, model paths, environment variables,
identity YAML, installation directories, privilege/network policy and host tests.
Pin the core revision/version explicitly; never track an unreviewed moving branch.
Keep credentials outside Git and outside dependency URLs.

All entry points must pass the same `--config-dir` and `--lock-path`; an existing
node should retain its existing lock during migration. Avoid deleting/recreating
the lock inode while any manager process remains alive. A supervisor's runtime
directory must be preserved across restarts when shared by multiple adapters.
Run mutation entry points under the same OS identity. The lock is opened without
following symlinks, must be a singly linked regular file owned by that identity,
and is restricted to mode 0600 in place, including legacy files. Keep its parent
directory protected from untrusted writers; do not replace an existing inode.

The Web listener validates one exact Host authority before routing any request.
Use its bound hostname/address or localhost/127.0.0.1 and the actual listening port;
wildcard bind addresses do not disable this check. Reverse proxies must enforce
authentication or a verified source allowlist before rewriting Host, for example
`proxy_set_header Host 127.0.0.1:8090;` for the default listener. Never expose the
unauthenticated control surface merely by forwarding to loopback. The intent
header prevents ordinary cross-origin browser mutations; it is not access control.

Example invocation shape (replace paths with verified deployment values):

```bash
gpu-node-manager --config-dir /etc/flamoris-gpu-node-manager/runtimes \
  --identity-config /etc/flamoris-gpu-node-manager/identity.yaml \
  --lock-path /run/flamoris-gpu-node-manager/transition.lock serve
```

When upgrading an older application, stop manager entry points before replacing
the environment, keep a rollback environment and config, then restart all entry
points together. Do not run two profile registries/lock namespaces on one GPU.
The renamed HTTP mutation header must be updated in external clients. MCP tool
names, `runtime_id` arguments and runtime IDs remain unchanged. Identity fields
in system status are additive. The bundled Web UI uses the new header.

Before rollout: validate profiles and ownership matchers, package assets, shared
lock path, permission boundaries and health semantics. Then verify a real
stop/release/start/health transition and rollback on the target host. Unit tests
cannot certify driver resource release or a host's actual service definitions.

Publication checks cover source, tests, docs, examples and package contents. Keep
real node inventories and deployment information in their private overlay.
