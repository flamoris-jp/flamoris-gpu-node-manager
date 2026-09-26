from __future__ import annotations

from pathlib import Path

from flamoris_gpu_node_manager.infrastructure.telemetry import AmdGpuTelemetryAdapter


def write_metric(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def test_amd_sysfs_telemetry_parses_available_fields(tmp_path: Path) -> None:
    device = tmp_path / "card1" / "device"
    write_metric(device / "gpu_busy_percent", "37\n")
    write_metric(device / "mem_info_vram_used", "4294967296\n")
    write_metric(device / "mem_info_vram_total", "17179869184\n")
    write_metric(device / "hwmon" / "hwmon2" / "temp1_input", "45500\n")
    write_metric(device / "hwmon" / "hwmon2" / "power1_average", "78250000\n")

    result = AmdGpuTelemetryAdapter(device_path=device).read()

    assert result == {
        "available": True,
        "gpu_utilization_percent": 37.0,
        "vram_used_bytes": 4_294_967_296,
        "vram_total_bytes": 17_179_869_184,
        "temperature_celsius": 45.5,
        "power_watts": 78.25,
        "error": None,
    }


def test_partial_telemetry_degrades_to_null_fields(tmp_path: Path) -> None:
    device = tmp_path / "card0" / "device"
    write_metric(device / "gpu_busy_percent", "8\n")

    result = AmdGpuTelemetryAdapter(device_path=device).read()

    assert result["available"] is True
    assert result["gpu_utilization_percent"] == 8.0
    assert result["vram_used_bytes"] is None
    assert result["temperature_celsius"] is None
    assert result["error"] is None


def test_invalid_or_unreadable_fields_do_not_raise(tmp_path: Path) -> None:
    device = tmp_path / "card0" / "device"
    write_metric(device / "gpu_busy_percent", "not-a-number\n")

    result = AmdGpuTelemetryAdapter(device_path=device).read()

    assert result["available"] is False
    assert result["gpu_utilization_percent"] is None
    assert "invalid number" in str(result["error"])


def test_adapter_discovers_first_amd_card(tmp_path: Path) -> None:
    write_metric(tmp_path / "card0" / "device" / "vendor", "0x8086\n")
    write_metric(tmp_path / "card1" / "device" / "vendor", "0x1002\n")
    write_metric(tmp_path / "card1" / "device" / "gpu_busy_percent", "61\n")

    result = AmdGpuTelemetryAdapter(drm_root=tmp_path).read()

    assert result["available"] is True
    assert result["gpu_utilization_percent"] == 61.0


def test_missing_amd_card_returns_unavailable(tmp_path: Path) -> None:
    result = AmdGpuTelemetryAdapter(drm_root=tmp_path).read()

    assert result["available"] is False
    assert result["error"] == "AMD GPU telemetry device not found"
