from __future__ import annotations

import copy
import json
import os
import socket
import struct
import sys
import threading
import time
from dataclasses import replace
from enum import Enum
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

from flamoris_gpu_node_manager.infrastructure import comfyui_closure as closure
from flamoris_gpu_node_manager.infrastructure import comfyui_runtime as runtime
from flamoris_gpu_node_manager.infrastructure import comfyui_schema as schema
from flamoris_gpu_node_manager.infrastructure import observation_transport as transport
from flamoris_gpu_node_manager.infrastructure.comfyui_capture import NodeBinding
from flamoris_gpu_node_manager.infrastructure.comfyui_measurement import (
    ComfyUIMeasurementError,
)
from tests.unit.test_comfyui_capture import runtime as capture_fixture
from tests.unit.test_comfyui_measurement import initialized as observation_fixture

capture_fixture = capture_fixture
observation_fixture = observation_fixture


@pytest.fixture
def unix_available():
    try:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.close()
    except PermissionError:
        pytest.skip("execution sandbox disallows UNIX sockets; exercised on Linux CI")


def definition():
    return {
        "input": {
            "required": {
                "image": (["one.png"], {"image_upload": True}),
                "amount": ("FLOAT", {"default": 1.0, "min": 0, "max": 2}),
            },
            "optional": {"mode": (["fixed", "other"],)},
            "hidden": {"prompt": "PROMPT"},
        },
        "function": "run",
        "input_is_list": False,
        "output": ("IMAGE", "MASK"),
        "output_name": ("image", "mask"),
        "output_is_list": [False, True],
        "output_node": True,
        "has_intermediate_output": False,
    }


def normalize(value, module="nodes", name="LoadImage"):
    return schema.normalize_legacy_schema(
        NodeBinding(module, name, Path("/synthetic/nodes.py")), value, deadline=time.monotonic() + 5
    )


@pytest.mark.parametrize("node", ["LoadImage", "LoadImageMask", "LoadImageOutput"])
def test_only_reviewed_image_inventories_are_stable(node):
    first = definition()
    second = copy.deepcopy(first)
    second["input"]["required"]["image"] = (["two.png", "other.png"], {"image_upload": True})
    assert normalize(first, name=node) == normalize(second, name=node)
    assert normalize(first, name="OtherImage") != normalize(second, name="OtherImage")
    assert normalize(first, module="custom") != normalize(second, module="custom")


def test_checkpoint_inventory_exact_builtin_binding():
    first = definition()
    first["input"]["required"] = {"ckpt_name": (["first.bin"],)}
    second = copy.deepcopy(first)
    second["input"]["required"]["ckpt_name"] = (["second.bin"],)
    assert normalize(first, name="CheckpointLoaderSimple") == normalize(
        second, name="CheckpointLoaderSimple"
    )
    assert normalize(first, name="OtherLoader") != normalize(second, name="OtherLoader")


@pytest.mark.parametrize(
    "change",
    [
        "enum",
        "default",
        "constraint",
        "order",
        "role",
        "function",
        "input_list",
        "output_list",
        "output_order",
        "output_node",
    ],
)
def test_execution_semantics_remain_identity_bearing(change):
    first = definition()
    second = copy.deepcopy(first)
    if change == "enum":
        second["input"]["optional"]["mode"][0].reverse()
    elif change in {"default", "constraint"}:
        second["input"]["required"]["amount"][1]["default" if change == "default" else "min"] = 0.5
    elif change == "order":
        second["input"]["required"] = dict(reversed(list(second["input"]["required"].items())))
    elif change == "role":
        second["input"]["optional"]["amount"] = second["input"]["required"].pop("amount")
    elif change == "function":
        second["function"] = "other"
    elif change == "input_list":
        second["input_is_list"] = True
    elif change == "output_list":
        second["output_is_list"] = [True, True]
    elif change == "output_order":
        second["output"] = tuple(reversed(second["output"]))
    elif change == "output_node":
        second["output_node"] = False
    assert normalize(first) != normalize(second)


@pytest.mark.parametrize(
    "case",
    [
        "role",
        "declaration",
        "object",
        "nan",
        "output_count",
        "bool",
        "combo",
        "empty_enum",
        "deadline",
        "depth",
    ],
)
def test_unknown_or_unbounded_schema_fails(case):
    value = definition()
    if case == "role":
        value["input"]["dynamic"] = {}
    elif case == "declaration":
        value["input"]["required"]["amount"] = ["FLOAT"]
    elif case in {"object", "nan", "depth"}:
        option = object() if case == "object" else float("nan")
        if case == "depth":
            option = {}
            for _ in range(70):
                option = {"next": option}
        value["input"]["required"]["amount"][1]["default"] = option
    elif case == "output_count":
        value["output_is_list"] = [False]
    elif case == "bool":
        value["input_is_list"] = 1
    elif case == "combo":
        value["input"]["required"]["amount"] = ("COMBO",)
    elif case == "empty_enum":
        value["input"]["optional"]["mode"] = ([],)
    with pytest.raises(ValueError):
        if case == "deadline":
            schema.normalize_legacy_schema(
                NodeBinding("nodes", "LoadImage", Path("/nodes.py")),
                value,
                deadline=time.monotonic() - 1,
            )
        else:
            normalize(value)


