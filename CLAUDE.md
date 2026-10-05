# CLAUDE.md

The project rules are in [.github/copilot-instructions.md](.github/copilot-instructions.md);
they apply to Claude sessions too. One addition, because PRs keep going red on lint alone:

## Lint before you commit Python

Before committing any change under `lambdas/` or `scripts/`, run from the repo root:

```sh
ruff check --fix lambdas/ scripts/
```

- Use the ruff version pinned in `lambdas/requirements-dev.txt` (`ruff --version` must match it).
  In a cloud session the SessionStart hook (`.claude/hooks/session-start.sh`) puts that version
  first on PATH. A newer ruff reports and fixes different things than CI does.
- Never pass `--unsafe-fixes`. Whatever is left after `--fix` (e.g. E501, F841) needs a hand edit.
- Don't run `ruff format` repo-wide: the tree has never been formatted, and it would rewrite
  dozens of unrelated files.
- Re-run it after merging `dev` into your branch; merges are where import order (I001) breaks.
