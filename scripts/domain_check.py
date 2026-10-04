#!/usr/bin/env python3
"""Is the custom domain wired up yet? A read-only checklist you can run any time.

    python scripts/domain_check.py                      # the domain in production/terraform.tfvars
    python scripts/domain_check.py bloggerbear.com
    python scripts/domain_check.py --expect-ns ns-1.awsdns-01.org ns-2.awsdns-02.net ...

It only looks: DNS lookups (over HTTPS to dns.google, so it needs nothing installed and sees what the
public sees, not your machine's cache) and ordinary web requests to the domain. It changes nothing and
needs no AWS credentials. Each line says what it found and, when something is not right yet, what to do.

    [ OK ]   done
    [WAIT]   not there yet (DNS spreading, the site not deployed yet): re-run in a few minutes
    [FAIL]   wrong: the hint says what to change
    [SKIP]   not checked, because something before it is not ready

Exit status is 0 only when everything is OK.
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import socket
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TFVARS = ROOT / "infra" / "environments" / "production" / "terraform.tfvars"
DOMAIN_RE = re.compile(r"^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")

OK, WAIT, FAIL, SKIP = "OK", "WAIT", "FAIL", "SKIP"
LABEL = {OK: "[ OK ]", WAIT: "[WAIT]", FAIL: "[FAIL]", SKIP: "[SKIP]"}

# Who runs a domain's DNS, from the name of its name servers.
ROUTE53 = re.compile(r"\.awsdns-\d+\.(org|net|com|co\.uk)\.?$", re.I)
GODADDY = re.compile(r"\.domaincontrol\.com\.?$", re.I)


class Check:
    def __init__(self, status: str, title: str, detail: str = "", hint: str = ""):
        self.status, self.title, self.detail, self.hint = status, title, detail, hint


# --- the two things that touch the network (replaced in tests) -----------------------------------------


def doh(name: str, record_type: str, timeout: float = 10.0) -> list[str]:
    """The answers for `name`/`record_type` as the public DNS sees it, via dns.google. [] if none."""
    url = "https://dns.google/resolve?" + urllib.parse.urlencode({"name": name, "type": record_type})
    request = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosec B310 - fixed https URL
        payload = json.loads(response.read().decode("utf-8"))
    wanted = {"NS": 2, "A": 1, "AAAA": 28, "CNAME": 5}[record_type]
    return [a["data"] for a in payload.get("Answer", []) if a.get("type") == wanted]


def web(
    host: str, scheme: str = "https", path: str = "/", method: str = "GET", timeout: float = 10.0
) -> dict:
    """One request, redirects NOT followed. Returns {status, headers, body, cert}; cert is the peer
    certificate (only for https). Raises OSError / ssl.SSLError when it cannot connect or verify."""
    cert = None
    if scheme == "https":
        context = ssl.create_default_context()
        raw = socket.create_connection((host, 443), timeout=timeout)
        sock = context.wrap_socket(raw, server_hostname=host)
        cert = sock.getpeercert()
        conn = http.client.HTTPSConnection(host, timeout=timeout)
        conn.sock = sock
    else:
        conn = http.client.HTTPConnection(host, timeout=timeout)
    try:
        conn.request(method, path, headers={"Host": host, "User-Agent": "bloggerbear-domain-check"})
        response = conn.getresponse()
        body = response.read(200_000).decode("utf-8", "replace") if method == "GET" else ""
        headers = {k.lower(): v for k, v in response.getheaders()}
        return {"status": response.status, "headers": headers, "body": body, "cert": cert}
    finally:
        conn.close()


# --- the checks ---------------------------------------------------------------------------------------


def _norm(name: str) -> str:
    return name.strip().lower().rstrip(".")


def check_nameservers(domain: str, expect: list[str] | None, lookup=doh) -> Check:
    try:
        servers = sorted({_norm(n) for n in lookup(domain, "NS")})
    except OSError as exc:
        return Check(
            WAIT,
            "Name servers",
            f"could not ask public DNS: {exc}",
            "Check your internet connection and run it again.",
        )
    if not servers:
        return Check(
            WAIT,
            "Name servers",
            "public DNS returned none for this domain",
            "It may be too new, or the registrar may not have published it yet. "
            "Check the domain is active at your registrar.",
        )
    shown = ", ".join(servers)
    if expect:
        wanted = sorted({_norm(n) for n in expect})
        if servers == wanted:
            return Check(OK, "Name servers", f"the four Route 53 name servers are in place: {shown}")
        if all(GODADDY.search(s) for s in servers):
            return Check(
                WAIT,
                "Name servers",
                f"still GoDaddy's own: {shown}",
                "At your registrar, switch the domain to custom name servers (in GoDaddy: Domain "
                "Settings > Nameservers > Change > Enter my own nameservers) and paste the four from "
                "`terraform -chdir=infra/bootstrap output hosted_zone_name_servers`. "
                "It can take minutes to a few hours.",
            )
        return Check(
            FAIL,
            "Name servers",
            f"found {shown}, expected {', '.join(wanted)}",
            "The name servers at the registrar do not match the Route 53 zone. "
            "Copy them again from the terraform output.",
        )
    if all(ROUTE53.search(s) for s in servers):
        return Check(OK, "Name servers", f"Route 53 runs this domain's DNS: {shown}")
    if all(GODADDY.search(s) for s in servers):
        return Check(
            WAIT,
            "Name servers",
            f"still GoDaddy's own: {shown}",
            "DNS has not been handed to Route 53 yet. See docs/production-runsheet.md step 3.",
        )
    return Check(
        FAIL,
        "Name servers",
        f"found {shown}",
        "These are neither GoDaddy's nor Route 53's. Check the registrar settings.",
    )


def check_addresses(name: str, lookup=doh) -> Check:
    try:
        found = lookup(name, "A") + lookup(name, "AAAA")
    except OSError as exc:
        return Check(WAIT, f"{name} resolves", f"could not ask public DNS: {exc}")
    if found:
        return Check(OK, f"{name} resolves", f"{len(found)} address(es), e.g. {found[0]}")
    return Check(
        WAIT,
        f"{name} resolves",
        "no address yet",
        "Expected once production has been deployed and the name servers have spread. "
        "Terraform creates the record.",
    )


def check_certificate(domain: str, fetch=web) -> tuple[Check, dict | None]:
    try:
        reply = fetch(domain, "https", "/", "GET")
    except ssl.SSLCertVerificationError as exc:
        return Check(
            FAIL,
            "HTTPS certificate",
            f"the certificate is not valid for {domain}: {exc.reason}",
            "Check the ACM certificate covers this name.",
        ), None
    except OSError as exc:
        return Check(
            WAIT,
            "HTTPS certificate",
            f"could not connect over HTTPS: {exc}",
            "Not deployed yet, or DNS has not reached here.",
        ), None
    cert = reply.get("cert") or {}
    names = [v.lower() for k, v in cert.get("subjectAltName", ()) if k == "DNS"]
    expires = cert.get("notAfter")
    days = None
    if expires:
        days = (
            datetime.strptime(expires, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC) - datetime.now(UTC)
        ).days
    issuer = dict(x[0] for x in cert.get("issuer", ()) if x).get("organizationName", "unknown issuer")
    detail = f"issued by {issuer}, covers {', '.join(names) or 'no names'}" + (
        f", {days} days left" if days is not None else ""
    )
    if domain.lower() not in names:
        return Check(FAIL, "HTTPS certificate", detail, f"The certificate does not list {domain}."), reply
    return Check(OK, "HTTPS certificate", detail), reply


def check_site_answers(reply: dict) -> Check:
    if reply["status"] == 200:
        return Check(OK, "The site answers", "HTTP 200 from the bare domain")
    return Check(
        FAIL,
        "The site answers",
        f"HTTP {reply['status']}",
        "The distribution is up but the site is not being served: has the frontend been uploaded?",
    )


def check_headers(reply: dict) -> Check:
    headers = reply["headers"]
    missing = [
        h
        for h in ("strict-transport-security", "content-security-policy", "x-content-type-options")
        if h not in headers
    ]
    if missing:
        return Check(
            FAIL,
            "Security headers",
            "missing: " + ", ".join(missing),
            "The CloudFront response headers policy is not being applied.",
        )
    return Check(OK, "Security headers", "HSTS, CSP and nosniff are all being sent")


def check_site_url(domain: str, fetch=web) -> Check:
    try:
        reply = fetch(domain, "https", "/config.js", "GET")
    except OSError as exc:
        return Check(WAIT, "Site knows its own address", f"could not fetch config.js: {exc}")
    expected = f'window.SITE_URL = "https://{domain}"'
    if reply["status"] == 200 and expected in reply["body"]:
        return Check(OK, "Site knows its own address", f"config.js says SITE_URL is https://{domain}")
    match = re.search(r'SITE_URL\s*=\s*"([^"]*)"', reply["body"])
    return Check(
        FAIL,
        "Site knows its own address",
        f"config.js says {match.group(1) if match else 'nothing'}",
        "RSS links and page addresses would point at the wrong host. "
        "Re-run the production apply with domain_name set.",
    )


def check_http_redirect(domain: str, fetch=web) -> Check:
    try:
        reply = fetch(domain, "http", "/", "GET")
    except OSError as exc:
        return Check(WAIT, "http:// goes to https://", f"could not connect over plain HTTP: {exc}")
    location = reply["headers"].get("location", "")
    if reply["status"] in (301, 302, 307, 308) and location.startswith(f"https://{domain}"):
        return Check(OK, "http:// goes to https://", f"HTTP {reply['status']} to {location}")
    return Check(
        FAIL,
        "http:// goes to https://",
        f"HTTP {reply['status']} {location}".strip(),
        "CloudFront should redirect all plain HTTP to HTTPS.",
    )


def check_www_redirect(domain: str, fetch=web) -> Check:
    www = f"www.{domain}"
    try:
        reply = fetch(www, "https", "/?x=1", "GET")
    except ssl.SSLCertVerificationError as exc:
        return Check(
            FAIL,
            f"{www} redirects",
            f"certificate problem: {exc.reason}",
            "The certificate should list www too (redirect_www = true).",
        )
    except OSError as exc:
        return Check(
            WAIT,
            f"{www} redirects",
            f"could not connect: {exc}",
            "Not deployed yet, or DNS has not reached here.",
        )
    location = reply["headers"].get("location", "")
    if reply["status"] == 301 and location == f"https://{domain}/?x=1":
        return Check(OK, f"{www} redirects", f"HTTP 301 to {location}")
    return Check(
        FAIL,
        f"{www} redirects",
        f"HTTP {reply['status']} {location}".strip(),
        f"Expected a 301 to https://{domain}/?x=1. Is redirect_www = true in production?",
    )


def run_checks(domain: str, expect_ns: list[str] | None = None, lookup=doh, fetch=web) -> list[Check]:
    later = [
        f"{domain} resolves",
        f"www.{domain} resolves",
        "HTTPS certificate",
        "The site answers",
        "Security headers",
        "Site knows its own address",
        "http:// goes to https://",
        f"www.{domain} redirects",
    ]
    checks = [check_nameservers(domain, expect_ns, lookup)]
    if checks[0].status != OK:
        # Until Route 53 runs the domain, whatever answers on it is not this project's (today: GoDaddy's own
        # page), so checking it would only mislead.
        return checks + [Check(SKIP, title, "waiting for the name servers") for title in later]
    checks.append(check_addresses(domain, lookup))
    checks.append(check_addresses(f"www.{domain}", lookup))
    if checks[1].status != OK:
        return checks + [Check(SKIP, title, f"waiting for {domain} to resolve") for title in later[2:]]
    certificate, reply = check_certificate(domain, fetch)
    checks.append(certificate)
    if reply is None:
        for title in later[3:6]:
            checks.append(Check(SKIP, title, "waiting for HTTPS"))
    else:
        checks.append(check_site_answers(reply))
        checks.append(check_headers(reply))
        checks.append(check_site_url(domain, fetch))
    checks.append(check_http_redirect(domain, fetch))
    checks.append(check_www_redirect(domain, fetch))
    return checks


def domain_from_tfvars(path: Path = TFVARS) -> str | None:
    if not path.is_file():
        return None
    match = re.search(r'^\s*domain_name\s*=\s*"([^"]*)"', path.read_text(encoding="utf-8"), re.M)
    return match.group(1) or None if match else None


def render(domain: str, checks: list[Check]) -> str:
    lines = [f"Checking {domain}", ""]
    for check in checks:
        lines.append(f"{LABEL[check.status]} {check.title}" + (f": {check.detail}" if check.detail else ""))
        if check.hint and check.status in (WAIT, FAIL):
            lines.append(f"       -> {check.hint}")
    counts = {s: sum(1 for c in checks if c.status == s) for s in (OK, WAIT, FAIL, SKIP)}
    lines += ["", f"{counts[OK]} ok, {counts[WAIT]} waiting, {counts[FAIL]} wrong, {counts[SKIP]} skipped."]
    if counts[FAIL]:
        lines.append("Something is wrong: see the -> lines above.")
    elif counts[WAIT] or counts[SKIP]:
        lines.append("Not there yet. Nothing is wrong so far; run it again in a few minutes.")
    else:
        lines.append("All good: the domain is live.")
    return "\n".join(lines)


def main(argv: list[str] | None = None, lookup=doh, fetch=web, out=sys.stdout) -> int:
    parser = argparse.ArgumentParser(description="Check whether the custom domain is wired up (read-only).")
    parser.add_argument(
        "domain", nargs="?", help="default: domain_name in infra/environments/production/terraform.tfvars"
    )
    parser.add_argument(
        "--expect-ns",
        nargs=4,
        metavar="NS",
        help="the four Route 53 name servers "
        "(terraform -chdir=infra/bootstrap output hosted_zone_name_servers)",
    )
    args = parser.parse_args(argv)
    domain = (args.domain or domain_from_tfvars() or "").strip().lower()
    if not DOMAIN_RE.match(domain):
        print("Give a bare domain such as bloggerbear.com (no https://, no www, no trailing dot).", file=out)
        return 2
    checks = run_checks(domain, args.expect_ns, lookup, fetch)
    print(render(domain, checks), file=out)
    return 0 if all(c.status == OK for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
