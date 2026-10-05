# Contributing

BloggerBear is a one-person portfolio project, open so people can read, learn from and reuse it.
**Pull requests and issues are limited to collaborators.** Everyone else is welcome in
[Discussions](https://github.com/AllainWoodsford/BloggerBear/discussions):

- **Q&A:** questions about how it works, or running your own copy.
- **General:** bugs and mistakes you've spotted, on the code or on bloggerbear.com.
- **Ideas:** suggestions.

Security problems go through [SECURITY.md](SECURITY.md) (private vulnerability reporting), never a
public discussion.

## If you fork it

The code is under the [Apache License 2.0](LICENSE). To run it yourself:

- **Deploying:** the "Deploying this" section of the [README](README.md) is the run sheet, from the
  one-time bootstrap to the first production release.
- **Running the checks locally:** the README's "Local development" section has the same commands
  CI runs: pytest (moto-mocked, no AWS credentials needed), ruff, bandit and `terraform validate`.
- **Lint before you push:** `ruff check --fix lambdas/ scripts/` with the ruff pinned in
  `lambdas/requirements-dev.txt` applies the safe fixes CI would otherwise fail on.
- **How it's built and why:** [docs/project-plan.md](docs/project-plan.md) covers the
  architecture and its rules; [docs/PROGRESS.md](docs/PROGRESS.md) is the phase-by-phase history.
