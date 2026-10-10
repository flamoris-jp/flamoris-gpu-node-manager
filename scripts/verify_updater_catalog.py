"""Validate published recipes using Updater's actual profile renderer, without installation."""

import argparse
import json
import tempfile
from pathlib import Path

from flamoris_update_core.wire import digest, dumps
from flamoris_updater_adapters.managed import Catalog, Manager


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    args = parser.parse_args()
    catalog = Catalog.model_validate_json(args.catalog.read_bytes())
    for recipe in catalog.recipes:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            manager = Manager(root / "state", root / "apps")
            values = {
                setting.key: setting.default
                or (
                    "x" * 40
                    if setting.secret
                    else "example"
                    if setting.kind == "name"
                    else "http://127.0.0.1:8188"
                    if "URL" in setting.key or "ENDPOINT" in setting.key
                    else "/tmp/example-models"
                    if setting.key == "MODEL_ROOT"
                    else "example"
                )
                for setting in recipe.settings
            }
            settings = manager.state / "settings" / recipe.application_id
            settings.mkdir(parents=True)
            (settings / "values.json").write_bytes(dumps(values))

            def local_download(url, limit, destination, expected):
                source = args.catalog.parent / url.rsplit("/", 1)[1]
                assert digest(source.read_bytes()) == expected
                destination.write_bytes(source.read_bytes())

            manager._download = local_download
            cfg = manager._profile(recipe, "catalog-verification")
            assert cfg.applications[0].release == recipe.release
            assert cfg.applications[0].application_id == recipe.application_id
    print(json.dumps({"validated_recipes": len(catalog.recipes), "installation_performed": False}))


if __name__ == "__main__":
    main()
