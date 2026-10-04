"""Bounded structural schemas from reviewed, initialized legacy node classes."""

from __future__ import annotations

import time
from collections.abc import Mapping
from enum import Enum
from typing import Any, cast

from .comfyui_capture import NodeBinding
from .comfyui_measurement import _Budget, _json_copy

# Only these exact built-in class bindings have reviewed inventory semantics.
_INVENTORIES = {
    ("nodes", "LoadImage", "required", "image"): "managed-image",
    ("nodes", "LoadImageMask", "required", "image"): "managed-image",
    ("nodes", "LoadImageOutput", "required", "image"): "output-image",
    ("nodes", "CheckpointLoaderSimple", "required", "ckpt_name"): "checkpoint",
}


def _type_name(value: object, budget: _Budget) -> str:
    # ComfyUI's IO enum subclasses str. Bypass enum/custom __str__ implementations.
    if isinstance(value, str) and isinstance(value, Enum):
        value = str.__str__(value)
    return budget.text(value)


def normalize_legacy_schema(
    binding: NodeBinding, definition: Mapping[str, object], *, deadline: float
) -> dict[str, object]:
    """Normalize an actual local class schema; unsupported semantics raise.

    This is not an object_info parser or a client-facing manifest import. Input
    order, fixed enums, defaults and every declared option remain identity-bearing.
    """
    budget = _Budget(deadline)
    if type(definition) is not dict or set(definition) != {
        "input",
        "function",
        "input_is_list",
        "output",
        "output_is_list",
        "output_name",
        "output_node",
        "has_intermediate_output",
    }:
        raise ValueError("unsupported local node schema")
    raw_inputs = definition["input"]
    if type(raw_inputs) is not dict or not set(raw_inputs) <= {"required", "optional", "hidden"}:
        raise ValueError("unsupported input roles")
    inputs: dict[str, object] = {}
    order: dict[str, object] = {}
    for role, raw_fields in raw_inputs.items():
        budget.text(role)
        if type(raw_fields) is not dict:
            raise ValueError("unsupported input fields")
        fields: dict[str, object] = {}
        order[role] = list(raw_fields)
        for name, entry in raw_fields.items():
            budget.text(name)
            if role == "hidden" and type(entry) is str:
                fields[name] = {"type": budget.text(entry), "options": {}}
                continue
            if type(entry) is not tuple or len(entry) not in (1, 2):
                raise ValueError("unsupported input declaration")
            kind = entry[0]
            inventory = _INVENTORIES.get((binding.module, binding.qualname, role, name))
            if type(kind) in (list, tuple):
                # Enumerated choices are exact strings in the supported contract.
                choices = [_type_name(item, budget) for item in kind]
                normalized_type: object = (
                    {"inventory": inventory, "type": "STRING"}
                    if inventory is not None
                    else {"enum": choices}
                )
                if inventory is None and not choices:
                    raise ValueError("empty fixed enum")
            else:
                if inventory is not None and not (inventory == "output-image" and kind == "COMBO"):
                    raise ValueError("reviewed inventory is not an enum")
                normalized_type = _type_name(kind, budget)
                if normalized_type == "COMBO":
                    if inventory is None:
                        raise ValueError("unreviewed dynamic combo")
                    normalized_type = {"inventory": inventory, "type": "STRING"}
            options = entry[1] if len(entry) == 2 else {}
            if type(options) is not dict:
                raise ValueError("unsupported input options")
            fields[name] = {"type": normalized_type, "options": _json_copy(options, budget)}
        inputs[role] = fields
    outputs = definition["output"]
    names = definition["output_name"]
    lists = definition["output_is_list"]
    if any(type(value) not in (tuple, list) for value in (outputs, names, lists)):
        raise ValueError("unsupported output declaration")
    output_types = [_type_name(value, budget) for value in cast(list[object], outputs)]
    output_names = [_type_name(value, budget) for value in cast(list[object], names)]
    if len(output_types) != len(output_names) or len(output_types) != len(
        cast(list[object], lists)
    ):
        raise ValueError("inconsistent output declaration")
    booleans = (
        definition["input_is_list"],
        definition["output_node"],
        definition["has_intermediate_output"],
        *cast(list[object], lists),
    )
    if any(type(value) is not bool for value in booleans):
        raise ValueError("unsupported list or output-node semantics")
    return {
        "input": inputs,
        "input_order": order,
        "function": budget.text(definition["function"]),
        "input_is_list": definition["input_is_list"],
        "output": output_types,
        "output_name": output_names,
        "output_is_list": list(cast(list[object], lists)),
        "output_node": definition["output_node"],
        "has_intermediate_output": definition["has_intermediate_output"],
    }


def read_legacy_node_schema(cls: type[Any], binding: NodeBinding, *, deadline: float) -> object:
    """Invoke reviewed local INPUT_TYPES once; never instantiate/execute a node.

    V3/dynamic classes need a separately reviewed adapter. The caller must cover
    schema methods with a watchdog and the coordinated mutation boundary.
    """
    if time.monotonic() >= deadline or hasattr(cls, "GET_NODE_INFO_V1"):
        raise ValueError("unsupported or expired schema acquisition")
    output = cls.RETURN_TYPES
    result = normalize_legacy_schema(
        binding,
        {
            "input": cls.INPUT_TYPES(),
            "function": cls.FUNCTION,
            "input_is_list": getattr(cls, "INPUT_IS_LIST", False),
            "output": output,
            "output_is_list": getattr(cls, "OUTPUT_IS_LIST", [False] * len(output)),
            "output_name": getattr(cls, "RETURN_NAMES", output),
            "output_node": getattr(cls, "OUTPUT_NODE", False),
            "has_intermediate_output": getattr(cls, "HAS_INTERMEDIATE_OUTPUT", False),
        },
        deadline=deadline,
    )
    if time.monotonic() >= deadline:
        raise ValueError("expired schema acquisition")
    return result
