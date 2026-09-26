"""Best-effort AMD GPU telemetry, deliberately separate from runtime ownership."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class GpuTelemetry:
    available: bool
    gpu_utilization_percent: float | None = None
    vram_used_bytes: int | None = None
    vram_total_bytes: int | None = None
    temperature_celsius: float | None = None
    power_watts: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class AmdGpuTelemetryAdapter:
    """Read stable amdgpu sysfs counters without invoking a privileged command."""

    def __init__(
        self,
        *,
        drm_root: Path = Path("/sys/class/drm"),
        device_path: Path | None = None,
        reader: Callable[[Path], str] | None = None,
    ) -> None:
        self._drm_root = drm_root
        self._device_path = device_path
        self._reader = reader or self._read_text

    def read(self) -> dict[str, object]:
        try:
            device = self._device_path or self._find_amd_device()
        except OSError as exc:
            return GpuTelemetry(available=False, error=self._message(exc)).to_dict()
        if device is None:
            return GpuTelemetry(
                available=False, error="AMD GPU telemetry device not found"
            ).to_dict()

        errors: list[str] = []
        utilization = self._optional_float(device / "gpu_busy_percent", errors)
        vram_used = self._optional_int(device / "mem_info_vram_used", errors)
        vram_total = self._optional_int(device / "mem_info_vram_total", errors)
        temperature = self._first_scaled(
            sorted(device.glob("hwmon/hwmon*/temp1_input")), 1_000, errors
        )
        power = self._first_scaled(
            sorted(device.glob("hwmon/hwmon*/power1_average")), 1_000_000, errors
        )
        available = any(
            value is not None for value in (utilization, vram_used, vram_total, temperature, power)
        )
        return GpuTelemetry(
            available=available,
            gpu_utilization_percent=utilization,
            vram_used_bytes=vram_used,
            vram_total_bytes=vram_total,
            temperature_celsius=temperature,
            power_watts=power,
            error="; ".join(errors) if errors else None,
        ).to_dict()

    def _find_amd_device(self) -> Path | None:
        for card in sorted(self._drm_root.glob("card[0-9]*")):
            device = card / "device"
            try:
                vendor = self._reader(device / "vendor").strip().lower()
            except FileNotFoundError:
                continue
            except OSError:
                continue
            if vendor == "0x1002":
                return device
        return None

    def _optional_float(self, path: Path, errors: list[str]) -> float | None:
        value = self._optional_text(path, errors)
        if value is None:
            return None
        try:
            return float(value)
        except ValueError:
            errors.append(f"{path.name}: invalid number")
            return None

    def _optional_int(self, path: Path, errors: list[str]) -> int | None:
        value = self._optional_text(path, errors)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            errors.append(f"{path.name}: invalid integer")
            return None

    def _first_scaled(self, paths: list[Path], divisor: int, errors: list[str]) -> float | None:
        if not paths:
            return None
        value = self._optional_float(paths[0], errors)
        return None if value is None else value / divisor

    def _optional_text(self, path: Path, errors: list[str]) -> str | None:
        try:
            return self._reader(path).strip()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as exc:
            errors.append(f"{path.name}: {self._message(exc)}")
            return None

    @staticmethod
    def _read_text(path: Path) -> str:
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _message(exc: BaseException) -> str:
        return str(exc) or type(exc).__name__
