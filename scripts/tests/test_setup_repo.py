"""Tests for scripts/setup_repo.py: the first-time setup of a repository's GitHub settings.

`gh` and `git` are played by FakeCommands and the keyboard by a list of answers, so nothing here
touches GitHub, AWS or this clone's git config.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

import setup_repo as sr

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
REPO = "me/my-fork"

# The answers a person gives. None of these may ever appear in the output: each is distinctive
# enough that finding it there means a value leaked. (Placeholder account IDs, a documentation
# address range and example.com, so the secret scanner accepts this file.)
CIDR = "198.51.100.23/32"
EMAIL = "zz9marker@example.com"
DEV_ACCOUNT = "111111111111"
PROD_ACCOUNT = "123456789012"
BUCKET = "zz9marker-state"
PII = "Zz9 Marker Street"
SUFFIX = "-zz9fork"
SECRET_VALUES = (CIDR, "198.51.100.23", EMAIL, DEV_ACCOUNT, PROD_ACCOUNT, BUCKET, PII, PII.lower())

# A whole first run, nothing set yet, in the order the questions come.
FULL_RUN = [
    "y",  # this is the repository
    CIDR,  # ADMIN_ALLOWED_CIDRS_DEV
    "y",  # ADMIN_ALLOWED_CIDRS_PROD: same as dev
    EMAIL,  # ALERT_EMAIL_DEV
    "y",  # ALERT_EMAIL_PROD: same as dev
    DEV_ACCOUNT,  # AWS_DEV_ACCOUNT_ID
    "n",  # AWS_PROD_ACCOUNT_ID: not the same as dev...
    PROD_ACCOUNT,  # ...this one
    "",  # AWS_DEV_DEPLOY_ROLE_ARN: accept the stub
    "",  # AWS_PROD_DEPLOY_ROLE_ARN: accept the stub
    BUCKET,  # TF_STATE_BUCKET_DEV
    "y",  # TF_STATE_BUCKET_PROD: same as dev
    SUFFIX,  # UNIQUE_NAME_SUFFIX
    PII,  # PII_DENYLIST, entry 1
    "",  # ...no more entries
    "y",  # save the local .pii-denylist
    "y",  # turn the hook on
]


class FakeCommands:
    """Plays `gh` and `git`. Records every call as (argv, stdin)."""

    def __init__(
        self,
        *,
        installed=True,
        signed_in=True,
        secrets=(),
        variables=(),
        env_secrets=(),
        environment=True,
        fail_on=(),
        hooks_path=None,
        ignored=True,
        aws_account=None,
        aws_signed_in=True,
        parameters=(),
        parameters_readable=True,
    ):
        # `aws`: not installed unless an account is given (most tests are about GitHub).
        self.aws_account, self.aws_signed_in = aws_account, aws_signed_in
        self.parameters, self.parameters_readable = set(parameters), parameters_readable
        self.installed, self.signed_in = installed, signed_in
        self.secrets, self.variables, self.env_secrets = set(secrets), set(variables), set(env_secrets)
        self.environment, self.fail_on = environment, set(fail_on)
        self.hooks_path, self.ignored = hooks_path, ignored
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, argv, stdin=None):
        self.calls.append((list(argv), stdin))
        if argv[0] == "git":
            rest = argv[3:]
            if rest[:2] == ["config", "--get"]:
                return sr.Result(0, f"{self.hooks_path}\n") if self.hooks_path else sr.Result(1)
            if rest[0] == "check-ignore":
                return sr.Result(0 if self.ignored else 1)
            return sr.Result(0)  # git config core.hooksPath .githooks
        if argv[0] == "aws":
            if self.aws_account is None:
                return sr.Result(127, "", "aws: not found")
            if argv[1:] == ["--version"]:
                return sr.Result(0, "aws-cli/2.27.40\n")
            if argv[1:3] == ["sts", "get-caller-identity"]:
                signed_out = sr.Result(255, "", "Unable to locate credentials.")
                return sr.Result(0, f"{self.aws_account}\n") if self.aws_signed_in else signed_out
            if argv[1:3] == ["ssm", "describe-parameters"]:
                if not self.parameters_readable:
                    return sr.Result(254, "", "AccessDeniedException")
                wanted = argv[argv.index("--parameter-filters") + 1].split("Values=")[1]
                return sr.Result(0, f"{wanted}\n" if wanted in self.parameters else "\n")
            raise AssertionError(f"unexpected aws command: {argv}")
        if not self.installed:
            return sr.Result(127, "", "gh: not found")
        rest = argv[1:]
        if rest == ["--version"]:
            return sr.Result(0, "gh version 2.60.0\n")
        if rest == ["auth", "status"]:
            return sr.Result(0 if self.signed_in else 1)
        if rest[:2] == ["repo", "view"]:
            return sr.Result(0, f"{REPO}\n")
        if rest[0] == "api":
            path = rest[1]
            if path.endswith(f"/environments/{sr.PRODUCTION}"):
                found = sr.Result(0, f"{sr.PRODUCTION}\n")
                return found if self.environment else sr.Result(1, "", "gh: Not Found (HTTP 404)")
            if path.endswith(f"/environments/{sr.PRODUCTION}/secrets"):
                return sr.Result(0, "\n".join(sorted(self.env_secrets)))
            if path.endswith("/actions/secrets"):
                return sr.Result(0, "\n".join(sorted(self.secrets)))
            repo_level = path.endswith("/actions/variables")
            return sr.Result(0, "\n".join(sorted(self.variables)) if repo_level else "")
        if rest[1] == "set":
            if rest[2] in self.fail_on:
                return sr.Result(1, "", f"failed to set {rest[2]}: HTTP 403 (the value was {stdin})")
            return sr.Result(0)
        raise AssertionError(f"unexpected command: {argv}")

    @property
    def writes(self):
        return [(argv, stdin) for argv, stdin in self.calls if not sr.is_read_only(argv)]


def clone(tmp_path: Path) -> Path:
    """A folder that looks enough like a clone: it has the hook."""
    (tmp_path / ".githooks").mkdir(exist_ok=True)
    (tmp_path / ".githooks" / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    return tmp_path


def run(argv, answers, commands, root):
    """Run main with fakes. Returns (exit code, stdout, stderr). Running out of answers is a test
    bug, so it fails loudly instead of looking like a cancel."""
    queue = list(answers)

    def reader():
        if not queue:
            raise AssertionError("the script asked more questions than the test answered")
        answer = queue.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    out, err = io.StringIO(), io.StringIO()
    code = sr.main(argv, reader=reader, out=out, err=err, run=commands, root=root)
    assert not queue, f"answers never asked for: {queue}"
    return code, out.getvalue(), err.getvalue()


# --- The settings table ------------------------------------------------------------------------------


def _workflow_names() -> tuple[set[str], set[str]]:
    secrets, variables = set(), set()
    for path in WORKFLOWS.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        secrets |= set(re.findall(r"\bsecrets\.([A-Za-z_][A-Za-z0-9_]*)", text))
        variables |= set(re.findall(r"\bvars\.([A-Za-z_][A-Za-z0-9_]*)", text))
    return secrets, variables


def test_the_table_knows_every_secret_and_variable_a_workflow_reads():
    """A workflow that starts reading a new name fails here until the table (or the commented
    ignore list) knows about it."""
    secrets, variables = _workflow_names()
    table_secrets = {s.name for s in sr.SETTINGS if s.kind == "secret"}
    table_variables = {s.name for s in sr.SETTINGS if s.kind == "variable"}
    assert secrets - table_secrets - sr.NOT_ASKED == set()
    assert variables - table_variables - sr.VARIABLE_FALLBACKS - sr.NOT_ASKED == set()
    # And nothing in the table (or the lists beside it) is stale.
    assert table_secrets <= secrets and table_variables <= variables
    assert sr.VARIABLE_FALLBACKS <= table_secrets & variables
    assert sr.NOT_ASKED <= secrets | variables


def test_the_sensitive_settings_are_secrets_with_no_variable_fallback():
    _, variables = _workflow_names()
    for name in ("AWS_DEV_ACCOUNT_ID", "AWS_PROD_ACCOUNT_ID", "TF_STATE_BUCKET_DEV", "TF_STATE_BUCKET_PROD"):
        setting = next(s for s in sr.SETTINGS if s.name == name)
        assert setting.kind == "secret"
        assert name not in variables and name not in sr.VARIABLE_FALLBACKS


def test_each_setting_is_where_the_workflow_that_reads_it_can_see_it():
    """The dev jobs run in no GitHub environment, so what they read must be at repository level."""
    dev_jobs = "".join(
        (WORKFLOWS / name).read_text(encoding="utf-8") for name in ("terraform.yml", "destroy-dev.yml")
    )
    release = (WORKFLOWS / "terraform-production-release.yml").read_text(encoding="utf-8")
    assert f"environment: {sr.PRODUCTION}" in release
    for setting in sr.SETTINGS:
        assert setting.kind in ("secret", "variable") and setting.where in ("repo", sr.PRODUCTION)
        assert setting.need in ("required", "fork", "optional") and setting.check in sr.CHECKS
        assert setting.help.strip()
        if re.search(rf"\b(?:secrets|vars)\.{setting.name}\b", dev_jobs):
            assert setting.where == "repo", setting.name
        elif setting.where == sr.PRODUCTION:
            assert re.search(rf"\bsecrets\.{setting.name}\b", release), setting.name
        if setting.same_as:  # the answer it reuses was asked for earlier
            names = [s.name for s in sr.SETTINGS]
            assert names.index(setting.same_as) < names.index(setting.name)


def test_each_tf_var_is_the_one_the_workflow_passes_and_terraform_declares():
    for setting in sr.SETTINGS:
        if not setting.tf_var:
            continue
        source = "secrets" if setting.kind == "secret" else "vars"
        workflows = "".join(path.read_text(encoding="utf-8") for path in WORKFLOWS.glob("terraform*.yml"))
        assert re.search(
            rf"TF_VAR_{setting.tf_var}: \$\{{\{{ {source}\.{setting.name}\b", workflows
        ), setting.name
        environments = {"": ("dev", "production"), "dev": ("dev",), "prod": ("production",)}[setting.env]
        for environment in environments:
            variables = (REPO_ROOT / "infra" / "environments" / environment / "variables.tf").read_text(
                encoding="utf-8"
            )
            assert f'variable "{setting.tf_var}" {{' in variables, (setting.name, environment)


def test_the_role_names_and_outputs_are_the_ones_bootstrap_really_creates():
    main = (REPO_ROOT / "infra" / "bootstrap" / "main.tf").read_text(encoding="utf-8")
    outputs = (REPO_ROOT / "infra" / "bootstrap" / "outputs.tf").read_text(encoding="utf-8")
    for env, resource in (("dev", "gha_dev_deploy"), ("prod", "gha_prod_deploy")):
        role = sr.ROLES[env]
        block = main.split(f'resource "aws_iam_role" "{resource}" {{')[1].split("\n}\n")[0]
        assert re.search(rf'^\s*name\s*=\s*"{role.name}"$', block, re.M)
        output = outputs.split(f'output "{role.output}" {{')[1].split("\n}\n")[0]
        assert f"aws_iam_role.{resource}.arn" in output
    # Every variable the printed bootstrap command passes is one bootstrap declares.
    variables = (REPO_ROOT / "infra" / "bootstrap" / "variables.tf").read_text(encoding="utf-8")
    steps = sr.role_steps(next(s for s in sr.SETTINGS if s.name == "AWS_DEV_DEPLOY_ROLE_ARN"), REPO, True)
    passed = re.findall(r'-var="([a-z_]+)=', steps)
    assert passed and all(f'variable "{name}" {{' in variables for name in passed)
    assert "cd infra/bootstrap" in steps and "terraform output dev_deploy_role_arn" in steps


def test_the_hook_and_the_denylist_file_are_what_the_repository_uses():
    hook = (REPO_ROOT / sr.HOOKS_PATH / "pre-commit").read_text(encoding="utf-8")
    assert f"git config core.hooksPath {sr.HOOKS_PATH}" in hook
    assert sr.DENYLIST_FILE == ".pii-denylist"
    assert re.search(r"^\.pii-denylist$", (REPO_ROOT / ".gitignore").read_text(encoding="utf-8"), re.M)
    # The allowlist really is IPv4-only, which is why check_cidrs refuses IPv6.
    for environment in ("dev", "production"):
        main = (REPO_ROOT / "infra" / "environments" / environment / "main.tf").read_text(encoding="utf-8")
        block = main.split("addresses          = var.admin_allowed_cidrs")[0][-400:]
        assert 'ip_address_version = "IPV4"' in block


# --- The checks ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("203.0.113.7/32", ["203.0.113.7/32"]),
        ("203.0.113.7", ["203.0.113.7/32"]),  # a bare address is that one address
        ("203.0.113.7/32, 198.51.100.0/24", ["203.0.113.7/32", "198.51.100.0/24"]),
        ("203.0.113.7/32 203.0.113.7/32", ["203.0.113.7/32"]),
        ('["203.0.113.7/32"]', ["203.0.113.7/32"]),  # the form the docs show
    ],
)
def test_cidrs_become_the_terraform_list_the_workflow_passes_on(raw, expected):
    assert json.loads(sr.check_cidrs(raw)) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "  ", "not-an-ip", "203.0.113.7/33", "203.0.113.7/24", "999.1.1.1/32", '["203.0.113.7/32"', "[1]"],
)
def test_bad_cidrs_are_refused(raw):
    with pytest.raises(sr.Invalid):
        sr.check_cidrs(raw)


def test_ipv6_is_recognised_and_refused_because_the_allowlist_is_ipv4_only():
    with pytest.raises(sr.Invalid, match="IPv6"):
        sr.check_cidrs("2001:db8::/32")


def test_the_whole_internet_is_refused_loudly():
    with pytest.raises(sr.Invalid, match="STOP.*every address"):
        sr.check_cidrs("0.0.0.0/0")
    assert sr.wide_ranges(sr.check_cidrs("10.0.0.0/8")) == 1
    assert sr.wide_ranges(sr.check_cidrs("203.0.113.7/32")) == 0


@pytest.mark.parametrize("raw", ["you@example.com", " first.last+tag@mail.example.org "])
def test_good_emails(raw):
    assert sr.check_email(raw) == raw.strip()


@pytest.mark.parametrize(
    "raw", ["", "you", "you@", "@example.com", "you@example", "a@example.com, b@example.com"]
)
def test_bad_emails(raw):
    with pytest.raises(sr.Invalid):
        sr.check_email(raw)


@pytest.mark.parametrize("raw", ["", "12345678901", "1234567890123", "1234-5678-9012", "12345678901a"])
def test_an_account_id_is_exactly_twelve_digits(raw):
    assert sr.check_account_id(" 123456789012 ") == "123456789012"
    with pytest.raises(sr.Invalid):
        sr.check_account_id(raw)


def test_a_role_arn_must_be_an_iam_role_in_the_account_given_for_that_environment():
    arn = "arn:aws:iam::111111111111:role/gha-bloggerbear-dev-deploy"
    assert sr.check_role_arn(arn) == arn
    assert sr.check_role_arn(f" {arn} ", "111111111111") == arn
    assert sr.check_role_arn("arn:aws:iam::111111111111:role/path/to/name", "111111111111")
    with pytest.raises(sr.Invalid, match="different AWS account"):
        sr.check_role_arn(arn, "123456789012")
    for bad in (
        "",
        "gha-bloggerbear-dev-deploy",
        "arn:aws:iam::1111:role/x",
        "arn:aws:iam::111111111111:user/someone",
        "arn:aws:s3:::a-bucket",
        "arn:aws:iam::111111111111:role/",
    ):
        with pytest.raises(sr.Invalid):
            sr.check_role_arn(bad)


def test_the_mismatch_message_does_not_repeat_either_account():
    with pytest.raises(sr.Invalid) as refused:
        sr.check_role_arn("arn:aws:iam::111111111111:role/x", "123456789012")
    assert "111111111111" not in str(refused.value) and "123456789012" not in str(refused.value)


@pytest.mark.parametrize("raw", ["my-state", "yourname-bloggerbear-terraform-state", "a.b-c.d", "abc"])
def test_good_bucket_names(raw):
    assert sr.check_bucket(raw) == raw


@pytest.mark.parametrize(
    "raw",
    ["", "ab", "a" * 64, "My-Bucket", "under_score", "-starts", "ends-", "two..dots", "192.168.1.1"]
    + ["xn--x", "name-s3alias", "dot.-hyphen"],
)
def test_bad_bucket_names(raw):
    with pytest.raises(sr.Invalid):
        sr.check_bucket(raw)


def test_the_suffix_rule_is_the_terraform_variables_rule():
    for env in ("dev", "production"):
        variables = (REPO_ROOT / "infra" / "environments" / env / "variables.tf").read_text(encoding="utf-8")
        assert 'can(regex("^[a-z0-9-]{0,19}[a-z0-9]$", var.unique_name_suffix))' in variables
    for good in ("-yourname", "x", "a" * 20):
        assert sr.check_suffix(good) == good
    for bad in ("", "-", "Upper", "ends-", "a" * 21, "under_score", "dot.dot"):
        with pytest.raises(sr.Invalid):
            sr.check_suffix(bad)


def test_the_denylist_is_stored_the_way_the_checker_reads_it():
    import pii_denylist_check

    value = sr.check_denylist("Jane Citizen\n\n# a comment\n12 Some Street\njane citizen\n")
    assert pii_denylist_check.parse_entries(value) == ["jane citizen", "12 some street"]
    with pytest.raises(sr.Invalid):
        sr.check_denylist("\n# only a comment\n")


def test_a_mask_shows_a_length_and_at_most_two_characters():
    assert sr.mask("123456789012") == '12 characters, ending "12"'
    assert sr.mask("short") == "5 characters"  # too short for two characters to be safe


# --- The read-only guard ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv, allowed",
    [
        (["gh", "--version"], True),
        (["gh", "auth", "status"], True),
        (["gh", "repo", "view", "--json", "nameWithOwner"], True),
        (["gh", "api", "repos/o/r/actions/secrets", "--paginate", "--jq", ".secrets[].name"], True),
        (["gh", "api", "repos/o/r/actions/secrets", "-X", "DELETE"], False),
        (["gh", "api", "repos/o/r/actions/variables", "-f", "name=X"], False),
        (["gh", "api", "--method=PUT", "repos/o/r"], False),
        (["gh", "secret", "set", "X", "--repo", "o/r"], False),
        (["gh", "variable", "set", "X", "--repo", "o/r"], False),
        (["gh", "secret", "delete", "X"], False),
        (["git", "-C", "/x", "config", "--get", "core.hooksPath"], True),
        (["git", "-C", "/x", "check-ignore", "-q", ".pii-denylist"], True),
        (["git", "-C", "/x", "config", "core.hooksPath", ".githooks"], False),
        (["rm", "-rf", "/"], False),
        # The CoinGecko step's looks, and the writes (and the decrypting read) it must never make.
        (["aws", "--version"], True),
        (["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"], True),
        (["aws", "ssm", "describe-parameters", "--region", "ap-southeast-2"], True),
        (["aws", "ssm", "put-parameter", "--name", "/x", "--type", "SecureString", "--value", "v"], False),
        (["aws", "ssm", "put-parameter", "--cli-input-json", "file://x.json"], False),
        (["aws", "ssm", "get-parameter", "--name", "/x", "--with-decryption"], False),
        (["aws", "ssm", "get-parameters-by-path", "--path", "/"], False),
        (["aws", "ssm", "delete-parameter", "--name", "/x"], False),
        (["aws", "s3", "rm", "s3://bucket", "--recursive"], False),
        (["aws"], False),
    ],
)
def test_only_commands_that_look_are_read_only(argv, allowed):
    assert sr.is_read_only(argv) is allowed


def test_the_guard_refuses_a_write_and_anything_given_a_value():
    guarded = sr.read_only(lambda argv, stdin=None: sr.Result(0))
    assert guarded(["gh", "--version"]).code == 0
    with pytest.raises(sr.WriteInDryRun):
        guarded(["gh", "secret", "set", "X", "--repo", "o/r"], "value")
    with pytest.raises(sr.WriteInDryRun):
        guarded(["gh", "api", "repos/o/r"], "a body")


def test_every_write_the_script_can_make_is_one_the_guard_refuses(tmp_path):
    for action in (
        sr.Action("secret", "X", "repo", "v"),
        sr.Action("secret", "X", sr.PRODUCTION, "v"),
        sr.Action("variable", "X", "repo", "v"),
        sr.Action("hooks", "core.hooksPath"),
    ):
        assert not sr.is_read_only(action.argv(REPO, tmp_path)), action


# --- A real run ------------------------------------------------------------------------------------


def test_a_first_run_sets_everything_with_values_on_stdin_and_never_in_argv(tmp_path):
    commands = FakeCommands()
    code, out, err = run([], [*FULL_RUN, "y"], commands, clone(tmp_path))
    assert code == 0 and err == ""

    sets = {argv[3]: (argv, stdin) for argv, stdin in commands.writes if argv[0] == "gh"}
    assert set(sets) == {setting.name for setting in sr.SETTINGS}
    assert sets["ADMIN_ALLOWED_CIDRS_DEV"][1] == json.dumps([CIDR])
    assert sets["ADMIN_ALLOWED_CIDRS_PROD"][1] == json.dumps([CIDR])
    assert sets["ALERT_EMAIL_PROD"][1] == EMAIL
    assert sets["AWS_DEV_ACCOUNT_ID"][1] == DEV_ACCOUNT and sets["AWS_PROD_ACCOUNT_ID"][1] == PROD_ACCOUNT
    # The stub: the account given for each environment, and the role name bootstrap creates.
    assert sets["AWS_DEV_DEPLOY_ROLE_ARN"][1] == f"arn:aws:iam::{DEV_ACCOUNT}:role/gha-bloggerbear-dev-deploy"
    prod_role = f"arn:aws:iam::{PROD_ACCOUNT}:role/gha-bloggerbear-prod-deploy"
    assert sets["AWS_PROD_DEPLOY_ROLE_ARN"][1] == prod_role
    assert sets["TF_STATE_BUCKET_PROD"][1] == BUCKET
    assert sets["PII_DENYLIST"][1] == PII.lower()
    assert sets["UNIQUE_NAME_SUFFIX"][0][:3] == ["gh", "variable", "set"]
    assert sets["UNIQUE_NAME_SUFFIX"][1] == SUFFIX

    for name, (argv, stdin) in sets.items():
        setting = next(s for s in sr.SETTINGS if s.name == name)
        assert argv[:3] == ["gh", setting.kind, "set"] and argv[4:6] == ["--repo", REPO]
        assert (argv[6:] == ["--env", sr.PRODUCTION]) == (setting.where == sr.PRODUCTION), name
        # On standard input, never as an argument (arguments show in the process list).
        assert stdin and all(stdin not in arg for arg in argv), name
        assert "--body" not in argv and "-b" not in argv

    # No command of any kind was given a secret as an argument.
    for argv, _ in commands.calls:
        assert not any(value in arg for value in SECRET_VALUES for arg in argv), argv
    # The local file and the hook, both after the GitHub settings.
    assert (tmp_path / sr.DENYLIST_FILE).read_text(encoding="utf-8") == PII.lower() + "\n"
    assert commands.writes[-1][0] == ["git", "-C", str(tmp_path), "config", "core.hooksPath", ".githooks"]
    # And nothing it printed holds a secret value.
    for value in SECRET_VALUES:
        assert value not in out
    assert "Done. 14 set." in out


def test_nothing_is_written_before_the_confirmation(tmp_path):
    commands = FakeCommands()
    seen_at_confirm = []

    answers = list(FULL_RUN)

    class Confirm(str):
        pass

    queue = [*answers, Confirm("y")]

    def reader():
        answer = queue.pop(0)
        if isinstance(answer, Confirm):
            seen_at_confirm.append((list(commands.writes), (tmp_path / sr.DENYLIST_FILE).exists()))
        return str(answer)

    out = io.StringIO()
    assert sr.main([], reader=reader, out=out, err=io.StringIO(), run=commands, root=clone(tmp_path)) == 0
    assert seen_at_confirm == [([], False)]  # at the last question: no command, no file
    assert len(commands.writes) == 13  # and then all of them
    # The summary came before the question, with values masked.
    summary = out.getvalue().split("== Summary ==")[1].split("Nothing has been written yet")[0]
    assert 'AWS_DEV_ACCOUNT_ID  [secret, repository]  12 characters, ending "11"' in summary
    assert "ADMIN_ALLOWED_CIDRS_PROD  [secret, production environment]  1 range" in summary
    assert "PII_DENYLIST  [secret, repository]  1 entry" in summary
    assert f"UNIQUE_NAME_SUFFIX  [variable, repository]  {SUFFIX}" in summary  # public by definition


@pytest.mark.parametrize("answer", ["n", "", "no"])
def test_saying_no_at_the_confirmation_writes_nothing(tmp_path, answer):
    commands = FakeCommands()
    code, out, _ = run([], [*FULL_RUN, answer], commands, clone(tmp_path))
    assert code == 1 and commands.writes == []
    assert not (tmp_path / sr.DENYLIST_FILE).exists()
    assert "Cancelled. Nothing was written." in out


@pytest.mark.parametrize("stop_after", [0, 1, 5, len(FULL_RUN)])
@pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), EOFError()])
def test_ctrl_c_at_any_question_exits_cleanly_having_written_nothing(tmp_path, stop_after, interrupt):
    commands = FakeCommands()
    code, out, _ = run([], [*FULL_RUN[:stop_after], interrupt], commands, clone(tmp_path))
    assert code == 130 and commands.writes == []
    assert not (tmp_path / sr.DENYLIST_FILE).exists()
    assert out.rstrip().endswith("Cancelled. Nothing was written.")


def test_a_failure_part_way_says_what_was_and_was_not_written_and_exits_non_zero(tmp_path):
    commands = FakeCommands(fail_on={"AWS_DEV_ACCOUNT_ID"})
    code, out, _ = run([], [*FULL_RUN, "y"], commands, clone(tmp_path))
    assert code == 1

    attempted = [argv[3] for argv, _ in commands.writes]
    assert attempted == [
        "ADMIN_ALLOWED_CIDRS_DEV", "ADMIN_ALLOWED_CIDRS_PROD", "ALERT_EMAIL_DEV", "ALERT_EMAIL_PROD",
        "AWS_DEV_ACCOUNT_ID",
    ]  # it stopped at the failure: nothing after it was tried
    report = out.split("Stopped at AWS_DEV_ACCOUNT_ID.")[1]
    written = report.split("Written:")[1].split("\n")[0]
    not_written = report.split("NOT written:")[1].split("\n")[0]
    assert [name.strip() for name in written.split(",")] == attempted[:4]
    assert [name.strip() for name in not_written.split(",")] == [
        "AWS_DEV_ACCOUNT_ID", "AWS_PROD_ACCOUNT_ID", "AWS_DEV_DEPLOY_ROLE_ARN", "AWS_PROD_DEPLOY_ROLE_ARN",
        "TF_STATE_BUCKET_DEV", "TF_STATE_BUCKET_PROD", "UNIQUE_NAME_SUFFIX", "PII_DENYLIST",
        ".pii-denylist (local file)", "core.hooksPath (this clone's git config)",
    ]
    assert "Nothing was undone." in out
    assert not (tmp_path / sr.DENYLIST_FILE).exists()
    # gh's own error repeated the value; what is shown has it removed.
    assert "HTTP 403" in out and DEV_ACCOUNT not in out
    for value in SECRET_VALUES:
        assert value not in out


def test_settings_already_present_are_skipped_by_default(tmp_path):
    present = {s.name for s in sr.SETTINGS if s.kind == "secret" and s.where == "repo"} - {"PII_DENYLIST"}
    commands = FakeCommands(
        secrets=present | {"ADMIN_ALLOWED_CIDRS_PROD"},  # a production secret kept at repository level
        env_secrets={"AWS_PROD_DEPLOY_ROLE_ARN"},
        variables={"UNIQUE_NAME_SUFFIX", "ALERT_EMAIL_DEV"},
        hooks_path=".githooks",
    )
    answers = [
        "y",  # the repository
        "",  # replace any? default: no
        "",  # ALERT_EMAIL_PROD: leave unset
        PROD_ACCOUNT,  # AWS_PROD_ACCOUNT_ID (dev's was not asked, so no "same as")
        BUCKET,  # TF_STATE_BUCKET_PROD
        "",  # PII_DENYLIST: no entries
        "y",  # confirm
    ]
    code, out, _ = run([], answers, commands, clone(tmp_path))
    assert code == 0
    assert [argv[3] for argv, _ in commands.writes] == ["AWS_PROD_ACCOUNT_ID", "TF_STATE_BUCKET_PROD"]
    table = out.split("== ")[0]
    assert re.search(r"^ADMIN_ALLOWED_CIDRS_DEV +secret +repository +required +set$", table, re.M)
    assert re.search(r"^ADMIN_ALLOWED_CIDRS_PROD .* set \(repository level\)$", table, re.M)
    assert re.search(r"^AWS_PROD_DEPLOY_ROLE_ARN .*production environment +required +set$", table, re.M)
    assert re.search(r"^TF_STATE_BUCKET_PROD .*required for a fork +not set$", table, re.M)
    assert re.search(r"^PII_DENYLIST .*optional +not set$", table, re.M)
    assert "8 already set." in out
    assert "ALERT_EMAIL_DEV also exists as a plain variable" in out
    assert "It is already turned on for this clone." in out


def test_a_setting_can_be_replaced_when_asked(tmp_path):
    commands = FakeCommands(secrets={"ALERT_EMAIL_DEV"}, hooks_path=".githooks")
    answers = ["y", "y", "y", EMAIL, "y"]  # repository; replace any; replace this one; value; confirm
    code, _, _ = run(["--only", "alert_email_dev"], answers, commands, clone(tmp_path))
    assert code == 0
    assert [(argv[3], stdin) for argv, stdin in commands.writes] == [("ALERT_EMAIL_DEV", EMAIL)]


def test_nothing_to_set_is_a_clean_exit(tmp_path):
    commands = FakeCommands(secrets={"ALERT_EMAIL_DEV"}, hooks_path=".githooks")
    code, out, _ = run(["--only", "ALERT_EMAIL_DEV"], ["y", "n"], commands, clone(tmp_path))
    assert code == 0 and commands.writes == [] and "Nothing to set." in out


def test_bad_answers_are_asked_again_and_a_required_one_cannot_be_left_blank(tmp_path):
    commands = FakeCommands(hooks_path=".githooks")
    answers = ["y", "", "0.0.0.0/0", "2001:db8::/32", "nonsense", CIDR, "y"]
    code, out, _ = run(["--only", "ADMIN_ALLOWED_CIDRS_DEV"], answers, commands, clone(tmp_path))
    assert code == 0
    assert "This one is required." in out and "STOP: 0.0.0.0/0" in out and "is IPv6" in out
    assert [stdin for _, stdin in commands.writes] == [json.dumps([CIDR])]


def test_a_wide_range_needs_a_second_yes(tmp_path):
    commands = FakeCommands(hooks_path=".githooks")
    answers = ["y", "10.0.0.0/8", "", CIDR, "y"]  # the warning's default is no, so it asks again
    code, out, _ = run(["--only", "ADMIN_ALLOWED_CIDRS_DEV"], answers, commands, clone(tmp_path))
    assert code == 0 and "WARNING" in out
    assert [stdin for _, stdin in commands.writes] == [json.dumps([CIDR])]


def test_a_role_arn_in_another_account_is_refused_until_it_matches(tmp_path):
    commands = FakeCommands(hooks_path=".githooks")
    wrong = f"arn:aws:iam::{PROD_ACCOUNT}:role/gha-bloggerbear-dev-deploy"
    right = f"arn:aws:iam::{DEV_ACCOUNT}:role/some-other-role"
    answers = ["y", DEV_ACCOUNT, wrong, right, "y"]
    code, out, _ = run(
        ["--only", "AWS_DEV_ACCOUNT_ID", "AWS_DEV_DEPLOY_ROLE_ARN"], answers, commands, clone(tmp_path)
    )
    assert code == 0 and "different AWS account" in out
    assert [stdin for _, stdin in commands.writes] == [DEV_ACCOUNT, right]


def test_a_role_asked_for_alone_checks_against_an_account_id_that_is_not_saved(tmp_path):
    commands = FakeCommands(secrets={"AWS_DEV_ACCOUNT_ID"}, hooks_path=".githooks")
    answers = ["y", "12", DEV_ACCOUNT, "", "y"]  # repository; a bad id; the id; accept the stub; confirm
    code, out, _ = run(["--only", "AWS_DEV_DEPLOY_ROLE_ARN"], answers, commands, clone(tmp_path))
    assert code == 0 and "It is not saved." in out
    assert [(argv[3], stdin) for argv, stdin in commands.writes] == [
        ("AWS_DEV_DEPLOY_ROLE_ARN", f"arn:aws:iam::{DEV_ACCOUNT}:role/gha-bloggerbear-dev-deploy")
    ]
    assert DEV_ACCOUNT not in out


def test_the_role_help_gives_the_steps_and_a_stub_without_printing_the_account(tmp_path):
    commands = FakeCommands(hooks_path=".githooks")
    answers = ["y", DEV_ACCOUNT, "", "n"]
    only = ["--only", "AWS_DEV_ACCOUNT_ID", "AWS_DEV_DEPLOY_ROLE_ARN"]
    _, out, _ = run(only, answers, commands, clone(tmp_path))
    assert "cd infra/bootstrap" in out and "terraform init" in out
    assert f'-var="github_repo={REPO}"' in out
    assert "terraform output dev_deploy_role_arn" in out
    assert "arn:aws:iam::<the account id you gave>:role/gha-bloggerbear-dev-deploy" in out
    assert "Press Enter to use the ARN from step 3." in out
    assert DEV_ACCOUNT not in out


def test_production_settings_wait_for_the_production_environment(tmp_path):
    commands = FakeCommands(environment=False, hooks_path=".githooks")
    answers = ["y", EMAIL, "y"]
    code, out, _ = run(["--only", "ALERT_EMAIL_DEV", "ALERT_EMAIL_PROD"], answers, commands, clone(tmp_path))
    assert code == 0
    assert "There is no 'production' environment" in out and "Settings > Environments" in out
    assert [argv[3] for argv, _ in commands.writes] == ["ALERT_EMAIL_DEV"]
    # It never tries to create the environment.
    assert not any("environments" in " ".join(argv) for argv, _ in commands.writes)


def test_an_unknown_setting_name_is_refused_before_anything_is_asked(tmp_path):
    with pytest.raises(SystemExit, match="no such setting: NOPE"):
        run(["--only", "NOPE"], [], FakeCommands(), clone(tmp_path))


def test_a_real_run_needs_gh_signed_in_and_a_repository(tmp_path):
    for commands in (FakeCommands(installed=False), FakeCommands(signed_in=False)):
        code, _, err = run([], [], commands, clone(tmp_path))
        assert code == 1 and "gh auth login" in err and commands.writes == []
    code, out, _ = run(["--repo", "someone/else"], ["n"], FakeCommands(), clone(tmp_path))
    assert code == 1 and "Repository: someone/else" in out
    with pytest.raises(SystemExit, match="owner/name"):
        run(["--repo", "not a repo"], [], FakeCommands(), clone(tmp_path))


def test_there_is_no_yes_flag():
    with pytest.raises(SystemExit):
        sr.build_parser().parse_args(["--yes"])
    help_text = sr.build_parser().format_help()
    assert help_text.index("--dry-run") < help_text.index("examples:")
    assert "setup_repo.py --dry-run" in help_text and "--only ADMIN_ALLOWED_CIDRS_DEV" in help_text


# --- PII_DENYLIST and the hook ---------------------------------------------------------------------


def test_the_denylist_is_explained_counted_and_never_printed(tmp_path):
    commands = FakeCommands(hooks_path=".githooks")
    answers = ["y", "#nope", "abc", PII, PII.upper(), "Second Entry Here", "", "y", "y"]
    code, out, _ = run(["--only", "PII_DENYLIST"], answers, commands, clone(tmp_path))
    assert code == 0
    assert "must never be committed" in out and "without ever printing it" in out
    assert "cannot start with #" in out and "Too short" in out and "Already in the list." in out
    assert "PII_DENYLIST  [secret, repository]  2 entries" in out
    assert PII not in out and PII.lower() not in out and "second entry" not in out.lower()
    assert commands.writes[0][1] == f"{PII.lower()}\nsecond entry here"
    assert (tmp_path / sr.DENYLIST_FILE).read_text(encoding="utf-8") == f"{PII.lower()}\nsecond entry here\n"


def test_the_local_file_is_not_written_when_git_does_not_ignore_it(tmp_path):
    commands = FakeCommands(hooks_path=".githooks", ignored=False)
    code, out, _ = run(["--only", "PII_DENYLIST"], ["y", PII, "", "y"], commands, clone(tmp_path))
    assert code == 0
    assert "git does not ignore that file here" in out
    assert not (tmp_path / sr.DENYLIST_FILE).exists()
    assert [argv[3] for argv, _ in commands.writes] == ["PII_DENYLIST"]  # the secret is still set


def test_an_existing_local_file_can_supply_the_secret_and_is_only_added_to(tmp_path):
    root = clone(tmp_path)
    (root / sr.DENYLIST_FILE).write_text("# mine\nOld Entry One", encoding="utf-8")  # no final newline
    commands = FakeCommands(hooks_path=".githooks")
    answers = ["y", "y", PII, "", "y", "y"]  # repository; use the file; one more; done; save; confirm
    code, out, _ = run(["--only", "PII_DENYLIST"], answers, commands, root)
    assert code == 0 and "has 1 entry" in out and "old entry" not in out.lower()
    assert commands.writes[0][1] == f"old entry one\n{PII.lower()}"
    assert (root / sr.DENYLIST_FILE).read_text(encoding="utf-8") == f"# mine\nOld Entry One\n{PII.lower()}\n"


def test_the_hook_is_explained_and_turned_on_only_with_a_yes(tmp_path):
    root = clone(tmp_path)
    hook = ["git", "-C", str(root), "config", "core.hooksPath", ".githooks"]

    commands = FakeCommands()
    code, out, _ = run(["--only", "ALERT_EMAIL_DEV"], ["y", "", "y", "y"], commands, root)
    assert code == 0
    assert "git config core.hooksPath .githooks" in out and "gitleaks" in out and "Once per" in out
    assert [argv for argv, _ in commands.writes] == [hook]

    commands = FakeCommands()
    code, _, _ = run(["--only", "ALERT_EMAIL_DEV"], ["y", "", "n"], commands, root)
    assert code == 0 and commands.writes == []

    # A clone that already points somewhere else is told so, and the default is to leave it.
    commands = FakeCommands(hooks_path=".husky")
    code, out, _ = run(["--only", "ALERT_EMAIL_DEV"], ["y", "", ""], commands, root)
    assert "different hooks folder" in out and commands.writes == []


# --- Dry run ---------------------------------------------------------------------------------------


def test_a_dry_run_asks_everything_writes_nothing_and_prints_no_secret(tmp_path, monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("apply_actions was called in a dry run")

    monkeypatch.setattr(sr, "apply_actions", never)
    commands = FakeCommands()
    code, out, err = run(["--dry-run"], FULL_RUN, commands, clone(tmp_path))  # no confirmation asked
    assert code == 0
    assert commands.writes == []
    assert all(stdin is None for _, stdin in commands.calls)  # no value ever left the script
    assert not (tmp_path / sr.DENYLIST_FILE).exists()
    for value in SECRET_VALUES:
        assert value not in out and value not in err, value

    assert out.startswith("DRY RUN.") and out.rstrip().endswith("Dry run: nothing was changed.")
    would = out.split("A real run would now ask you to confirm, then run:")[1]
    for setting in sr.SETTINGS:
        env = f" --env {sr.PRODUCTION}" if setting.where == sr.PRODUCTION else ""
        command = f"  gh {setting.kind} set {setting.name} --repo {REPO}{env}"
        assert f"{command}   (value on standard input: <redacted>)\n" in would, setting.name
    assert f"{sr.DENYLIST_FILE}   (entries: <redacted>)" in would
    assert "  git config core.hooksPath .githooks\n" in would
    # The same summary a real run shows.
    assert 'AWS_DEV_ACCOUNT_ID  [secret, repository]  12 characters, ending "11"' in out


def test_a_dry_run_cannot_write_even_if_the_code_tried(tmp_path, monkeypatch):
    """The second lock: with the early return removed, the guarded runner still refuses."""
    commands = FakeCommands()
    guarded = sr.read_only(commands)
    with pytest.raises(sr.WriteInDryRun):
        sr.apply_actions([sr.Action("secret", "X", "repo", "v")], REPO, tmp_path, guarded, io.StringIO())
    assert commands.calls == []
    # And main really does run a dry run through that guard.
    wrapped = []
    real = sr.read_only
    monkeypatch.setattr(sr, "read_only", lambda run: wrapped.append(run) or real(run))
    run(["--dry-run", "--only", "ALERT_EMAIL_DEV"], ["y", "", "n"], commands, clone(tmp_path))
    assert wrapped == [commands]


@pytest.mark.parametrize("commands", [FakeCommands(installed=False), FakeCommands(signed_in=False)])
def test_a_dry_run_finishes_without_gh_signed_in(tmp_path, commands):
    code, out, _ = run(["--dry-run"], FULL_RUN, commands, clone(tmp_path))
    assert code == 0 and commands.writes == []
    assert "Could not check what is already set" in out and "Repository: <owner>/<name>" in out
    assert re.search(r"^ADMIN_ALLOWED_CIDRS_DEV .* unknown$", out, re.M)
    assert not any(argv[:2] == ["gh", "api"] for argv, _ in commands.calls)
    for value in SECRET_VALUES:
        assert value not in out


def test_a_dry_run_only_describes_the_hook_and_the_local_file(tmp_path):
    root = clone(tmp_path)
    commands = FakeCommands()
    answers = ["y", PII, "", "y", "y"]  # repository; entry; done; save the file; turn the hook on
    code, out, _ = run(["--dry-run", "--only", "PII_DENYLIST"], answers, commands, root)
    assert code == 0
    assert "git config core.hooksPath .githooks" in out.split("then run:")[1]
    assert not any(argv[3:5] == ["config", "core.hooksPath"] for argv, _ in commands.calls)
    assert not (root / sr.DENYLIST_FILE).exists()


def test_a_dry_run_still_asks_for_production_when_the_environment_is_missing(tmp_path):
    commands = FakeCommands(environment=False, hooks_path=".githooks")
    code, out, _ = run(
        ["--dry-run", "--only", "ALERT_EMAIL_PROD"], ["y", EMAIL], commands, clone(tmp_path)
    )
    assert code == 0 and "There is no 'production' environment" in out
    assert "gh secret set ALERT_EMAIL_PROD" in out and commands.writes == []


# --- The CoinGecko API key (kept in AWS; the script reads, explains and prints the command) --------

ENVIRONMENTS = REPO_ROOT / "infra" / "environments"
TF_FOLDER = {"dev": "dev", "prod": "production"}
DEV_PARAMETER = sr.COINGECKO_PARAMETERS["dev"]
PROD_PARAMETER = sr.COINGECKO_PARAMETERS["prod"]
# What a person's key would look like if the script ever asked for one. It never does, so this
# must never reach a command or the output.
KEY_MARKER = "CG-zz9markerNotARealKey"


def _aws_calls(commands):
    return [argv for argv, _ in commands.calls if argv[0] == "aws"]


def _coingecko_section(out: str) -> str:
    return out.split("== CoinGecko API key")[1].split("== Summary ==")[0]


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_the_parameter_names_and_region_are_the_ones_terraform_uses(env):
    """The script's names are a copy of Terraform's; renaming either side fails here."""
    main_tf = (ENVIRONMENTS / TF_FOLDER[env] / "main.tf").read_text(encoding="utf-8")
    declared = re.findall(r'^\s*coingecko_api_key_parameter\s*=\s*"([^"]+)"', main_tf, re.M)
    assert declared == [sr.COINGECKO_PARAMETERS[env]]
    # The Lambda is granted read access to that very name, in the region the script names.
    assert f"arn:aws:ssm:{sr.AWS_REGION}:" in main_tf
    assert "parameter${local.coingecko_api_key_parameter}" in main_tf
    regions = set(re.findall(r'^\s*region\s*=\s*"([^"]+)"', main_tf, re.M))
    assert sr.AWS_REGION in regions
    # And the hand-run command in Terraform's own comment stores to the same name and type.
    assert f"aws ssm put-parameter --name {sr.COINGECKO_PARAMETERS[env]} --type SecureString" in main_tf
    assert sr.coingecko_command(env).startswith(
        f"aws ssm put-parameter --name {sr.COINGECKO_PARAMETERS[env]} --type SecureString "
    )


def test_the_adapter_reads_the_parameter_the_way_the_script_tells_you_to_store_it():
    adapter = (REPO_ROOT / "lambdas" / "common" / "adapters" / "crypto_feed.py").read_text(encoding="utf-8")
    assert 'API_KEY_PARAMETER_ENV = "COINGECKO_API_KEY_PARAMETER"' in adapter
    assert "WithDecryption=True" in adapter  # so it has to be a SecureString, as the command makes it


def test_the_step_explains_the_key_and_prints_a_command_with_a_placeholder(tmp_path):
    commands = FakeCommands()  # no AWS CLI
    code, out, _ = run([], [*FULL_RUN, "y"], commands, clone(tmp_path))
    assert code == 0
    section = _coingecko_section(out)
    assert "optional" in section and "https://www.coingecko.com/en/api" in section
    assert "require the site to credit them" in section  # attribution is required either way
    assert "does not store it and does not ask for it" in section
    assert "the AWS CLI is not installed or not signed in" in section
    for env in ("dev", "prod"):
        assert f"      {sr.coingecko_command(env)}\n" in section
    assert sr.coingecko_command("dev") == (
        f"aws ssm put-parameter --name {DEV_PARAMETER} --type SecureString --overwrite "
        f"--region {sr.AWS_REGION} --value YOUR_COINGECKO_API_KEY"
    )
    # Only looked for the CLI; with none there, nothing else was run.
    assert _aws_calls(commands) == [["aws", "--version"]]
    # The summary and the final report both say what is still the person's to do.
    assert out.count("Left for you to run (optional; this script does not write to AWS):") == 2
    assert out.rstrip().endswith(f"CoinGecko API key, production: {sr.coingecko_command('prod')}")


def test_the_step_never_asks_for_the_key_and_never_runs_anything_but_reads(tmp_path):
    """A full run with the CLI signed in: every `aws` call is one of the three reads, no value is
    sent to any of them, and the person was asked nothing new (FULL_RUN is unchanged)."""
    commands = FakeCommands(aws_account=DEV_ACCOUNT)
    code, out, err = run([], [*FULL_RUN, "y"], commands, clone(tmp_path))
    assert code == 0
    calls = _aws_calls(commands)
    assert calls and all(sr.is_read_only(argv) for argv in calls)
    assert not any("put-parameter" in argv or "get-parameter" in argv for argv in calls)
    assert all(stdin is None for argv, stdin in commands.calls if argv[0] == "aws")
    for argv, _ in commands.calls:
        assert not any(KEY_MARKER in arg or "--value" == arg for arg in argv), argv
    assert KEY_MARKER not in out and KEY_MARKER not in err
    # The account is treated as a secret: only its last four digits are shown.
    assert 'signed in to the account ending "1111". Region: ap-southeast-2.' in out
    for value in SECRET_VALUES:
        assert value not in out


def test_an_environment_whose_account_is_not_the_signed_in_one_is_not_checked(tmp_path):
    """Dev's account ID (given earlier in the run) is the signed-in one; production's is not. The
    script must not look in the wrong account and call production's parameter missing or set."""
    commands = FakeCommands(aws_account=DEV_ACCOUNT, parameters={PROD_PARAMETER})
    code, out, _ = run([], [*FULL_RUN, "y"], commands, clone(tmp_path))
    assert code == 0
    section = _coingecko_section(out)
    assert f"  dev: {DEV_PARAMETER} is not set. To store a key, run:" in section
    assert "signed in to a different account from production's. Sign in to it first" in section
    described = [argv for argv in _aws_calls(commands) if argv[1:3] == ["ssm", "describe-parameters"]]
    assert len(described) == 1 and f"Key=Name,Option=Equals,Values={DEV_PARAMETER}" in described[0]
    assert described[0][described[0].index("--region") + 1] == sr.AWS_REGION


