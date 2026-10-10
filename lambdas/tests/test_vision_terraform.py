"""The vision worker's Terraform (infra/modules/vision-worker), its wiring into dev, and the deploy
role's grants for it (infra/bootstrap), held to what the code relies on.

Read as text, like test_terraform_wiring.py: nothing here plans or applies.
"""

from __future__ import annotations

import ast
import re

import pytest
from terraform_text import INFRA, ROOT

from common import vision_client

MODULE = (INFRA / "modules" / "vision-worker" / "main.tf").read_text(encoding="utf-8")
MODULE_VARS = (INFRA / "modules" / "vision-worker" / "variables.tf").read_text(encoding="utf-8")
ENVS = ("dev", "production")
MAIN = {env: (INFRA / "environments" / env / "main.tf").read_text(encoding="utf-8") for env in ENVS}
VARS = {env: (INFRA / "environments" / env / "variables.tf").read_text(encoding="utf-8") for env in ENVS}
BOOTSTRAP = (INFRA / "bootstrap" / "main.tf").read_text(encoding="utf-8")
BOOTSTRAP_VARS = (INFRA / "bootstrap" / "variables.tf").read_text(encoding="utf-8")


def block(text: str, kind: str, type_: str, name: str) -> str:
    match = re.search(rf'^{kind} "{type_}" "{name}" \{{\n(.*?)^\}}', text, re.S | re.M)
    assert match, f"{kind} {type_}.{name} not found"
    return match.group(1)


def resource(text: str, type_: str, name: str) -> str:
    return block(text, "resource", type_, name)


def variable_default(text: str, name: str) -> str:
    body = re.search(rf'^variable "{name}" \{{\n(.*?)^\}}', text, re.S | re.M).group(1)
    return re.search(r"^\s*default\s*=\s*(.+)$", body, re.M).group(1).strip()


# --- the module ----------------------------------------------------------------------------------


def test_the_function_is_arm64_python312_and_runs_the_worker_handler():
    function = resource(MODULE, "aws_lambda_function", "worker")
    assert 'architectures = ["arm64"]' in function
    assert 'runtime       = "python3.12"' in function
    assert 'handler       = "vision_worker_handler.lambda_handler"' in function
    import vision_worker_handler

    assert callable(vision_worker_handler.lambda_handler)
    assert 'VISION_BACKEND = "opencv"' in function


def test_every_regional_resource_is_in_the_vision_region():
    regional = re.findall(r'^resource "(aws_(?:s3|lambda|cloudwatch)[a-z_]*)" "(\w+)"', MODULE, re.M)
    assert len(regional) >= 7
    for type_, name in regional:
        assert re.search(r"^\s*region\s*=\s*var\.region$", resource(MODULE, type_, name), re.M), (type_, name)


def test_the_package_goes_through_s3_and_changes_key_when_the_code_does():
    function = resource(MODULE, "aws_lambda_function", "worker")
    assert "s3_bucket        = aws_s3_object.package.bucket" in function
    assert "s3_key           = aws_s3_object.package.key" in function
    assert "source_code_hash = data.archive_file.package.output_base64sha256" in function
    assert "filename" not in function
    obj = resource(MODULE, "aws_s3_object", "package")
    assert "${data.archive_file.package.output_md5}.zip" in obj
    bucket = resource(MODULE, "aws_s3_bucket_public_access_block", "artifacts")
    settings = ("block_public_acls", "block_public_policy", "ignore_public_acls", "restrict_public_buckets")
    for setting in settings:
        assert re.search(rf"{setting}\s*=\s*true", bucket)


