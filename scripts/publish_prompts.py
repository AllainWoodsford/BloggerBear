#!/usr/bin/env python3
"""Publish the prompts tree to one deployment's prompts bucket: each run is one release.

docs/enhancements/model-ranch-prompt-assets-evals.md, section 4.2. The repository's latest/ folders
(prompts/README.md) are the desired state, and a run makes the bucket `<prefix>-<env>-prompts` hold
exactly them, recording the bytes it approves in the table `<prefix>-<env>-prompt-hashes`. Only the
publisher's role may write either (infra/modules/prompts, infra/bootstrap); a Lambda serves a file
only when the table says `live` (or `retired` less than prompts.RETIRE_GRACE_S ago) for the sha256
of its bytes. The steps run in this order, so a run that stops part-way leaves the previous release
serving:

1. for each file whose sha256 has no hash row: the row, `status = pending`, put only if there is
   still none (`attribute_not_exists(prompt_hash)`: the write-once guard on versions/, a conditional
   PutItem before the upload, as the pinned boto3 has no PutObject IfNoneMatch), then the file to
   `<prompt_id>/versions/<sha256>.txt`. A pending row is never served, and nothing in latest/ has
   changed yet. A file whose row already exists is not uploaded again, unless the row is still
   pending and its versions/ copy is missing (a run that stopped between the two);
2. every file of the release: its row `live`, naming the release;
3. latest/: each file copied from versions/ to `<prompt_id>/latest/vN.txt`, tagged `version`,
   `release` and `sha256` (a file already there with the same bytes only has its tags replaced),
   then every latest/ object the release does not hold deleted: added first, removed after;
4. the rows the previous release made live that this one does not hold (every other `live` row,
   so one a stopped run left behind is caught too): `retired`, with `retired_at`. The Lambdas still
   accept them for the grace, so a container that listed the old files keeps serving them;
5. `releases/<release_id>.json`, what the release holds, then `releases/current.json`, which
   names it (prompts.CURRENT_RELEASE_FIELD) and the release a rollback holds to
   (prompts.PINNED_RELEASE_FIELD, null when none).

`release_id` is `<env>-<yyyymmddThhmm>-<short sha>` (UTC, the commit published). Nothing is ever
deleted outside latest/: versions/ is written once and kept, and releases/ only grows.

**A version names one text for ever**, and the hash table is the record of every version this
deployment has published. Before anything is written, a tree is refused (as is one the lint refuses)
when a file's version was published with other bytes, when its bytes were published as another
version, or when a new version is not numbered above every published version of its prompt: the
lint holds the tree to the same rules against git, and a revert in git can hide what was published.

**Production** runs the latest release's code, so the lint's second pass (scripts/prompt_lint.py)
names the prompts that release cannot read yet (`skip-at-production`); a production run leaves each
of those exactly as it is in the bucket (no upload, no change to its latest/ files or its rows) and
records its files as carried forward, so a later rollback to this release restores them too.

**Rollback** is `git revert` and a publish. For a faster one, `--release-id <id>` pins an earlier
release: its files (from its releases/ record, and versions/) are made live and synced into latest/
again (steps 2 and 3), every other live row is retired (step 4), and current.json names it as both
the release in place and `pinned_release` (no new release record). While a pin is set, every run
(a merge, a release) publishes the pinned release again instead of the tree, until a run with
`--clear-pin` publishes the tree as a new release and clears it.

Nothing printed holds a prompt's text: the plan names prompts, versions, keys and short hashes.

    python scripts/publish_prompts.py --environment dev --dry-run      # print the plan, write nothing
    python scripts/publish_prompts.py --environment dev
    python scripts/publish_prompts.py --environment production --release-ref v0.4.0
    python scripts/publish_prompts.py --environment production --release-id production-20261026T0304-1a2b3c4
    python scripts/publish_prompts.py --environment production --release-ref v0.4.0 --clear-pin
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import boto3
from botocore.exceptions import BotoCoreError, ClientError

ROOT = Path(__file__).resolve().parents[1]
LAMBDAS = ROOT / "lambdas"
if str(LAMBDAS) not in sys.path:
    sys.path.insert(0, str(LAMBDAS))

from common import naming, prompts  # noqa: E402 - after the path above

import prompt_lint  # noqa: E402

ENVIRONMENTS = ("dev", "production")
PENDING, LIVE, RETIRED = "pending", "live", "retired"
RELEASES_PREFIX = prompts.RELEASES_PREFIX
CURRENT_KEY = prompts.CURRENT_RELEASE_KEY
RELEASE_ID = re.compile(r"^(dev|production)-[0-9]{8}T[0-9]{4}-[0-9a-f]{7}$")
# The only keys this script ever deletes: a file in a prompt's latest/ folder.
LATEST_KEY = re.compile(r"^([a-z][a-z0-9_]{0,39}/[a-z][a-z0-9_]{0,39})/latest/([^/]+)$")
CONTENT_TYPE = "text/plain; charset=utf-8"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PublishError(Exception):
    """The run cannot go ahead (a lint error, an unknown release, a version reused with new bytes),
    or a step failed. Nothing after the failing step has been written."""


def bucket_name(environment: str) -> str:
    """The prompts bucket Terraform makes (infra/modules/prompts): `<prefix>-<env>-prompts`."""
    return f"{naming.environment_prefix(environment)}prompts"


def table_name(environment: str) -> str:
    """The hash table Terraform makes: `<prefix>-<env>-prompt-hashes`."""
    return f"{naming.environment_prefix(environment)}prompt-hashes"


def bucket_region(s3, bucket: str) -> str:
    """The region `bucket` is in, from HeadBucket (which s3:ListBucket allows). The hash table is
    made in the same Terraform root as the bucket (infra/modules/prompts), so it is there too."""
    response = s3.head_bucket(Bucket=bucket)
    headers = response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
    region = response.get("BucketRegion") or headers.get("x-amz-bucket-region")
    if not region:
        raise PublishError(f"{bucket} did not say which region it is in")
    return region


def make_release_id(environment: str, commit: str, now: datetime) -> str:
    return f"{environment}-{now.astimezone(UTC):%Y%m%dT%H%M}-{commit[:7]}"


def iso(now: datetime) -> str:
    """UTC with a Z: what prompts._epoch reads back for the retire grace."""
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def head_commit(root: Path) -> str:
    done = subprocess.run(  # noqa: S603 - argv is a list built here, never a shell string
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=60, check=False
    )
    commit = done.stdout.strip()
    if done.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise PublishError("cannot tell which commit is being published (git rev-parse HEAD failed)")
    return commit


@dataclass(frozen=True)
class Item:
    """One file of a release. `raw` is the repository's bytes (None for a file carried forward or
    restored by a pin, which comes from versions/); `carried` marks a skipped prompt's file."""

    prompt_id: str
    version: str
    sha256: str
    raw: bytes | None = None
    carried: bool = False

    @property
    def latest_key(self) -> str:
        return f"{self.prompt_id}/latest/{self.version}.txt"

    @property
    def versions_key(self) -> str:
        return f"{self.prompt_id}/versions/{self.sha256}.txt"

    def record(self) -> dict:
        return {
            "prompt_id": self.prompt_id,
            "version": self.version,
            "sha256": self.sha256,
            "key": self.latest_key,
            "carried_forward": self.carried,
        }