def test_a_parameter_that_is_already_set_is_skipped_and_its_value_never_fetched(tmp_path):
    commands = FakeCommands(aws_account=DEV_ACCOUNT, parameters={DEV_PARAMETER, PROD_PARAMETER})
    answers = [*FULL_RUN, "y"]
    answers[answers.index("n") : answers.index("n") + 2] = ["y"]  # one account: production's is dev's
    code, out, _ = run([], answers, commands, clone(tmp_path))
    assert code == 0
    section = _coingecko_section(out)
    assert f"  dev: {DEV_PARAMETER} is already set. Nothing to do." in section
    assert f"  production: {PROD_PARAMETER} is already set. Nothing to do." in section
    assert "put-parameter" not in out and "Left for you to run" not in out
    assert not any("get-parameter" in argv for argv in _aws_calls(commands))


@pytest.mark.parametrize(
    "commands, expected",
    [
        (FakeCommands(aws_account=DEV_ACCOUNT, aws_signed_in=False), "not installed or not signed in"),
        (FakeCommands(aws_account=DEV_ACCOUNT, parameters_readable=False), "could not be read"),
        (FakeCommands(aws_account="not-an-account"), "not installed or not signed in"),
    ],
)
def test_when_aws_cannot_be_read_the_step_says_so_and_still_prints_the_command(tmp_path, commands, expected):
    code, out, _ = run([], [*FULL_RUN, "y"], commands, clone(tmp_path))
    assert code == 0
    section = _coingecko_section(out)
    assert expected in section and sr.coingecko_command("dev") in section


