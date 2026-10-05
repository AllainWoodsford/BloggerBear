# Run sheet: making the repo public

**Date:** 2026-10-03 · **Status:** ready, not started (the repository was still private on
2026-10-05) · **Why:** the Qloo and Nebius hackathons need a public repository with an open-source
license (Amazon accepts a public one too).

**Using this for your own fork:** "Already done" below is the original repository's history, and
does not apply to you. Steps 1 to 4 do: they are the GitHub settings a public copy should have.
Replace `AllainWoodsford/BloggerBear` with your repository
(`gh repo view --json nameWithOwner --jq .nameWithOwner`) and the reviewer id in step 3e with your
own (`gh api user --jq .id`). Rulesets, secret scanning and fork-PR approval exist only on public
repositories or paid plans, which is why they wait until the repository is public.

## Already done

- History rewritten: the TAFE email replaced by the GitHub no-reply address on every commit in `dev`,
  `master`, `prod` and the release tags (force-pushed 2026-10-03). Pre-rewrite backup:
  `D:\BloggerBear\BloggerBear-pre-rewrite.bundle` (outside the repo, deliberately).
- Global git `user.email` is the no-reply address. GitHub blocks pushes that expose a private email.
- PR #163 merged: Apache-2.0 `LICENSE`, `SECURITY.md`, `CONTRIBUTING.md`, the Claude issue worker
  limited to the repo owner, and the budget alert email moved out of the repo.
- Repo settings, checked 2026-10-03: the Actions default `GITHUB_TOKEN` is read-only; the
  `production` environment deploys only from `v*` tags (a hand-added `dev` branch rule was removed).
- `master` deleted on 2026-10-04: 316 commits behind `dev`, nothing unique on it, no release from it.
  `dev` (default) and `prod` are the only long-lived branches.
- Local branches: everything carrying the old email was deleted. The one unmerged document
  (the multi-account IAM feasibility analysis) was recovered onto `docs/multi-account-iam-feasibility`
  with the account IDs replaced by placeholders.

**Accepted, not fixed:** GitHub keeps a read-only copy of every PR's original commits (`refs/pull/*`,
159 PRs), so old PR pages (and their `.patch` views) still show the TAFE email. Only GitHub Support
can purge them; decided not to.

## 1. Before going public: check production's allowed refs

Only `terraform-production-release.yml` uses the `production` environment, and it runs from a release
tag. Any other allowed ref would let a workflow on it take the production deploy role.

```bash
gh api repos/AllainWoodsford/BloggerBear/environments/production/deployment-branch-policies \
  --jq '.branch_policies[] | "\(.id) \(.type) \(.name)"'
# expect exactly one line: <id> tag v*
```

## 2. Make the repo public

Settings → General → Danger Zone → **Change visibility** → Public. Or:

```bash
gh repo edit AllainWoodsford/BloggerBear --visibility public --accept-visibility-change-consequences
```

## 3. Lock it down — immediately after step 2

These settings only exist on public repositories (or paid plans), which is why they wait until now.
Run in Git Bash, back to back with step 2:

```bash
R=AllainWoodsford/BloggerBear

# 3a. Workflows from fork PRs wait for approval, for every outside contributor
gh api -X PUT repos/$R/actions/permissions/fork-pr-contributor-approval \
  -f approval_policy=all_external_contributors

# 3b. Secret scanning + push protection
gh api -X PATCH repos/$R --input - <<'EOF'
{"security_and_analysis":{"secret_scanning":{"status":"enabled"},"secret_scanning_push_protection":{"status":"enabled"}}}
EOF

# 3c. Dependabot alerts and private vulnerability reporting (SECURITY.md points people there)
gh api -X PUT repos/$R/vulnerability-alerts
gh api -X PUT repos/$R/private-vulnerability-reporting

# 3d. Ruleset on dev and prod: no force-push, no deletion, changes via PR.
#     Admins (you) can bypass, so solo work is never locked out; the rules stop everyone else.
gh api -X POST repos/$R/rulesets --input - <<'EOF'
{
  "name": "protect-deploy-branches",
  "target": "branch",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["refs/heads/dev", "refs/heads/prod"], "exclude": []}},
  "bypass_actors": [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}],
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "pull_request", "parameters": {
      "required_approving_review_count": 0, "dismiss_stale_reviews_on_push": false,
      "require_code_owner_review": false, "require_last_push_approval": false,
      "required_review_thread_resolution": false}}
  ]
}
EOF

# 3e. Production deploys wait for your approval (the README already says they do).
#     Keeps the custom ref policy from step 1.
gh api -X PUT repos/$R/environments/production --input - <<'EOF'
{"reviewers":[{"type":"User","id":39211453}],"prevent_self_review":false,
 "deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}
EOF
```

## 4. Check

- **Settings → Rules:** `protect-deploy-branches` is active on `dev` and `prod`.
- **Settings → Environments → production:** you as required reviewer; allowed refs `v*` only.
- **Settings → Code security:** secret scanning, push protection, Dependabot alerts and private
  vulnerability reporting all on.
- **Settings → Actions → General:** fork PR workflows need approval for all outside contributors;
  workflow permissions read-only.
- The next release should pause for your approval before `terraform-production-release` applies.

## 5. Follow-ups

- **README "Branch & release model"** and `docs/configuration.md` ("GitHub repository settings")
  describe the ruleset, the required reviewer and the token settings from step 3 as settings to
  apply. If step 3 changes, update them to match.
- **Backup bundle** (`D:\BloggerBear\BloggerBear-pre-rewrite.bundle`): it is the only copy of the
  pre-rewrite history, and it still contains the old email. Keep it off GitHub and out of the repo.
  Delete it after the next production release has deployed cleanly from the rewritten history.
- **Old email in branches you create later:** rebasing or cherry-picking keeps a commit's original
  author. Anything recovered from the bundle needs
  `git rebase origin/dev --exec "git commit --amend --no-edit --reset-author"`.