@dataclass
class Plan:
    """Everything a run will do, worked out before it writes anything."""

    environment: str
    release_id: str
    commit: str
    mode: str  # "publish" (the tree) or "pin" (an earlier release again)
    pinned: str | None  # what current.json's pinned_release will say
    previous: str | None  # current.json's release before this run
    items: list[Item] = field(default_factory=list)
    new_rows: list[Item] = field(default_factory=list)  # step 1: no row yet: the row, then the upload
    repairs: list[Item] = field(default_factory=list)  # step 1: a row, but its versions/ copy is missing
    live: list[Item] = field(default_factory=list)  # step 2
    copies: list[Item] = field(default_factory=list)  # step 3: written into latest/
    retags: list[Item] = field(default_factory=list)  # step 3: already there, tags replaced
    stale: list[str] = field(default_factory=list)  # step 3: latest/ keys to delete
    retire: list[str] = field(default_factory=list)  # step 4: sha256 of rows to retire
    skipped: dict[str, str] = field(default_factory=dict)  # prompt_id -> why (production)
    write_manifest: bool = True  # step 5: a pin re-uses the release's own record


class Publisher:
    """Reads and writes one deployment's bucket and hash table."""

    def __init__(self, *, s3, table, bucket: str, environment: str, say: Callable[[str], None] = print):
        self.s3, self.table, self.bucket = s3, table, bucket
        self.environment = environment
        self.say = say

    # --- reading -------------------------------------------------------------------------------

    def current(self) -> dict | None:
        """releases/current.json, or None before the first publish."""
        try:
            raw = self.s3.get_object(Bucket=self.bucket, Key=CURRENT_KEY)["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return None
        return _json_object(raw, CURRENT_KEY)

    def manifest(self, release_id: str) -> dict:
        try:
            key = f"{RELEASES_PREFIX}{release_id}.json"
            raw = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self.s3.exceptions.NoSuchKey as exc:
            raise PublishError(f"there is no release {release_id} in {self.bucket}") from exc
        manifest = _json_object(raw, key)
        if not isinstance(manifest.get("files"), list):
            raise PublishError(f"{key} has no list of files")
        return manifest

    def rows(self) -> dict[str, dict]:
        """Every hash row, by sha256 (the table holds one per file ever published: a scan is cheap)."""
        found, kwargs = {}, {"ConsistentRead": True}
        while True:
            page = self.table.scan(**kwargs)
            for row in page.get("Items", []):
                found[row["prompt_hash"]] = row
            if "LastEvaluatedKey" not in page:
                return found
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def latest_objects(self) -> dict[str, dict[str, str]]:
        """Every file in a prompt's latest/ folder, with its tags. Other keys are not this script's."""
        found = {}
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket):
            for entry in page.get("Contents", []):
                match = LATEST_KEY.fullmatch(entry["Key"])
                if match and prompts.is_prompt_id(match.group(1)):
                    found[entry["Key"]] = self.tags(entry["Key"])
        return found

    def tags(self, key: str) -> dict[str, str]:
        response = self.s3.get_object_tagging(Bucket=self.bucket, Key=key)
        return {tag["Key"]: tag["Value"] for tag in response.get("TagSet", [])}

    def exists(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True

    def carried_items(self, prompt_id: str, latest: dict[str, dict[str, str]]) -> list[Item]:
        """A skipped prompt's files as the bucket has them now, by the sha256 of their bytes."""
        items = []
        for key, tags in sorted(latest.items()):
            if not key.startswith(f"{prompt_id}/latest/"):
                continue
            raw = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            sha = prompts.sha256_text(raw.decode("utf-8", errors="surrogateescape"))
            version = tags.get("version") or key.rsplit("/", 1)[1].removesuffix(".txt")
            items.append(Item(prompt_id, version, sha, carried=True))
        return items

    # --- planning ------------------------------------------------------------------------------

    def plan_publish(
        self,
        files: list[prompt_lint.AssetFile],
        *,
        release_id: str,
        commit: str,
        skips: dict[str, str],
        current: dict | None,
    ) -> Plan:
        """The tree as a new release. `skips` (production) are left as they are."""
        rows, latest = self.rows(), self.latest_objects()
        previous = _field(current, prompts.CURRENT_RELEASE_FIELD)
        plan = Plan(self.environment, release_id, commit, "publish", None, previous)
        plan.skipped = dict(sorted(skips.items()))
        for file in sorted((f for f in files if f.folder == prompt_lint.LATEST), key=lambda f: f.path):
            if file.prompt_id in skips:
                continue
            plan.items.append(Item(file.prompt_id, file.version, file.sha256, raw=file.raw))
        for prompt_id in plan.skipped:
            plan.items += self.carried_items(prompt_id, latest)
        self._refuse_reused_versions(plan.items, rows)
        self._plan_rows_and_latest(plan, rows, latest)
        return plan

    def plan_pin(self, release_id: str, *, current: dict | None) -> Plan:
        """An earlier release again, from its own record, held by current.json's pinned_release."""
        if not RELEASE_ID.fullmatch(release_id) or not release_id.startswith(f"{self.environment}-"):
            raise PublishError(f"{release_id!r} is not a release id of {self.environment}")
        manifest = self.manifest(release_id)
        rows, latest = self.rows(), self.latest_objects()
        plan = Plan(
            self.environment,
            release_id,
            str(manifest.get("commit", "")),
            "pin",
            release_id,
            _field(current, prompts.CURRENT_RELEASE_FIELD),
            write_manifest=False,
        )
        for entry in manifest["files"]:
            fields = [entry.get(name) if isinstance(entry, dict) else None for name in _FILE_FIELDS]
            if not all(isinstance(value, str) for value in fields):
                missing = ", ".join(_FILE_FIELDS)
                raise PublishError(f"{release_id}'s record has a file entry without {missing}")
            item = Item(*fields, carried=bool(entry.get("carried_forward")))
            named = prompt_lint.VERSION_FILE.fullmatch(f"{item.version}.txt")
            if not prompts.is_prompt_id(item.prompt_id) or not named or not SHA256.fullmatch(item.sha256):
                raise PublishError(f"{release_id} names {item.latest_key}, which is not a prompt file")
            if item.sha256 not in rows or not self.exists(item.versions_key):
                raise PublishError(f"{release_id}'s {item.latest_key} is not in versions/ with a hash row")
            plan.items.append(item)
        self._plan_rows_and_latest(plan, rows, latest, carry_untouched=False)
        return plan

    def _refuse_reused_versions(self, items: list[Item], rows: dict[str, dict]) -> None:
        """A version names one text for ever, and the hash table is the record of every one this
        deployment has published (a `live` or `retired` row; a `pending` row was never served). So a
        file is refused when its version was published with other bytes, when its bytes were
        published as another version (restore that one), or when it is a new version not numbered
        above every published version of its prompt; and two files of this release may not have the
        same bytes. The lint holds the tree to the same rules against git; this holds it to what
        was really published, which a revert in git can hide."""
        published: dict[tuple[str, str], str] = {}
        highest: dict[str, int] = {}
        for sha, row in rows.items():
            prompt_id, version = row.get("prompt_id"), row.get("version")
            if row.get("status") not in (LIVE, RETIRED) or not isinstance(version, str):
                continue
            published[(prompt_id, version)] = sha
            named = prompt_lint.VERSION_FILE.fullmatch(f"{version}.txt")
            if named:
                highest[prompt_id] = max(highest.get(prompt_id, 0), int(named.group(2)))
        seen: dict[str, Item] = {}
        for item in items:
            if item.carried:
                continue
            label = f"{item.prompt_id}@{item.version}"
            other = published.get((item.prompt_id, item.version))
            if other is not None and other != item.sha256:
                raise PublishError(
                    f"{label} was published before with other bytes "
                    f"(sha256 {other[:12]}): a changed text is a new version"
                )
            row = rows.get(item.sha256)
            if row and row.get("status") in (LIVE, RETIRED):
                was = (row.get("prompt_id"), row.get("version"))
                if was != (item.prompt_id, item.version):
                    raise PublishError(
                        f"{item.latest_key} has the bytes published as {was[0]}@{was[1]}: "
                        "a text keeps its version (restore that one)"
                    )
            top = highest.get(item.prompt_id, 0)
            if other is None and int(item.version[1:]) <= top:
                raise PublishError(
                    f"{label} is new but not numbered above v{top}, the highest version of "
                    f"{item.prompt_id} published here: number a new version above every earlier one"
                )
            twin = seen.setdefault(item.sha256, item)
            if twin is not item:
                raise PublishError(f"{item.latest_key} has the same bytes as {twin.latest_key}")

    def _plan_rows_and_latest(
        self,
        plan: Plan,
        rows: dict[str, dict],
        latest: dict[str, dict[str, str]],
        carry_untouched: bool = True,
    ) -> None:
        touched = [item for item in plan.items if not (carry_untouched and item.carried)]
        for item in touched:
            row = rows.get(item.sha256)
            if row is None:
                if item.raw is None:
                    raise PublishError(f"{item.latest_key} has no hash row and no bytes to publish")
                plan.new_rows.append(item)
            elif row.get("status") == PENDING:
                # A run that stopped after the row and before (or during) the upload: the row is
                # the guard, and it is this run's to finish, so the copy is written if it is missing.
                if item.raw is not None and not self.exists(item.versions_key):
                    plan.repairs.append(item)
            if row is None or row.get("status") != LIVE or row.get("release") != plan.release_id:
                plan.live.append(item)
            there = latest.get(item.latest_key)
            if there is not None and there.get("sha256") == item.sha256:
                plan.retags.append(item)
            else:
                plan.copies.append(item)
        keep = {item.latest_key for item in plan.items}
        plan.stale = sorted(key for key in latest if key not in keep)
        holds = {item.sha256 for item in plan.items}
        plan.retire = sorted(
            sha for sha, row in rows.items() if row.get("status") == LIVE and sha not in holds
        )

    # --- writing -------------------------------------------------------------------------------

    def apply(self, plan: Plan, now: datetime) -> None:
        """Steps 1 to 5, in order. A failure raises PublishError naming the step; what the steps
        before it wrote stays, and is harmless until latest/ changes (step 3)."""
        stamp = iso(now)
        for number, step in enumerate((self._step1, self._step2, self._step3, self._step4, self._step5), 1):
            try:
                step(plan, stamp)
            except PublishError:
                raise
            except Exception as exc:  # noqa: BLE001 - any failure stops the run where it is
                raise PublishError(f"step {number} failed: {exc!r}") from exc

    def _step1(self, plan: Plan, stamp: str) -> None:
        for item in plan.new_rows:
            try:
                self.table.put_item(
                    Item={
                        "prompt_hash": item.sha256,
                        "prompt_id": item.prompt_id,
                        "version": item.version,
                        "release": plan.release_id,
                        "status": PENDING,
                    },
                    ConditionExpression="attribute_not_exists(prompt_hash)",
                )
            except self.table.meta.client.exceptions.ConditionalCheckFailedException:
                # The row appeared since the plan: versions/ is that writer's, so it is written
                # here only if its copy is still missing. The guard is the row, never the upload.
                self.say(f"  {item.prompt_id}@{item.version}: its hash row appeared since the plan")
                if self.exists(item.versions_key):
                    continue
            self._upload(item)
        for item in plan.repairs:
            self._upload(item)

    def _upload(self, item: Item) -> None:
        self.s3.put_object(
            Bucket=self.bucket,
            Key=item.versions_key,
            Body=item.raw.replace(b"\r\n", b"\n"),
            ContentType=CONTENT_TYPE,
        )

    def _step2(self, plan: Plan, stamp: str) -> None:
        for item in plan.live:
            self.table.update_item(
                Key={"prompt_hash": item.sha256},
                UpdateExpression=(
                    "SET #status = :live, #release = :release, prompt_id = :prompt_id, #version = :version, "
                    "approved_at = :now REMOVE retired_at"
                ),
                ConditionExpression="attribute_exists(prompt_hash)",
                ExpressionAttributeNames={"#status": "status", "#release": "release", "#version": "version"},
                ExpressionAttributeValues={
                    ":live": LIVE,
                    ":release": plan.release_id,
                    ":prompt_id": item.prompt_id,
                    ":version": item.version,
                    ":now": stamp,
                },
            )

    def _tagging(self, plan: Plan, item: Item) -> str:
        return urlencode({"version": item.version, "release": plan.release_id, "sha256": item.sha256})

    def _step3(self, plan: Plan, stamp: str) -> None:
        for item in plan.copies:
            self.s3.copy_object(
                Bucket=self.bucket,
                Key=item.latest_key,
                CopySource={"Bucket": self.bucket, "Key": item.versions_key},
                ContentType=CONTENT_TYPE,
                MetadataDirective="REPLACE",
                Tagging=self._tagging(plan, item),
                TaggingDirective="REPLACE",
            )
        for item in plan.retags:
            tags = [
                {"Key": "version", "Value": item.version},
                {"Key": "release", "Value": plan.release_id},
                {"Key": "sha256", "Value": item.sha256},
            ]
            self.s3.put_object_tagging(Bucket=self.bucket, Key=item.latest_key, Tagging={"TagSet": tags})
        for key in plan.stale:
            self._delete_latest(key)

    def _delete_latest(self, key: str) -> None:
        """The one delete this script makes, and only of a file in a prompt's latest/ folder."""
        match = LATEST_KEY.fullmatch(key)
        if not match or not prompts.is_prompt_id(match.group(1)):
            raise PublishError(f"refusing to delete {key!r}: only latest/ files are ever deleted")
        self.s3.delete_object(Bucket=self.bucket, Key=key)

    def _step4(self, plan: Plan, stamp: str) -> None:
        for sha in plan.retire:
            try:
                self.table.update_item(
                    Key={"prompt_hash": sha},
                    UpdateExpression="SET #status = :retired, retired_at = :now",
                    ConditionExpression="#status = :live",
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={":retired": RETIRED, ":live": LIVE, ":now": stamp},
                )
            except self.table.meta.client.exceptions.ConditionalCheckFailedException:
                pass  # no longer live: nothing to retire

    def _step5(self, plan: Plan, stamp: str) -> None:
        if plan.write_manifest:
            manifest = {
                "release_id": plan.release_id,
                "environment": plan.environment,
                "commit": plan.commit,
                "published_at": stamp,
                "previous_release": plan.previous,
                "files": [item.record() for item in plan.items],
                "skipped": [{"prompt_id": pid, "reason": why} for pid, why in plan.skipped.items()],
            }
            self._put_json(f"{RELEASES_PREFIX}{plan.release_id}.json", manifest)
        current = {
            prompts.CURRENT_RELEASE_FIELD: plan.release_id,
            prompts.PINNED_RELEASE_FIELD: plan.pinned,
            "environment": plan.environment,
            "commit": plan.commit,
            "published_at": stamp,
        }
        self._put_json(CURRENT_KEY, current)

    def _put_json(self, key: str, value: dict) -> None:
        body = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType="application/json")


