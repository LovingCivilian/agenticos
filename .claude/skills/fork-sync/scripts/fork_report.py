#!/usr/bin/env python3
"""Report where a fork stands against its upstream, without changing anything that matters.

The fork model this assumes: `<fork>/<mirror>` is a pure mirror of
`<upstream>/<mirror>`, and `<fork>/<work>` holds the fork's own work, taking
upstream in by merging the mirror. The report answers, before anything is
touched: is the mirror still a mirror, which tags and labels are missing, what
a merge of the mirror into the work branch would conflict on and why, which of
our changes upstream may now have made redundant, and which open fork pull
requests the merge would break.

What it writes, all of it harmless: `git fetch` updates remote-tracking refs and,
with `--tags`, local `refs/tags/*`; `git merge-tree` and `git commit-tree` write
unreferenced objects that `git gc` collects. Nothing touches a branch, the index or
the work tree. `--no-fetch` skips the fetches, but `git ls-remote` and `gh` still
read from the network.

Usage:
    python3 fork_report.py                  # markdown summary
    python3 fork_report.py --json           # the same, as JSON for an agent
    python3 fork_report.py --no-fetch       # report on what is already fetched

Exit status: 0 for a report, 1 when it cannot report, 2 when a fetch failed and the
report may be stale.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

FORK_CHANGELOG = "CHANGELOG.fork.md"
LOCKFILES = {
    "uv.lock",
    "bun.lock",
    "bun.lockb",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Cargo.lock",
}
TRANSLATION = re.compile(r"\.(?P<locale>[a-z]{2})\.md$")
# Site pages carry the stamp as front matter; the four repository files GitHub renders
# (README, CONTRIBUTING, SECURITY, CODE_OF_CONDUCT) carry it as an HTML comment.
STAMP = re.compile(r"^\s*(?:<!--\s*)?source_sha:")
# Files a generator writes from a source elsewhere in the repository. A conflict in one
# is resolved by re-running the generator, never by hand.
GENERATED_PREFIXES = ("backend/app/core/catalog/skill_gallery/software/agenticos-guide/docs/",)
GENERATED_PATHS = {"docs/assets/amigo-walk.svg"}
IDENT_PATTERNS = (
    re.compile(r"^\+\s*(?:async\s+)?def\s+([A-Za-z_]\w{3,})"),
    re.compile(r"^\+\s*class\s+([A-Za-z_]\w{3,})"),
    re.compile(r"^\+\s*([A-Z][A-Z0-9_]{3,})\s*[:=]"),
    re.compile(r"^\+\s*#?\s*([A-Z][A-Z0-9_]{3,})="),
    re.compile(r"^\+\s*export\s+(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_]\w{3,})"),
    re.compile(r"^\+\s*export\s+(?:const|let|class|interface|type|enum)\s+([A-Za-z_]\w{3,})"),
)
IGNORED_IDENTS = {"__init__", "TYPE_CHECKING", "TODO", "NOTE"}


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)


def out(*args: str) -> str:
    result = git(*args)
    return result.stdout.strip() if result.returncode == 0 else ""


def lines(*args: str) -> list[str]:
    return [line for line in out(*args).splitlines() if line.strip()]


def ref_exists(ref: str) -> bool:
    return git("rev-parse", "--verify", "--quiet", ref).returncode == 0


def github_slug(remote_url: str) -> str | None:
    match = re.search(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?/?$", remote_url)
    return match.group(1) if match else None


def gh_json(*args: str):
    """The parsed output of a `gh` call, or None when it failed - never an empty stand-in."""
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def remote_tags(remote: str) -> dict[str, str] | None:
    """Tag name to the commit it points at (annotated tags peeled), or None if unreachable."""
    result = git("ls-remote", "--tags", remote)
    if result.returncode != 0:
        return None
    tags: dict[str, str] = {}
    for line in result.stdout.splitlines():
        sha, ref = line.split("\t", 1)
        name = ref.removeprefix("refs/tags/")
        if name.endswith("^{}"):
            tags[name[:-3]] = sha
        else:
            tags.setdefault(name, sha)
    return tags


def conflict_hunks(text: str) -> list[list[str]]:
    hunks: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("<<<<<<< "):
            current = []
        elif line.startswith(">>>>>>> ") and current is not None:
            hunks.append(current)
            current = None
        elif current is not None and not line.startswith("======="):
            current.append(line)
    return hunks


def classify(path: str, tree: str, message: str, ours: str, theirs: str) -> str:
    """Name the kind of conflict, so the skill knows which playbook applies."""
    if "modify/delete" in message or "rename/delete" in message:
        if f"deleted in {theirs}" in message:
            return "deleted-upstream"
        if f"deleted in {ours}" in message:
            return "deleted-ours"
        return "deleted-on-one-side"
    name = Path(path).name
    if path == "CHANGELOG.md":
        return "changelog"
    if name in LOCKFILES:
        return "lockfile"
    if path.startswith(".github/workflows/"):
        return "workflow"
    if "/alembic/versions/" in f"/{path}":
        return "migration"
    if path in GENERATED_PATHS or path.startswith(GENERATED_PREFIXES):
        return "generated"
    body = [
        line
        for hunk in conflict_hunks(out("show", f"{tree}:{path}"))
        for line in hunk
        if line.strip()
    ]
    if TRANSLATION.search(path):
        if body and all(STAMP.match(line) for line in body):
            return "translation-stamp"
        return "translation-text"
    if (
        name in {"pyproject.toml", "package.json"}
        and body
        and all(re.match(r'\s*"?version"?\s*[:=]', line) for line in body)
    ):
        return "version"
    return "code"


def dry_run_merge(ours: str, theirs: str) -> dict:
    """What `git merge theirs` on `ours` would conflict on, computed without a work tree."""
    result = git("merge-tree", "--write-tree", ours, theirs)
    if result.returncode not in (0, 1):
        return {"error": result.stderr.strip() or "git merge-tree failed (needs git 2.38+)"}
    head, _, messages = result.stdout.partition("\n\n")
    tree = head.splitlines()[0] if head else ""
    paths: list[str] = []
    for line in head.splitlines()[1:]:
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[1] not in paths:
            paths.append(parts[1])
    conflicts = []
    for path in paths:
        named = re.compile(rf"(?<![\w./-]){re.escape(path)}(?![\w./-])")
        message = " ".join(m for m in messages.splitlines() if named.search(m))
        kind = classify(path, tree, message, ours, theirs)
        conflicts.append({"path": path, "kind": kind, "message": message})
    return {"clean": result.returncode == 0, "tree": tree, "conflicts": conflicts}


def added_identifiers(base: str, tip: str) -> dict[str, str]:
    """Names a side introduced (functions, classes, constants, env vars, TS exports), with a file."""
    found: dict[str, str] = {}
    current_file = ""
    for line in out("diff", "-U0", base, tip).splitlines():
        if line.startswith("+++ "):
            current_file = line[6:] if line.startswith("+++ b/") else ""
            continue
        if not line.startswith("+") or not current_file:
            continue
        for pattern in IDENT_PATTERNS:
            match = pattern.match(line)
            if (
                match
                and match.group(1) not in IGNORED_IDENTS
                and not match.group(1).startswith("test_")
            ):
                found.setdefault(match.group(1), current_file)
    return found


def backticked(text: str) -> set[str]:
    return {t for t in re.findall(r"`([A-Za-z_][\w.]{3,})`", text) if not t.endswith(".md")}


def collisions(
    ours: dict[str, str], known_at: str, up_base: str, up_tip: str
) -> list[dict[str, str]]:
    """Our new names that upstream's new changes also add or mention.

    `known_at` is the commit whose tree counts as "already there": a name the
    codebase had before either side changed it is not ours to collide on - every
    migration defines `upgrade`, and a function we edited keeps its name.
    """
    if ours:
        patterns = [arg for name in ours for arg in ("-e", name)]
        existing = set(lines("grep", "-o", "-h", "-w", "-F", *patterns, known_at))
        ours = {name: where for name, where in ours.items() if name not in existing}
    if not ours:
        return []
    upstream_added = [
        line[1:].strip()
        for line in out("diff", "-U0", up_base, up_tip).splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    subjects = lines("log", "--format=%s", f"{up_base}..{up_tip}")
    names = sorted(ours, key=len, reverse=True)
    pattern = re.compile(r"(?<!\w)(" + "|".join(map(re.escape, names)) + r")(?!\w)")
    first_line: dict[str, str] = {}
    first_subject: dict[str, str] = {}
    for line in upstream_added:
        for name in set(pattern.findall(line)):
            first_line.setdefault(name, line)
    for subject in subjects:
        for name in set(pattern.findall(subject)):
            first_subject.setdefault(name, subject)
    return [
        {
            "identifier": name,
            "ours_in": ours[name],
            "upstream_line": first_line.get(name, "")[:160],
            "upstream_commit": first_subject.get(name, ""),
        }
        for name in sorted(set(first_line) | set(first_subject))
    ]


def bold_leads(text: str, diff: bool = False) -> list[str]:
    """The bold lead of each Keep a Changelog entry: `- **This.** ...` gives `This.`.

    A lead may wrap onto the next line, so the bold span is matched across line
    breaks. From a diff, only the added lines count, with their `+` dropped.
    """
    if diff:
        text = "\n".join(
            line[1:]
            for line in text.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
    leads = re.findall(r"^-\s+\*\*((?:[^*]|\*(?!\*))+?)\*\*", text, re.MULTILINE)
    return [" ".join(lead.split()) for lead in leads]


def migrations(base: str, tip: str) -> list[dict[str, str]]:
    found = []
    for line in lines("diff", "--name-status", "--diff-filter=A", base, tip):
        _, path = line.split("\t", 1)
        if "/alembic/versions/" not in f"/{path}" or not path.endswith(".py"):
            continue
        text = out("show", f"{tip}:{path}")
        revision = re.search(r'^revision[^=]*=\s*["\']([^"\']+)', text, re.MULTILINE)
        down = re.search(r'^down_revision[^=]*=\s*["\']([^"\']+)', text, re.MULTILINE)
        found.append(
            {
                "path": path,
                "revision": revision.group(1) if revision else "",
                "down_revision": down.group(1) if down else "",
            }
        )
    return found


def fork_changelog_state(text: str) -> dict:
    if not text:
        return {"exists": False}
    unreleased = re.search(
        r"^## \[Unreleased\]\n(.*?)(?=^## \[|\Z)", text, re.MULTILINE | re.DOTALL
    )
    body = unreleased.group(1) if unreleased else ""
    releases = re.findall(r"^## \[([^\]]+)\] - (\d{4}-\d{2}-\d{2})", text, re.MULTILINE)
    return {
        "exists": True,
        "unreleased_entries": len([line for line in body.splitlines() if line.startswith("- ")]),
        "unreleased_leads": bold_leads(body),
        "latest_release": releases[0][0] if releases else None,
    }


def next_fork_version(latest: str | None, base_tag: str | None) -> str | None:
    if not base_tag:
        return None
    base = base_tag.removeprefix("v")
    if latest and latest.rsplit("-", 1)[0] == base:
        return f"{base}-{int(latest.rsplit('-', 1)[1]) + 1}"
    return f"{base}-1"


def merged_commit(work: str, mirror: str, tree: str) -> str:
    """A throwaway commit standing for `work` once the mirror is merged in (unreferenced)."""
    return out(
        "commit-tree", tree, "-p", work, "-p", mirror, "-m", "fork_report: develop after sync"
    )


def check_prs(
    prs: list[dict], args: argparse.Namespace, merge: dict, base: str, up_mirror: str
) -> list[dict]:
    fork_work = f"{args.fork}/{args.work}"
    after_sync = merged_commit(fork_work, up_mirror, merge["tree"]) if merge.get("clean") else ""
    develop_conflicts = {c["path"] for c in merge.get("conflicts", [])}
    checked = []
    for pr in prs:
        head = f"{args.fork}/{pr['headRefName']}"
        entry = {"number": pr["number"], "title": pr["title"], "head": pr["headRefName"]}
        if not ref_exists(head):
            entry["error"] = f"{head} not fetched"
            checked.append(entry)
            continue
        pr_base = out("merge-base", head, fork_work)
        entry["touches"] = lines("diff", "--name-only", pr_base, head)
        if after_sync:
            result = dry_run_merge(head, after_sync)
            entry["conflicts_after_sync"] = [c["path"] for c in result.get("conflicts", [])]
            entry["exact"] = True
        else:
            # develop itself conflicts, so there is no merged develop to test against yet:
            # count only the PR's own files beyond develop's conflicts, as an estimate.
            result = dry_run_merge(head, up_mirror)
            entry["conflicts_after_sync"] = [
                c["path"]
                for c in result.get("conflicts", [])
                if c["path"] not in develop_conflicts and c["path"] in entry["touches"]
            ]
            entry["exact"] = False
        entry["collisions"] = collisions(added_identifiers(pr_base, head), pr_base, base, up_mirror)
        checked.append(entry)
    return checked


def build(args: argparse.Namespace) -> dict:
    report: dict = {}
    if not out("rev-parse", "--show-toplevel"):
        return {"error": "not inside a git repository"}
    fork_url = out("remote", "get-url", args.fork)
    upstream_url = out("remote", "get-url", args.upstream)
    if not fork_url or not upstream_url:
        return {
            "error": f"need remotes '{args.fork}' and '{args.upstream}'",
            "remotes": lines("remote", "-v"),
        }
    report["remotes"] = {"fork": fork_url, "upstream": upstream_url}
    report["fetch_errors"] = []
    if args.fetch:
        for command in (
            ("fetch", "--prune", args.upstream),
            ("fetch", "--tags", args.upstream),
            ("fetch", "--prune", args.fork),
        ):
            result = git(*command)
            if result.returncode != 0:
                report["fetch_errors"].append(f"git {' '.join(command)}: {result.stderr.strip()}")
    report["working_tree"] = {
        "branch": out("branch", "--show-current"),
        "clean": out("status", "--porcelain") == "",
    }

    fork_mirror, up_mirror = f"{args.fork}/{args.mirror}", f"{args.upstream}/{args.mirror}"
    fork_work = f"{args.fork}/{args.work}"
    report["missing_refs"] = [
        ref for ref in (fork_mirror, up_mirror, fork_work) if not ref_exists(ref)
    ]
    if report["missing_refs"]:
        return report

    ahead = lines("log", "--format=%h %s", f"{up_mirror}..{fork_mirror}")
    behind = int(out("rev-list", "--count", f"{fork_mirror}..{up_mirror}") or 0)
    if not ahead:
        state = "in-sync" if not behind else "behind"
    else:
        state = "ahead" if not behind else "diverged"
    report["mirror"] = {"state": state, "behind": behind, "fork_only_commits": ahead}

    def local_vs(branch: str, against: str) -> dict:
        if not ref_exists(f"refs/heads/{branch}"):
            return {"exists": False}
        return {
            "exists": True,
            "ahead": int(out("rev-list", "--count", f"{against}..{branch}") or 0),
            "behind": int(out("rev-list", "--count", f"{branch}..{against}") or 0),
        }

    report["local"] = {
        args.mirror: local_vs(args.mirror, up_mirror) | {"against": up_mirror},
        args.work: local_vs(args.work, fork_work) | {"against": fork_work},
    }

    up_base = out("describe", "--tags", "--abbrev=0", "--match", "v*", up_mirror) or None
    work_base = out("describe", "--tags", "--abbrev=0", "--match", "v*", fork_work) or None
    report["upstream_release"] = {"upstream": up_base, "work_branch_is_on": work_base}

    up_tags, fork_tags = remote_tags(args.upstream), remote_tags(args.fork)
    if up_tags is None or fork_tags is None:
        unreachable = [
            r for r, t in ((args.upstream, up_tags), (args.fork, fork_tags)) if t is None
        ]
        report["tags"] = {"error": f"could not list tags on {', '.join(unreachable)}"}
    else:
        missing = sorted((t for t in up_tags if t not in fork_tags), reverse=True)
        report["tags"] = {
            "missing_on_fork": len(missing),
            "newest_missing": missing[:5],
            "clashing": [t for t in up_tags if t in fork_tags and fork_tags[t] != up_tags[t]],
            "fork_releases": sorted(t for t in fork_tags if t.startswith("fork/")),
            "fork_only_other": sorted(
                t for t in fork_tags if t not in up_tags and not t.startswith("fork/")
            ),
        }

    base = out("merge-base", fork_work, up_mirror)
    report["work"] = {
        "merge_base": base[:10],
        "upstream_commits_to_take": int(
            out("rev-list", "--count", f"{fork_work}..{up_mirror}") or 0
        ),
        "upstream_releases_to_take": [
            s
            for s in lines("log", "--first-parent", "--format=%s", f"{fork_work}..{up_mirror}")
            if re.search(r"\bcut \d+\.\d+\.\d+", s)
        ],
        "fork_only_commits": lines(
            "log", "--no-merges", "--format=%h %s", f"{up_mirror}..{fork_work}"
        ),
        "already_upstream": [
            line[2:]
            for line in lines("cherry", "-v", up_mirror, fork_work)
            if line.startswith("- ")
        ],
    }
    merge = dry_run_merge(fork_work, up_mirror)
    report["merge"] = {k: v for k, v in merge.items() if k != "tree"}

    fork_files = set(lines("diff", "--name-only", base, fork_work))
    up_files = set(lines("diff", "--name-only", base, up_mirror))
    conflicted = {c["path"] for c in merge.get("conflicts", [])}
    report["overlap_merged_cleanly"] = sorted((fork_files & up_files) - conflicted)

    changelog = out("show", f"{fork_work}:{FORK_CHANGELOG}")
    ours = added_identifiers(base, fork_work)
    for token in backticked(changelog):
        ours.setdefault(token, FORK_CHANGELOG)
    report["collisions"] = collisions(ours, base, base, up_mirror)
    report["fork_changelog"] = fork_changelog_state(changelog)
    report["fork_changelog"]["next_version_now"] = next_fork_version(
        report["fork_changelog"].get("latest_release"), work_base
    )
    report["fork_changelog"]["next_version_after_sync"] = next_fork_version(
        report["fork_changelog"].get("latest_release"), up_base
    )
    # Upstream doing what we did under other names is invisible to `collisions`; the
    # two changelogs, read side by side, are where an agent can still see it.
    report["intent"] = {
        "upstream_new_changelog_entries": bold_leads(
            out("diff", "-U0", base, up_mirror, "--", "CHANGELOG.md"), diff=True
        ),
        "ours": report["fork_changelog"].get("unreleased_leads", []),
    }

    ours_m, theirs_m = migrations(base, fork_work), migrations(base, up_mirror)
    report["migrations"] = {
        "ours": ours_m,
        "theirs": theirs_m,
        "same_parent": [
            {"ours": o["path"], "theirs": t["path"], "parent": o["down_revision"]}
            for o in ours_m
            for t in theirs_m
            if o["down_revision"] and o["down_revision"] == t["down_revision"]
        ],
    }
    report["workflows_changed_upstream"] = [
        line.replace("\t", " ")
        for line in lines("diff", "--name-status", base, up_mirror, "--", ".github/workflows/")
    ]

    slug, up_slug = github_slug(fork_url), github_slug(upstream_url)
    if shutil.which("gh") and slug:
        prs = gh_json(
            "pr",
            "list",
            "--repo",
            slug,
            "--state",
            "open",
            "--base",
            args.work,
            "--json",
            "number,title,headRefName",
        )
        report["open_fork_prs"] = (
            None if prs is None else check_prs(prs, args, merge, base, up_mirror)
        )
        if up_slug:
            fork_labels = gh_json(
                "label", "list", "--repo", slug, "--limit", "500", "--json", "name"
            )
            up_labels = gh_json(
                "label", "list", "--repo", up_slug, "--limit", "500", "--json", "name"
            )
            report["labels_missing_on_fork"] = (
                None
                if fork_labels is None or up_labels is None
                else sorted({x["name"] for x in up_labels} - {x["name"] for x in fork_labels})
            )
        workflows = gh_json("api", f"repos/{slug}/actions/workflows")
        report["fork_workflows"] = (
            None
            if workflows is None
            else {w["path"]: w["state"] for w in workflows.get("workflows", [])}
        )
        status = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True, check=False
        )
        scopes = re.search(r"Token scopes: (.*)", status.stdout + status.stderr)
        report["gh_token_has_workflow_scope"] = bool(scopes and "workflow" in scopes.group(1))
    else:
        report["github"] = "skipped: no gh, or the fork remote is not on github.com"
    return report


def plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def markdown(report: dict) -> str:
    if "error" in report:
        return f"**Cannot report:** {report['error']}\n"
    rows = []
    stale = " (report may be stale)" if report["fetch_errors"] else ""
    for error in report["fetch_errors"]:
        rows.append(f"- **Fetch failed - stop and fix this first:** {error}")
    if report["missing_refs"]:
        rows.append(
            f"- **Missing refs:** {', '.join(report['missing_refs'])}. Fetch, or pass the right branch names."
        )
        return "\n".join(rows) + "\n"
    m, w, merge, tags = report["mirror"], report["work"], report["merge"], report["tags"]
    rows.append(
        f"- **Mirror:** {m['state']} (behind {m['behind']}, fork-only commits {len(m['fork_only_commits'])}){stale}"
    )
    for branch, state in report["local"].items():
        if state.get("exists") and (state["ahead"] or state["behind"]):
            rows.append(
                f"- **Local `{branch}`** differs from `{state['against']}`: ahead {state['ahead']}, behind {state['behind']}"
            )
    rel = report["upstream_release"]
    rows.append(
        f"- **Upstream release:** {rel['upstream']}; work branch is on {rel['work_branch_is_on']}"
    )
    if "error" in tags:
        rows.append(f"- **Tags:** {tags['error']}")
    else:
        rows.append(
            f"- **Tags missing on the fork:** {tags['missing_on_fork']}{stale}; clashing: {tags['clashing'] or 'none'}"
            + (
                f"; fork-only, outside `fork/`: {', '.join(tags['fork_only_other'])}"
                if tags["fork_only_other"]
                else ""
            )
        )
    rows.append(
        f"- **Upstream commits to take:** {w['upstream_commits_to_take']} "
        f"(releases: {', '.join(w['upstream_releases_to_take']) or 'none'})"
    )
    rows.append(
        f"- **Our commits:** {len(w['fork_only_commits'])}; already in upstream by patch: {len(w['already_upstream'])}"
    )
    if merge.get("error"):
        rows.append(f"- **Dry-run merge:** {merge['error']}")
    else:
        conflicts = merge["conflicts"]
        rows.append(
            f"- **Dry-run merge:** {'clean' if merge['clean'] else plural(len(conflicts), 'conflicted file', 'conflicted files')}"
        )
        rows += [f"  - `{c['path']}`: {c['kind']}" for c in conflicts]
    if report["overlap_merged_cleanly"]:
        rows.append(
            f"- **Both sides changed, merged cleanly (check for semantic breaks):** {', '.join(report['overlap_merged_cleanly'])}"
        )
    for hit in report["collisions"]:
        rows.append(
            f"- **Possible duplicate of our work:** `{hit['identifier']}` (ours in {hit['ours_in']}) "
            f"appears upstream: {hit['upstream_commit'] or hit['upstream_line']}"
        )
    intent = report["intent"]
    if intent["upstream_new_changelog_entries"] and intent["ours"]:
        rows.append(
            "- **Compare by intent** (upstream may have built one of ours under other names):"
        )
        rows.append(
            f"  - upstream's new entries: {'; '.join(intent['upstream_new_changelog_entries'])}"
        )
        rows.append(f"  - our unreleased entries: {'; '.join(intent['ours'])}")
    for pair in report["migrations"]["same_parent"]:
        rows.append(
            f"- **Migration collision:** {pair['ours']} and {pair['theirs']} both follow {pair['parent']}"
        )
    if report["workflows_changed_upstream"]:
        rows.append(
            f"- **Upstream workflow changes:** {', '.join(report['workflows_changed_upstream'])}"
        )
    fc = report["fork_changelog"]
    if fc.get("exists"):
        rows.append(
            f"- **Fork changelog:** {plural(fc['unreleased_entries'], 'unreleased entry', 'unreleased entries')}; "
            f"latest fork release {fc['latest_release']}; next if cut now {fc['next_version_now']}, "
            f"after this sync {fc['next_version_after_sync']}"
        )
    if "open_fork_prs" in report:
        if report["open_fork_prs"] is None:
            rows.append("- **Open fork PRs:** could not list them (gh failed)")
        for pr in report["open_fork_prs"] or []:
            if pr.get("error"):
                rows.append(f"- **Open fork PR #{pr['number']}** ({pr['head']}): {pr['error']}")
                continue
            how = (
                "after the sync"
                if pr["exact"]
                else "estimate, until develop's own conflicts are resolved"
            )
            dupes = (
                f"; may duplicate upstream: {', '.join(c['identifier'] for c in pr['collisions'])}"
                if pr["collisions"]
                else ""
            )
            rows.append(
                f"- **Open fork PR #{pr['number']}** ({pr['head']}): conflicts {how} in "
                f"{', '.join(pr['conflicts_after_sync']) or 'nothing'}{dupes}"
            )
    if "labels_missing_on_fork" in report:
        missing = report["labels_missing_on_fork"]
        if missing is None:
            rows.append("- **Labels:** could not list them (gh failed)")
        elif missing:
            rows.append(f"- **Labels missing on the fork:** {', '.join(missing)}")
    if report.get("fork_workflows", "") is None:
        rows.append("- **Fork workflows:** could not list them (gh failed)")
    if "gh_token_has_workflow_scope" in report:
        rows.append(
            f"- **gh token has `workflow` scope:** {report['gh_token_has_workflow_scope']} "
            "(without it, push tags and workflow changes over SSH)"
        )
    if not report["working_tree"]["clean"]:
        rows.append("- **Working tree is not clean:** commit or stash before syncing.")
    return "\n".join(rows) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fork", default="origin", help="the fork's remote (default origin)")
    parser.add_argument(
        "--upstream", default="upstream", help="the upstream remote (default upstream)"
    )
    parser.add_argument("--mirror", default="main", help="the mirrored branch (default main)")
    parser.add_argument("--work", default="develop", help="the fork's own branch (default develop)")
    parser.add_argument("--no-fetch", dest="fetch", action="store_false", help="skip the fetches")
    parser.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    args = parser.parse_args()
    report = build(args)
    print(json.dumps(report, indent=2) if args.json else markdown(report))
    if "error" in report:
        return 1
    return 2 if report.get("fetch_errors") else 0


if __name__ == "__main__":
    sys.exit(main())
