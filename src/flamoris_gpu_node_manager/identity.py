"""Small, immutable presentation configuration; never runtime ownership policy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from flamoris_gpu_node_manager.domain.errors import ProfileValidationError
from flamoris_gpu_node_manager.infrastructure.config import (
    _mapping,
    _strict_keys,
    _StrictSafeLoader,
)


def _display_name(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 120
        or not value.isprintable()
    ):
        raise ProfileValidationError(f"{field} must be 1–120 printable characters")
    return value


@dataclass(frozen=True)
class NodeIdentity:
    node_id: str = "local"
    node_display_name: str = "GPU Node"
    manager_display_name: str = "GPU Node Manager"

    def __post_init__(self) -> None:
        if not isinstance(self.node_id, str) or not re.fullmatch(
            r"[a-z][a-z0-9-]{0,62}", self.node_id
        ):
            raise ProfileValidationError("node.id must be a lowercase identifier (1–63 characters)")
        _display_name(self.node_display_name, "node.display_name")
        _display_name(self.manager_display_name, "manager.display_name")

    def to_dict(self) -> dict[str, dict[str, str]]:
        return {
            "node": {"id": self.node_id, "display_name": self.node_display_name},
            "manager": {"display_name": self.manager_display_name},
        }


def load_identity(path: Path | None = None) -> NodeIdentity:
    if path is None:
        return NodeIdentity()
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictSafeLoader)
    except (OSError, UnicodeError, yaml.YAMLError, TypeError) as exc:
        raise ProfileValidationError(f"cannot load identity {path}: {exc}") from exc
    data = _mapping(raw, "identity")
    _strict_keys(data, allowed={"node", "manager"}, required=set(), location="identity")
    node = _mapping(data.get("node", {}), "node")
    manager = _mapping(data.get("manager", {}), "manager")
    _strict_keys(node, allowed={"id", "display_name"}, required=set(), location="node")
    _strict_keys(manager, allowed={"display_name"}, required=set(), location="manager")
    return NodeIdentity(
        node_id=node.get("id", "local"),
        node_display_name=node.get("display_name", "GPU Node"),
        manager_display_name=manager.get("display_name", "GPU Node Manager"),
    )


DEFAULT_IDENTITY = NodeIdentity()
