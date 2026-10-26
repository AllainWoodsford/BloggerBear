# Prompts

This folder holds the prompts the Lambdas may read from S3 instead of the copies in their code
(prompts as assets: [the design](../docs/enhancements/model-ranch-prompt-assets-evals.md), sections
4.1 and 4.2). What is in each prompt's `latest/` folder here is what each deployment serves: every
publish makes the bucket match it. A prompt with nothing here is served from the code, as it always
was, and so is every prompt while a deployment's `PROMPT_AS_ASSET_ENABLED` switch is off.

## The rules of the tree

- **One folder per prompt**, named for its id: `prompts/<lambda_id>/<prompt_name>/`, two lower-case
  codes (letters, digits and underscores, starting with a letter). The id must be one the registry
  knows ([`lambdas/common/prompts_registry.py`](../lambdas/common/prompts_registry.py)), and the
  registry's spec for it must be an asset: the code-only prompts (`asset=False`: the ops agent's
  system prompt, the compliance review, vision triage, comment screening, the judge's rubric) have
  no files here, ever.
- **`latest/vN.txt` is what is served.** One file is the prompt. Two or more are an A/B split: each
  run lands on one file, the same one for every prompt of that run's split, and the split is even
  across runs. Lineage records which version each run was given.
- **The file's name is its version**: `v1.txt`, `v2.txt`, and so on. A version names one text for
  ever. To change a prompt, add a new file numbered above every version the prompt has had, in
  `latest/` or `archive/`, here or in any earlier commit; never edit a published file in place and
  never reuse a number, even one that was reverted.
- **`archive/vN.txt` holds retired files**, moved there from `latest/` with their names and bytes
  unchanged. Nothing in `archive/` is ever published. To bring a version back, move it back.
- **One file is the whole prompt.** It is a template: `{{name}}` is a value the code fills in, and
  `{{locked:name}}` is a block the code fills in that the file can place but not change (a
  guardrail, such as the financial-topic rules or the reply format a parser needs). The spec in the
  registry says which values a prompt requires or may use, which locked blocks it has, in what
  order, and how long it may be. The locked blocks come last: nothing but locked blocks may follow
  the first of them. Anything that is not exactly `{{name}}` or `{{locked:name}}` is plain text.
- **UTF-8, LF line endings only**, no byte-order mark. The publisher hashes the bytes, and the hash
  is what approves a file.

## What the lint checks

[`scripts/prompt_lint.py`](../scripts/prompt_lint.py) runs on every pull request that touches this
folder (the `gates` job of [the prompts workflow](../.github/workflows/prompts.yml)) and again before
every publish of the tree, and refuses the tree on any error:

- every path is `<lambda_id>/<prompt_name>/latest/vN.txt` or `.../archive/vN.txt` (this README and
  `.gitkeep` are the only other files), and nothing in it is a symlink;
- every prompt id is in the registry, as an asset;
- every file is UTF-8 with LF line endings and no byte-order mark;
- every file in `latest/` passes the registry's template rules for its prompt (the same check the
  Lambdas run on each file they read): every required value present, no placeholder the spec does
  not name, each locked block once, in order, at the end, and no longer than the spec allows;
- no file in `latest/` holds a phrase that tells the model to set a locked block aside (the short
  list is in the script, with its reasons);
- no file in `latest/` has the same bytes as another file here;
- each version is used once per prompt, and against the pull request's base and the latest release
  a version keeps its bytes and a new one is numbered above every earlier one.

It also reads the latest release's registry, because production runs that release's code: a prompt
the release does not know, has as code-only, or whose file its rules refuse is reported as
`skip-at-production`. That is not an error. Dev publishes it; production leaves that prompt exactly
as it is until a release that can read it is published.

The lint prints file names, line numbers and rules, never a prompt's words. Run it yourself with
`python scripts/prompt_lint.py`.

## How it is published

- **Dev**: every merge to `dev` that touches this folder publishes it to dev's bucket.
- **Production**: every published release publishes it (under the `production` environment), so the
  prompts and the code never drift; between releases the owner can publish `dev`'s tree to
  production by hand (the workflow's *Run workflow* button on `dev`, environment `production`,
  under the `production-prompts` environment). Either needs the owner's approval.

Each publish is a release named `<env>-<yyyymmddThhmm>-<short commit>`. The publisher
([`scripts/publish_prompts.py`](../scripts/publish_prompts.py)) records every file's hash as
pending, uploads it once, approves the release's hashes, syncs `latest/` (adding before removing),
retires the previous release's hashes and only then records the release. A publish that stops
part-way leaves the previous release serving, and a Lambda that cannot read a file serves the code's
copy. Nothing is published until the `PROMPT_AS_ASSET_ENABLED` variable is `true` at repository
level: a publish job decides whether to run before it enters an environment, so it never sees a
value set only on one.

## How to roll back

- **The usual way:** `git revert` the change and merge it. The next publish serves the old file again
  (a reverted version keeps its number: the next new text needs a higher one).
- **Faster, without a merge:** run the prompts workflow by hand on `dev` with `release_id` set to an
  earlier release of that deployment (`admin_cli prompts releases` lists them). That release's files
  are served again and the release is **pinned**: every later publish, merges and releases included,
  publishes the pinned release again until a run with `clear_pin` publishes the tree and clears it.
- **One version, at once:** `admin_cli prompts pause <prompt_id> <version>` stops serving it within
  ten minutes while the rest of its split keeps serving; `admin_cli prompts set --assets off` turns
  every asset off without a publish.
