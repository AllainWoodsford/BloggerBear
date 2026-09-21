"""Tests for scripts/domain_check.py: the read-only "is the domain wired up yet?" checklist.

No test touches the network: DNS lookups and web requests are handed in as fakes.
"""

from __future__ import annotations

import io
import ssl

import pytest

import domain_check as dc

DOMAIN = "bloggerbear.com"
ROUTE53 = ["ns-1.awsdns-01.org", "ns-2.awsdns-02.net", "ns-3.awsdns-03.co.uk", "ns-4.awsdns-04.com"]
GODADDY = ["ns19.domaincontrol.com", "ns20.domaincontrol.com"]
CERT = {
    "subjectAltName": (("DNS", DOMAIN), ("DNS", f"www.{DOMAIN}")),
    "notAfter": "Jan  1 00:00:00 2099 GMT",
    "issuer": ((("organizationName", "Amazon"),),),
}
GOOD_HEADERS = {
    "strict-transport-security": "max-age=63072000",
    "content-security-policy": "default-src 'self'",
    "x-content-type-options": "nosniff",
}


def dns(**records):
    """A fake lookup: dns(NS=[...], A=[...]) answers by record type for any name."""

    def lookup(name, record_type):
        answer = records.get(f"{name}/{record_type}", records.get(record_type, []))
        if isinstance(answer, Exception):
            raise answer
        return list(answer)

    return lookup


def reply(status=200, headers=None, body="", cert=None):
    return {"status": status, "headers": headers or {}, "body": body, "cert": cert}


def site(overrides=None):
    """A fake fetch that behaves like the finished production site, with `overrides` keyed by
    (host, scheme, path) for what to change (a reply dict, or an exception to raise)."""
    replies = {
        (DOMAIN, "https", "/"): reply(200, GOOD_HEADERS, "<html>", CERT),
        (DOMAIN, "https", "/config.js"): reply(200, {}, f'window.SITE_URL = "https://{DOMAIN}";', CERT),
        (DOMAIN, "http", "/"): reply(301, {"location": f"https://{DOMAIN}/"}),
        (f"www.{DOMAIN}", "https", "/?x=1"): reply(301, {"location": f"https://{DOMAIN}/?x=1"}, "", CERT),
        **(overrides or {}),
    }

    def fetch(host, scheme="https", path="/", method="GET"):
        found = replies[(host, scheme, path)]
        if isinstance(found, Exception):
            raise found
        return found

    return fetch


def by_title(checks):
    return {c.title: c for c in checks}


# --- who runs the domain's DNS ---------------------------------------------------------------------


def test_route_53_name_servers_are_recognised_without_being_told_which():
    check = dc.check_nameservers(DOMAIN, None, dns(NS=ROUTE53))

    assert check.status == dc.OK and "Route 53" in check.detail


def test_godaddys_own_name_servers_mean_it_has_not_been_handed_over_yet():
    check = dc.check_nameservers(DOMAIN, None, dns(NS=GODADDY))

    assert check.status == dc.WAIT and "GoDaddy" in check.detail
    assert "runsheet" in check.hint


def test_the_exact_four_expected_name_servers_pass_whatever_the_case_or_trailing_dots():
    published = [n.upper() + "." for n in ROUTE53]

    assert dc.check_nameservers(DOMAIN, ROUTE53, dns(NS=published)).status == dc.OK


def test_expecting_four_but_finding_godaddys_says_how_to_change_them():
    check = dc.check_nameservers(DOMAIN, ROUTE53, dns(NS=GODADDY))

    assert check.status == dc.WAIT
    assert "Enter my own nameservers" in check.hint and "hosted_zone_name_servers" in check.hint


def test_the_wrong_route_53_zone_is_a_failure_not_a_wait():
    other = ["ns-9.awsdns-09.org", "ns-8.awsdns-08.net", "ns-7.awsdns-07.co.uk", "ns-6.awsdns-06.com"]

    check = dc.check_nameservers(DOMAIN, ROUTE53, dns(NS=other))

    assert check.status == dc.FAIL and "expected" in check.detail


