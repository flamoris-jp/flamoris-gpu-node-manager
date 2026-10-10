"""Package this repository's release and its direct-download Updater catalog."""

import argparse
import hashlib
import json
import re
import shutil
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def encode(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def checksum(path):
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def package(output, platform, revision, *, image=None, image_id=None, wheels=None):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    app, version = project["name"], project["version"]
    if platform not in {"linux/amd64", "linux/arm64"} or not re.fullmatch(
        r"[0-9a-f]{40}", revision
    ):
        raise ValueError("An exact platform and source revision are required")
    if output.exists():
        raise ValueError("Refusing to replace packaged assets")
    template = json.loads((ROOT / "release/install-recipe.json").read_text())
    if template["application_id"] != app:
        raise ValueError("Recipe belongs to another repository")
    files = {}
    if image is not None:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id or ""):
            raise ValueError("An immutable Docker image ID is required")
        files["image.tar"] = image
        for name in template.get("sql_files", []):
            files[name] = ROOT / name
    else:
        files = {p.name: p for p in wheels.glob("*.whl")}
        if not files:
            raise ValueError("An offline wheelhouse is required")
    if any(not p.is_file() or p.is_symlink() for p in files.values()):
        raise ValueError("All release inputs must be regular files")
    arch = platform.split("/")[1]
    location = "https://github.com/flamoris-jp/" + app + "/releases/download/v" + version
    names = {name: "linux-" + arch + "--" + name.replace("/", "__") for name in files}
    if len(set(names.values())) != len(names):
        raise ValueError("Release asset names collide")
    hashes = {name: checksum(path) for name, path in files.items()}

    def bind(value):
        if isinstance(value, list):
            return [bind(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: bind(item) for key, item in value.items()}
        if result.get("digest") == "artifact":
            logical = result["path"].removeprefix("${package}/")
            result["digest"] = hashes[logical]
        return result

    recipe = bind(template["recipe"])
    recipe.update(application_id=app, release=version, platform=platform)
    recipe["application"].update(application_id=app, release=version)
    recipe["downloads"] = {
        name: {"url": location + "/" + names[name], "digest": hashes[name]} for name in files
    }
    if image is not None:
        recipe["application"]["image_id"] = image_id
    else:
        recipe["application"]["wheels"] = [
            {"path": "${package}/" + name, "digest": hashes[name]} for name in sorted(files)
        ]
    output.mkdir(parents=True)
    for name, path in files.items():
        shutil.copyfile(path, output / names[name])
    (output / ("catalog-linux-" + arch + ".json")).write_bytes(
        encode({"catalog_version": 1, "recipes": [recipe], "updater_releases": []})
    )
    (output / ("candidate-linux-" + arch + ".json")).write_bytes(
        encode(
            {
                "application_id": app,
                "release": version,
                "platform": platform,
                "source_revision": revision,
                "files": hashes,
                "image_id": image_id,
            }
        )
    )
    return recipe


def combine(inputs, output):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    app, version = project["name"], project["version"]
    catalogs = sorted(inputs.rglob("catalog-linux-*.json"))
    recipes = [json.loads(path.read_text())["recipes"][0] for path in catalogs]
    if len(recipes) != 2 or {r["platform"] for r in recipes} != {"linux/amd64", "linux/arm64"}:
        raise ValueError("Both platform catalogs are required")
    candidates = [json.loads(path.read_text()) for path in inputs.rglob("candidate-linux-*.json")]
    if len(candidates) != 2 or len({item["source_revision"] for item in candidates}) != 1:
        raise ValueError("Both candidates must come from the same exact source revision")
    paths = {path.name: path for path in inputs.rglob("*") if path.is_file()}
    if len(paths) != len([path for path in inputs.rglob("*") if path.is_file()]):
        raise ValueError("Duplicate assets")
    location = "https://github.com/flamoris-jp/" + app + "/releases/download/v" + version + "/"
    for recipe in recipes:
        if (recipe["application_id"], recipe["release"]) != (app, version):
            raise ValueError("Foreign release recipe")
        for download in recipe["downloads"].values():
            if not download["url"].startswith(location):
                raise ValueError("Payloads must remain in this repository's Release")
            name = download["url"][len(location) :]
            if "/" in name or checksum(paths[name]) != download["digest"]:
                raise ValueError("Published payload bytes disagree with the catalog")
    expected = {"catalog-linux-" + arch + ".json" for arch in ("amd64", "arm64")}
    expected |= {"candidate-linux-" + arch + ".json" for arch in ("amd64", "arm64")}
    expected |= {
        download["url"].rsplit("/", 1)[1]
        for recipe in recipes
        for download in recipe["downloads"].values()
    }
    if set(paths) != expected or any(path.is_symlink() for path in paths.values()):
        raise ValueError("Unexpected or linked assets must not be published")
    if output.exists():
        raise ValueError("Refusing to replace assembled assets")
    output.mkdir(parents=True)
    for path in paths.values():
        if path.is_symlink():
            raise ValueError("Linked assets are not published")
        shutil.copyfile(path, output / path.name)
    (output / "catalog.json").write_bytes(
        encode(
            {
                "catalog_version": 1,
                "recipes": recipes,
                "updater_releases": [],
            }
        )
    )
    (output / "SHA256SUMS").write_text(
        "".join(
            checksum(path).removeprefix("sha256:") + "  " + path.name + "\n"
            for path in sorted(output.iterdir())
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--platform")
    parser.add_argument("--revision")
    parser.add_argument("--image", type=Path)
    parser.add_argument("--image-id")
    parser.add_argument("--wheels", type=Path)
    parser.add_argument("--combine", type=Path)
    args = parser.parse_args()
    if args.combine:
        combine(args.combine, args.output)
    else:
        if (
            not args.platform
            or not args.revision
            or ((args.image is None) == (args.wheels is None))
        ):
            parser.error("Supply platform/revision and exactly one image or wheelhouse")
        package(
            args.output,
            args.platform,
            args.revision,
            image=args.image,
            image_id=args.image_id,
            wheels=args.wheels,
        )


if __name__ == "__main__":
    main()
