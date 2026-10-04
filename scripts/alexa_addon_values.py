"""Print what an environment's Alexa+ add-on needs, from that environment's Terraform outputs.

    python scripts/alexa_addon_values.py dev
    python scripts/alexa_addon_values.py dev --write-manifest alexa/addon-package/addon.json
    python scripts/alexa_addon_values.py production --show-secret   # only when you are typing it in

It runs `terraform -chdir=infra/environments/<env> output -json` and prints the values that
`alexa-ai configure-account-linking` asks for (alexa/README.md): the MCP endpoint, its Protected
Resource Metadata, Cognito's authorization and token URLs, the scope and the Alexa app client's
id. It needs Terraform and credentials that can read that environment's state; it changes nothing.

How it is built, and why:

* **The client secret stays hidden.** `output -json` returns sensitive outputs in plain text, so
  this script holds the secret, but it prints it only with --show-secret, and then warns on
  stderr. No message, error or manifest ever contains it.
* **One environment at a time.** Each environment has its own add-on, its own MCP URL and its own
  user pool; dev must never be linked to production. The values are checked to belong to the
  environment asked for (see `check_environment`), so a mixed-up state or a wrong -chdir fails
  loudly instead of linking dev's add-on to production.
* **The manifest is a template, not a schema.** Amazon's addon.json schema is not public to us.
  addon.template.json holds only the values we know (manifestVersion 1.0, an en-US listing and the
  MCP endpoint); `alexa-ai` generates and validates the real file.
* **Pure functions, one subprocess.** Everything but `run_terraform` takes and returns plain
  data, so scripts/tests/test_alexa_addon_values.py runs without Terraform or a network.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = REPO_ROOT / "alexa" / "addon-package" / "addon.template.json"

ENVIRONMENTS = ("dev", "production")

# The scope every ops route requires (infra/modules/ops-assistant/main.tf, read_scope). Fixed in
# the module, so not a Terraform output of the environment.
SCOPE = "bloggerbear-ops/read"

# The add-on's own name. Never "Alexa": Amazon's rules allow the word only descriptively
# ("works with Alexa"), never in a skill's or add-on's name.
ADDON_NAMES = {"dev": "BloggerBear Ops (dev)", "production": "BloggerBear Ops"}
ADDON_DESCRIPTION = (
    "Ask how the BloggerBear blog pipeline is doing: its health, the admin inbox, alarms, and a "
    "spoken briefing. Read-only. For the pipeline's own operator."
)

PLACEHOLDER_OPEN = "{{"


class CliError(Exception):
    """Something the person must fix. The message is shown as it is; it never holds the secret."""


@dataclass(frozen=True)
class AddonValues:
    environment: str
    mcp_url: str
    protected_resource_url: str
    authorize_url: str
    token_url: str
    scope: str
    client_id: str | None
    client_secret: str | None


# --- Reading the outputs ------------------------------------------------------------------------


def output_value(outputs: dict, name: str) -> object:
    """One output's value from `terraform output -json` ({name: {"value": ..., ...}}), or None."""
    entry = outputs.get(name)
    if not isinstance(entry, dict):
        return None
    return entry.get("value")


def require_https(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise CliError(
            f"Terraform has no {name} output. Apply the environment first; if it is applied, "
            "check that this branch's infra/environments/<env>/outputs.tf has it."
        )
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.netloc:
        raise CliError(f"{name} is not an https:// URL. Alexa refuses anything else; check the apply.")
    return value


def extract_values(outputs: dict, environment: str) -> AddonValues:
    """The add-on's values from one environment's `output -json`, checked."""
    if environment not in ENVIRONMENTS:
        raise CliError(f"Unknown environment: give one of {', '.join(ENVIRONMENTS)}.")

    def url(name: str) -> str:
        return require_https(name, output_value(outputs, name))

    client_id = output_value(outputs, "ops_alexa_client_id")
    client_secret = output_value(outputs, "ops_alexa_client_secret")
    values = AddonValues(
        environment=environment,
        mcp_url=url("ops_mcp_url"),
        protected_resource_url=url("ops_oauth_protected_resource_url"),
        authorize_url=url("ops_oauth_authorize_url"),
        token_url=url("ops_oauth_token_url"),
        scope=SCOPE,
        client_id=client_id if isinstance(client_id, str) and client_id else None,
        client_secret=client_secret if isinstance(client_secret, str) and client_secret else None,
    )
    check_environment(values)
    return values


def check_environment(values: AddonValues) -> None:
    """Refuse values that are not all from the one environment asked for.

    Every API stage in this repo is named after its environment (stage_name = "dev" or
    "production"), so the MCP URL's first path segment says which environment it is. The metadata
    document sits under the same stage, and the two Cognito URLs on the same hosted domain."""
    env = values.environment
    mcp = urlsplit(values.mcp_url)
    segments = [part for part in mcp.path.split("/") if part]
    if segments != [env, "mcp"]:
        raise CliError(
            f"ops_mcp_url is not {env}'s MCP endpoint (expected .../{env}/mcp). Check that you "
            f"read infra/environments/{env} and that its state is {env}'s."
        )
    stage_url = values.mcp_url[: -len("/mcp")]
    if not values.protected_resource_url.startswith(stage_url + "/"):
        raise CliError("ops_oauth_protected_resource_url is not on the same API stage as ops_mcp_url.")
    authorize_host = urlsplit(values.authorize_url).netloc
    if urlsplit(values.token_url).netloc != authorize_host:
        raise CliError("ops_oauth_authorize_url and ops_oauth_token_url are on different hosts.")
    for other in ENVIRONMENTS:
        if other != env and authorize_host.startswith(f"bloggerbear-{other}-"):
            raise CliError(f"The Cognito URLs are {other}'s sign-in domain, not {env}'s.")


# --- Printing -----------------------------------------------------------------------------------


def format_values(values: AddonValues, show_secret: bool = False) -> str:
    """The text for stdout. The secret only appears when show_secret is True."""
    if values.client_id:
        client_id = values.client_id
    else:
        client_id = (
            f"(none yet: put Alexa's redirect URLs in ops_alexa_redirect_uris for {values.environment} "
            "and apply; alexa/README.md, step 3)"
        )
    if not values.client_secret:
        secret = "(none)"
    elif show_secret:
        secret = values.client_secret
    else:
        secret = (
            f"(hidden; terraform -chdir=infra/environments/{values.environment} "
            "output -raw ops_alexa_client_secret, or --show-secret)"
        )
    rows = [
        ("Environment", values.environment),
        ("Add-on name", ADDON_NAMES[values.environment]),
        ("MCP endpoint", values.mcp_url),
        ("Protected resource metadata", values.protected_resource_url),
        ("Authorization URL", values.authorize_url),
        ("Token URL", values.token_url),
        ("Scope", values.scope),
        ("Client id", client_id),
        ("Client secret", secret),
    ]
    width = max(len(label) for label, _ in rows)
    return "\n".join(f"{label.ljust(width)}  {value}" for label, value in rows) + "\n"


# --- The manifest -------------------------------------------------------------------------------


def manifest_substitutions(values: AddonValues) -> dict[str, str]:
    return {
        "{{ADDON_NAME}}": ADDON_NAMES[values.environment],
        "{{ADDON_DESCRIPTION}}": ADDON_DESCRIPTION,
        "{{MCP_URL}}": values.mcp_url,
    }


def _fill(node: object, substitutions: dict[str, str]) -> object:
    # Substituting in the parsed JSON, not in its text, so a value can never break the quoting.
    if isinstance(node, str):
        for placeholder, value in substitutions.items():
            node = node.replace(placeholder, value)
        return node
    if isinstance(node, list):
        return [_fill(item, substitutions) for item in node]
    if isinstance(node, dict):
        return {key: _fill(item, substitutions) for key, item in node.items()}
    return node


def _strings(node: object):
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)
    elif isinstance(node, dict):
        for key, item in node.items():
            yield key
            yield from _strings(item)


