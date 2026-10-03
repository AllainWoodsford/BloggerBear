# Contributing

BloggerBear is a one-person portfolio project, open so people can read, learn from and reuse it.
**Pull requests aren't expected**, and may be closed without review. Issues pointing out bugs or
mistakes are welcome.

Security problems go through [SECURITY.md](SECURITY.md), never a public issue.

## If you fork it

The code is under the [Apache License 2.0](LICENSE). To run it yourself:

- **Deploying:** the "Deploying this" section of the [README](README.md) is the run sheet, from the
  one-time bootstrap to the first production release.
- **Running the checks locally:** the README's "Local development" section has the same commands
  CI runs: pytest (moto-mocked, no AWS credentials needed), ruff, bandit and `terraform validate`.
- **How it's built and why:** [docs/project-plan.md](docs/project-plan.md) covers the
  architecture and its rules; [docs/PROGRESS.md](docs/PROGRESS.md) is the phase-by-phase history.