def test_string_io_enum_is_normalized_without_custom_string_hook():
    class IO(str, Enum):  # noqa: UP042 - exercise the provider's existing IO shape
        IMAGE = "IMAGE"

        def __str__(self):
            raise AssertionError("enum string hook")

    value = definition()
    value["output"] = (IO.IMAGE, "MASK")
    assert normalize(value)["output"] == ["IMAGE", "MASK"]


def test_runtime_port_initialization_revisions_and_no_node_execution(
    capture_fixture, observation_fixture
):
    modules, nodes, _, root = capture_fixture

    class LoadImage:
        __module__ = "nodes"
        RETURN_TYPES = ("IMAGE",)
        FUNCTION = "execute"

        @classmethod
        def INPUT_TYPES(cls):
            return {"required": {"image": (["one.png"], {"image_upload": True})}}

        def __init__(self):
            raise AssertionError("node must not be instantiated")

        def execute(self):
            raise AssertionError("node execution")

    LoadImage.__qualname__ = "LoadImage"
    nodes.NODE_CLASS_MAPPINGS = {"LoadImage": LoadImage}
    port = runtime.LocalInitializedRuntimePort(lambda: observation_fixture.closure)
    with patch.object(sys, "modules", modules), patch.object(sys, "path", [str(root)]):
        with pytest.raises(ComfyUIMeasurementError):
            port.observe(deadline=time.monotonic() + 5)
        port.mark_initialized()
        first = port.observe(deadline=time.monotonic() + 5)
        assert port.observe(deadline=time.monotonic() + 5).snapshot_token == first.snapshot_token
        # Same normalized upload schema, changed actual class method object.
        LoadImage.INPUT_TYPES = classmethod(
            lambda cls: {"required": {"image": (["new.png"], {"image_upload": True})}}
        )
        second = port.observe(deadline=time.monotonic() + 5)
        assert second.node_interfaces == first.node_interfaces
        assert second.snapshot_token != first.snapshot_token
        nodes.extra_binding = object()
        assert port.observe(deadline=time.monotonic() + 5).snapshot_token != second.snapshot_token
        port.invalidate()
        with pytest.raises(ComfyUIMeasurementError):
            port.observe(deadline=time.monotonic() + 5)
        port.mark_initialized()
        assert port.observe(deadline=time.monotonic() + 5).snapshot_token != second.snapshot_token


def test_class_or_module_mutation_during_schema_acquisition_rejects(
    capture_fixture, observation_fixture
):
    modules, nodes, _, root = capture_fixture
    cls = nodes.NODE_CLASS_MAPPINGS["Image"]

    def reader(*args, **kwargs):
        cls.FUNCTION = "changed-during-acquisition"
        return {"synthetic": True}

    port = runtime.LocalInitializedRuntimePort(
        lambda: observation_fixture.closure, schema_reader=reader
    )
    port.mark_initialized()
    with (
        patch.object(sys, "modules", modules),
        patch.object(sys, "path", [str(root)]),
        pytest.raises(ComfyUIMeasurementError),
    ):
        port.observe(deadline=time.monotonic() + 5)


def test_modern_schema_and_private_errors_fail_closed():
    class Modern:
        GET_NODE_INFO_V1 = None

    with pytest.raises(ValueError):
        schema.read_legacy_node_schema(
            Modern, NodeBinding("plugin", "Modern", Path("/node.py")), deadline=time.monotonic() + 5
        )
    port = runtime.LocalInitializedRuntimePort(lambda: (_ for _ in ()).throw(ValueError("secret")))
    with pytest.raises(
        ComfyUIMeasurementError, match="^initialized runtime measurement unavailable$"
    ):
        port.observe(deadline=time.monotonic() + 5)


class Observations:
    def __init__(self, observation):
        self.observation = observation

    def observe(self, *, deadline):
        return self.observation


def test_wire_roundtrip_exact_peer_and_no_input_inventory(
    tmp_path, observation_fixture, unix_available
):
    observed = replace(
        observation_fixture,
        bindings=replace(observation_fixture.bindings, pid=os.getpid(), uid=os.geteuid()),
    )
    endpoint = tmp_path / "observation.sock"
    with transport.UnixRuntimeObservationServer(
        endpoint, Observations(observed), authority_uid=os.geteuid()
    ):
        client = transport.UnixInitializedRuntimeClient(endpoint, pid=os.getpid(), uid=os.geteuid())
        assert client.observe(deadline=time.monotonic() + 5) == observed
        with pytest.raises(ComfyUIMeasurementError):
            transport.UnixInitializedRuntimeClient(
                endpoint, pid=os.getpid() + 1, uid=os.geteuid()
            ).observe(deadline=time.monotonic() + 5)
    assert not endpoint.exists()


