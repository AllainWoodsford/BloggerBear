"""The Admin CLI's prompt commands: the `prompts` group of scripts/admin_cli.py, in its own module.

Prompts as assets (docs/enhancements/model-ranch-prompt-assets-evals.md): prompt templates the
publisher puts in the prompts bucket, served by every Lambda in place of the copy in its code when
the deployment's switch is on and every check passes. These commands read what is published and
change what is served, through the Admin API's prompts routes:

    prompts list                 each registered prompt's files under latest/: version, release,
                                 sha256, whether the hash table approves it, whether it is paused
    prompts pause <id> <v>       stop serving one version (the prompt's other files, or the copy in
                                 the code, are served instead); prompts resume undoes it
    prompts releases             what each publish did, newest first, and the one in place
    prompts set --assets off     the kill switch: every prompt is the copy in the code, no deploy

A change reaches every run within five minutes (each Lambda container keeps the settings that long).
`register` wires the group into admin_cli's parser; admin_cli.build_parser calls it.
"""

from __future__ import annotations

import argparse
import urllib.parse

# How much of a sha256 the table shows: enough to tell files apart and to find one in the bucket.
SHA_SHOWN = 12


def register(subparsers: argparse._SubParsersAction, helpers: dict) -> None:
    """Add the `prompts` commands to admin_cli's top-level subparsers.

    `helpers` is what admin_cli lends this module, by name: "do_request" (its signed call to the
    Admin API, which prints the answer), "fetch_json" (the same call, handing back the answer for a
    table; a non-2xx answer is printed and exits), "print_table" (headers and rows, aligned) and
    "CliError" (what a command raises to stop with a message). They are passed in rather than
    imported because admin_cli imports this module, and an import back would be a cycle.
    """
    do_request = helpers["do_request"]
    fetch_json = helpers["fetch_json"]
    print_table = helpers["print_table"]
    cli_error = helpers["CliError"]

    def prompts_list(args: argparse.Namespace) -> None:
        if args.as_json:
            do_request(args, "GET", "/prompts")
            return
        data = fetch_json(args, "GET", "/prompts")
        _print_summary(data)
        listed = data.get("prompts") or []
        if not listed:
            print("No prompt is registered yet: every prompt is still written in the code alone.")
            return
        print()
        headers = ["PROMPT", "VERSION", "RELEASE", "SHA256", "HASH", "PAUSED", "NOTE"]
        print_table(headers, _prompt_rows(listed))

    def prompts_pause(args: argparse.Namespace) -> None:
        body = {"prompt_id": args.prompt_id, "version": args.version}
        do_request(args, "POST", "/prompts/pause", body=body)

    def prompts_resume(args: argparse.Namespace) -> None:
        body = {"prompt_id": args.prompt_id, "version": args.version}
        do_request(args, "POST", "/prompts/resume", body=body)

    def prompts_releases(args: argparse.Namespace) -> None:
        path = "/prompts/releases"
        if args.limit is not None:
            path += "?" + urllib.parse.urlencode({"limit": args.limit})
        if args.as_json:
            do_request(args, "GET", path)
            return
        data = fetch_json(args, "GET", path)
        if data.get("note"):
            print(data["note"])
        if data.get("error"):
            print(f"Note: {data['error']}.")
        releases = data.get("releases") or []
        if not releases:
            return
        total = data.get("total", len(releases))
        print(f"{len(releases)} of {total} release{'s' if total != 1 else ''}, newest first:")
        rows = [
            [
                release.get("release_id"),
                release.get("written_at"),
                ", ".join(word for word in ("current", "pinned") if release.get(word)) or "",
            ]
            for release in releases
        ]
        print_table(["RELEASE", "WRITTEN", "STATE"], rows)

    def prompts_set(args: argparse.Namespace) -> None:
        if args.assets is None:
            raise cli_error("prompts set needs --assets on|off ('' clears it, back to on)")
        do_request(args, "PUT", "/prompts/config", body={"prompt_assets": args.assets or None})

    prompts_parser = subparsers.add_parser(
        "prompts",
        help=(
            "Prompts as assets: what the publisher put in the prompts bucket, pausing one version, "
            "the releases, and the kill switch"
        ),
    )
    prompts_sub = prompts_parser.add_subparsers(dest="action", required=True)

    list_parser = prompts_sub.add_parser(
        "list",
        help=(
            "Each registered prompt's files under latest/ with their version, release, sha256, "
            "hash status and pause state, and whether this deployment serves assets at all"
        ),
    )
    list_parser.add_argument(
        "--json", dest="as_json", action="store_true", default=False, help="Print the raw answer as JSON"
    )
    list_parser.set_defaults(func=prompts_list)

    pause_parser = prompts_sub.add_parser(
        "pause",
        help=(
            "Stop serving one version of a prompt: its other files under latest/, or the copy in the "
            "code, are served instead, within five minutes"
        ),
    )
    pause_parser.add_argument("prompt_id", help="<lambda_id>/<prompt_name>, as `prompts list` shows it")
    pause_parser.add_argument("version", help="the version to stop serving, e.g. v2")
    pause_parser.set_defaults(func=prompts_pause)

    resume_parser = prompts_sub.add_parser("resume", help="Serve a paused version of a prompt again")
    resume_parser.add_argument("prompt_id", help="<lambda_id>/<prompt_name>, as `prompts list` shows it")
    resume_parser.add_argument("version", help="the paused version, e.g. v2")
    resume_parser.set_defaults(func=prompts_resume)

    releases_parser = prompts_sub.add_parser(
        "releases", help="What each publish did, newest first, and which release is in place (or pinned)"
    )
    releases_parser.add_argument(
        "--limit", type=int, default=None, help="how many releases to show (default 20, at most 200)"
    )
    releases_parser.add_argument(
        "--json", dest="as_json", action="store_true", default=False, help="Print the raw answer as JSON"
    )
    releases_parser.set_defaults(func=prompts_releases)

    set_parser = prompts_sub.add_parser(
        "set", help="The kill switch: --assets off serves every prompt's copy in the code, without a deploy"
    )
    set_parser.add_argument(
        "--assets",
        choices=["on", "off", ""],
        default=None,
        help="on (the default) or off; '' clears it, back to on",
    )
    set_parser.set_defaults(func=prompts_set)