def test_with_no_account_id_given_in_this_run_the_step_says_to_check_the_account(tmp_path):
    present = {setting.name for setting in sr.SETTINGS}
    commands = FakeCommands(
        secrets=present, env_secrets=present, variables={"UNIQUE_NAME_SUFFIX"}, hooks_path=".githooks",
        aws_account=DEV_ACCOUNT,
    )
    code, out, _ = run([], ["y", ""], commands, clone(tmp_path))  # the repository; replace any? no
    assert code == 0 and commands.writes == []
    section = _coingecko_section(out)
    assert f"  dev: {DEV_PARAMETER} is not set in this account (make sure it is dev's)." in section
    # Nothing to set on GitHub, and the summary still says what is left.
    summary = out.split("== Summary ==")[1]
    assert "Nothing to set." in summary and sr.coingecko_command("prod") in summary


def test_a_dry_run_shows_the_step_does_not_need_aws_and_cannot_run_the_ssm_write(tmp_path):
    for commands in (FakeCommands(), FakeCommands(aws_account=DEV_ACCOUNT, aws_signed_in=False)):
        code, out, _ = run(["--dry-run"], FULL_RUN, commands, clone(tmp_path))
        assert code == 0 and commands.writes == []
        assert sr.coingecko_command("dev") in _coingecko_section(out)
        assert f"CoinGecko API key, dev: {sr.coingecko_command('dev')}" in out.split("== Summary ==")[1]
        assert out.rstrip().endswith("Dry run: nothing was changed.")
    # The lock itself: the guarded runner a dry run uses refuses the write, however it is spelled,
    # and never hands it to the real runner.
    reached = []
    guarded = sr.read_only(lambda argv, stdin=None: reached.append(argv) or sr.Result(0))
    for write in (
        ["aws", "ssm", "put-parameter", "--name", DEV_PARAMETER, "--type", "SecureString",
         "--value", KEY_MARKER],
        ["aws", "ssm", "put-parameter", "--cli-input-json", "file:///dev/stdin"],
    ):
        with pytest.raises(sr.WriteInDryRun) as refused:
            guarded(write)
        assert KEY_MARKER not in str(refused.value)  # the refusal shows only the command's first words
    with pytest.raises(sr.WriteInDryRun):
        guarded(["aws", "ssm", "describe-parameters"], KEY_MARKER)  # anything given a value on stdin
    assert reached == []