@pytest.mark.parametrize("case", ["duplicate", "bool_version", "nan", "path", "extra", "uid"])
def test_wire_untrusted_values_reject(observation_fixture, case):
    encoded = transport._encode(observation_fixture)
    value = json.loads(encoded)
    if case == "duplicate":
        encoded = b'{"version":1,"version":1}'
    else:
        if case == "bool_version":
            value["version"] = True
        elif case == "nan":
            value["node_interfaces"]["Image"]["bad"] = float("nan")
        elif case == "path":
            value["cwd"] = "/root/../other"
        elif case == "extra":
            value["private"] = "unexpected"
        elif case == "uid":
            value["uid"] = False
        encoded = json.dumps(value).encode()
    with pytest.raises((ValueError, ComfyUIMeasurementError)):
        transport._decode(encoded, deadline=time.monotonic() + 5)


@pytest.mark.parametrize(
    "response", [b"", struct.pack("!I", 20) + b"partial", struct.pack("!I", 17 * 1024**2)]
)
def test_client_eof_partial_and_oversized_frames_are_bounded(tmp_path, response, unix_available):
    endpoint = tmp_path / "partial.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(endpoint))
        listener.listen()

        def serve():
            connection, _ = listener.accept()
            with connection:
                connection.sendall(response)
                time.sleep(0.2)

        thread = threading.Thread(target=serve)
        thread.start()
        client = transport.UnixInitializedRuntimeClient(endpoint, pid=os.getpid(), uid=os.geteuid())
        started = time.monotonic()
        with pytest.raises(ComfyUIMeasurementError):
            client.observe(deadline=started + 0.05)
        assert time.monotonic() - started < 0.5
        thread.join()


def test_server_preserves_existing_path_and_checks_authority(
    tmp_path, observation_fixture, unix_available
):
    endpoint = tmp_path / "existing"
    endpoint.write_text("preserve")
    server = transport.UnixRuntimeObservationServer(
        endpoint, Observations(observation_fixture), authority_uid=os.geteuid()
    )
    with pytest.raises(OSError):
        server.start()
    assert endpoint.read_text() == "preserve"
    other = tmp_path / "restricted.sock"
    with (
        transport.UnixRuntimeObservationServer(
            other, Observations(observation_fixture), authority_uid=os.geteuid() + 1
        ),
        pytest.raises(ComfyUIMeasurementError),
    ):
        transport.UnixInitializedRuntimeClient(other, pid=os.getpid(), uid=os.geteuid()).observe(
            deadline=time.monotonic() + 5
        )


def test_reviewed_closure_copies_plan_observes_lookup_and_defaults_unavailable(tmp_path):
    groups = {
        "core": {"source": tmp_path},
        "dependencies": {"libs": tmp_path},
        "config": {"settings": tmp_path / "settings"},
    }
    plan = closure.ReviewedRuntimeClosure(groups, ("nested/model.bin",))
    groups["core"].clear()
    assert plan.groups["core"]
    folders = ModuleType("folder_paths")
    calls = []
    folders.get_full_path = lambda kind, name: calls.append((kind, name)) or str(tmp_path / name)
    with (
        patch.dict(sys.modules, {"folder_paths": folders}),
        patch.object(closure, "mapped_native_libraries", return_value=(tmp_path / "library.so",)),
    ):
        observed = plan()
    assert calls == [("checkpoints", "nested/model.bin")]
    assert observed.checkpoints == {"nested/model.bin": tmp_path / "nested/model.bin"}
    assert observed.complete is False and observed.mutation_coverage_complete is False


@pytest.mark.parametrize("name", ["../model.bin", "/model.bin", "a//b.bin", "a\\b.bin"])
def test_invalid_closure_checkpoint_names(tmp_path, name):
    with pytest.raises(ValueError):
        closure.ReviewedRuntimeClosure(
            {g: {"root": tmp_path} for g in ("core", "dependencies", "config")}, (name,)
        )


def test_native_mappings_reject_unmeasurable_code(tmp_path):
    maps = b"1000-2000 r-xp 00000000 00:00 12 /synthetic/lib.so\n2000-3000 r-xp 0 00:00 0 [vdso]\n"
    from unittest.mock import mock_open

    with patch("builtins.open", mock_open(read_data=maps)):
        assert closure.mapped_native_libraries(deadline=time.monotonic() + 5) == (
            Path("/synthetic/lib.so"),
        )
    for bad in [
        b"1000-2000 r-xp 0 00:00 0\n",
        maps.replace(b"/synthetic/lib.so", b"/lib.so (deleted)"),
        maps.replace(b"/synthetic/lib.so", b"/lib\\040name.so"),
    ]:
        with patch("builtins.open", mock_open(read_data=bad)), pytest.raises(ValueError):
            closure.mapped_native_libraries(deadline=time.monotonic() + 5)