def _print_summary(data: dict) -> None:
    """Whether this deployment serves assets, why not when it does not, and what is paused."""
    serving = "yes" if data.get("serving") else "no"
    print(f"Serving assets: {serving}. {data.get('serving_note') or ''}".rstrip())
    effective = data.get("effective") or {}
    print(
        f"Deployment switch (PROMPT_AS_ASSET_ENABLED): {data.get('deployment_switch') or '-'}; "
        f"kill switch (prompt_assets): {effective.get('prompt_assets') or '-'}; "
        f"bucket: {data.get('bucket') or 'none'}"
    )
    paused = effective.get("paused_versions") or []
    print(f"Paused: {', '.join(paused) if paused else 'nothing'}")


def _prompt_rows(listed: list[dict]) -> list[list]:
    """One row per file under latest/, and one for a prompt with none (code-only, nothing
    published, or its listing failed), which says so in NOTE."""
    rows = []
    for prompt in listed:
        files = prompt.get("files") or []
        if not files:
            note = prompt.get("note") or prompt.get("error") or "nothing under latest/"
            served = f"{prompt.get('vanilla_version')} (code)"
            rows.append([prompt.get("prompt_id"), served, None, None, None, "", note])
            continue
        for found in files:
            sha = found.get("sha256")
            hash_status = found.get("hash_status")
            if hash_status == "retired" and found.get("retired_at"):
                hash_status = f"retired {found['retired_at']}"
            rows.append(
                [
                    prompt.get("prompt_id"),
                    found.get("version"),
                    found.get("release"),
                    sha[:SHA_SHOWN] if sha else None,
                    hash_status,
                    "paused" if found.get("paused") else "",
                    found.get("error") or "",
                ]
            )
    return rows
