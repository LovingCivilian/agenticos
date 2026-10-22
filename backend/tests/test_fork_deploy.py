"""This fork's prod images: the `*.fork.yml` overlays and `GNUmakefile`.

Fork-only. `fork-images.yml` publishes the fork's backend and frontend under
`ghcr.io/lovingcivilian`, the overlays put them in place of upstream's, and
`GNUmakefile` adds the overlays to `make prod` and the other prod targets. Both
tests fail on a drift a sync with upstream could bring in without a conflict:

  - A prod service upstream starts on its backend or frontend image, and the
    overlay does not replace, runs upstream's code beside the fork's.
  - `GNUmakefile` appends to file lists upstream's `Makefile` defines. A rename
    there leaves `make prod` on upstream's images, so the targets are dry-run
    (`make -n`) and the commands they would run are read back.

A server's own `*.local.yml` overlay is tried in a copy of the two makefiles,
so the test never writes a file into the checkout.
"""

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKE = shutil.which("make") or "make"

# Upstream's image, the prod compose file that runs it, the overlay that
# replaces it, and the fork's image.
OVERLAYS = (
    (
        "ghcr.io/vstorm-co/agenticos-backend",
        "docker-compose-prod.yml",
        "docker-compose-prod.fork.yml",
        "ghcr.io/lovingcivilian/agenticos-backend",
    ),
    (
        "ghcr.io/vstorm-co/agenticos-frontend",
        "docker-compose-prod.frontend.yml",
        "docker-compose-prod.frontend.fork.yml",
        "ghcr.io/lovingcivilian/agenticos-frontend",
    ),
)


def _services(file_name: str) -> dict[str, Any]:
    with (REPO_ROOT / file_name).open() as handle:
        loaded: dict[str, Any] = yaml.safe_load(handle)
    services: dict[str, Any] = loaded["services"]
    return services


def _on_image(file_name: str, image: str) -> set[str]:
    return {
        name
        for name, service in _services(file_name).items()
        if str(service.get("image", "")).startswith(f"{image}:")
    }


@pytest.mark.parametrize(("upstream", "base", "overlay", "fork_image"), OVERLAYS)
def test_the_overlay_replaces_every_service_on_the_upstream_image(
    upstream: str, base: str, overlay: str, fork_image: str
) -> None:
    on_upstream = _on_image(base, upstream)
    assert on_upstream, f"{base} no longer runs {upstream}: the parametrization tests nothing"
    overrides = _services(overlay)
    assert set(overrides) == on_upstream, (
        f"{overlay} must replace exactly the services {base} runs on {upstream}"
    )
    for name in on_upstream:
        assert overrides[name]["image"] == f"{fork_image}:${{AGENTICOS_VERSION:-latest}}"


@pytest.mark.parametrize("proxy", ["", "traefik"])
@pytest.mark.parametrize(
    ("targets", "overlay", "count"),
    [
        (("prod",), "docker-compose-prod.fork.yml", 2),
        (("prod-frontend",), "docker-compose-prod.frontend.fork.yml", 2),
        (("prod-down", "prod-logs"), ".fork.yml", 3),
    ],
)
def test_the_prod_make_targets_take_the_overlays(
    targets: tuple[str, ...], overlay: str, count: int, proxy: str
) -> None:
    result = subprocess.run(
        [MAKE, "--no-print-directory", "-C", str(REPO_ROOT), "-n", *targets, f"PROXY={proxy}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    commands = [line for line in result.stdout.splitlines() if line.startswith("docker compose")]
    assert len(commands) == count
    assert all(overlay in command for command in commands), commands


def _dry_run_commands(directory: Path, *targets: str) -> list[str]:
    result = subprocess.run(
        [MAKE, "--no-print-directory", "-C", str(directory), "-n", *targets],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return [line for line in result.stdout.splitlines() if line.startswith("docker compose")]


@pytest.mark.parametrize(
    ("target", "local"),
    [
        ("prod", "docker-compose-prod.local.yml"),
        ("prod-frontend", "docker-compose-prod.frontend.local.yml"),
    ],
)
class TestAServersOwnOverlayComesLast:
    def test_it_is_the_last_file_when_it_exists(
        self, tmp_path: Path, target: str, local: str
    ) -> None:
        shutil.copy(REPO_ROOT / "GNUmakefile", tmp_path)
        shutil.copy(REPO_ROOT / "Makefile", tmp_path)
        (tmp_path / local).write_text("services: {}\n")
        commands = _dry_run_commands(tmp_path, target)
        assert commands
        assert all(command.split(" -f ")[-1].split()[0] == local for command in commands), commands

    def test_a_missing_one_is_skipped(self, tmp_path: Path, target: str, local: str) -> None:
        shutil.copy(REPO_ROOT / "GNUmakefile", tmp_path)
        shutil.copy(REPO_ROOT / "Makefile", tmp_path)
        commands = _dry_run_commands(tmp_path, target)
        assert commands
        assert not any(".local.yml" in command for command in commands), commands
