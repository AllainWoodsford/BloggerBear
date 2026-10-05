# Admin CLI quickstart

Get `admin_cli.py` talking to your deployed environment, and know where to look when something is
missing. The full reference is [README.md](README.md) in this folder.

## 1. Set it up (once per terminal)

Use the region your copy is deployed in; `ap-southeast-2` is the original deployment's, and the
default.

Git Bash:

```bash
export AWS_DEFAULT_REGION=ap-southeast-2
export BLOGGERBEAR_ADMIN_API_URL=$(terraform -chdir=infra/environments/dev output -raw admin_api_url)
```

PowerShell:

```powershell
$env:AWS_DEFAULT_REGION = "ap-southeast-2"
$env:BLOGGERBEAR_ADMIN_API_URL = terraform -chdir=infra/environments/dev output -raw admin_api_url
```

Run these from the repo root. Use `production` instead of `dev` for prod: each environment has its own
URL, so never reuse one for the other. To have them in every new Git Bash window, put the two `export`
lines in `~/.bashrc`.

The CLI signs each request with your AWS credentials, so those need to be set up too (`aws configure`, or
`export AWS_PROFILE=<name>` for a named profile). Check with `aws sts get-caller-identity`. Terraform
needs the same credentials to read the state.

Instead of exporting, you can pass `--api-url` and `--region` on every command.

## 2. Try it

```bash
python scripts/admin_cli.py inbox      # what is waiting for you, at a glance
python scripts/admin_cli.py approve    # go through it, one keystroke each
python scripts/admin_cli.py topics list
python scripts/admin_cli.py equipment list   # what the bear is wearing
```

`python scripts/admin_cli.py --help` lists every command, and `<command> --help` shows its options.
`python scripts/admin_cli.py approve --mock` practises the keys on made-up items with no AWS at all.

## 3. Where to find things yourself

| What you need | Where it is |
|---|---|
| **Admin API URL** | Run `terraform -chdir=infra/environments/dev output admin_api_url` from the repo root. Use `production` instead of `dev` for prod. `output` also lists `public_api_url` and the other outputs. Add `-raw` for just the value, with no quotes. |
| **How the CLI reads its settings** | [README.md](README.md), the "Configuration" section: `BLOGGERBEAR_ADMIN_API_URL` (or `--api-url`) and `AWS_REGION` / `AWS_DEFAULT_REGION` (or `--region`). |
| **Every command** | [README.md](README.md) "Usage", or `--help`. |
| **Approving things, and the gear** | [README.md](README.md): "What is waiting for you: `inbox` and `approve`" and "What the bear wears: `equipment`". |
| **Seeding a topic** | [../docs/deployment-runsheet.md](../docs/deployment-runsheet.md#5-after-the-first-deploy), step 5. |
| **Every setting, and where it goes** | [../docs/configuration.md](../docs/configuration.md). |
| **How it all fits together** | The top-level `README.md` ("Architecture"), then `docs/project-plan.md`. |

## Checking the custom domain

```bash
python scripts/domain_check.py      # is your domain wired up yet? Read-only, needs no AWS login
```

It looks the domain up in public DNS and asks the site a few questions, and says at each line what is wrong and
what to do about it. See [../docs/production-runsheet.md](../docs/production-runsheet.md) for the steps it checks.

## If a command fails

- **403 from the API:** your IP is not on the admin allowlist, or your AWS credentials are missing or
  wrong. Check `aws sts get-caller-identity` first.
- **"Admin API URL not configured" or "AWS region not configured":** step 1 was not done in this
  terminal. Each new terminal needs it again unless you put it in `~/.bashrc`.
- **`terraform output` fails:** it needs your AWS credentials to read the remote state. Check
  `aws sts get-caller-identity`, and that you ran it from the repo root (or pass the full path to
  `-chdir`).