def _field(current: dict | None, name: str):
    return current.get(name) if current else None


# What a release's record holds for each file, in Item's order.
_FILE_FIELDS = ("prompt_id", "version", "sha256")


def _json_object(raw: bytes, key: str) -> dict:
    """`raw`, the publisher's own JSON record at `key`, as a dict; PublishError when it is not one."""
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise PublishError(f"{key} is not JSON: {exc!r}") from exc
    if not isinstance(value, dict):
        raise PublishError(f"{key} is not a JSON object")
    return value


def describe(plan: Plan, say: Callable[[str], None] = print) -> None:
    """The plan in words: prompts, versions, keys and short hashes, never a prompt's text."""
    if plan.mode == "pin":
        say(f"release {plan.release_id} again (pinned), in {plan.environment}; in place now: {plan.previous}")
    else:
        say(
            f"release {plan.release_id} of commit {plan.commit[:7]}, in {plan.environment}; "
            f"previous: {plan.previous}"
        )
    by_prompt: dict[str, list[Item]] = {}
    for item in plan.items:
        by_prompt.setdefault(item.prompt_id, []).append(item)
    new = {item.sha256 for item in plan.new_rows}
    for prompt_id, items in sorted(by_prompt.items()):
        parts = []
        for item in sorted(items, key=lambda i: int(i.version[1:])):
            state = "new" if item.sha256 in new else "published before"
            state = "carried forward" if item.carried else state
            parts.append(f"{item.version} ({item.sha256[:12]}, {state})")
        split = f"A/B split of {len(items)}" if len(items) > 1 else "one file"
        say(f"  {prompt_id}: {split}: {', '.join(parts)}")
    if not plan.items:
        say("  no files: every prompt serves its vanilla")
    for prompt_id, why in plan.skipped.items():
        say(f"  skip-at-production: {prompt_id} ({why}): left as it is")
    say(f"step 1: {len(plan.new_rows)} hash row(s) to add as pending, each with its file to versions/")
    for item in plan.repairs:
        say(f"  {item.versions_key}: its hash row exists but the file does not: uploading it")
    say(f"step 2: {len(plan.live)} row(s) to set live with the release")
    say(
        f"step 3: {len(plan.copies)} file(s) into latest/, {len(plan.retags)} retagged, "
        f"{len(plan.stale)} to remove"
    )
    for key in plan.stale:
        say(f"  remove {key}")
    say(f"step 4: {len(plan.retire)} row(s) to retire")
    records = f"releases/{plan.release_id}.json and " if plan.write_manifest else ""
    say(f"step 5: {records}{CURRENT_KEY} ({prompts.PINNED_RELEASE_FIELD}: {plan.pinned})")


