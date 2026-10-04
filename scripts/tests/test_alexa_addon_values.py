"""Tests for scripts/alexa_addon_values.py: an environment's Alexa+ add-on values from Terraform.

Terraform is played by fake `output -json` dicts, so nothing here runs Terraform or touches AWS.
The URLs use example hosts; the secret is a marker that must never appear unless asked for.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import alexa_addon_values as av

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET = "zz9-secret-marker-value"


def fake_outputs(env: str, *, client: bool = True, secret: str | None = SECRET) -> dict:
    """What `terraform output -json` prints for one environment, with made-up hosts."""
    api = f"https://{env}api.execute-api.eu-west-1.amazonaws.com/{env}"
    hosted = f"https://bloggerbear-{env}-ops.auth.eu-west-1.amazoncognito.com"
    values = {
        "ops_mcp_url": f"{api}/mcp",
        "ops_oauth_protected_resource_url": f"{api}/.well-known/oauth-protected-resource",
        "ops_oauth_authorize_url": f"{hosted}/oauth2/authorize",
        "ops_oauth_token_url": f"{hosted}/oauth2/token",
        "ops_alexa_client_id": f"{env}clientid123" if client else None,
        "ops_alexa_client_secret": secret if client else None,
        "ops_user_pool_id": "eu-west-1_Example",
    }
    return {
        name: {"value": value, "sensitive": name.endswith("secret"), "type": "string"}
        for name, value in values.items()
    }


def run(argv: list[str], outputs: dict) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = av.main(argv, terraform=lambda env: outputs, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


# --- Values ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("env", av.ENVIRONMENTS)
def test_values_are_extracted(env):
    values = av.extract_values(fake_outputs(env), env)
    raw = fake_outputs(env)
    assert values.mcp_url == raw["ops_mcp_url"]["value"]
    assert values.protected_resource_url == raw["ops_oauth_protected_resource_url"]["value"]
    assert values.authorize_url == raw["ops_oauth_authorize_url"]["value"]
    assert values.token_url == raw["ops_oauth_token_url"]["value"]
    assert values.scope == "bloggerbear-ops/read"
    assert values.client_id == f"{env}clientid123"
    assert values.client_secret == SECRET


def test_output_lists_every_value():
    code, out, err = run(["dev"], fake_outputs("dev"))
    assert code == 0, err
    for value in av.extract_values(fake_outputs("dev"), "dev").__dict__.values():
        if value and value != SECRET:
            assert value in out


def test_no_client_yet_says_what_to_do():
    code, out, _ = run(["dev"], fake_outputs("dev", client=False))
    assert code == 0
    assert "ops_alexa_redirect_uris" in out


def test_scope_matches_the_module():
    main_tf = (REPO_ROOT / "infra" / "modules" / "ops-assistant" / "main.tf").read_text()
    assert 'resource_server_identifier = "bloggerbear-ops"' in main_tf
    assert av.SCOPE == "bloggerbear-ops/read"


# --- The secret -----------------------------------------------------------------------------------


def test_secret_is_never_printed_without_the_flag(tmp_path):
    code, out, err = run(["dev", "--write-manifest", str(tmp_path / "addon.json")], fake_outputs("dev"))
    assert code == 0
    assert SECRET not in out
    assert SECRET not in err
    assert SECRET not in (tmp_path / "addon.json").read_text()


def test_secret_is_printed_with_the_flag_and_warned_about():
    code, out, err = run(["dev", "--show-secret"], fake_outputs("dev"))
    assert code == 0
    assert SECRET in out
    assert SECRET not in err
    assert "WARNING" in err


def test_errors_never_carry_the_secret():
    outputs = fake_outputs("dev")
    outputs["ops_mcp_url"]["value"] = "http://example.com/dev/mcp"
    code, out, err = run(["dev", "--show-secret"], outputs)
    assert code == 1
    assert SECRET not in out + err


# --- Checks ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["ops_mcp_url", "ops_oauth_protected_resource_url", "ops_oauth_authorize_url", "ops_oauth_token_url"],
)
def test_refuses_http(name):
    outputs = fake_outputs("dev")
    outputs[name]["value"] = outputs[name]["value"].replace("https://", "http://")
    with pytest.raises(av.CliError, match="https"):
        av.extract_values(outputs, "dev")


def test_refuses_a_missing_output():
    outputs = fake_outputs("dev")
    del outputs["ops_oauth_token_url"]
    with pytest.raises(av.CliError, match="ops_oauth_token_url"):
        av.extract_values(outputs, "dev")


def test_refuses_unknown_environment():
    with pytest.raises(av.CliError):
        av.extract_values(fake_outputs("dev"), "staging")
    with pytest.raises(SystemExit) as exc:
        av.build_parser().parse_args(["staging"])
    assert exc.value.code == 2


def test_production_values_are_refused_for_dev():
    with pytest.raises(av.CliError, match="dev"):
        av.extract_values(fake_outputs("production"), "dev")


def test_dev_values_are_refused_for_production():
    with pytest.raises(av.CliError, match="production"):
        av.extract_values(fake_outputs("dev"), "production")


def test_mixed_outputs_are_refused():
    # dev's MCP endpoint with production's sign-in domain: the add-on would link to the wrong pool.
    outputs = fake_outputs("dev")
    prod = fake_outputs("production")
    outputs["ops_oauth_authorize_url"] = prod["ops_oauth_authorize_url"]
    outputs["ops_oauth_token_url"] = prod["ops_oauth_token_url"]
    with pytest.raises(av.CliError, match="production"):
        av.extract_values(outputs, "dev")

    outputs = fake_outputs("dev")
    outputs["ops_oauth_protected_resource_url"] = prod["ops_oauth_protected_resource_url"]
    with pytest.raises(av.CliError, match="stage"):
        av.extract_values(outputs, "dev")


def test_cli_exits_nonzero_on_mixed_values():
    code, out, err = run(["production"], fake_outputs("dev"))
    assert code == 1
    assert out == ""
    assert "error:" in err


# --- The manifest ---------------------------------------------------------------------------------


def template_text() -> str:
    return av.TEMPLATE_PATH.read_text(encoding="utf-8")


def test_template_has_only_known_placeholders():
    text = template_text()
    found = set()
    start = text.find("{{")
    while start != -1:
        end = text.index("}}", start)
        found.add(text[start : end + 2])
        start = text.find("{{", end)
    values = av.extract_values(fake_outputs("dev"), "dev")
    assert found == set(av.manifest_substitutions(values))
    assert json.loads(text)["manifestVersion"] == "1.0"


@pytest.mark.parametrize("env", av.ENVIRONMENTS)
def test_rendering_fills_every_placeholder(env):
    values = av.extract_values(fake_outputs(env), env)
    rendered = av.render_manifest(template_text(), values)
    assert "{{" not in rendered and "}}" not in rendered
    manifest = json.loads(rendered)
    assert manifest["manifestVersion"] == "1.0"
    assert values.mcp_url in rendered
    assert manifest["storeListing"]["en-US"]["name"] == av.ADDON_NAMES[env]


def test_rendering_refuses_an_unknown_placeholder():
    values = av.extract_values(fake_outputs("dev"), "dev")
    with pytest.raises(av.CliError, match="placeholder"):
        av.render_manifest('{"manifestVersion": "1.0", "x": "{{SOMETHING_ELSE}}"}', values)


@pytest.mark.parametrize("env", av.ENVIRONMENTS)
def test_add_on_name_never_says_alexa(env):
    values = av.extract_values(fake_outputs(env), env)
    manifest = json.loads(av.render_manifest(template_text(), values))
    for listing in manifest["storeListing"].values():
        assert "alexa" not in listing["name"].lower()


@pytest.mark.parametrize("env", av.ENVIRONMENTS)
def test_manifest_holds_only_its_own_environment(env):
    other = next(name for name in av.ENVIRONMENTS if name != env)
    rendered = av.render_manifest(template_text(), av.extract_values(fake_outputs(env), env))
    assert f"/{env}/mcp" in rendered
    assert f"/{other}/mcp" not in rendered
    assert f"{other}api" not in rendered


def test_write_manifest_writes_the_file(tmp_path):
    path = tmp_path / "addon-package" / "addon.json"
    code, _, err = run(["dev", "--write-manifest", str(path)], fake_outputs("dev"))
    assert code == 0, err
    assert json.loads(path.read_text())["mcpServer"]["url"] == fake_outputs("dev")["ops_mcp_url"]["value"]


def test_write_manifest_will_not_overwrite_without_force(tmp_path):
    path = tmp_path / "addon.json"
    path.write_text("keep me")
    code, _, err = run(["dev", "--write-manifest", str(path)], fake_outputs("dev"))
    assert code == 1
    assert "--force" in err
    assert path.read_text() == "keep me"

    code, _, err = run(["dev", "--write-manifest", str(path), "--force"], fake_outputs("dev"))
    assert code == 0, err
    assert json.loads(path.read_text())["manifestVersion"] == "1.0"


def test_nothing_is_written_when_the_values_are_wrong(tmp_path):
    path = tmp_path / "addon.json"
    code, _, _ = run(["production", "--write-manifest", str(path)], fake_outputs("dev"))
    assert code == 1
    assert not path.exists()
