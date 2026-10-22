# Admin CLI quickstart

Get `admin_cli.py` talking to your deployed environment, and know where to look when something is
missing. The full reference is [README.md](README.md) in this folder.

## 1. Set it up (once per terminal)

First, once per Python environment (or venv), install what the CLI needs, from the repo root:
`pip install -r scripts/requirements.txt`. Without it every command stops with
`ModuleNotFoundError` (for example `No module named 'requests'`), even `--help` and
`approve --mock`.

Then set these in each terminal. Use the region your copy is deployed in; `ap-southeast-2` is the
original deployment's, and the default.

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
URL, so never reuse one for the other. To have them in every new Git Bash window, put them in
`~/.bashrc`, but write the URL out: `export BLOGGERBEAR_ADMIN_API_URL=<the URL>`
(`echo $BLOGGERBEAR_ADMIN_API_URL` prints it once the line above has run). A new window usually
opens in your home folder, where `-chdir=infra/environments/dev` finds nothing, so the URL would be
left empty. The `AWS_DEFAULT_REGION` line can go in as it is. To keep the terraform call instead,
give `-chdir` the full path to `infra/environments/dev`. It then runs every time a window opens,
and needs your AWS credentials.

The CLI signs each request with your AWS credentials, so those need to be set up too (`aws configure`, or
`export AWS_PROFILE=<name>` for a named profile). Check with `aws sts get-caller-identity`. Terraform
needs the same credentials to read the state.

Instead of exporting, you can pass `--api-url` and `--region` on every command. They go right
after `admin_cli.py`, before the command name:
`python scripts/admin_cli.py --region ap-southeast-2 --api-url <url> topics list`. Anywhere after
the command name (`topics list --region ...`), the CLI stops with a usage error.

## 2. Try it

```bash
python scripts/admin_cli.py inbox      # what is waiting for you, at a glance
python scripts/admin_cli.py approve    # go through it, one keystroke each
python scripts/admin_cli.py topics list
python scripts/admin_cli.py topics schedule    # each topic's slot, state and the minutes it runs at
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

- **403 from the API:** your IP is not on the admin allowlist, or your AWS credentials are wrong
  (expired, for another account, or without permission to call the API). With no credentials at
  all, the CLI stops before sending anything and prints "Error: No AWS credentials found". Check
  `aws sts get-caller-identity` first. A new deploy lets nobody in until you add your IP:
  `python scripts/admin_allowlist.py --prefix <your prefix> set --env dev --my-ip` (use
  `--env production` for prod). The script reads neither its prefix nor its region from your
  environment: leave out `--prefix` only if your prefix is the default `bloggerbear`, and add
  `--region <your region>` if your region is not `ap-southeast-2`. Both go before `set`. Run it
  again whenever your IP changes; you need no Terraform apply.
  `python scripts/admin_allowlist.py --prefix <your prefix> show --env dev` shows whether this
  machine is listed.
- **"Admin API URL not configured" or "AWS region not configured":** step 1 was not done in this
  terminal. Each new terminal needs it again unless you put it in `~/.bashrc`.
- **`terraform output` fails:** on a fresh checkout, run `init` once for that environment first
  (Terraform says "Backend initialization required"). The backend block names the original
  deployment's state bucket, so in a copy of the project give `init` the bucket CI uses (your
  `TF_STATE_BUCKET_DEV`, or `TF_STATE_BUCKET_PROD` for `production`), and its region if that is not
  `ap-southeast-2`:
  `terraform -chdir=infra/environments/dev init -backend-config="bucket=<your state bucket>" -backend-config="region=<its region>"`.
  See [../docs/deployment-runsheet.md](../docs/deployment-runsheet.md#4-first-deploy-dev), section
  4. Terraform also needs your AWS credentials to read the remote state. Check
  `aws sts get-caller-identity`, and that you ran it from the repo root (or pass the full path to
  `-chdir`).
