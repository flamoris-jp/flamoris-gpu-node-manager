# FLAMORIS GPU Node Manager

A local Linux service for safely handing one GPU resource class between configured
AI runtimes. CLI, HTTP/Web and MCP use the same `RuntimeManager` implementation and
host-wide transition lock. systemd supervises runtime processes.
Each process has its own manager and transition progress. Status is reconstructed
from host services/processes; a missing local transition does not mean another
entry point is idle. The shared lock serializes mutations across processes.

## Release status

**Stable — v1.0** (Python package version `1.0.0`).

The stable scope is configured Linux/systemd runtime lifecycle control, validated
profiles, owned-process release, health checks, and the shared CLI/HTTP/MCP
interfaces. Incompatible public interface or configuration changes require a new
major release.

A stable manager does not certify every runtime or GPU configuration. Deployment
overlays must verify their own services, health contracts, process matchers and
privilege/network boundaries. Optional lifecycle features and provider-specific
integration remain separate work.

## Quick start

Requires Python 3.11+ and Linux with systemd/procfs for actual runtime control.
Tests use synthetic runtimes and do not require a GPU or systemd.

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/gpu-node-manager --help
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy src
```

`config/examples/` contains disabled illustrative HTTP and TCP profiles. Copy and
adapt them to installed services; verify release matchers against actual processes
before enabling. No runtime or model is installed by this package.

The CLI defaults to `/etc/flamoris-gpu-node-manager/runtimes` and the lock
`/run/flamoris-gpu-node-manager/transition.lock`. All adapters on a node **must use
identical profile directories and lock paths**, including during upgrades.
These are defaults for a new deployment, not claims about an existing host.

```bash
.venv/bin/gpu-node-manager --config-dir config/examples runtime list
.venv/bin/gpu-node-manager --config-dir config/examples system status
.venv/bin/gpu-node-manager --config-dir config/examples serve
.venv/bin/gpu-node-manager --config-dir config/examples mcp
.venv/bin/gpu-node-manager --config-dir config/examples mcp --transport streamable-http
```

Web defaults to `127.0.0.1:8090`; MCP HTTP defaults to `127.0.0.1:8766/mcp`.
Startup observes state and never activates a runtime. Actual operations require
appropriate host permissions. HTTP/MCP have no application authentication:
keep listeners on loopback and restrict local callers in privileged deployments.
A deployment overlay owns service accounts, privilege grants and network policy.
Web requests require exactly one trusted Host: the bound host, `127.0.0.1`, or
`localhost`, with the listening port (port 80 may be omitted). Wildcard bind
addresses are not trusted hosts. A reverse proxy must rewrite Host to the backend
authority **after** enforcing client authentication or a source allowlist.
Host validation and the intent header are not user authentication.

## Identity configuration

```yaml
node:
  id: render
  display_name: Render Node
manager:
  display_name: Render Manager
```

Pass `--identity-config path/to/identity.yaml` **before** the subcommand. Omission
uses `local` / `GPU Node` / `GPU Node Manager`; an explicitly missing or invalid
file fails startup. Unknown and duplicate fields are rejected. Names are bounded
plain text, not HTML or configuration templates. Restart to load changes.

Identity appears in the Web title/header/status label, CLI `system status`, MCP
server name and `system.status`. It never changes runtime IDs, endpoints, lock
paths, service selection or transition behavior.

## API and boundaries

- CLI: `runtime list`, `runtime status [id]`, `runtime activate id`,
  `runtime stop [id]`, `system status`, `serve`, `mcp`.
- HTTP: `GET /api/status`, `/api/runtimes`, `/api/runtimes/{id}`, `/api/telemetry`;
  `POST /api/runtimes/{id}/activate` and `/stop` with an empty body and
  `X-GPU-Node-Manager-Intent: runtime-mutation` header.
- MCP: `runtime.list`, `runtime.status`, `runtime.activate`, `runtime.stop`,
  `system.status`; mutation arguments use `runtime_id`.

No arbitrary shell/service control, model downloads or generation-job management.
See [architecture](docs/ARCHITECTURE.md) and [deployment contract](docs/DEPLOYMENT.md).

Profiles may optionally set `evidence_record` to a protected, pre-provisioned JSON
path. Managed transitions then lock and withdraw old Workflow qualification
evidence before any service mutation. This is **invalidation only**: it does not
measure/publish evidence, cover external writers or make a Workflow ready.
Existing profiles are unchanged when the field is omitted. See
[runtime evidence boundary](docs/RUNTIME_EVIDENCE.md) before provisioning it.

Portable trusted publication, expiry renewal and ComfyUI measurement adapters
are available for reviewed deployment composition. They are opt-in Python ports;
default entry points retain invalidation-only behavior. Actual initialization,
protected content, lifetime and all-writer integration must still be established
before enabling qualification. See [lifecycle composition](docs/EVIDENCE_LIFECYCLE.md).

## FLAMORIS

FLAMORIS is open-source software for creative work and AI-native production.

Use it however you like.

Commercial use is welcome and does not require permission. If you'd like, we'd be happy to hear what you used FLAMORIS for. This is completely optional.

FLAMORIS software is provided as-is. We do not provide individual support or guaranteed assistance.

If you run into trouble, let your AI assistant read the repository, documentation, Issues, tests, logs, and source code and help you solve it.

If FLAMORIS helps you or you find it interesting, your support helps fund development and keeps the project growing. 🌱

<sub>Mostly GPU bills.</sub>

---

## FLAMORISについて

FLAMORISは、クリエイティブ制作とAIネイティブな制作環境のためのオープンソースソフトウェアです。

勝手に使ってください。改造しても、組み込んでも、面白いものや変なものを作ってもOKです。

商用作品や製品で使う場合も許可は不要です。もしよければ「こんなのに使ったよ」と教えてもらえるとうれしいです。もちろん強制ではありません。

FLAMORISのソフトウェアは現状のまま提供されます。個別サポートや動作保証はありません。

困ったときは、README、ドキュメント、Issue、テスト、ログ、ソースコードをあなたのAIに読ませて、自己サポートしてもらってください。

もしお役に立てたり、面白いと思っていただけたなら、開発費用をご支援いただけるとうれしいです。FLAMORISは元気になって育ちます。🌱

<sub>主にGPU代とか。</sub>

## License

Code in this repository is licensed under the [Apache License 2.0](LICENSE), unless otherwise noted.

AI models, model weights, datasets, media, and other non-code assets may use separate licenses. State their applicable licenses alongside those assets.
