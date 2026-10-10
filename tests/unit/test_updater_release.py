"""App-owned packaging rejects mismatched bytes and foreign release destinations."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = next(
    parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").exists()
)
SPEC = importlib.util.spec_from_file_location(
    "repository_packager", ROOT / "scripts/package_updater_release.py"
)
PACKAGER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGER)


@pytest.fixture
def candidates(tmp_path):
    image = tmp_path / "image.tar"
    image.write_bytes(b"controlled image fixture")
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "example-1.0-py3-none-any.whl").write_bytes(b"controlled wheel fixture")
    native = (
        json.loads((ROOT / "release/install-recipe.json").read_text())["recipe"]["application"][
            "kind"
        ]
        == "native"
    )
    source = tmp_path / "candidates"
    for arch in ("amd64", "arm64"):
        options = (
            {"wheels": wheels} if native else {"image": image, "image_id": "sha256:" + "b" * 64}
        )
        PACKAGER.package(source / arch, "linux/" + arch, "a" * 40, **options)
    return source, tmp_path / "distribution"


def test_both_platforms_use_own_repository_payloads_and_exact_checksums(candidates):
    source, output = candidates
    PACKAGER.combine(source, output)
    catalog = json.loads((output / "catalog.json").read_text())
    assert catalog["updater_releases"] == []
    assert {recipe["platform"] for recipe in catalog["recipes"]} == {"linux/amd64", "linux/arm64"}
    for recipe in catalog["recipes"]:
        assert recipe["compatible_from"] == []
        prefix = (
            "https://github.com/flamoris-jp/"
            + recipe["application_id"]
            + "/releases/download/v"
            + recipe["release"]
            + "/"
        )
        for logical, download in recipe["downloads"].items():
            assert download["url"].startswith(prefix)
            assert (
                PACKAGER.checksum(output / download["url"].rsplit("/", 1)[1]) == download["digest"]
            )
            assert not logical.startswith("/")
    for line in (output / "SHA256SUMS").read_text().splitlines():
        checksum, name = line.split("  ")
        assert PACKAGER.checksum(output / name) == "sha256:" + checksum
    with pytest.raises(ValueError, match="replace"):
        PACKAGER.combine(source, output)


@pytest.mark.parametrize("fault", ["payload", "foreign_url", "platform", "private_extra"])
def test_mismatched_or_unexpected_publication_inputs_fail_before_output(candidates, fault):
    source, output = candidates
    catalog_path = source / "amd64/catalog-linux-amd64.json"
    catalog = json.loads(catalog_path.read_text())
    recipe = catalog["recipes"][0]
    if fault == "payload":
        next(path for path in (source / "amd64").iterdir() if "--" in path.name).write_bytes(
            b"altered"
        )
    elif fault == "foreign_url":
        next(iter(recipe["downloads"].values()))["url"] = (
            "https://github.com/other/repo/releases/download/v1/image.tar"
        )
    elif fault == "platform":
        recipe["platform"] = "linux/arm64"
    else:
        (source / "amd64/private.env").write_bytes(b"must not publish")
    catalog_path.write_text(json.dumps(catalog))
    with pytest.raises(ValueError):
        PACKAGER.combine(source, output)
    assert not output.exists()
