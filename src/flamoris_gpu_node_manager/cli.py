"""Recovery-oriented CLI backed only by the shared runtime authority."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from flamoris_gpu_node_manager.bootstrap import build_manager
from flamoris_gpu_node_manager.domain.errors import NodeManagerError
from flamoris_gpu_node_manager.http_server import serve
from flamoris_gpu_node_manager.identity import load_identity
from flamoris_gpu_node_manager.infrastructure.locking import DEFAULT_TRANSITION_LOCK_PATH
from flamoris_gpu_node_manager.infrastructure.telemetry import AmdGpuTelemetryAdapter
from flamoris_gpu_node_manager.mcp_server import serve_mcp
from flamoris_gpu_node_manager.serialization import profile_to_dict


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _port(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from exc
    if not 1 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("must be an integer from 1 to 65535")
    return parsed


def _mcp_host(value: str) -> str:
    if not value or len(value) > 253 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
        raise argparse.ArgumentTypeError("MCP host must be a hostname or unbracketed IP")
    return value


def _mcp_path(value: str) -> str:
    if value != "/" and (
        not re.fullmatch(r"/[A-Za-z0-9_.~-]+(?:/[A-Za-z0-9_.~-]+)*", value)
        or any(segment in {".", ".."} for segment in value.split("/"))
    ):
        raise argparse.ArgumentTypeError("MCP path must be a literal absolute URL path")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gpu-node-manager", description="Manage configured GPU node runtimes"
    )
    parser.add_argument("--identity-config", type=Path, help="node/manager identity YAML file")
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("/etc/flamoris-gpu-node-manager/runtimes"),
        help="directory containing validated runtime YAML profiles",
    )
    parser.add_argument(
        "--lock-path",
        type=Path,
        default=DEFAULT_TRANSITION_LOCK_PATH,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--lock-timeout", type=_positive_float, default=5.0, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="group", required=True)

    runtime = commands.add_parser("runtime", help="inspect or change configured runtimes")
    runtime_commands = runtime.add_subparsers(dest="command", required=True)
    runtime_commands.add_parser("list", help="list configured runtimes")
    runtime_status = runtime_commands.add_parser("status", help="show runtime status")
    runtime_status.add_argument("id", nargs="?")
    runtime_activate = runtime_commands.add_parser("activate", help="activate a runtime")
    runtime_activate.add_argument("id")
    runtime_stop = runtime_commands.add_parser("stop", help="stop a runtime")
    runtime_stop.add_argument("id", nargs="?")

    system = commands.add_parser("system", help="inspect manager system state")
    system_commands = system.add_subparsers(dest="command", required=True)
    system_commands.add_parser("status", help="show all runtime and anomaly state")

    server = commands.add_parser("serve", help="run the local HTTP API and Web UI")
    server.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    server.add_argument("--port", type=_port, default=8090, help="HTTP bind port")
    mcp = commands.add_parser("mcp", help="run bounded MCP over stdio or Streamable HTTP")
    mcp.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    mcp.add_argument("--host", type=_mcp_host, default="127.0.0.1")
    mcp.add_argument("--port", type=_port, default=8766)
    mcp.add_argument("--mcp-path", type=_mcp_path, default="/mcp")
    return parser


def run(
    argv: Sequence[str] | None = None,
    *,
    manager_factory: Any = build_manager,
    server_runner: Any = serve,
    mcp_runner: Any = serve_mcp,
    telemetry_factory: Any = AmdGpuTelemetryAdapter,
) -> int:
    args = _parser().parse_args(argv)
    try:
        identity = load_identity(args.identity_config)
        manager = manager_factory(
            args.config_dir,
            lock_path=args.lock_path,
            lock_timeout=args.lock_timeout,
        )
        manager.reconstruct()
        if args.group == "serve":
            server_runner(
                manager,
                host=args.host,
                port=args.port,
                telemetry=telemetry_factory(),
                identity=identity,
            )
            return 0
        if args.group == "mcp":
            if args.transport == "stdio":
                mcp_runner(manager, telemetry=telemetry_factory(), identity=identity)
            else:
                mcp_runner(
                    manager,
                    telemetry=telemetry_factory(),
                    identity=identity,
                    transport=args.transport,
                    host=args.host,
                    port=args.port,
                    mcp_path=args.mcp_path,
                )
            return 0
        result: object
        if args.group == "runtime" and args.command == "list":
            result = [
                profile_to_dict(profile, manager.runtime_status(profile.id))
                for profile in manager.list_runtimes()
            ]
        elif args.group == "runtime" and args.command == "status":
            result = (
                manager.runtime_status(args.id).to_dict()
                if args.id
                else manager.system_status().to_dict()
            )
        elif args.group == "runtime" and args.command == "activate":
            result = manager.activate(args.id).to_dict()
        elif args.group == "runtime" and args.command == "stop":
            result = [status.to_dict() for status in manager.stop(args.id)]
        elif args.group == "system" and args.command == "status":
            result = {**manager.system_status().to_dict(), **identity.to_dict()}
        else:  # pragma: no cover - argparse makes this unreachable
            raise AssertionError("unhandled command")
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (NodeManagerError, OSError) as exc:
        print(
            json.dumps(
                {"error": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
