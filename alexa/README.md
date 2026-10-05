# The Alexa+ add-on

How to put the operator's assistant on Alexa+: one add-on per environment, each linked to that
environment's own sign-in and MCP server. This is a one-time bootstrap, run by a person on their
own machine. Nothing here runs in CI.

The design is in `docs/enhancements/alexa-plus.md`, section 4.6. The infrastructure it relies on is
in `infra/modules/ops-assistant/alexa.tf`.

## What it is

Amazon's Alexa+ MCP Toolkit (the `alexa-ai` CLI) publishes an *add-on*: a manifest that points
Alexa+ at an HTTPS MCP endpoint. Ours is the ops MCP server, `POST <stage URL>/mcp`. Every call
needs a Cognito access token with the scope `bloggerbear-ops/read`; Alexa gets one through
*account linking*: OAuth 2.1, authorization code with PKCE (S256), and a client registered by hand
(`alexa-ai configure-account-linking`), not dynamically.

Things to know before you start:

- **US only, and gated.** The toolkit needs an Amazon developer account in the US marketplace, and
  Amazon admits partners to it. Without access, none of this works.
- **MCP spec `2025-11-25`.** That is the version the toolkit speaks, and what our server answers.
- **No linking, no deploy.** `alexa-ai deploy` fails until account linking is configured. Linking
  needs the Alexa app client, and that client needs Alexa's redirect URLs. So the order is fixed:
  redirect URLs first, then the client, then the rest of linking, then deploy (step 3).
- **One add-on per environment.** Dev's add-on points at dev's MCP URL and signs in to dev's user
  pool; production's at production's. Never link dev's add-on to production, or the reverse.
- **`assistant_access` must be `open`.** Alexa calls from Amazon's addresses, not yours, so
  `allowlist` refuses every call Alexa makes (step 4).

## Files here

| File | What it is |
|---|---|
| `addon-package/addon.template.json` | The values our add-on needs, with `{{PLACEHOLDERS}}`. Not Amazon's schema: see below. |
| `addon-package/addon.json` | Written by the helper script if you point it here. Generated per environment, so gitignored. |

**About the manifest.** `alexa-ai` reads the add-on manifest from `addon-package/addon.json`
(`manifestVersion` `"1.0"`, the store listing per locale, and the HTTPS MCP endpoint). Amazon's
exact schema is not public to us, so the template holds only those three things, under key names
of our own except `manifestVersion`. Let `alexa-ai` generate and validate the real file, and copy
the values in from ours where its key names differ. Do not add fields you have not seen the CLI
ask for: an unknown key may be rejected.

The add-on's name is `BloggerBear Ops (dev)` in dev and `BloggerBear Ops` in production. It never
contains the word "Alexa": Amazon allows that word only descriptively ("works with Alexa"), never
in a skill's or add-on's own name. The helper's tests hold this.

## The helper script

`scripts/alexa_addon_values.py` reads one environment's Terraform outputs and prints every value the
steps below ask for, after checking that they are https and all belong to that environment:

```bash
python scripts/alexa_addon_values.py dev
python scripts/alexa_addon_values.py dev --write-manifest ~/bloggerbear-addon-dev/addon-package/addon.json
python scripts/alexa_addon_values.py dev --show-secret     # only while you type it in
```

It needs Terraform and credentials that can read that environment's state. It never prints the
client secret unless you pass `--show-secret`, and then warns on stderr. It will not replace an
existing manifest unless you pass `--force`.

## The bootstrap, per environment

Do all of this once for **dev**, then again, separately, for **production**. Below, `<env>` is
`dev` or `production`; every command names it, so you cannot mix them up by accident. Use a
separate `alexa-ai` project directory per environment (below, `~/bloggerbear-addon-<env>`), so
dev's add-on and production's never share a manifest or linking settings.

**Production needs a release first.** Production's root has had the `ops_assistant` module and its
`ops_*` outputs since #206, but they exist only once a release has applied it. Until then the
helper stops with "Terraform has no ops_mcp_url output", and that is correct: there is nothing for
a production add-on to point at yet.

### 1. Read the environment's outputs

```bash
python scripts/alexa_addon_values.py <env>
```

or by hand:

```bash
terraform -chdir=infra/environments/<env> output ops_mcp_url
terraform -chdir=infra/environments/<env> output ops_oauth_protected_resource_url
terraform -chdir=infra/environments/<env> output ops_oauth_authorize_url
terraform -chdir=infra/environments/<env> output ops_oauth_token_url
```