def test_name_servers_that_are_neither_are_a_failure():
    assert dc.check_nameservers(DOMAIN, None, dns(NS=["ns1.someone-else.net"])).status == dc.FAIL


def test_a_mix_of_old_and_new_name_servers_is_not_mistaken_for_done():
    assert dc.check_nameservers(DOMAIN, None, dns(NS=ROUTE53[:2] + GODADDY)).status == dc.FAIL


def test_no_answer_or_no_internet_is_a_wait_with_advice():
    assert dc.check_nameservers(DOMAIN, None, dns(NS=[])).status == dc.WAIT
    check = dc.check_nameservers(DOMAIN, None, dns(NS=OSError("network is unreachable")))
    assert check.status == dc.WAIT and "internet" in check.hint


# --- the whole checklist -----------------------------------------------------------------------------


def test_until_route_53_runs_the_domain_nothing_else_is_judged():
    """Today the domain serves a GoDaddy page: checking it would report nonsense about headers."""

    def never(*_a, **_k):
        raise AssertionError("the site must not be fetched before DNS is handed over")

    checks = dc.run_checks(DOMAIN, None, dns(NS=GODADDY, A=["1.2.3.4"]), never)

    assert checks[0].status == dc.WAIT
    assert all(c.status == dc.SKIP for c in checks[1:]) and len(checks) == 9


def test_a_finished_site_passes_every_check():
    lookup = dns(NS=ROUTE53, A=["13.1.2.3"], AAAA=["2600::1"])

    checks = dc.run_checks(DOMAIN, ROUTE53, lookup, site())

    assert [c.status for c in checks] == [dc.OK] * 9, [
        (c.title, c.detail) for c in checks if c.status != dc.OK
    ]
    assert dc.main([DOMAIN, "--expect-ns", *ROUTE53], lookup, site(), io.StringIO()) == 0


def test_dns_handed_over_but_no_records_yet_waits_for_the_deploy():
    checks = dc.run_checks(DOMAIN, None, dns(NS=ROUTE53), site())

    assert by_title(checks)[DOMAIN + " resolves"].status == dc.WAIT
    assert all(c.status == dc.SKIP for c in checks[3:])


def test_a_certificate_that_does_not_list_the_domain_fails():
    wrong = {**CERT, "subjectAltName": (("DNS", "example.org"),)}
    fetch = site({(DOMAIN, "https", "/"): reply(200, GOOD_HEADERS, "", wrong)})

    check, _ = dc.check_certificate(DOMAIN, fetch)

    assert check.status == dc.FAIL and "does not list" in check.hint


def test_a_bad_certificate_is_reported_in_words():
    err = ssl.SSLCertVerificationError(1, "certificate verify failed")
    err.reason = "CERTIFICATE_VERIFY_FAILED"

    check, got = dc.check_certificate(DOMAIN, site({(DOMAIN, "https", "/"): err}))

    assert check.status == dc.FAIL and got is None and "CERTIFICATE_VERIFY_FAILED" in check.detail


def test_https_that_will_not_connect_yet_is_a_wait_and_holds_back_the_rest():
    lookup = dns(NS=ROUTE53, A=["13.1.2.3"])

    checks = dc.run_checks(
        DOMAIN, None, lookup, site({(DOMAIN, "https", "/"): ConnectionRefusedError("refused")})
    )

    titles = by_title(checks)
    assert titles["HTTPS certificate"].status == dc.WAIT
    for held_back in ("The site answers", "Security headers", "Site knows its own address"):
        assert titles[held_back].status == dc.SKIP


def test_the_certificates_expiry_is_shown():
    check, _ = dc.check_certificate(DOMAIN, site())

    assert "days left" in check.detail and "Amazon" in check.detail


def test_a_site_that_is_up_but_empty_is_called_out():
    assert dc.check_site_answers(reply(403)).status == dc.FAIL
    assert dc.check_site_answers(reply(200)).status == dc.OK


