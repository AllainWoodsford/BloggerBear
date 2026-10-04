#!/bin/bash
# SessionStart hook (Claude Code cloud sessions only): put the ruff version CI lints with first on
# PATH, so `ruff check --fix lambdas/ scripts/` before a commit matches python-ci exactly. A newer
# ruff flags and fixes different things (0.15 reports UP042 on code 0.6.9 passes).
#
# Installs into its own venv, outside the repo, keyed by version: idempotent, and it doesn't
# touch whatever other ruff the container already has. Never fails the session -- on any problem
# it warns and exits 0.
set -uo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

project_dir="${CLAUDE_PROJECT_DIR:-$(pwd)}"
pin="$(grep -E '^ruff==' "${project_dir}/lambdas/requirements-dev.txt" 2>/dev/null | tr -d '[:space:]')"
if [ -z "${pin}" ]; then
  echo "session-start: no ruff== pin in lambdas/requirements-dev.txt; skipping ruff install." >&2
  exit 0
fi
version="${pin#ruff==}"
venv="${HOME}/.cache/bloggerbear/ruff-${version}"

if [ "$("${venv}/bin/ruff" --version 2>/dev/null)" != "ruff ${version}" ]; then
  if ! { python3 -m venv "${venv}" \
      && "${venv}/bin/python" -m pip install --quiet --disable-pip-version-check "${pin}"; } >/dev/null 2>&1; then
    echo "session-start: could not install ${pin}; lint with CI's version by hand." >&2
    exit 0
  fi
fi

if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PATH=\"${venv}/bin:\${PATH}\"" >> "${CLAUDE_ENV_FILE}"
fi
exit 0
