"""First-time setup: put the GitHub secrets and variables a deployment needs on your repository.

    python scripts/setup_repo.py --dry-run     # step through everything; changes nothing
    python scripts/setup_repo.py               # the real thing
    python scripts/setup_repo.py --repo your-name/your-fork
    python scripts/setup_repo.py --only ADMIN_ALLOWED_CIDRS_DEV ADMIN_ALLOWED_CIDRS_PROD

It needs Python 3.11+ and the GitHub CLI (`gh`), signed in. It never calls AWS. The settings are
the ones the deploy workflows read (.github/workflows/terraform.yml, destroy-dev.yml,
terraform-production-release.yml, pr-checks.yml); SETTINGS below is the one list of them, and
scripts/tests/test_setup_repo.py fails if a workflow starts reading a name that is not in it.
docs/deploying-your-own.md explains each setting at more length.

How it is built, and why:

* **Ask first, write last.** Every answer is collected and checked before anything is changed.
  The person then sees a summary (secret values masked) and confirms once. Only then does
  `apply_actions` run, and it is the ONLY function that changes anything: GitHub settings, the
  local `.pii-denylist`, this clone's git config.
* **"Atomic", honestly.** GitHub cannot set several secrets as one step, so this cannot be a real
  transaction. What it promises: nothing is written if any answer is invalid or the person
  cancels; if a write fails part-way it stops at once and says exactly what was written and what
  was not. It never tries to put an old value back, because a secret's value cannot be read. The
  next run sees what is set and asks only for the rest.
* **A dry run cannot write.** `--dry-run` returns before `apply_actions`, and for the whole run
  the command runner is wrapped by `read_only`, which refuses any command that is not on a short
  list of ones that only read. So a mistake elsewhere in this file fails loudly instead of writing.
* **Secret values stay out of sight.** They go to `gh` on standard input, never as an argument
  (arguments show in the process list) and never through a file. They are never printed: the
  summary shows a length and at most the last two characters, and the personal-data list shows
  only a count.
* **Testable without GitHub.** `main` takes the command runner, the line reader and the output
  streams as arguments, the way review_inbox.py takes its key reader.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# The checker owns the denylist's format and file name; reading them from it keeps the secret this
# script sets and the file it writes in the form the hook and the CI job parse.
from pii_denylist_check import DENYLIST_FILE, parse_entries

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = "production"  # the GitHub environment terraform-production-release.yml deploys through
HOOKS_PATH = ".githooks"  # what the README and .githooks/pre-commit say to set core.hooksPath to
REDACTED = "<redacted>"
MIN_DENYLIST_ENTRY = 4  # shorter than this and an entry matches ordinary text all over the repo


@dataclass(frozen=True)
class Role:
    """A deploy role as infra/bootstrap creates it (main.tf's aws_iam_role names, outputs.tf's
    outputs). The test suite checks both against those files, so they cannot drift."""

    name: str
    output: str
    credentials: str


ROLES = {
    "dev": Role("gha-bloggerbear-dev-deploy", "dev_deploy_role_arn", "the dev account"),
    "prod": Role("gha-bloggerbear-prod-deploy", "prod_deploy_role_arn", "the production account"),
}


@dataclass(frozen=True)
class Setting:
    """One GitHub setting.

    kind:    "secret" or "variable". A variable prints unmasked in public logs, so only a value
             that is public anyway is one.
    where:   "repo" (repository level) or "production" (that GitHub environment). Dev's must be
             at repository level: the dev job runs in no environment.
    need:    "required"; "fork" (the original deployment leaves it unset, a fork must set it); or
             "optional". Enter leaves a "fork" or "optional" setting unset.
    check:   which entry of CHECKS validates the answer.
    env:     "dev" or "prod", for settings that belong to one deployment (pairs a role ARN with
             its account ID).
    same_as: an earlier setting whose answer is offered again, without printing it.
    tf_var:  the Terraform variable the workflow passes it to as TF_VAR_<name>, if any.
    """

    name: str
    kind: str
    where: str
    need: str
    check: str
    help: str
    env: str = ""
    same_as: str = ""
    tf_var: str = ""


_CIDR_HELP = (
    "The public IP addresses allowed to call the admin API. Everything else is blocked, and with\n"
    "this unset nothing can call it at all. Give one or more IPv4 ranges separated by commas, such\n"
    "as 203.0.113.7/32 for a single address (/32 means exactly that one). Find yours by searching\n"
    "for \"what is my IP\"."
)
_EMAIL_HELP = (
    "Where alarm emails go. AWS sends a confirmation email first; alarms only arrive after you\n"
    "click the link in it."
)
_ACCOUNT_HELP = (
    "The 12-digit AWS account ID. It does not choose the account (the role does that). It is a\n"
    "check: a deploy stops before changing anything if the role turns out to be in another account."
)
_BUCKET_HELP = (
    "The S3 bucket that holds Terraform's state: the bootstrap's state_bucket_name output. Leave\n"
    "it blank to use the bucket named in the backend block (bloggerbear-terraform-state), which\n"
    "belongs to the original deployment, so a fork must set its own."
)

# THE list. Order matters: an account ID comes before the role ARN that is checked against it, and
# a dev setting before the production one that can reuse its answer.
SETTINGS: tuple[Setting, ...] = (
    Setting("ADMIN_ALLOWED_CIDRS_DEV", "secret", "repo", "required", "cidrs",
            _CIDR_HELP, env="dev", tf_var="admin_allowed_cidrs"),
    Setting("ADMIN_ALLOWED_CIDRS_PROD", "secret", PRODUCTION, "required", "cidrs",
            _CIDR_HELP, env="prod", same_as="ADMIN_ALLOWED_CIDRS_DEV", tf_var="admin_allowed_cidrs"),
    Setting("ALERT_EMAIL_DEV", "secret", "repo", "optional", "email",
            _EMAIL_HELP, env="dev", tf_var="alert_email"),
    Setting("ALERT_EMAIL_PROD", "secret", PRODUCTION, "optional", "email",
            _EMAIL_HELP, env="prod", same_as="ALERT_EMAIL_DEV", tf_var="alert_email"),
    Setting("AWS_DEV_ACCOUNT_ID", "secret", "repo", "optional", "account_id",
            _ACCOUNT_HELP, env="dev", tf_var="aws_account_id"),
    Setting("AWS_PROD_ACCOUNT_ID", "secret", PRODUCTION, "optional", "account_id",
            _ACCOUNT_HELP + "\nWith one AWS account for both, it is the same as dev's.",
            env="prod", same_as="AWS_DEV_ACCOUNT_ID", tf_var="aws_account_id"),
    Setting("AWS_DEV_DEPLOY_ROLE_ARN", "secret", "repo", "required", "role_arn",
            "The IAM role the dev deploy signs in as, through GitHub OIDC. No access keys are stored.",
            env="dev"),
    Setting("AWS_PROD_DEPLOY_ROLE_ARN", "secret", PRODUCTION, "required", "role_arn",
            "The IAM role a production release signs in as, through GitHub OIDC.",
            env="prod"),
    Setting("TF_STATE_BUCKET_DEV", "secret", "repo", "fork", "bucket", _BUCKET_HELP, env="dev"),
    Setting("TF_STATE_BUCKET_PROD", "secret", PRODUCTION, "fork", "bucket",
            _BUCKET_HELP + "\nWith one AWS account for both, it is the same bucket as dev's.",
            env="prod", same_as="TF_STATE_BUCKET_DEV"),
    Setting("UNIQUE_NAME_SUFFIX", "variable", "repo", "fork", "suffix",
            "A short word added to names that must be unique across all of AWS: the content and site\n"
            "buckets and the sign-in address. The original deployment holds the plain names, so a fork\n"
            "needs its own, such as -yourname (it is added to the end as typed, so start with a\n"
            "hyphen). Lowercase letters, digits and hyphens, at most 20. Set it BEFORE your first\n"
            "deploy and never change it: a bucket cannot be renamed, so changing it later deletes the\n"
            "buckets and makes empty ones. It is a variable, not a secret: it ends up in public names.",
            tf_var="unique_name_suffix"),
    Setting("PII_DENYLIST", "secret", "repo", "optional", "denylist",
            "A list of strings, such as your real name or home address, that must never be committed\n"
            "or pushed. The pre-commit hook and the pii-denylist check on pull requests refuse any\n"
            "change that adds one, without ever printing it."),
)

# Names the workflows read that this script deliberately does not ask for.
NOT_ASKED = {
    # GitHub creates this for every workflow run. Nobody sets it.
    "GITHUB_TOKEN",
    # Only claude-issue-worker.yml reads it. That workflow is an optional extra with its own
    # sign-in step; no deploy needs it.
    "CLAUDE_CODE_OAUTH_TOKEN",
}

# Secrets the workflows still read as `secrets.X || vars.X`, left over from when they were
# variables. This script only ever sets the secret, and tells you if the variable is still there.
# The account IDs and the state buckets are deliberately NOT here: they are read from secrets only.
VARIABLE_FALLBACKS = {
    "AWS_DEV_DEPLOY_ROLE_ARN",
    "AWS_PROD_DEPLOY_ROLE_ARN",
    "ALERT_EMAIL_DEV",
    "ALERT_EMAIL_PROD",
}

_NEED_LABEL = {"required": "required", "fork": "required for a fork", "optional": "optional"}


# --- Checks ----------------------------------------------------------------------------------------


class Invalid(ValueError):
    """An answer failed its check. The message is shown to the person; it never repeats the answer."""


def check_cidrs(raw: str) -> str:
    """One or more IPv4 ranges -> the Terraform list the workflow passes on, e.g. ["203.0.113.7/32"].

    IPv6 is recognised and refused rather than passed through: the allowlist is a WAF IP set with
    ip_address_version = "IPV4" (infra/environments/*/main.tf), so one IPv6 entry fails the whole
    apply. A /0 is refused too: it would open the admin API to everyone, and AWS WAF rejects it."""
    text = raw.strip()
    if text.startswith("["):  # already a Terraform/JSON list, as the docs show it
        try:
            parts = json.loads(text)
        except ValueError:
            raise Invalid("That list is not valid. Type the ranges separated by commas.") from None
        if not isinstance(parts, list) or not all(isinstance(part, str) for part in parts):
            raise Invalid("That list is not valid. Type the ranges separated by commas.")
    else:
        parts = [part for part in re.split(r"[,\s]+", text) if part]
    if not parts:
        raise Invalid("Give at least one range, such as 203.0.113.7/32.")
    ranges: list[str] = []
    for position, part in enumerate(parts, start=1):
        try:
            network = ipaddress.ip_network(part.strip(), strict=True)
        except ValueError:
            raise Invalid(
                f"Range {position} is not valid. Use an address and a size, such as 203.0.113.7/32. "
                "The address must be the first one in its range."
            ) from None
        if network.version == 6:
            raise Invalid(
                f"Range {position} is IPv6. The admin allowlist only takes IPv4 ranges; "
                "an IPv6 one would make the deploy fail."
            )
        if network.prefixlen == 0:
            raise Invalid(
                "STOP: 0.0.0.0/0 is every address on the internet. It would open the admin API to "
                "everyone, and AWS refuses it. Give your own address, such as 203.0.113.7/32."
            )
        if str(network) not in ranges:
            ranges.append(str(network))
    return json.dumps(ranges)


def wide_ranges(value: str) -> int:
    """How many of a checked CIDR answer's ranges cover more than 65,536 addresses."""
    return sum(1 for item in json.loads(value) if ipaddress.ip_network(item).prefixlen < 16)


def check_email(raw: str) -> str:
    text = raw.strip()
    if not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;.]{2,}", text):
        raise Invalid("That does not look like one email address (name@example.com).")
    return text


def check_account_id(raw: str) -> str:
    text = raw.strip()
    if not re.fullmatch(r"[0-9]{12}", text):
        raise Invalid("An AWS account ID is exactly 12 digits, with no spaces or dashes.")
    return text


_ROLE_ARN = re.compile(r"arn:aws:iam::([0-9]{12}):role/([\w+=,.@/-]{1,128})")


def check_role_arn(raw: str, account: str = "") -> str:
    """`account` is the ID given for the same environment; an ARN in any other account is refused,
    because the deploy would stop on exactly that mismatch."""
    text = raw.strip()
    match = _ROLE_ARN.fullmatch(text)
    if not match:
        raise Invalid("A role ARN looks like arn:aws:iam::<12 digits>:role/<name>.")
    if account and match.group(1) != account:
        raise Invalid(
            "That role is in a different AWS account from the account ID you gave for this "
            "environment. Fix whichever is wrong: the deploy refuses to run when they differ."
        )
    return text


def check_bucket(raw: str) -> str:
    text = raw.strip()
    problem = ""
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", text):
        problem = (
            "3 to 63 lowercase letters, digits, dots and hyphens, starting and ending with a letter or digit"
        )
    elif ".." in text or ".-" in text or "-." in text:
        problem = "no two dots together, and no dot next to a hyphen"
    elif re.fullmatch(r"[0-9]+(\.[0-9]+){3}", text):
        problem = "not shaped like an IP address"
    elif text.startswith(("xn--", "sthree-", "amzn-s3-demo-")) or text.endswith(("-s3alias", "--ol-s3")):
        problem = "not a prefix or ending that S3 reserves"
    if problem:
        raise Invalid(f"That is not a valid S3 bucket name. The rule: {problem}.")
    return text


def check_suffix(raw: str) -> str:
    """The same rule as the Terraform variable's validation (unique_name_suffix)."""
    text = raw.strip()
    if not re.fullmatch(r"[a-z0-9-]{0,19}[a-z0-9]", text):
        raise Invalid(
            "Use up to 20 lowercase letters, digits and hyphens, ending in a letter or digit, "
            "such as -yourname."
        )
    return text


def check_denylist(raw: str) -> str:
    """Entries, one per line -> the text the checker parses. Used on the whole collected list."""
    entries = parse_entries(raw)
    if not entries:
        raise Invalid("Give at least one entry.")
    return "\n".join(entries)


CHECKS: dict[str, Callable[..., str]] = {
    "cidrs": check_cidrs,
    "email": check_email,
    "account_id": check_account_id,
    "role_arn": check_role_arn,
    "bucket": check_bucket,
    "suffix": check_suffix,
    "denylist": check_denylist,
}


def mask(value: str) -> str:
    """What a summary may show of a secret: its length, and the last two characters when it is
    long enough that two characters give nothing away."""
    if len(value) >= 8:
        return f'{len(value)} characters, ending "{value[-2:]}"'
    return f"{len(value)} characters"


def shown(setting: Setting, value: str) -> str:
    if setting.kind == "variable":
        return value  # public by definition: it prints in every log
    if setting.check == "cidrs":
        count = len(json.loads(value))
        return f"{count} range{'s' if count != 1 else ''}"
    if setting.check == "denylist":
        count = len(value.splitlines())
        return f"{count} entr{'ies' if count != 1 else 'y'}"
    return mask(value)


# --- Running commands ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Result:
    code: int
    out: str = ""
    err: str = ""


Run = Callable[..., Result]  # run(argv, stdin=None) -> Result


def run_command(argv: list[str], stdin: str | None = None) -> Result:
    """Run one command. `stdin` is sent as UTF-8 bytes, exactly as given: text mode would turn a
    newline into CRLF on Windows and change a multi-line secret."""
    try:
        done = subprocess.run(  # noqa: S603 - argv is a list built in this file, never a shell string
            argv, input=None if stdin is None else stdin.encode("utf-8"), capture_output=True, check=False
        )
    except OSError as error:  # not installed, or not runnable
        return Result(127, "", str(error))
    return Result(
        done.returncode,
        done.stdout.decode("utf-8", errors="replace"),
        done.stderr.decode("utf-8", errors="replace"),
    )


class WriteInDryRun(RuntimeError):
    """A dry run tried to run a command that is not on the read-only list. A bug, never a mode."""


_GH_API_WRITE_FLAGS = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"}


def is_read_only(argv: list[str]) -> bool:
    """True only for the handful of commands this script uses to look, never to change."""
    if argv[:1] == ["gh"]:
        rest = argv[1:]
        if rest in (["--version"], ["auth", "status"]) or rest[:2] == ["repo", "view"]:
            return True
        # `gh api` is a GET unless told otherwise or given fields.
        return rest[:1] == ["api"] and not any(
            arg in _GH_API_WRITE_FLAGS or arg.startswith(("--method=", "--field=", "--input="))
            for arg in rest
        )
    if argv[:1] == ["git"]:
        rest = argv[3:] if argv[1:2] == ["-C"] else argv[1:]
        return rest[:2] == ["config", "--get"] or rest[:1] == ["check-ignore"]
    return False


def read_only(run: Run) -> Run:
    """Wrap a runner so that anything but a read raises. A dry run uses this for its whole life."""

    def guarded(argv: list[str], stdin: str | None = None) -> Result:
        if stdin is not None or not is_read_only(argv):
            raise WriteInDryRun(f"dry run refused to run: {' '.join(argv[:3])} ...")
        return run(argv, stdin)

    return guarded


# --- What is already set ---------------------------------------------------------------------------


@dataclass
class State:
    """Names (never values) of what exists. None means "could not find out"."""

    repo_secrets: set[str] | None = None
    repo_variables: set[str] | None = None
    env_exists: bool | None = None
    env_secrets: set[str] | None = None
    env_variables: set[str] | None = None


def _names(run: Run, path: str, key: str) -> set[str] | None:
    result = run(["gh", "api", path, "--paginate", "--jq", f".{key}[].name"])
    return set(result.out.split()) if result.code == 0 else None


def read_state(run: Run, repo: str) -> State:
    """List secrets and variables by name. The API has no way to return a secret's value."""
    state = State(
        repo_secrets=_names(run, f"repos/{repo}/actions/secrets", "secrets"),
        repo_variables=_names(run, f"repos/{repo}/actions/variables", "variables"),
    )
    environment = run(["gh", "api", f"repos/{repo}/environments/{PRODUCTION}", "--jq", ".name"])
    if environment.code == 0:
        state.env_exists = True
        state.env_secrets = _names(run, f"repos/{repo}/environments/{PRODUCTION}/secrets", "secrets")
        state.env_variables = _names(run, f"repos/{repo}/environments/{PRODUCTION}/variables", "variables")
    elif "404" in environment.err or "Not Found" in environment.err:
        # Only trust "missing" when the repository itself could be read.
        state.env_exists = False if state.repo_secrets is not None else None
    return state


def status(setting: Setting, state: State) -> str:
    """"set", "set (repository level)", "not set" or "unknown"."""
    secret = setting.kind == "secret"
    at_repo = state.repo_secrets if secret else state.repo_variables
    if setting.where == "repo":
        if at_repo is None:
            return "unknown"
        return "set" if setting.name in at_repo else "not set"
    at_env = state.env_secrets if secret else state.env_variables
    if at_env is not None and setting.name in at_env:
        return "set"
    # A production setting may also live at repository level; the workflow reads either.
    if at_repo is not None and setting.name in at_repo:
        return "set (repository level)"
    if at_repo is None or (at_env is None and state.env_exists is not False):
        return "unknown"
    return "not set"


def status_table(settings: tuple[Setting, ...], state: State) -> str:
    rows = [("Setting", "Kind", "Where", "Needed", "Now")]
    for setting in settings:
        where = "repository" if setting.where == "repo" else f"{PRODUCTION} environment"
        rows.append((setting.name, setting.kind, where, _NEED_LABEL[setting.need], status(setting, state)))
    widths = [max(len(row[column]) for row in rows) for column in range(5)]
    lines = [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip() for row in rows
    ]
    lines.insert(1, "  ".join("-" * width for width in widths))
    return "\n".join(lines)


# --- The plan, and the one function that carries it out ----------------------------------------------


@dataclass(frozen=True)
class Action:
    """One change to make. `value` is only ever read by apply_actions."""

    kind: str  # "secret" | "variable" | "file" | "hooks"
    name: str
    where: str = "repo"
    value: str = field(default="", repr=False)  # repr=False: a stray print or traceback cannot show it

    def argv(self, repo: str, root: Path) -> list[str]:
        if self.kind == "hooks":
            return ["git", "-C", str(root), "config", "core.hooksPath", HOOKS_PATH]
        argv = ["gh", self.kind, "set", self.name, "--repo", repo]
        return argv + (["--env", PRODUCTION] if self.where == PRODUCTION else [])

    def label(self) -> str:
        if self.kind == "file":
            return f"{DENYLIST_FILE} (local file)"
        if self.kind == "hooks":
            return "core.hooksPath (this clone's git config)"
        return self.name

    def described(self, repo: str, root: Path) -> str:
        """The command a dry run shows. The value is never part of it."""
        if self.kind == "file":
            return f"add entries to {root / DENYLIST_FILE}   (entries: {REDACTED})"
        if self.kind == "hooks":
            return "git config core.hooksPath " + HOOKS_PATH
        return " ".join(self.argv(repo, root)) + f"   (value on standard input: {REDACTED})"


def _scrub(text: str, actions: list[Action]) -> str:
    """Remove every planned value from text that came back from a command, before it is shown."""
    for action in actions:
        for piece in [action.value, *action.value.splitlines()]:
            if len(piece) >= MIN_DENYLIST_ENTRY:
                text = text.replace(piece, REDACTED)
    return text.strip()


def _append_denylist(path: Path, entries: str) -> None:
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    have = set(parse_entries(existing))
    new = [entry for entry in entries.splitlines() if entry not in have]
    if new:
        separator = "" if not existing or existing.endswith("\n") else "\n"
        path.write_text(existing + separator + "\n".join(new) + "\n", encoding="utf-8", newline="\n")


def apply_actions(
    actions: list[Action], repo: str, root: Path, run: Run, out
) -> tuple[list[Action], Action | None, list[Action]]:
    """Carry out the plan, in order. THE ONLY FUNCTION IN THIS FILE THAT CHANGES ANYTHING.

    Stops at the first failure (or Ctrl-C) and returns (written, failed, not attempted). Nothing is
    undone: an overwritten secret's old value cannot be read, so there is nothing to put back.
    Values go to `gh` on standard input only."""
    written: list[Action] = []
    for index, action in enumerate(actions):
        try:
            if action.kind == "file":
                _append_denylist(root / DENYLIST_FILE, action.value)
                result = Result(0)
            elif action.kind == "hooks":
                result = run(action.argv(repo, root))
            else:
                result = run(action.argv(repo, root), action.value)
        except KeyboardInterrupt:
            result = Result(130, "", "interrupted")
        except OSError as error:
            result = Result(1, "", str(error))
        if result.code != 0:
            print(f"  FAILED   {action.label()}", file=out)
            detail = _scrub(result.err or result.out, actions)
            if detail:
                print(f"           {detail.splitlines()[-1]}", file=out)
            return written, action, actions[index + 1 :]
        written.append(action)
        print(f"  set      {action.label()}", file=out)
    return written, None, []


# --- Asking ----------------------------------------------------------------------------------------


class Prompter:
    """Writes a question to `out`, reads one line from `reader`. Ctrl-C and end-of-input are left
    to rise to main, which turns them into a clean "nothing was written" exit."""

    def __init__(self, reader: Callable[[], str], out):
        self._reader = reader
        self.out = out

    def say(self, text: str = "") -> None:
        print(text, file=self.out)

    def line(self, prompt: str) -> str:
        self.out.write(prompt)
        self.out.flush()
        return self._reader().strip()

    def yes(self, question: str, default: bool) -> bool:
        hint = "[Y/n]" if default else "[y/N]"
        while True:
            answer = self.line(f"{question} {hint} ").lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self.say("Please answer y or n.")


def role_steps(setting: Setting, repo: str, have_account: bool) -> str:
    """How to create the role and find its ARN: what infra/bootstrap does, with placeholders."""
    role = ROLES[setting.env]
    # A placeholder, never the ID itself: the steps are printed, and the ID is treated as a secret.
    account = "<the account id you gave>" if have_account else "<account id>"
    lines = [
        "How to get it:",
        "  1. The role is made by the one-time bootstrap in infra/bootstrap. Run it yourself, with",
        f"     admin credentials for {role.credentials}. CI never runs it.",
        "       cd infra/bootstrap",
        "       terraform init",
        "       terraform apply \\",
        '         -var="aws_account_id=<the account id>" \\',
        f'         -var="github_repo={repo}" \\',
        '         -var="state_bucket_name=<a bucket name of your own>" \\',
        '         -var="domain_name=<example.com, or empty for no domain>" \\',
        '         -var="budget_alert_email=<you@example.com>"',
        "  2. Read the ARN from the bootstrap's outputs:",
        f"       terraform output {role.output}",
        f"  3. Bootstrap always names this role {role.name}, so the ARN is:",
        f"       arn:aws:iam::{account}:role/{role.name}",
    ]
    if setting.env == "prod":
        lines.append(
            "  With two AWS accounts, run the bootstrap a second time with the production account's\n"
            "  credentials and its own state bucket. See docs/deploying-your-own.md, \"One account or two\"."
        )
    if have_account:
        lines.append(
            "  Press Enter to use the ARN from step 3. You can accept it before running the bootstrap:\n"
            "  the name is fixed, and a deploy simply fails to sign in until the role exists."
        )
    return "\n".join(lines)


def ask_denylist(prompter: Prompter, root: Path) -> tuple[str, int]:
    """Collect the personal strings. Returns (the secret's text or "", how many came from the
    local file). Entries are never written to the output."""
    path = root / DENYLIST_FILE
    entries: list[str] = []
    local = parse_entries(path.read_text(encoding="utf-8")) if path.is_file() else []
    from_file = 0
    if local:
        count = f"{len(local)} entr{'ies' if len(local) != 1 else 'y'}"
        if prompter.yes(f"Your local {DENYLIST_FILE} has {count}. Use them for the secret?", True):
            entries, from_file = list(local), len(local)
    prompter.say(
        "Type one entry per line"
        + (" to add more" if entries else "")
        + ". Matching ignores case. An empty line finishes."
    )
    while True:
        raw = prompter.line(f"  entry {len(entries) + 1}: ")
        if not raw:
            break
        if raw.startswith("#"):
            prompter.say("  An entry cannot start with #: the checker reads that as a comment.")
        elif len(raw) < MIN_DENYLIST_ENTRY:
            prompter.say(f"  Too short. Under {MIN_DENYLIST_ENTRY} characters it would match ordinary text.")
        elif raw.lower() in entries:
            prompter.say("  Already in the list.")
        else:
            entries.append(raw.lower())
    return "\n".join(entries), from_file


def ask_setting(
    setting: Setting, prompter: Prompter, answers: dict[str, str], repo: str
) -> str:
    """Ask for one ordinary setting until the answer passes its check. "" means leave it unset."""
    account = ""
    default = ""
    if setting.check == "role_arn":
        account = answers.get(f"AWS_{setting.env.upper()}_ACCOUNT_ID", "")
        if not account:
            # Not asked in this run (already set, or left out by --only). Its value cannot be read
            # back, so ask again, only to check the ARN against. It is not saved.
            prompter.say(
                "The role must be in the account you gave as the account ID. Type that ID again so\n"
                "the ARN can be checked against it. It is not saved. Enter skips the check."
            )
            while True:
                raw = prompter.line("  account ID to check against: ")
                if not raw:
                    break
                try:
                    account = check_account_id(raw)
                    break
                except Invalid as problem:
                    prompter.say(f"  {problem}")
        prompter.say(role_steps(setting, repo, bool(account)))
        if account:
            default = f"arn:aws:iam::{account}:role/{ROLES[setting.env].name}"

    reuse = answers.get(setting.same_as, "") if setting.same_as else ""
    if reuse and prompter.yes(f"Use the same value as {setting.same_as}?", True):
        return reuse  # already checked when it was given

    if default:
        blank = "use the ARN above"
    else:
        blank = "ask again" if setting.need == "required" else "leave it unset"
    while True:
        raw = prompter.line(f"{setting.name} (Enter: {blank}): ")
        if not raw:
            if default:
                return default
            if setting.need != "required":
                return ""
            prompter.say("This one is required. Ctrl-C stops the whole setup with nothing written.")
            continue
        try:
            check = CHECKS[setting.check]
            value = check(raw, account) if setting.check == "role_arn" else check(raw)
        except Invalid as problem:
            prompter.say(f"  {problem}")
            continue
        if setting.check == "cidrs" and wide_ranges(value):
            prompter.say("  WARNING: at least one of those ranges covers more than 65,000 addresses.")
            if not prompter.yes("  Allow all of them to reach the admin API?", False):
                continue
        return value


# --- The run ---------------------------------------------------------------------------------------


def select(only: list[str] | None) -> tuple[Setting, ...]:
    if not only:
        return SETTINGS
    wanted = {name.upper() for name in only}
    unknown = sorted(wanted - {setting.name for setting in SETTINGS})
    if unknown:
        known = ", ".join(setting.name for setting in SETTINGS)
        raise SystemExit(f"setup_repo: no such setting: {', '.join(unknown)}. The settings are: {known}")
    return tuple(setting for setting in SETTINGS if setting.name in wanted)


def find_repo(args, run: Run, gh_ready: bool) -> str:
    if args.repo:
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo):
            raise SystemExit("setup_repo: --repo must look like owner/name")
        return args.repo
    if gh_ready:
        found = run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"])
        if found.code == 0 and found.out.strip():
            return found.out.strip()
    return ""


def hooks_step(prompter: Prompter, run: Run, root: Path) -> Action | None:
    """Explain the pre-commit hook; return the action that turns it on, if the person wants it."""
    prompter.say("\n== The git hook that stops personal data being committed ==")
    prompter.say(
        "This repository has a pre-commit hook (.githooks/pre-commit). Before each commit it checks\n"
        f"what you are adding against your {DENYLIST_FILE}, and runs gitleaks (if installed) for\n"
        "secrets, email addresses and AWS account IDs. CI runs the same checks, but only after a\n"
        "push, when the content is already on GitHub. Git does not turn hooks on by itself. Once per\n"
        "clone:\n"
        f"    git config core.hooksPath {HOOKS_PATH}"
    )
    current = run(["git", "-C", str(root), "config", "--get", "core.hooksPath"])
    # Git reads a relative hooksPath from the top of the clone; some tools write it out in full.
    if current.code == 0 and (root / current.out.strip()).resolve() == (root / HOOKS_PATH).resolve():
        prompter.say("It is already turned on for this clone.")
        return None
    if current.code not in (0, 1) or not (root / HOOKS_PATH / "pre-commit").is_file():
        prompter.say("Could not check this clone. Run the command above from the repository's folder.")
        return None
    if current.code == 0:
        prompter.say("This clone already uses a different hooks folder. Turning this on replaces that.")
    if prompter.yes("Turn it on for this clone? It changes only your local git config.", current.code == 1):
        return Action("hooks", "core.hooksPath")
    return None


def _setup(args, reader: Callable[[], str], out, err, run: Run, root: Path) -> int:
    dry = args.dry_run
    if dry:
        run = read_only(run)  # from here on, a write is impossible, not merely skipped
    prompter = Prompter(reader, out)
    settings = select(args.only)

    if dry:
        prompter.say("DRY RUN. You will be asked everything; nothing will be changed.\n")

    # 1. gh, and which repository.
    gh_ready = run(["gh", "--version"]).code == 0 and run(["gh", "auth", "status"]).code == 0
    if not gh_ready and not dry:
        print(
            "setup_repo: the GitHub CLI is not installed or not signed in.\n"
            "Install it from https://cli.github.com, run `gh auth login`, then run this again.",
            file=err,
        )
        return 1
    repo = find_repo(args, run, gh_ready)
    if not repo:
        if not dry:
            print("setup_repo: could not tell which repository this is. Pass --repo owner/name.", file=err)
            return 1
        repo = "<owner>/<name>"
    prompter.say(f"Repository: {repo}")
    prompter.say("In a fork, gh can pick the original repository instead of yours. Read the name.")
    if not prompter.yes("Is this the repository to set up?", False):
        prompter.say("Stopped. Nothing was written. Pass --repo owner/name to choose another.")
        return 1

    # 2. What is already there.
    state = read_state(run, repo) if gh_ready else State()
    prompter.say("")
    prompter.say(status_table(settings, state))
    prompter.say("")
    if not gh_ready:
        prompter.say("Could not check what is already set: gh is not installed or not signed in.")
    elif state.repo_secrets is None:
        prompter.say(
            "Could not check what is already set (you may not have admin access to this repository).\n"
            "Anything you answer below would replace what is there."
        )
    for setting in settings:
        variables = (state.repo_variables or set()) | (state.env_variables or set())
        if setting.kind == "secret" and setting.name in variables:
            prompter.say(
                f"Note: {setting.name} also exists as a plain variable. A variable prints in logs "
                "unmasked. Delete it once the secret is set."
            )

    asking = list(settings)
    if state.env_exists is False:
        prompter.say(
            f"There is no '{PRODUCTION}' environment on this repository yet. Create it under\n"
            "Settings > Environments, add yourself as a required reviewer and limit deployments to\n"
            "v* tags. This script does not create it: who may approve a release is yours to decide."
        )
        if not dry:
            asking = [setting for setting in asking if setting.where != PRODUCTION]
            prompter.say("Production's settings are left out of this run. Run it again afterwards.")

    already = [setting for setting in asking if status(setting, state).startswith("set")]
    if already:
        prompter.say(f"{len(already)} already set. They are skipped unless you choose to replace them.")
        replace = set()
        if prompter.yes("Replace any of them?", False):
            replace = {
                setting.name for setting in already if prompter.yes(f"  Replace {setting.name}?", False)
            }
        asking = [setting for setting in asking if setting not in already or setting.name in replace]

    # 3. Ask. Nothing is written in this part.
    answers: dict[str, str] = {}
    actions: list[Action] = []
    for setting in asking:
        where = "repository" if setting.where == "repo" else f"{PRODUCTION} environment"
        prompter.say(f"\n== {setting.name} ({setting.kind}, {where}, {_NEED_LABEL[setting.need]}) ==")
        prompter.say(setting.help)
        if setting.check == "denylist":
            value, from_file = ask_denylist(prompter, root)
        else:
            value, from_file = ask_setting(setting, prompter, answers, repo), 0
        if not value:
            prompter.say("Left unset.")
            continue
        answers[setting.name] = value
        actions.append(Action(setting.kind, setting.name, setting.where, value))
        if setting.check == "denylist" and len(value.splitlines()) > from_file:
            ignored = run(["git", "-C", str(root), "check-ignore", "-q", DENYLIST_FILE])
            if ignored.code != 0:
                prompter.say(
                    f"Not saving a local {DENYLIST_FILE}: git does not ignore that file here, so it could\n"
                    "be committed, which would publish the very strings it lists."
                )
            elif prompter.yes(f"Also save them to your local {DENYLIST_FILE}, which the hook reads?", True):
                actions.append(Action("file", DENYLIST_FILE, "local", value))

    hooks = hooks_step(prompter, run, root)
    if hooks:
        actions.append(hooks)

    # 4. Summary. Secret values are masked; the personal-data list is only a count.
    prompter.say("\n== Summary ==")
    if not actions:
        prompter.say("Nothing to set.")
        return 0
    by_name = {setting.name: setting for setting in SETTINGS}
    for action in actions:
        if action.name in by_name and action.kind in ("secret", "variable"):
            place = "repository" if action.where == "repo" else f"{PRODUCTION} environment"
            value = shown(by_name[action.name], action.value)
            prompter.say(f"  {action.name}  [{action.kind}, {place}]  {value}")
        else:
            prompter.say(f"  {action.label()}")

    if dry:
        prompter.say("\nA real run would now ask you to confirm, then run:")
        for action in actions:
            prompter.say(f"  {action.described(repo, root)}")
        prompter.say("\nDry run: nothing was changed.")
        return 0

    # 5. One confirmation, then the only call that writes.
    prompter.say(
        "\nNothing has been written yet. GitHub sets these one at a time: if one fails, the script\n"
        "stops and tells you which were set. Old values are not restored (they cannot be read)."
    )
    if not prompter.yes(f"Set these {len(actions)} on {repo} now?", False):
        prompter.say("Cancelled. Nothing was written.")
        return 1
    prompter.say("")
    written, failed, remaining = apply_actions(actions, repo, root, run, out)
    if failed is None:
        prompter.say(f"\nDone. {len(written)} set.")
        return 0
    prompter.say(f"\nStopped at {failed.label()}. This is where things stand:")
    prompter.say("  Written:     " + (", ".join(action.label() for action in written) or "nothing"))
    prompter.say("  NOT written: " + ", ".join(action.label() for action in [failed, *remaining]))
    prompter.say("Nothing was undone. Fix the problem and run this again: it asks only for what is missing.")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="setup_repo.py",
        description="Set the GitHub secrets and variables a BloggerBear deployment needs.",
        epilog=(
            "examples:\n"
            "  python scripts/setup_repo.py --dry-run\n"
            "      Ask everything, check every answer, show what would be set. Changes nothing.\n"
            "  python scripts/setup_repo.py\n"
            "      The same, then set them after you confirm.\n"
            "  python scripts/setup_repo.py --repo your-name/your-fork\n"
            "      Name the repository instead of letting gh work it out.\n"
            "  python scripts/setup_repo.py --only ADMIN_ALLOWED_CIDRS_DEV ADMIN_ALLOWED_CIDRS_PROD\n"
            "      Just these, for example after your IP address changed.\n\n"
            "settings: " + ", ".join(setting.name for setting in SETTINGS) + "\n\n"
            "There is no --yes: secrets are only ever set after you have seen the summary."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="step through every question and check; change nothing"
    )
    parser.add_argument("--repo", metavar="OWNER/NAME", help="the repository to set up (default: ask gh)")
    parser.add_argument("--only", nargs="+", metavar="SETTING", help="ask only for these settings")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    reader: Callable[[], str] = input,
    out=None,
    err=None,
    run: Run = run_command,
    root: Path = ROOT,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        return _setup(args, reader, out, err, run, root)
    except (KeyboardInterrupt, EOFError):
        # Every question comes before the first write, and apply_actions handles its own Ctrl-C,
        # so reaching here always means nothing was changed.
        print("\nCancelled. Nothing was written.", file=out)
        return 130


if __name__ == "__main__":
    sys.exit(main())