def main(argv: list[str] | None = None, *, s3=None, dynamodb=None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--environment", required=True, choices=ENVIRONMENTS, help="the deployment to publish to"
    )
    parser.add_argument(
        "--release-ref",
        help="production: the release production runs (its registry decides what is skipped); "
        "required to publish the tree there",
    )
    pin = parser.add_mutually_exclusive_group()
    pin.add_argument("--release-id", help="pin this earlier release (a rollback) until --clear-pin")
    pin.add_argument("--clear-pin", action="store_true", help="publish the tree and clear a pinned release")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and write nothing")
    parser.add_argument(
        "--root", type=Path, default=ROOT, help="the repository to publish (default: this one)"
    )
    parser.add_argument("--commit", help="the commit being published (default: the root's HEAD)")
    parser.add_argument("--bucket", help="the prompts bucket (default: <prefix>-<env>-prompts)")
    parser.add_argument("--table", help="the hash table (default: <prefix>-<env>-prompt-hashes)")
    args = parser.parse_args(argv)
    now = now or datetime.now(UTC)

    bucket = args.bucket or bucket_name(args.environment)
    s3 = s3 or boto3.client("s3")
    try:
        # The run signs in wherever AWS_REGION says, and a `production` value of it reaches only a
        # release's run, not a dispatch's (prompts.yml): the table is read where the bucket is,
        # whichever region the run started in. S3 itself follows a bucket to its region.
        if dynamodb is None:
            dynamodb = boto3.resource("dynamodb", region_name=bucket_region(s3, bucket))
        table = dynamodb.Table(args.table or table_name(args.environment))
        publisher = Publisher(s3=s3, table=table, bucket=bucket, environment=args.environment)
        current = publisher.current()
        held = _field(current, prompts.PINNED_RELEASE_FIELD)
        if args.release_id or (held and not args.clear_pin):
            release_id = args.release_id or held
            if not args.release_id:
                print(f"{release_id} is pinned: publishing it again; the tree waits until --clear-pin")
            plan = publisher.plan_pin(release_id, current=current)
        else:
            plan = _plan_tree(publisher, args, current, now)
        describe(plan)
        if args.dry_run:
            print("dry run: nothing was written")
            return 0
        publisher.apply(plan, now)
    except (PublishError, prompt_lint.LintError) as exc:
        print(f"publish: {exc}", file=sys.stderr)
        return 1
    except (BotoCoreError, ClientError) as exc:
        print(f"publish: AWS refused or could not be reached: {exc!r}", file=sys.stderr)
        return 1
    print(f"published {plan.release_id} to {bucket}")
    return 0


def _plan_tree(publisher: Publisher, args, current: dict | None, now: datetime) -> Plan:
    """Lint the tree (refusing on any error), and plan it as a new release."""
    if args.environment == "production" and not args.release_ref:
        raise PublishError("production needs --release-ref: the release whose code production runs")
    release_ref = args.release_ref if args.environment == "production" else None
    report = prompt_lint.lint(args.root, release_ref=release_ref)
    if not report.ok:
        for finding in report.errors:
            print(f"error: {finding}", file=sys.stderr)
        raise PublishError(f"the prompt lint found {len(report.errors)} error(s); nothing was published")
    commit = args.commit or head_commit(args.root)
    skips = report.skip_at_production if args.environment == "production" else {}
    release_id = make_release_id(args.environment, commit, now)
    return publisher.plan_publish(
        report.files, release_id=release_id, commit=commit, skips=skips, current=current
    )


if __name__ == "__main__":
    sys.exit(main())