The MCP URL ends `/<env>/mcp`: the stage is named after the environment. If it does not, you are
reading the wrong state; stop.

You can check the discovery documents now. They are public and hold nothing secret:

```bash
curl -s "$(terraform -chdir=infra/environments/<env> output -raw ops_oauth_protected_resource_url)"
```

It names the stage URL as the authorization server. That server's metadata, at
`<stage URL>/.well-known/oauth-authorization-server`, lists `S256` in
`code_challenge_methods_supported`.

### 2. Install the CLI and sign in to Amazon

Install the Alexa AI CLI as Amazon's toolkit documentation says, then:

```bash
alexa-ai configure
```

Sign in with the Amazon developer account that has toolkit access, in the US marketplace.

### 3. Account linking, in two halves

Account linking needs a client id and secret, and Terraform only makes the client once it knows
Alexa's redirect URLs. So:

1. Run `alexa-ai configure-account-linking` and give it:
   - **Authorization URL:** `ops_oauth_authorize_url`
   - **Token URL:** `ops_oauth_token_url`
   - **Scope:** `bloggerbear-ops/read` (only this one; not `openid`)

   It prints Alexa's redirect URLs. If it insists on a client id and secret before it shows them,
   stop there and come back once the next part is done.
2. Put those URLs into this environment's `ops_alexa_redirect_uris`, in its tracked
   `infra/environments/<env>/terraform.tfvars`, and apply:

   ```hcl
   ops_alexa_redirect_uris = ["https://...", "https://..."]
   ```

   ```bash
   terraform -chdir=infra/environments/<env> apply
   ```

   Commit that line, through a pull request like any other change. The deploy workflows apply
   from the repository and pass no `TF_VAR_ops_alexa_redirect_uris`, so URLs that live only in
   your shell (`export TF_VAR_ops_alexa_redirect_uris='[...]'`) are fine for a quick local trial,
   but the next CI apply sets the list back to empty and deletes the Alexa client. The URLs are
   Amazon's addresses, not credentials. They may carry an id of your Amazon developer account;
   that is not a secret either, but check the PII guard (`scripts/pii_denylist_check.py`) agrees.

   Every URL must be `https://`; the variable's validation refuses anything else. The apply makes
   the Alexa app client (`aws_cognito_user_pool_client.alexa`), in this environment's pool only.
3. Read the client's id and secret, on your own machine:

   ```bash
   terraform -chdir=infra/environments/<env> output -raw ops_alexa_client_id
   terraform -chdir=infra/environments/<env> output -raw ops_alexa_client_secret
   ```

   (or `python scripts/alexa_addon_values.py <env> --show-secret`). Type them into
   `alexa-ai configure-account-linking` and finish it. Then clear your terminal's scrollback.

### 4. Check that `assistant_access` is open

```bash
python scripts/admin_cli.py pipeline-config get
```

`assistant_access` must be `open`, or absent (which means open). If it is `allowlist`, every call
from Alexa is refused, because Alexa calls from Amazon's addresses. To open it:

```bash
python scripts/admin_cli.py pipeline-config set --assistant-access open
```

It applies from the next request; no deploy. Point the admin CLI at the same environment
(`BLOGGERBEAR_ADMIN_API_URL`) as the add-on you are setting up. Open does not mean unauthenticated:
every call still needs a token from this environment's pool with the read scope.

### 5. Write the manifest, deploy, and test

```bash
python scripts/alexa_addon_values.py <env> --write-manifest ~/bloggerbear-addon-<env>/addon-package/addon.json
```