def test_missing_security_headers_are_named():
    check = dc.check_headers(reply(200, {"strict-transport-security": "x"}))

    assert check.status == dc.FAIL
    assert "content-security-policy" in check.detail and "x-content-type-options" in check.detail


def test_config_js_must_carry_the_real_address_or_rss_links_are_wrong():
    wrong = reply(200, {}, 'window.SITE_URL = "https://abc.cloudfront.net";')

    check = dc.check_site_url(DOMAIN, site({(DOMAIN, "https", "/config.js"): wrong}))

    assert check.status == dc.FAIL and "abc.cloudfront.net" in check.detail


def test_plain_http_must_go_to_https_on_the_same_host():
    no_redirect = reply(200)
    elsewhere = reply(301, {"location": "https://evil.example/"})

    assert dc.check_http_redirect(DOMAIN, site({(DOMAIN, "http", "/"): no_redirect})).status == dc.FAIL
    assert dc.check_http_redirect(DOMAIN, site({(DOMAIN, "http", "/"): elsewhere})).status == dc.FAIL
    assert dc.check_http_redirect(DOMAIN, site()).status == dc.OK


def test_www_must_301_to_the_bare_domain_keeping_the_query():
    wrong = reply(302, {"location": f"https://{DOMAIN}/"})

    check = dc.check_www_redirect(DOMAIN, site({(f"www.{DOMAIN}", "https", "/?x=1"): wrong}))

    assert check.status == dc.FAIL and "redirect_www" in check.hint
    assert dc.check_www_redirect(DOMAIN, site()).status == dc.OK


def test_www_with_no_certificate_for_it_is_a_failure_with_the_fix():
    err = ssl.SSLCertVerificationError(1, "no match")
    err.reason = "hostname mismatch"

    check = dc.check_www_redirect(DOMAIN, site({(f"www.{DOMAIN}", "https", "/?x=1"): err}))

    assert check.status == dc.FAIL and "redirect_www = true" in check.hint


# --- the command line ------------------------------------------------------------------------------


def test_the_report_says_what_to_do_and_summarises():
    text = dc.render(DOMAIN, dc.run_checks(DOMAIN, None, dns(NS=GODADDY), site()))

    assert "[WAIT] Name servers" in text and "-> " in text
    assert "0 ok, 1 waiting, 0 wrong, 8 skipped." in text
    assert "Nothing is wrong so far" in text


def test_a_failure_is_shouted_about():
    text = dc.render(DOMAIN, [dc.Check(dc.FAIL, "Thing", "broke", "fix it")])

    assert "[FAIL] Thing: broke" in text and "-> fix it" in text and "Something is wrong" in text


@pytest.mark.parametrize(
    "bad", ["https://bloggerbear.com", "www.bloggerbear.com.", "BLOGGER BEAR", "localhost"]
)
def test_a_domain_that_is_not_a_bare_domain_is_refused_before_any_lookup(bad):
    out = io.StringIO()

    code = dc.main([bad], dns(), site(), out)

    assert code == 2 and "bare domain" in out.getvalue()


def test_the_domain_defaults_to_the_one_in_production_tfvars():
    assert dc.domain_from_tfvars() == "bloggerbear.com"
    out = io.StringIO()

    dc.main([], dns(NS=GODADDY), site(), out)

    assert "Checking bloggerbear.com" in out.getvalue()


def test_tfvars_with_no_domain_gives_none(tmp_path):
    empty = tmp_path / "t.tfvars"
    empty.write_text('domain_name    = ""\nhosted_zone_id = ""\n', encoding="utf-8")

    assert dc.domain_from_tfvars(empty) is None
    assert dc.domain_from_tfvars(tmp_path / "missing.tfvars") is None


def test_exit_status_is_zero_only_when_everything_is_ok():
    assert dc.main([DOMAIN], dns(NS=GODADDY), site(), io.StringIO()) == 1