def render_manifest(template_text: str, values: AddonValues) -> str:
    """The template with every placeholder filled, as JSON text. Refuses to leave one behind."""
    try:
        template = json.loads(template_text)
    except ValueError as exc:
        raise CliError(f"The add-on template is not valid JSON: {exc}") from None
    manifest = _fill(template, manifest_substitutions(values))
    if any(PLACEHOLDER_OPEN in text for text in _strings(manifest)):
        raise CliError("The add-on template has a placeholder this script does not know. Nothing written.")
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def write_manifest(path: Path, text: str, force: bool = False) -> None:
    if path.exists() and not force:
        raise CliError(f"{path} already exists. Pass --force to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --- Terraform ----------------------------------------------------------------------------------


def run_terraform(environment: str) -> dict:
    """`terraform output -json` for one environment. The only function that runs anything."""
    chdir = REPO_ROOT / "infra" / "environments" / environment
    try:
        result = subprocess.run(
            ["terraform", f"-chdir={chdir}", "output", "-json"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        raise CliError("terraform is not installed, or not on PATH.") from None
    if result.returncode != 0:
        # Terraform's own error, which names no output values: it failed before printing any.
        raise CliError(f"terraform output failed for {environment}:\n{result.stderr.strip()}")
    try:
        outputs = json.loads(result.stdout or "{}")
    except ValueError:
        raise CliError("terraform output -json did not print JSON.") from None
    if not outputs:
        raise CliError(f"{environment} has no Terraform outputs: run terraform init and apply first.")
    return outputs


# --- Entry point --------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Print the values an environment's Alexa+ add-on needs, from its Terraform outputs "
            "(alexa/README.md). Changes nothing unless --write-manifest is given."
        )
    )
    parser.add_argument("environment", choices=ENVIRONMENTS, help="whose add-on: dev or production")
    parser.add_argument(
        "--write-manifest",
        metavar="PATH",
        type=Path,
        help="also fill alexa/addon-package/addon.template.json and write it to PATH",
    )
    parser.add_argument("--force", action="store_true", help="let --write-manifest replace an existing file")
    parser.add_argument(
        "--show-secret",
        action="store_true",
        help="print the Alexa client secret too (only while you type it into alexa-ai)",
    )
    parser.add_argument("--template", type=Path, default=TEMPLATE_PATH, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None, terraform=run_terraform, out=sys.stdout, err=sys.stderr) -> int:
    args = build_parser().parse_args(argv)
    try:
        values = extract_values(terraform(args.environment), args.environment)
        if args.write_manifest is not None:
            text = render_manifest(args.template.read_text(encoding="utf-8"), values)
            write_manifest(args.write_manifest, text, force=args.force)
            print(f"Wrote {args.write_manifest}. Check it with alexa-ai before you deploy.", file=err)
    except CliError as exc:
        print(f"error: {exc}", file=err)
        return 1
    except OSError as exc:  # the template unreadable, or PATH unwritable
        print(f"error: {exc.strerror or exc}: {exc.filename}", file=err)
        return 1
    if args.show_secret and values.client_secret:
        print(
            "WARNING: the client secret is printed below. Do not paste it into a chat, an issue, a "
            "commit or a shared terminal; clear your scrollback when you are done.",
            file=err,
        )
    out.write(format_values(values, show_secret=args.show_secret))
    return 0


if __name__ == "__main__":
    sys.exit(main())