That is this environment's own `alexa-ai` project directory (any path will do, one per
environment). Check its values against the manifest `alexa-ai` expects (see "About the
manifest"), then deploy from that directory:

```bash
cd ~/bloggerbear-addon-<env>
alexa-ai deploy
```

In the simulator, or on a device on the same Amazon account:

1. Link the account. Alexa opens Cognito's hosted sign-in page for this environment; sign in with
   this environment's Cognito user. In production the pool requires MFA, so have your
   authenticator ready.
2. Say "ask BloggerBear to start a briefing". Alexa calls `start_briefing`.
3. A little later, say "ask BloggerBear for the briefing". Alexa calls `latest_briefing`.

`start_briefing` and `latest_briefing` (the asynchronous briefing the Strands agent writes) come in
a separate change; until then, try the read tools: "ask BloggerBear how the pipeline is doing"
(`pipeline_health`), "...what is in the admin inbox" (`admin_inbox`), "...about alarms"
(`alarms`).

**Reading logs.** The add-on lists the same tools as the page's agent, so it can read logs too,
read-only, with the same PII sweep (addresses only as `123.XXX.XXX.34`, said as "an address ending
in .34"):

| Say | Tool |
|---|---|
| "ask BloggerBear whether there were any errors in the logs" | `log_review` |
| "...to look at the logs for the crypto topic" | `log_review` with `topic` |
| "...about API failures", "...about 400s on the public API" | `api_errors` |
| "...to watch research tick" | `watch` (function), then `watch_list` in the next briefing |
| "...to watch candidate ideas" | `watch` (table) |
| "...what it is watching" | `watch_list`: still happening, or calmed down |
| "...what's happening with the firewall" (production only) | `firewall_review` |

A log read waits for Logs Insights (up to 15 seconds), which can be close to Alexa's own time limit.
If Alexa gives up, ask for a briefing instead ("ask BloggerBear to start a briefing"): the agent
reads the logs in the background, and "ask BloggerBear for the briefing" reads its answer back.

### 6. If sign-in fails with `invalid_request`

Unsettled: Alexa may send the RFC 8707 `resource` parameter on the authorization request, and we do
not yet know whether Cognito's hosted page accepts it. If linking fails on Cognito's page with
`invalid_request`:

- write what you saw in `docs/friction.md`, section 10 (Alexa+ and MCP), in its usual form;
- the fallback is a small authorize redirector Lambda: it takes Alexa's request, drops `resource`,
  and redirects to Cognito's authorization endpoint. `ops_oauth_authorize_url` would then point at
  the redirector. That is a separate change; do not hand-edit the metadata documents.

## Unlinking and revoking

From gentlest to most thorough:

1. **Unlink in the Alexa app** (the add-on's settings, "Disable" or "Unlink"). Alexa forgets its
   tokens. A refresh token it already held stays valid until it expires (30 days by default,
   `alexa_refresh_token_days`), so for a lost or shared device do the next step too.
2. **Sign the user out everywhere.** This revokes every refresh token the user holds, Alexa's
   included, in this environment's pool:

   ```bash
   aws cognito-idp admin-user-global-sign-out \
     --user-pool-id "$(terraform -chdir=infra/environments/<env> output -raw ops_user_pool_id)" \
     --username <the user>
   ```

   Access tokens already issued still work until they expire, within the hour.
3. **Remove the Alexa client.** Set `ops_alexa_redirect_uris` back to `[]` (remove the line from
   `terraform.tfvars`) and apply, or merge that change and let CI apply it. Terraform deletes the
   Alexa app client. Its refresh tokens can no longer be used, and nothing can link to this
   environment again until you repeat step 3 of the bootstrap, which makes a new client with a new
   id and secret. As in step 2, an access token already issued may work until it expires.

To stop all assistant traffic at once, from Alexa and everyone else:
`python scripts/admin_cli.py pipeline-config set --assistant-access off`.

## Security

- **Dev and production are separate.** Each has its own add-on, MCP URL, user pool and Alexa
  client. A dev token is not valid on production's API, and the helper refuses values from the
  wrong environment. Never put production's redirect URLs into dev's variables, or the reverse.
- **Production requires MFA** at sign-in, and so at link time. Dev's pool has it optional, for the
  judges' shared login; treat dev's data as visible to anyone with that login.
- **Read-only.** The scope is `bloggerbear-ops/read` and the MCP server's role can only read. A
  linked device can hear about the pipeline; it cannot change it.
- **A device in a room speaks its answers.** Anyone near a linked Echo hears what the assistant
  says, and anyone who can talk to it can ask. Link only devices in places you control, and unlink
  (above) a device that leaves your hands.
- **Never commit the client secret.** Not in `terraform.tfvars` (that file is tracked), not in
  `addon.json`, not in an issue, a chat or a pull request. It lives in Terraform state and in
  Amazon's linking settings, nowhere else. If it leaks, remove the client and make a new one
  (unlinking, step 3).
- **The redirect URLs are not secret**, but they decide where Cognito sends a sign-in code. Only
  ever put in the URLs `alexa-ai configure-account-linking` printed.