def test_the_package_is_built_for_graviton_with_only_what_the_handler_imports():
    build = resource(MODULE, "terraform_data", "package")
    for needed in (
        "--platform manylinux2014_aarch64 --platform manylinux_2_28_aarch64",
        "--implementation cp --python-version 3.12 --only-binary=:all:",
        '-r "${local.lambdas_dir}/requirements-vision.txt"',
        'cp -r "${local.lambdas_dir}/vision" "$build_dir/vision"',
        'cp "${local.lambdas_dir}/vision_worker_handler.py" "$build_dir/vision_worker_handler.py"',
        'cp "${local.lambdas_dir}/common/__init__.py" "$build_dir/common/__init__.py"',
        'cp "${local.lambdas_dir}/common/vision_contract.py" "$build_dir/common/vision_contract.py"',
        "always_run = timestamp()",
    ):
        assert needed in build, needed
    # Not the shared pipeline package, and not its requirements.
    assert 'cp -r "${local.lambdas_dir}/common"' not in build
    assert "/requirements.txt" not in build

    # Everything of ours the worker imports is copied, and nothing it imports needs more.
    def ours(path):
        tree = ast.parse((ROOT / "lambdas" / path).read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                names |= {f"{node.module}.{a.name}" for a in node.names}
            elif isinstance(node, ast.Import):
                names |= {a.name for a in node.names}
        return {n for n in names if n.split(".")[0] in ("common", "vision")}

    reached = set()
    todo = ["vision_worker_handler.py"]
    while todo:
        for name in ours(todo.pop()):
            parts = name.split(".")
            for candidate in (f"{'/'.join(parts)}.py", f"{'/'.join(parts[:-1])}.py"):
                if (ROOT / "lambdas" / candidate).is_file() and candidate not in reached:
                    reached.add(candidate)
                    todo.append(candidate)
    common_reached = {path for path in reached if path.startswith("common/")}
    assert common_reached == {"common/vision_contract.py"}, common_reached


def test_the_requirements_pin_what_the_worker_needs():
    requirements = (ROOT / "lambdas" / "requirements-vision.txt").read_text(encoding="utf-8")
    pins = dict(re.findall(r"^([a-z0-9-]+)==(\S+)$", requirements, re.M))
    assert pins["opencv-python-headless"].startswith("5.")
    assert set(pins) == {"opencv-python-headless", "numpy", "requests", "networkx"}
    shared = (ROOT / "lambdas" / "requirements.txt").read_text(encoding="utf-8")
    assert f"requests=={pins['requests']}" in shared  # one requests release across packages
    # The graph library is the worker's alone, and the COOL image pins the same release.
    assert "networkx" not in shared
    cool = (ROOT / "lambdas" / "requirements-vision-cool.txt").read_text(encoding="utf-8")
    assert f"networkx=={pins['networkx']}" in cool


def test_the_role_can_write_its_logs_and_nothing_else():
    role = resource(MODULE, "aws_iam_role", "worker")
    assert 'name               = "${local.name}-lambda-exec"' in role  # the deploy role's pattern
    policy = block(MODULE, "data", "aws_iam_policy_document", "worker")
    actions = re.findall(r"actions\s*=\s*\[(.*?)\]", policy, re.S)
    assert actions == ['"logs:CreateLogStream", "logs:PutLogEvents"']
    assert "aws_cloudwatch_log_group.worker.arn" in policy
    assert '"*"' not in policy


def test_the_worker_times_out_before_the_client_gives_up():
    timeout = int(variable_default(MODULE_VARS, "timeout"))
    assert timeout < vision_client._CLIENT_CONFIG.read_timeout


# --- the environments ----------------------------------------------------------------------------


@pytest.mark.parametrize("env", ENVS)
def test_nothing_is_created_until_vision_is_enabled(env):
    assert variable_default(VARS[env], "vision_enabled") == "false"
    module = re.search(r'^module "vision_worker" \{\n(.*?)^\}', MAIN[env], re.S | re.M).group(1)
    assert "count  = var.vision_enabled ? 1 : 0" in module
    assert "region             = var.vision_region" in module
    assert f'environment_name   = "{env}"' in module
    grant = resource(MAIN[env], "aws_iam_role_policy", "lambda_vision_worker")
    assert "count  = var.vision_enabled ? 1 : 0" in grant


@pytest.mark.parametrize("env", ENVS)
def test_the_research_tick_is_told_the_worker_and_may_invoke_only_it(env):
    tick = resource(MAIN[env], "aws_lambda_function", "research_tick")
    assert "local.vision_env_variables" in tick
    assert vision_client._ENV_BY_BACKEND["opencv"] == "VISION_WORKER_ARN"
    assert 'VISION_WORKER_ARN = var.vision_enabled ? module.vision_worker[0].function_arn : ""' in MAIN[env]
    policy = block(MAIN[env], "data", "aws_iam_policy_document", "lambda_vision_worker")
    assert 'actions   = ["lambda:InvokeFunction"]' in policy
    assert "resources = [module.vision_worker[0].function_arn]" in policy


@pytest.mark.parametrize("env", ENVS)
def test_the_environments_and_bootstrap_agree_on_the_vision_region(env):
    assert variable_default(VARS[env], "vision_region") == variable_default(BOOTSTRAP_VARS, "vision_region")
    assert variable_default(VARS[env], "vision_region") == '"us-west-2"'  # where sentinel-cogs is


# --- bootstrap -----------------------------------------------------------------------------------


def test_the_deploy_role_gains_only_vision_named_lambdas_and_logs_in_the_vision_region():
    functions = re.search(r'sid       = "VisionWorkerFunctions"(.*?)\n  \}', BOOTSTRAP, re.S).group(1)
    assert 'actions   = ["lambda:*"]' in functions
    arn = '"arn:aws:lambda:${var.vision_region}:*:function:${var.unique_name_prefix}-*-vision-*"'
    assert arn in functions
    logs = re.search(r'sid     = "VisionWorkerLogGroups"(.*?)\n  \}', BOOTSTRAP, re.S).group(1)
    assert "${var.vision_region}" in logs and "/aws/lambda/${var.unique_name_prefix}-*-vision-*" in logs
    # The module's names fall inside those patterns.
    assert '"${var.unique_name_prefix}-${var.environment_name}-vision-worker"' in MODULE