def test_a_part_way_failure_still_says_what_is_left_for_you(tmp_path):
    commands = FakeCommands(fail_on={"AWS_DEV_ACCOUNT_ID"})
    code, out, _ = run([], [*FULL_RUN, "y"], commands, clone(tmp_path))
    assert code == 1
    report = out.split("Stopped at AWS_DEV_ACCOUNT_ID.")[1]
    assert "NOT written:" in report and "Left for you to run" in report
    assert sr.coingecko_command("dev") in report and "Nothing was undone." in report


def test_a_run_narrowed_with_only_leaves_the_step_out(tmp_path):
    commands = FakeCommands(secrets={"ALERT_EMAIL_DEV"}, hooks_path=".githooks", aws_account=DEV_ACCOUNT)
    code, out, _ = run(["--only", "ALERT_EMAIL_DEV"], ["y", "n"], commands, clone(tmp_path))
    assert code == 0 and "CoinGecko" not in out and _aws_calls(commands) == []


def test_the_script_has_no_way_to_take_the_key():
    """It stores nothing in AWS, so its source must hold no SSM write and no prompt for the key:
    the only place `put-parameter` appears is the command it prints."""
    source = (REPO_ROOT / "scripts" / "setup_repo.py").read_text(encoding="utf-8")
    code_lines = [line for line in source.splitlines() if not line.lstrip().startswith("#")]
    assert sum("put-parameter" in line for line in code_lines) == 1
    assert "boto3" not in "\n".join(code_lines) and "tempfile" not in source
    assert "getpass" not in source


def test_the_deploy_guide_documents_the_key_with_the_same_names():
    guide = (REPO_ROOT / "docs" / "deploying-your-own.md").read_text(encoding="utf-8")
    for env in ("dev", "prod"):
        assert sr.COINGECKO_PARAMETERS[env] in guide
    assert "https://www.coingecko.com/en/api" in guide and "SecureString" in guide


# --- The real command runner -----------------------------------------------------------------------


def test_the_runner_sends_the_value_as_bytes_on_stdin_and_reports_a_missing_program():
    import sys

    echo = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"
    echoed = sr.run_command([sys.executable, "-c", echo], "one\ntwo")
    assert echoed.code == 0 and echoed.out == "one\ntwo"  # no CRLF added on Windows
    assert sr.run_command(["definitely-not-a-real-program-zz9"]).code == 127


def test_an_actions_value_never_shows_in_its_repr(tmp_path):
    action = sr.Action("secret", "X", "repo", "hunter2-value")
    assert "hunter2-value" not in repr(action)
    assert "hunter2-value" not in action.described(REPO, tmp_path)
