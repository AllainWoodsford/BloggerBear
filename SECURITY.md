# Security policy

## Reporting a vulnerability

Please report vulnerabilities **privately**, through GitHub's
[private vulnerability reporting](https://github.com/AllainWoodsford/BloggerBear/security/advisories/new)
(the repository's **Security** tab → **Report a vulnerability**). Do not open a public issue.

Include what you found, where (file and line, or URL), and how to reproduce it. This is a
one-person project: expect an acknowledgement within a week, and a fix or a decision as soon as
practical after that.

## Scope

- The code in this repository: the Lambdas, the Terraform, the GitHub Actions workflows, the
  frontend and the admin CLI.
- The live site, **bloggerbear.com**, and its public API.

## Testing the live site

Testing the live site is welcome within reason: no denial of service, no load testing, no
automated scanning at volume, and no attempts to read or change other people's data. The public
API is rate-limited by WAF, and blocked requests are logged as security incidents
(see `lambdas/common/security_events.py`), so a noisy test will be noticed.

## Supported versions

Only the latest release (what runs on bloggerbear.com) and the `dev` branch receive fixes.
