# Fork sync scenarios

Each scenario: how the report shows it, what to tell the user, the options with
the recommended one first, and how to carry out each. Ask in the decision format
from SKILL.md. Where a scenario says "apply and report", there is one correct
outcome and no question to ask.

## Contents

1. [A routine sync](#1-a-routine-sync)
2. [The mirror has drifted](#2-the-mirror-has-drifted)
3. [Mechanical conflicts](#3-mechanical-conflicts)
4. [Migration collision](#4-migration-collision)
5. [A real code conflict](#5-a-real-code-conflict)
6. [Upstream built or fixed the same thing](#6-upstream-built-or-fixed-the-same-thing)
7. [A clean merge that still breaks our code](#7-a-clean-merge-that-still-breaks-our-code)
8. [Deleted on one side](#8-deleted-on-one-side)
9. [Our open PRs](#9-our-open-prs)
10. [Upstream workflows](#10-upstream-workflows)
11. [Fork plumbing](#11-fork-plumbing)
12. [Sending a change upstream](#12-sending-a-change-upstream)

## 1. A routine sync

**Shows as:** mirror `behind`, a clean dry-run merge, no collisions, nothing to
compare under `intent`, no `same_parent`, nothing in `overlap_merged_cleanly`.

Still run Step 6's checks: a clean merge is not proof that nothing broke. Say
plainly that nothing needed a decision, and why.

## 2. The mirror has drifted

**Shows as:** `mirror.state` is `ahead` or `diverged` with
`mirror.fork_only_commits` listed, or `local.main` ahead of `upstream/main`.

Find out which of two things happened before asking:

- **Someone committed to the fork's `main`** (or the local one). The commits are
  ours: authored by the fork's owner, absent from upstream entirely.
- **Upstream rewrote its history** (a force-push). The extra commits have the
  same subjects as upstream commits under different hashes. Compare
  `git log --format='%s' upstream/main..origin/main` with
  `git log --format='%s' origin/main..upstream/main`.

Tell the user which it is, and that until `main` is a mirror again every later
sync is unsafe.

**Our commits on `main`:**

1. **Move them to a branch off `develop`, then reset `main` to upstream**
   (recommended). Cherry-pick them onto `fix/<topic>` from `develop`, push it and
   open a PR into `develop`. Then, after a separate yes, force-push `main`:
   `git push --force-with-lease=main:<old-sha> <fork> upstream/main:main`.
   The work survives on its branch, and the mirror is a mirror again. A local
   `main` alone needs no push: `git branch -f main upstream/main` once the
   commits are safe on their branch.
2. **Keep them on `main` and merge upstream into it.** No force-push, but `main`
   stops being a mirror for good: every later sync merges, and nothing sent
   upstream can start from a clean base.
3. **Stop** and leave everything as it is.

**Upstream rewrote history.** A rewrite of a public branch is rare, and
sometimes a sign of a compromised account, so check before recommending
anything:

- Do upstream's release tags still point at the same trees?
  `git diff --stat <tag> <old-commit-for-that-tag>` for each recent tag.
- Is there an announcement - release notes, an issue, a discussion?

Then:

1. **Stop and ask upstream** (recommended when tagged releases changed, or
   nothing explains the rewrite).
2. **Reset `main` to upstream** (recommended when the rewrite is announced and
   releases are intact), after a yes:
   `git push --force-with-lease=main:<old-sha> <fork> upstream/main:main`.
   `develop` still holds the old commits, so dry-run the merge into it. If it
   shows conflicts or duplicated subjects, that is a decision of its own:
   - **merge and resolve** (recommended - no rewrite of `develop`), or
   - **rebuild `develop` from the new `main` by cherry-picking our commits.**
     This is the one case where `develop` may be force-pushed: only with
     `--force-with-lease`, only after its own yes, and after saying it breaks
     every open PR into `develop`.

## 3. Mechanical conflicts

These have one right outcome. Apply, then report what was done, file by file.
Ask only where a rule below says so. In a merge of upstream into `develop`,
`--ours` is our side and `--theirs` is upstream's.

### `changelog` - `CHANGELOG.md`

`CHANGELOG.md` conflicts only if a fork entry leaked into it. Take upstream's
file whole, `git checkout --theirs CHANGELOG.md`, and move each fork entry it
held into `CHANGELOG.fork.md` under `[Unreleased]` in the same category. Ask
only if you cannot tell whose an entry is. For conflicts inside
`CHANGELOG.fork.md`, which only fork branches touch, use the
`resolve-changelog-conflict` skill.

### `translation-stamp` - only the `source_sha` stamp conflicts

The stamp is a front-matter line on site pages and an HTML comment
(`<!-- source_sha: ... -->`) on the four repository files. Each side restamped
after translating its own English changes, and the text merged cleanly. Before
restamping, confirm three things:

1. The English page merged without conflict.
2. Each side's translation of its own changes is present in the merged file:
   look at the two English diffs from the merge base and find each changed
   passage in the translation.
3. The two sides' passages do not overlap, and row and list order match the
   English.

Then take either value and restamp exactly those files with
`python3 scripts/check_docs_i18n.py --update <file>...`, and run
`python3 scripts/check_docs_i18n.py`. If any check fails, the file is
`translation-text`.

### `translation-text` - translated prose conflicts

Resolve the English page first. Then retranslate the conflicting passage from
the merged English, in the page's own terms (`docs/howto/translate.md`), and
restamp only what you retranslated. If both sides changed the same English
passage, the English conflict is a [scenario 5](#5-a-real-code-conflict)
decision first.

### `lockfile` - `backend/uv.lock`, `frontend/bun.lock`

Never hand-merge a lockfile. Take upstream's and let the tool re-resolve with the
merged manifest: `git checkout --theirs <lockfile>`, then `cd backend && uv
lock` or `cd frontend && bun install`. Confirm any dependency we added is still
there. If re-resolving moves a version our code relies on, check it as in
[scenario 7](#7-a-clean-merge-that-still-breaks-our-code).

### `version` - `version` lines in `pyproject.toml` or `package.json`

Take upstream's. The fork's version lives in `CHANGELOG.fork.md` and tags, never
in these files.

### `generated` - a file a generator writes

Take either side, re-run the generator listed in references/agenticos.md, and
check with its `--check`. Never hand-edit a generated file.

### `workflow` - `.github/workflows/`

Take upstream's unless the fork changed that workflow on purpose (one of our
commits touches it). If it did, it is a decision: keep ours, take upstream's, or
combine. Explain what each version runs and when. Then
[scenario 10](#10-upstream-workflows).

## 4. Migration collision

**Shows as:** a `migrations.same_parent` pair, or a `migration` conflict: our
migration and upstream's both chain onto the same parent. Git usually merges
this cleanly; `backend/tests/test_migration_chain.py` is what fails.

That test requires exactly one head and no two migrations sharing a parent, so
an alembic merge revision is not allowed here. The later migration is re-chained
after the earlier instead, and upstream's file is never edited (it would
conflict on every sync), so ours moves:

- Rename our file to the next number after upstream's newest, and set its
  `revision` to the new stem - `0139_fork_x.py` becomes `0140_fork_x.py` with
  `revision = "0140_fork_x"`. Keep the stem within the chain test's revision-id
  length limit.
- Set its `down_revision` to upstream's newest revision, and re-point any later
  fork migration that chained onto ours.
- **Never keep the old `revision` id and only re-point `down_revision`.** A
  database that ran ours would then look up to date, and `alembic upgrade head`
  would skip upstream's migration without a word. The rename is what makes the
  problem loud instead.
- Run `cd backend && uv run pytest tests/test_migration_chain.py -q`, then
  `make db-check` if a database is available.

Then ask: **has our migration already run on a database you care about** - a
deployment, a staging database, data you want to keep?

**It has not** (recommended answer when no fork release with this migration
exists): the re-chain is enough. A developer database that ran the old revision
now fails every alembic command with `Can't locate revision identified by
'<old id>'`. Fixing it is a question, never something to run on your own,
because it acts on whatever database `backend/.env` names:

- **Recreate it** (recommended for throwaway data), or
- **repair it in place** with the same three commands as the first option below.

**It has.** Explain what happens: with the renamed revision, every alembic
command on that database - `upgrade`, `downgrade`, even `current` - fails with
`Can't locate revision identified by '<old id>'` until it is repaired. Nothing
is skipped silently. The deployment is stuck until someone acts, and there are
two ways out:

1. **Stamp, then apply only upstream's** (recommended - no data is lost).
   It only works if upstream's `upgrade()` and ours do not create or alter the
   same table, column or index, so compare the two bodies and say whether they
   do. Then, with the merged code, and each step asked before it runs against
   the database:
   1. back up the database;
   2. `uv run alembic stamp --purge <shared parent>` - forget the unknown id;
   3. `uv run alembic upgrade <upstream's newest revision>` - apply only
      upstream's DDL;
   4. `uv run alembic stamp <our newest revision>` - ours is already in the
      schema;
   5. `uv run alembic current` - one head, ours.
2. **Downgrade with the old code, then upgrade.** From the pre-merge checkout,
   with the old code still deployed: back up, then `uv run alembic downgrade
   <shared parent>`. That runs our `downgrade()`, which drops what ours created,
   with its data - restore it from the backup afterwards. Then deploy the merged
   code and `uv run alembic upgrade head`.
3. **Stop** - `git merge --abort`, and plan it first.

## 5. A real code conflict

**Shows as:** `kind: code`, or an English page conflict.

Ask even when the combination looks obvious - the user said every conflict is
their call. Read both sides first: `git show :2:<path>` is ours and
`:3:<path>` is upstream's, plus their commit messages, our `CHANGELOG.fork.md`
entry, and any `docs/design/` plan. Tell the user what each side was trying to
do, in a sentence each, and quote the clashing lines.

1. **Combine** (recommended when both intents survive). Describe the combined
   code concretely before writing it: which lines come from where, and why
   neither intent is lost.
2. **Take upstream's**, and adapt or drop our dependent changes. Say which of
   ours stop working or become dead, and update `CHANGELOG.fork.md`.
3. **Keep ours.** Say that the same hunk will conflict again at the next sync
   that touches it, and that upstream's intent is lost.
4. **Stop** - `git merge --abort`.

Run the test files covering that code after resolving. A conflict a test did not
catch the first time - say whether a test now pins the combined behaviour, and
offer to add one if not.

## 6. Upstream built or fixed the same thing

**Shows as** any of:

- `collisions`, ours or an open PR's - an identifier of ours that upstream's new
  changes add or mention;
- `work.already_upstream` - our commit's exact patch is in upstream's history;
- `intent` - an upstream changelog entry that describes one of ours, whatever
  the names. Upstream rarely picks our names, so read each of upstream's new
  entries against each of our unreleased entries and open PRs, by what they do.

The report finds candidates; confirm each by reading upstream's diff and its
changelog entry before telling the user. A shared generic name is not a
duplicate, and two entries about the same screen need not be the same change.

**Our exact commit is upstream** (`already_upstream`, usually one we sent
there): first confirm the change is still in `upstream/main`'s tree (grep for
the hunk) - `git cherry` also matches a patch upstream merged and later
reverted. If it is there, the merge absorbs it: apply and report. Record it in
`CHANGELOG.fork.md` as a `### Removed` line under `[Unreleased]` reading "now in
upstream `<version>`". If our entry sits in a released fork section, add the
new line and leave the released section alone - released history is not
rewritten.

**Upstream did the same thing its own way.** Compare the two: behaviour,
configuration names, defaults, edge cases, and what ours has that theirs lacks.
Show the user that comparison, then:

1. **Adopt upstream's and remove ours** (recommended when theirs covers ours).
   Revert our implementation in the merge commit. Rename our configuration to
   theirs, calling it out as a breaking change for the fork's operators: a
   renamed env var, or a value whose meaning changed, silently stops working.
   Record it under `### Removed`.
2. **Port our extras onto upstream's** (recommended when ours does more). Keep
   their core, re-apply only what theirs lacks on top, and record the remaining
   delta as our changelog entry.
3. **Keep both, renamed apart.** Say that the codebase then has two ways to do
   one thing, and that every later sync risks touching both.
4. **Keep ours and replace theirs.** The worst for future syncs: their code
   changes again and we keep undoing it. Recommend it only if theirs is wrong for
   the fork, and say why.

**Upstream fixed the same bug differently:** usually take upstream's fix and
drop ours (option 1). Keep ours only if theirs leaves a case ours covered, and
then add that case to theirs rather than keeping both fixes.

**An open PR builds what upstream just shipped:** tell the user before anyone
spends more time on it. Offer to close it in favour of upstream's, to rework it
as an extension of upstream's, or to keep it.

## 7. A clean merge that still breaks our code

**Shows as:** `overlap_merged_cleanly` (both sides changed the same files and git
merged them), or failing checks after a clean merge.

Git merges lines, not meaning. For each file both sides changed:

- Run the test files covering it, and ours that touch it.
- `make lint-backend` - `ty` catches a renamed function, a changed signature, a
  removed import; ruff catches a class attribute defined twice.
- Grep our changes for every function, class and setting they use that
  upstream's diff renamed, removed or changed the signature of.
- Look for a new upstream caller our change should also cover, or must not
  affect: grep upstream's new code for our touch points.

If something broke, tell the user what upstream changed and how it affects ours,
then:

1. **Adapt ours to upstream's new shape** (recommended).
2. **Revert that part of ours** if it no longer makes sense.
3. **Stop**, leaving the merge uncommitted, to look at it together.

## 8. Deleted on one side

**Shows as:** `deleted-upstream` (upstream deleted or moved a file we changed),
`deleted-ours` (we deleted or moved a file upstream changed), or scenario 7
finding a function or setting we call gone.

**Upstream deleted it.** Find out why: its commit message, changelog entry or
PR (`gh pr list --repo <upstream> --search <sha> --state merged`). A revert
usually means it was broken; a removal usually comes with a replacement.

1. **Move ours onto upstream's replacement** (recommended when there is one).
2. **Drop our feature** if it only made sense on top of what is gone, and record
   it in `CHANGELOG.fork.md` under `### Removed`.
3. **Carry the deleted code in the fork** (keep our side). Say that the fork
   then maintains it alone, security fixes included.

**We deleted it, upstream changed it.** Find what our deletion was for (our
commit, our changelog entry) and what upstream's change does.

1. **Re-apply upstream's change to our replacement** (recommended when we moved
   or replaced the file).
2. **Restore the file with upstream's change** if our reason for deleting it no
   longer holds.
3. **Keep it deleted and drop upstream's change**, saying what of upstream's
   the fork then lacks.

## 9. Our open PRs

**Shows as:** `open_fork_prs` entries with `conflicts_after_sync` or
`collisions`. `exact: true` means the conflict was computed against `develop`
as it will be after the sync; `exact: false` means `develop`'s own merge
conflicts, so the list is an estimate - re-check once `develop` has moved, with
`git merge-tree --write-tree origin/<head> origin/develop`.

For each PR, tell the user what conflicts and with which upstream change:

1. **Merge `develop` into the PR branch** (recommended). No force-push, review
   comments stay attached, and the PR is squashed on merge anyway. Resolve with
   these same scenarios, then ask before pushing the branch.
2. **Rebase it on `develop`.** A cleaner history, but a force-push. Ask
   separately, and only for a branch nobody else has checked out.
3. **Leave it** for later, saying it will not merge until updated.

A PR with `collisions` is also a [scenario 6](#6-upstream-built-or-fixed-the-same-thing)
question: upstream may have built what it builds.

## 10. Upstream workflows

**Shows as:** `workflows_changed_upstream`, plus `fork_workflows` (the fork's
current state of each).

- **A new workflow file is active on the fork the moment it lands.** Read its
  triggers and jobs. If it deploys, publishes, pushes images, posts to external
  services or runs on a schedule with upstream's secrets, recommend disabling it
  on the fork (`gh workflow disable <file> --repo <fork>`). The options are
  disable (recommended) or keep. Ask, since it is a change on GitHub.
- **A changed workflow we disabled stays disabled.** Say what changed, in case it
  now matters to the fork.
- **A changed workflow we keep** (CI, CodeQL, AI review): say what changed.
- **Pushing a commit that changes workflow files** needs the `workflow` scope, or
  the SSH push (references/agenticos.md).

## 11. Fork plumbing

- **Actions do not run on the fork:** references/agenticos.md has the known cause
  and fix.
- **A clashing tag** - same name on both, different commits: the fork's tag is
  wrong, or upstream re-tagged. Show both commits. Options: replace ours with
  upstream's (a tag force-push, so ask), or keep ours renamed under `fork/`.
  Upstream's names (`v*`) belong to upstream.
- **A fork tag outside `fork/`** (`tags.fork_only_other`): offer to rename it
  into `fork/`, or to delete it if it was a scratch tag. Both are pushes, so ask.

## 12. Sending a change upstream

When the user wants to offer a fork change to upstream:

1. **Ask whether to open an issue upstream first** (recommended for a feature),
   so the maintainers can say whether they want it before work goes into a PR.
2. **Branch from the mirror:** `git switch -c contrib/<topic> upstream/main`,
   never from `develop`, which carries everything else of ours. (`contrib/`
   rather than `upstream/`, which would shadow the remote's own branch names.)
3. **Bring the change over:** cherry-pick our commits, or squash them into one.
   Drop fork-only content: `CHANGELOG.fork.md`, and `AGENTS.md` lines about the
   fork.
4. **Write its entry in upstream's style** under `[Unreleased]` in
   `CHANGELOG.md` on that branch. Upstream feature PRs carry one.
5. **Push the branch to the fork and open the PR** from
   `<fork>:contrib/<topic>` against `<upstream>:main`. Ask before each step. Tell
   the user that upstream's AI reviewer skips fork PRs, and that a maintainer has
   to approve CI for a first-time contributor.
6. **After upstream merges it,** the next sync shows our commit under
   `already_upstream`, as a collision, or as a matching changelog entry. Handle it
   with scenario 6.
