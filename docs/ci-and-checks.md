# CI and the checks: what runs, when, and how to run it yourself

Nine GitHub Actions workflows live in `.github/workflows/`. Four check pull requests, three deploy
or destroy, one runs every scan on demand, and one hands an issue to Claude. This page says what
each one does, what stands between a merge and a deploy, and how to run the same checks on your
own machine before you push.

Every setting a workflow reads (secrets, variables, switches) is in
[configuration.md](configuration.md#github-secrets-and-variables).

## Contents

- [The workflows](#the-workflows)
- [What gates a deploy](#what-gates-a-deploy)
- [What is required to merge](#what-is-required-to-merge)
- [ruff-autofix](#ruff-autofix)
- [on-demand-scan](#on-demand-scan)
- [claude-issue-worker](#claude-issue-worker)
- [Run the same checks yourself](#run-the-same-checks-yourself)
- [The pre-commit hook](#the-pre-commit-hook)
- [Where the code is](#where-the-code-is)

## The workflows

| Workflow (file) | Runs on | What it runs | What it changes |
|---|---|---|---|
| `pr-checks.yml` | Every pull request. | `validate`: `terraform init` (no backend), `fmt -check` and `validate` in each `infra/environments/*`, then `terraform test` in each module that has tests. `secret-scan`: Trufflehog over the pull request's commits. `gitleaks`: Gitleaks over the pull request's commits, with `.gitleaks.toml`. `pii-denylist`: `scripts/pii_denylist_check.py` over what the pull request adds. | Nothing. No AWS credentials. |
| `security.yml` | Every pull request. Also called by `terraform.yml` and `terraform-production-release.yml` before they apply. | `security`: Trivy over `infra/` (fails on HIGH and CRITICAL), Trivy over the whole repository for vulnerable dependencies and secrets (reports MEDIUM and up, fails on HIGH and CRITICAL), Bandit over `lambdas/` and `scripts/`. `checkov`: Checkov over the Terraform, as `.checkov.yaml` says. | Nothing. |
| `python-ci.yml` | Pull requests that touch `lambdas/**`, `scripts/**`, `pyproject.toml`, `.claude/skills/**`, `docs/hackathon/**` or the workflow itself. Pushes to `prod` that touch `lambdas/**`, `scripts/**`, `pyproject.toml`, `.claude/skills/**` or the workflow. Also called by `terraform.yml` and `terraform-production-release.yml`. | `lint-test`: installs the requirements on Python 3.11, prints `pip freeze --all`, runs `ruff check lambdas/ scripts/`, then `pytest lambdas/ scripts/`. | Nothing. A Ruff failure puts the diff `ruff check --fix` would make in the job summary. |
| `ruff-autofix.yml` | Pull requests opened, pushed to or reopened that touch `lambdas/**`, `scripts/**` or `pyproject.toml`. | `autofix`: Ruff's safe fixes, then `ruff check` again. [More below](#ruff-autofix). | With the `RUFF_AUTOFIX_TOKEN` secret: pushes a commit to the pull request's branch. Without it: nothing (the diff goes in the job summary). |
| `terraform.yml` | Pushes to `dev` that touch `infra/**`, `lambdas/**`, `frontend/**`, `scripts/minify_frontend.py`, `scripts/requirements.txt`, or `terraform.yml`, `security.yml` or `python-ci.yml`. Also by hand (Actions tab, the `dev` branch), with an optional `debug_permissions` switch. | `security` (calls `security.yml`), `lint-test` (calls `python-ci.yml`), then `apply-dev`. | Applies `infra/environments/dev`. Deploys the dev admin API's stage once more. With `debug_permissions`, writes the AWS calls the deploy role made to the job summary. |
| `terraform-production-release.yml` | A GitHub Release is published. | `security` and `lint-test` on the release tag, then `apply`. | Applies `infra/environments/production`. Deploys the production admin API's stage once more. |
| `destroy-dev.yml` | By hand only, with the input `confirm` set to `destroy`. | `destroy`. | Destroys `infra/environments/dev`. |
| `on-demand-scan.yml` | A pull request labelled `security-scan`, or by hand (Actions tab, with an optional ref). | Every security, secret and personal-data check, more thoroughly than a pull request runs them. [More below](#on-demand-scan). | Nothing. A results table in the job summary. |
| `claude-issue-worker.yml` | An issue opened or labelled by the repository owner, with the `claude` label or `@claude` in its body. | `run-claude`: `anthropics/claude-code-action`. [More below](#claude-issue-worker). | Runs with write access to the repository's contents, issues and pull requests. |

Every action is pinned to a commit, and Terraform and every scanner to a version. The versions
are in [the table below](#the-tool-versions).

A pull request from `dev` into `prod` gets `pr-checks`, `security`, and `python-ci` if its paths
match. `ruff-autofix` skips it: it never touches the `dev` or `prod` branch.

## What gates a deploy

Nothing reaches AWS without passing the security scans, lint and tests in the same workflow run.

**Dev** (`terraform.yml`):

1. A push to `dev` that matches the paths above starts the run. `security.yml` and `python-ci.yml`
   do not run on their own for that push; they run inside this workflow.
2. `security` and `lint-test` run on the pushed commit. `apply-dev` needs both.
3. `apply-dev` signs in to AWS as the dev deploy role (`AWS_DEV_DEPLOY_ROLE_ARN`), through
   GitHub OIDC. The role trusts the `dev` branch, so the job runs in no GitHub environment: a
   manual run must be started on the `dev` branch.
4. It builds `frontend-dist/` (`python scripts/minify_frontend.py`), then runs `terraform init`,
   `fmt -check`, `validate`, `plan` and `apply` in `infra/environments/dev`.
5. It deploys the admin API's stage once more, even if the apply failed, so the stage keeps the
   allowlist policy `scripts/admin_allowlist.py` wrote
   ([configuration.md](configuration.md#the-admin-apis-address-allowlist)).

Only one dev deploy or destroy runs at a time: `apply-dev` and `destroy-dev.yml` share the
`deploy-dev` concurrency group, and neither cancels a run in progress.

**Production** (`terraform-production-release.yml`):

1. Publishing a GitHub Release starts the run.
2. `security` and `lint-test` run on the release tag. `apply` needs both.
3. `apply` runs in the `production` GitHub environment. It waits for whatever protection that
   environment has; [configuration.md](configuration.md#github-repository-settings) recommends a
   required reviewer and `v*` tags only. The production deploy role trusts that environment.
4. It checks the tagged commit is on `prod`, and stops if it is not.
5. It builds `frontend-dist/`, then runs `terraform init`, `fmt -check`, `validate`, `plan` and
   `apply` in `infra/environments/production`, and deploys the admin API's stage once more.

What a deploy does not run again: `pr-checks.yml`. So `terraform test`, Trufflehog, Gitleaks and
the personal-data check run on the pull request only.

**Destroying dev** (`destroy-dev.yml`): start it by hand on the `dev` branch and type `destroy`.
It runs `terraform destroy -auto-approve` in `infra/environments/dev`. There is no workflow that
destroys production.

## What is required to merge

Which checks must pass before a pull request can merge is a repository setting, not code: see
[GitHub repository settings](configuration.md#github-repository-settings). The workflows above
report their results; a check blocks a merge only if the repository makes it required.

Mind the path filters when you choose:

- `python-ci` does not run on a pull request that touches only `infra/`, `frontend/` or docs
  outside `docs/hackathon/`. For a change to `infra/` or `frontend/`, lint and tests first run in
  `terraform.yml`, after the merge to `dev` and before the apply.
- `ruff-autofix` runs only for `lambdas/`, `scripts/` and `pyproject.toml`.
- `pr-checks` and `security` run on every pull request.

GitHub leaves a required check pending when path filtering skips its workflow, so a pull request
that never triggers `python-ci` would wait for it forever if `python-ci` were required.

## ruff-autofix

`ruff-autofix.yml` tries to make `python-ci`'s Ruff step pass, so a pull request that is red only
on an import order (I001) or a pyupgrade rewrite (UP) fixes itself. It is separate from
`python-ci.yml` on purpose: the deploy workflows call `python-ci.yml`, and a deploy must never
change the code it ships.

**When it runs:** a pull request is opened, pushed to or reopened, and touches `lambdas/**`,
`scripts/**` or `pyproject.toml`.

**When it skips:**

- The branch is in a fork (fork pull requests get no secrets and are never written to).
- The branch is `dev` or `prod`.
- The pull request's title contains `[skip-autofix]`.
- The branch's newest commit is already one of its own, or the branch has moved past the commit
  the run was started for (a newer run handles it).

**What it does:** installs only Ruff, at the version pinned in `lambdas/requirements-dev.txt`, and
runs `ruff check --fix --exit-zero lambdas/ scripts/`. Safe fixes only: never `--unsafe-fixes`, and
never `ruff format`.

- **With the `RUFF_AUTOFIX_TOKEN` secret** (push mode), it commits the fixes as `ruff-autofix` and
  pushes them to your branch. **Pull before you push again** (`git pull --rebase`), or your push
  is refused. The token must be a fine-grained token or GitHub App token with contents:write on
  this repository only, not `GITHUB_TOKEN`: a push made with `GITHUB_TOKEN` starts no workflow, so
  `python-ci` would never run on the fixed commit.
- **Without it** (report-only mode), it pushes nothing and puts the diff in the job summary. Apply
  it with `ruff check --fix lambdas/ scripts/`.

Then it runs `ruff check lambdas/ scripts/` again. It stays red when something is left that has no
safe fix (E501 line too long, F841 unused variable): fix those by hand. In report-only mode it also
stays red whenever it found fixes, because they exist only in the runner, not on your branch.

## on-demand-scan

`on-demand-scan.yml` runs every security, secret and personal-data check, and nothing else: no AWS
credentials, no plan, no deploy.

**Start it** one of two ways:

- add the `security-scan` label to a pull request (only collaborators can label);
- Actions tab → **on-demand-scan** → **Run workflow**, on a branch, with an optional ref (a branch,
  tag or commit), or `gh workflow run on-demand-scan.yml --ref <branch>`.

**What it scans**, in four jobs:

| Job | What it does, beyond what a pull request runs |
|---|---|
| `terraform` | `terraform fmt -check -recursive -diff infra/` (every module and bootstrap, with diffs); `validate` in `infra/bootstrap` as well as every environment; `terraform test -verbose` in each module with tests. |
| `code-and-dependencies` | Trivy over `infra/` and over the repository at every severity (report only), with every dependency it found listed; then the same HIGH and CRITICAL gates as `security.yml`. Bandit at every severity and confidence (report only), then the same gate. |
| `checkov` | Every Checkov check, `.checkov.yaml`'s repository-wide skips included (report only); then the same gate as `security.yml`. A `checkov:skip` comment on a resource still applies. |
| `secrets-and-personal-data` | Gitleaks and Trufflehog over the **whole history**, every branch and tag (Trufflehog with unverified results too); the personal-data check over **every tracked file**, not just what a change adds. Accepted history findings are listed in `.gitleaksignore`. |

A `summary` job writes one row per job to the run's summary. The run fails if any job failed.

## claude-issue-worker

`claude-issue-worker.yml` hands an issue to Claude Code (`anthropics/claude-code-action`).

**Who can start it:** only the repository owner. It runs when an issue is opened or labelled,
and only if the person who did that is the owner, and the issue has the `claude` label or
`@claude` in its body. On a public repository anyone can open an issue; without that check, a
stranger's issue would become the job's prompt. Adding any label to an issue that already
qualifies starts it again.

**What it can do:** the job has `contents: write`, `issues: write`, `pull-requests: write` and
`id-token: write`, and signs in to Claude with the `CLAUDE_CODE_OAUTH_TOKEN` secret. The setup
script does not set that secret.

**What it runs:** the prompt `/run-task-from-issue "<issue number>"`. That command is **not defined
in this repository**: `.claude/` holds only `settings.json` (a SessionStart hook),
`hooks/session-start.sh` and `skills/topic-findings/`, with no `commands/` folder.

## Run the same checks yourself

From the repository root, in Git Bash on Windows or any shell on Linux or macOS. CI runs on Linux.

### The tool versions

| Tool | CI's version | Pinned in |
|---|---|---|
| Python | 3.11 | `python-ci.yml`, `ruff-autofix.yml`, the deploy workflows (`actions/setup-python`) |
| Ruff | the `ruff==` line in `lambdas/requirements-dev.txt` (0.6.9 today) | `lambdas/requirements-dev.txt` |
| Bandit | 1.7.10 | `security.yml`, `on-demand-scan.yml` |
| Checkov | 3.3.25, in a virtual environment of its own | `security.yml`, `on-demand-scan.yml` |
| Trivy | 0.74.0, checksum-verified | `security.yml` ("Install Trivy"), `on-demand-scan.yml` |
| Gitleaks | 8.30.1; `.gitleaks.toml` needs 8.30.0 or newer | `pr-checks.yml`, `on-demand-scan.yml` ("Install Gitleaks and Trufflehog", with checksums) |
| Trufflehog | 3.97.4 | `pr-checks.yml`, `on-demand-scan.yml` |
| Terraform | 1.16.2 (the code allows 1.10 or newer) | every workflow that runs Terraform |

Another Ruff reports and fixes different things than CI does, so use the pinned one:
`ruff --version` must match it.

### Python: lint and tests (`python-ci`)

```bash
python -m venv .venv && source .venv/bin/activate   # Git Bash: . .venv/Scripts/activate
python -m pip install --upgrade pip
pip install -r lambdas/requirements.txt
pip install -r lambdas/requirements-dev.txt
pip install -r scripts/requirements.txt

ruff check --fix lambdas/ scripts/   # CI runs `ruff check lambdas/ scripts/`; --fix applies the safe fixes
pytest lambdas/ scripts/
```

- Never pass `--unsafe-fixes`. Fix what is left after `--fix` by hand.
- Do not run `ruff format` across the repository: the tree has never been formatted, and it would
  rewrite dozens of unrelated files ([CLAUDE.md](../CLAUDE.md)).
- Run Ruff again after you merge `dev` into your branch: import order (I001) breaks there.
- The tests use mocked AWS (moto) and need no credentials. Some of them check more than code:
  `scripts/tests/test_generate_cli_reference.py` fails when `lambdas/ops_mcp/cli_reference.json`
  is out of date (fix: `python scripts/generate_cli_reference.py`), and
  `scripts/tests/test_setup_repo.py` fails when a workflow reads a secret or variable that
  `scripts/setup_repo.py` does not know about.

### Code and dependency scans (`security`)

```bash
pip install bandit==1.7.10
bandit -r lambdas/ scripts/ -x lambdas/tests,scripts/tests \
  --severity-level high --confidence-level high

trivy config --severity HIGH,CRITICAL --exit-code 1 \
  --skip-dirs infra/bootstrap/.terraform,infra/environments/dev/.terraform,infra/environments/production/.terraform \
  infra/

trivy fs --scanners vuln,secret --severity HIGH,CRITICAL --exit-code 1 \
  --skip-dirs '**/.venv,**/venv,**/.terraform,**/node_modules,**/__pycache__,frontend-dist,lambda-build' \
  --file-patterns 'pip:requirements-(dev|ops-mcp|ops-agent|vision|vision-cool)\.txt' \
  .
```

Keep Trivy's `--file-patterns`: Trivy only recognises files named exactly `requirements.txt`, so
without it the dev, ops and vision requirements are not scanned. Without `--severity` and
`--exit-code 1`, `trivy fs` reports but never fails. CI also runs `trivy fs` once with
`--severity MEDIUM,HIGH,CRITICAL --exit-code 0`, to show the MEDIUM findings without failing.

```bash
python3 -m venv /tmp/checkov-venv
/tmp/checkov-venv/bin/pip install "checkov==3.3.25"
/tmp/checkov-venv/bin/checkov --config-file .checkov.yaml
```

`.checkov.yaml` names what Checkov scans (`infra/`) and what it skips, each with its reason. A
finding about one resource is accepted only by a `checkov:skip` comment on that resource.

### Terraform (`pr-checks`, `on-demand-scan`)

What `pr-checks` runs in every environment:

```bash
for dir in infra/environments/*; do
  terraform -chdir="$dir" init -backend=false -input=false -get=true -upgrade=false
  terraform -chdir="$dir" fmt -check
  terraform -chdir="$dir" validate
done
```

`fmt -check` there covers each environment's own files only. `on-demand-scan` checks every
module and the bootstrap too, and validates the bootstrap, which no other workflow does:

```bash
terraform fmt -check -recursive -diff infra/
terraform -chdir=infra/bootstrap init -backend=false -input=false
terraform -chdir=infra/bootstrap validate
```

The module tests (`pr-checks`): today the `observability` and `ops-assistant` modules have them.
They plan each module with a mocked AWS provider, so they need no credentials, and they catch an
expression that only fails once real values flow through it, which `validate` does not.

```bash
for dir in infra/modules/*; do
  if ls "$dir"/tests/*.tftest.hcl >/dev/null 2>&1; then
    terraform -chdir="$dir" init -backend=false -input=false
    terraform -chdir="$dir" test
  fi
done
```

### Secrets and personal data (`pr-checks`, `on-demand-scan`)

`pr-checks` runs Gitleaks and Trufflehog through their actions over the pull request's commits.
Locally, check what you are about to commit, or the whole history as `on-demand-scan` does:

```bash
gitleaks git --pre-commit --staged --redact --no-banner --config .gitleaks.toml   # staged changes
gitleaks git --redact --verbose --no-banner --config .gitleaks.toml --log-opts="--all" .   # whole history
trufflehog git file://. --results=verified,unverified,unknown --fail --no-update   # whole history
```

`.gitleaks.toml` adds email addresses and AWS account IDs to Gitleaks' own rules. Tests and docs
use `@example.com` and the account ID `123456789012`.

The personal-data check reads your own exact strings (a name, a home IP, a personal address) from
the `PII_DENYLIST` environment variable, else from `.pii-denylist` at the repository root
(gitignored, one per line). Findings name the file, line and entry number, never the entry.

```bash
python3 scripts/pii_denylist_check.py --staged                # what you are about to commit
python3 scripts/pii_denylist_check.py --range origin/dev HEAD  # what your branch adds, as pr-checks checks a pull request
python3 scripts/pii_denylist_check.py --all                   # every tracked file, as on-demand-scan does
```

With no list at all, it prints that there is nothing to check and passes. In CI the list is the
`PII_DENYLIST` secret.

### Before a local `terraform plan`

```bash
pip install -r scripts/requirements.txt
python scripts/minify_frontend.py
```

This builds `frontend-dist/` from `frontend/`, with the `.js` and `.css` minified (it is
gitignored and rebuilt each time). Both environments deploy the site from `frontend-dist/`, not
`frontend/`, and each site file is in the plan only if it exists there. Without it, a plan from
your machine shows every site file already deployed being deleted. CI runs this step before
every apply. The `init` and `plan` commands for your own machine are in
[the deployment runsheet](deployment-runsheet.md#4-first-deploy-dev).

## The pre-commit hook

`pr-checks` runs after you push, when the content is already on GitHub. The hook runs the same
checks before each commit. Turn it on once per clone:

```bash
git config core.hooksPath .githooks
```

`.githooks/pre-commit` runs, in order:

1. `scripts/pii_denylist_check.py --staged`: blocks a commit that adds one of your personal
   strings, and refuses to commit `.pii-denylist` itself.
2. `gitleaks git --pre-commit --staged --redact --no-banner --config .gitleaks.toml`: blocks
   secrets, email addresses and AWS account IDs. If Gitleaks is not installed, it warns and goes
   on.
3. `ruff check --quiet lambdas/ scripts/` over the working tree: blocks on lint errors (check
   only; it never fixes). If Ruff is not installed, or is not the version
   `lambdas/requirements-dev.txt` pins, it warns and goes on.

It needs a working Python (`python3`, `python` or `py -3`), and fails if it finds none. To skip
it once, on purpose: `git commit --no-verify`.

## Where the code is

- The workflows: `.github/workflows/`. Each one's comments say why it is built the way it is.
- What Checkov runs and skips: `.checkov.yaml`.
- Gitleaks' extra rules: `.gitleaks.toml`; accepted findings in the history: `.gitleaksignore`.
- The personal-data check: `scripts/pii_denylist_check.py`.
- The commit hook: `.githooks/pre-commit`.
- Ruff's settings: `[tool.ruff]` in `pyproject.toml` (and the same in `lambdas/` and `scripts/`).
- In a Claude Code cloud session, `.claude/hooks/session-start.sh` puts the pinned Ruff first on
  `PATH`.
