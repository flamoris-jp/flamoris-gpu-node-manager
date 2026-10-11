"""Validate app-owned artifacts; optionally install/start on a disposable CI runner."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from flamoris_updater_adapters.managed import Catalog, Manager, Start


def command(*args):
    return subprocess.check_output(args, stderr=subprocess.STDOUT, timeout=60).decode().strip()


def verify(catalog_path, install):
    catalog = Catalog.model_validate_json(catalog_path.read_bytes())
    if install and (os.geteuid() != 0 or os.environ.get("GITHUB_ACTIONS") != "true"):
        raise SystemExit("Actual installation is restricted to a disposable root CI runner")
    for recipe in catalog.recipes:
        with tempfile.TemporaryDirectory(prefix="flamoris-install-check-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            manager = Manager(root / "state", root / "apps")
            manager.catalog = lambda refresh=False: catalog
            manager.accept_catalog(catalog)

            def local_download(url, limit, destination, expected):
                source = catalog_path.parent / url.rsplit("/", 1)[1]
                if source.stat().st_size > limit:
                    raise ValueError("Artifact exceeds its installer limit")
                with source.open("rb") as stream:
                    actual = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
                if actual != expected:
                    raise ValueError("Artifact digest mismatch")
                shutil.copyfile(source, destination)

            manager._download = local_download
            values = {}
            models = None
            for setting in recipe.settings:
                if setting.key in {"ADMIN_DSN", "PGHOST"}:
                    from psycopg.conninfo import conninfo_to_dict

                    dsn = os.environ.get("FLAMORIS_INSTALL_TEST_POSTGRES_DSN", "")
                    if not dsn:
                        raise ValueError("A disposable PostgreSQL service is required")
                    values[setting.key] = (
                        dsn if setting.key == "ADMIN_DSN" else conninfo_to_dict(dsn)["host"]
                    )
                elif setting.key == "MODEL_ROOT":
                    models = tempfile.TemporaryDirectory(prefix="flamoris-empty-models-")
                    Path(models.name).chmod(0o755)
                    values[setting.key] = models.name
                elif setting.required and not setting.default and not setting.generate:
                    raise ValueError("Missing a required CI setting: " + setting.key)
            cfg = None
            try:
                job = manager.start(
                    "install",
                    Start(
                        application_id=recipe.application_id,
                        release=recipe.release,
                        request_key="repository-release-verification",
                        settings=values,
                    ),
                )
                # Preview uses the same settings validation/profile as actual installation.
                if not install:
                    cfg = manager._profile(recipe, job["job_id"])
                else:
                    manager.run_job(job["job_id"])
                    stored = manager.journal.get("managed_job", job["job_id"])
                    if stored["phase"] != "awaiting_setup":
                        raise ValueError("Installer failed: " + stored.get("error", "unknown"))
                    installed = manager.journal.get("managed_app", recipe.application_id)
                    cfg = manager._profile(recipe, "cleanup-profile")
                    assert installed["release"] == recipe.release
                    manager.start_setup(recipe.application_id)
                    result = manager.complete(recipe.application_id)
                    assert result["phase"] == "succeeded"
                assert cfg.applications[0].application_id == recipe.application_id
                assert cfg.applications[0].release == recipe.release
                settings = json.loads(
                    (
                        manager.state / "settings" / recipe.application_id / "values.json"
                    ).read_bytes()
                )
                if recipe.application_id == "flamoris-studio":
                    assert settings["GENERATION_TOKEN"] == settings["AGENT_TOKEN"] == ""
            finally:
                if install:
                    app = recipe.application
                    if app["kind"] == "docker":
                        subprocess.run(
                            ["docker", "rm", "--force", app["container_name"]],
                            check=False,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=60,
                        )
                    else:
                        for unit in app["units"]:
                            name = unit["name"]
                            subprocess.run(
                                ["systemctl", "disable", "--now", name],
                                check=False,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                timeout=60,
                            )
                            (Path("/etc/systemd/system") / name).unlink(missing_ok=True)
                        command("systemctl", "daemon-reload")
                if models:
                    models.cleanup()
    print(
        json.dumps(
            {
                "validated_recipes": len(catalog.recipes),
                "installation_performed": install,
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    verify(args.catalog, args.install)


if __name__ == "__main__":
    main()
