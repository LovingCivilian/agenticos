"""This fork's server build: `scripts/fork-deploy.sh` and the two `*.fork.yml` overlays.

Fork-only. Upstream publishes images of its own code, so a fork server builds
`agenticos-fork-backend` and `agenticos-fork-frontend` and starts them through
overlays on upstream's prod compose files. Each test here fails on a drift a
sync with upstream could bring in without a conflict:

  - `fork-deploy.sh` copies three functions of `deploy.sh`. Upstream's tests run
    `deploy.sh`'s, so a copy that still matches is covered by them, and one that
    no longer matches is a change upstream made that this fork has not ported.
  - A prod service upstream starts on its backend or frontend image, and the
    overlay does not replace, runs upstream's code beside the fork's.
  - The script builds with the Dockerfiles and contexts `images.yml` names, so
    the fork's images are built the way upstream's are.

`prune_builds` is the script's own, so it runs here against a stubbed `docker`,
the way `test_compose_healthchecks.py` runs `deploy.sh`'s functions.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy.sh"
FORK_DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "fork-deploy.sh"
IMAGES_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "images.yml"

# Upstream's image, the prod compose file that runs it, the overlay that replaces
# it, and the image the script builds instead.
OVERLAYS = (
    (
        "ghcr.io/vstorm-co/agenticos-backend",
        "docker-compose-prod.yml",
        "docker-compose-prod.fork.yml",
        "agenticos-fork-backend",
    ),
    (
        "ghcr.io/vstorm-co/agenticos-frontend",
        "docker-compose-prod.frontend.yml",
        "docker-compose-prod.frontend.fork.yml",
        "agenticos-fork-frontend",
    ),
)

BASH = shutil.which("bash") or "/bin/bash"


def _function(script: Path, name: str) -> str:
    """One function verbatim out of a deploy script, which cannot be sourced: it deploys on read."""
    lines = script.read_text().splitlines()
    start = lines.index(f"{name}() {{")
    end = lines.index("}", start)
    return "\n".join(lines[start : end + 1])


def _services(file_name: str) -> dict[str, Any]:
    with (REPO_ROOT / file_name).open() as handle:
        loaded: dict[str, Any] = yaml.safe_load(handle)
    services: dict[str, Any] = loaded["services"]
    return services


@pytest.mark.parametrize("name", ["env_value", "compose_up", "wait_healthy"])
def test_a_function_copied_from_deploy_sh_still_matches_it(name: str) -> None:
    assert _function(FORK_DEPLOY_SCRIPT, name) == _function(DEPLOY_SCRIPT, name), (
        f"scripts/deploy.sh changed {name}(). Port the change into scripts/fork-deploy.sh."
    )


@pytest.mark.parametrize(("upstream", "base", "overlay", "fork_image"), OVERLAYS)
class TestTheOverlaysReplaceEveryUpstreamApplicationImage:
    def test_the_base_file_still_runs_the_upstream_image(
        self, upstream: str, base: str, overlay: str, fork_image: str
    ) -> None:
        """Guards the parametrization: a base file with no such service would pass the next test."""
        assert any(
            str(service.get("image", "")).startswith(f"{upstream}:")
            for service in _services(base).values()
        )

    def test_each_service_on_the_upstream_image_runs_the_fork_build(
        self, upstream: str, base: str, overlay: str, fork_image: str
    ) -> None:
        overrides = _services(overlay)
        for name, service in _services(base).items():
            if not str(service.get("image", "")).startswith(f"{upstream}:"):
                continue
            assert name in overrides, (
                f"{base}: {name} runs {upstream} and {overlay} does not replace it"
            )
            assert overrides[name]["image"] == f"{fork_image}:${{AGENTICOS_FORK_VERSION:-latest}}"
            assert overrides[name]["pull_policy"] == "never", (
                f"{overlay}: {name} would look for {fork_image} in a registry"
            )

    def test_the_overlay_replaces_nothing_else(
        self, upstream: str, base: str, overlay: str, fork_image: str
    ) -> None:
        on_upstream = {
            name
            for name, service in _services(base).items()
            if str(service.get("image", "")).startswith(f"{upstream}:")
        }
        assert set(_services(overlay)) == on_upstream


def test_every_compose_command_in_the_script_takes_the_overlays() -> None:
    """Both proxies' file lists, or a Traefik host would start upstream's images."""
    text = FORK_DEPLOY_SCRIPT.read_text()
    backend = re.findall(r"^\s+BACKEND=\((.*)\)$", text, re.MULTILINE)
    frontend = re.findall(r"^\s+FRONTEND=\((.*)\)$", text, re.MULTILINE)
    assert len(backend) == 2 and len(frontend) == 2
    assert all("-f docker-compose-prod.fork.yml" in files for files in backend)
    assert all("-f docker-compose-prod.frontend.fork.yml" in files for files in frontend)


def test_the_script_builds_with_the_dockerfiles_and_contexts_images_yml_names() -> None:
    with IMAGES_WORKFLOW.open() as handle:
        workflow: dict[str, Any] = yaml.safe_load(handle)
    upstream = {
        entry["image"]: (PurePosixPath(entry["file"]), PurePosixPath(entry["context"]))
        for entry in workflow["jobs"]["build"]["strategy"]["matrix"]["include"]
        if "context" in entry
    }
    builds = re.findall(
        r"^build_image agenticos-fork-(\w+) (\S+) (\S+)$",
        FORK_DEPLOY_SCRIPT.read_text(),
        re.MULTILINE,
    )
    assert {image for image, _, _ in builds} == {"backend", "frontend"}
    for image, dockerfile, context in builds:
        assert (PurePosixPath(dockerfile), PurePosixPath(context)) == upstream[image]


# `docker image ls` lists the stubbed tags newest first, the way `--format
# '{{.CreatedAt}}|{{.Tag}}'` sorts; `docker image rm` records what it removed.
_PRUNE_HARNESS = """
set -euo pipefail
KEEP_BUILDS=2
AGENTICOS_FORK_VERSION={current}
{function}
removed=$(mktemp)
docker() {{
  case "$1 $2" in
    "image ls") printf '%s\\n' {listing} ;;
    "image rm") echo "$3" >> "$removed" ;;
  esac
}}
prune_builds agenticos-fork-backend >/dev/null
echo "removed: $(tr '\\n' ' ' < "$removed")"
"""


def _prune(current: str, tags: list[str]) -> subprocess.CompletedProcess[str]:
    listing = " ".join(
        f"'2026-10-{30 - day:02d} 10:00:00 +0000 UTC|{tag}'" for day, tag in enumerate(tags)
    )
    script = _PRUNE_HARNESS.format(
        current=current,
        function=_function(FORK_DEPLOY_SCRIPT, "prune_builds"),
        listing=listing,
    )
    return subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=60)


class TestPruningKeepsWhatARollbackNeeds:
    def test_builds_past_the_newest_two_are_removed(self) -> None:
        result = _prune(
            "sha-ddddddd", ["sha-ddddddd", "latest", "sha-ccccccc", "sha-bbbbbbb", "sha-aaaaaaa"]
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == [
            "removed:",
            "agenticos-fork-backend:sha-bbbbbbb",
            "agenticos-fork-backend:sha-aaaaaaa",
        ]

    def test_the_build_just_deployed_is_never_removed(self) -> None:
        """A rollback to a build older than the newest two runs it, and its tag must stay."""
        result = _prune("sha-aaaaaaa", ["sha-ccccccc", "sha-bbbbbbb", "sha-aaaaaaa"])
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == ["removed:"]

    def test_a_host_with_no_builds_yet_is_not_an_error(self) -> None:
        result = _prune("sha-aaaaaaa", [])
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == ["removed:"]
