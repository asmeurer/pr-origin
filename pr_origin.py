#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "beautifulsoup4", "keyring", "rookiepy"]
# ///
"""
Report which client opened a GitHub pull request: a browser, the GitHub CLI,
an AI agent driving the GitHub CLI, or some other API client.

GitHub records the user agent and token type of every pull_request.create
event in the organization audit log. On non-Enterprise plans that log is only
reachable through the web UI, so this tool reads the same HTML fragment the
audit log page loads, authenticated with the session cookie of an
organization owner. The endpoint is undocumented and may change.

Usage:

    pr-origin --from-browser        # import your github.com session cookie
    pr-origin 30548 30566           # PRs in the default repo
    pr-origin https://github.com/sympy/live/pull/123
    pr-origin --export export-sympy-123.json 30548

--from-browser copies the github.com session cookie out of a browser that is
logged in to GitHub (Chrome by default) into the system keychain.
--set-cookie prompts for the value of the "user_session" cookie instead. With
--export, a JSON export downloaded from the audit log page is used and no
cookie is needed.
"""

import argparse
import datetime
import getpass
import json
import re
import subprocess
import sys
from urllib.parse import parse_qs, urlparse

import keyring
import requests
import rookiepy
from bs4 import BeautifulSoup

DEFAULT_REPO = "sympy/sympy"
KEYRING_SERVICE = "pr-origin"
KEYRING_USERNAME = "github.com session cookies"
SESSION_COOKIE = "user_session"
SAME_SITE_COOKIE = "__Host-user_session_same_site"
DEFAULT_BROWSER = "chrome"
BROWSERS = (
    "arc", "brave", "chrome", "chromium", "edge", "firefox", "librewolf",
    "opera", "opera_gx", "safari", "vivaldi",
)
GITHUB_CLI_OAUTH_APP_ID = "1157143"
# The GitHub Mobile apps use the API with tokens for their own OAuth apps.
# Their user agents look like "GitHub/1.275.0 (com.github.stormbreaker.prod;
# ...)" on iOS and "GitHub/1.269.0 (com.github.android; ...)" on Android.
GITHUB_MOBILE_OAUTH_APPS = {
    "1079828": "GitHub Mobile for iOS",
    "1119845": "GitHub Mobile for Android",
}
RESULTS_MARKER = "Filter audit logs"

PR_URL_RE = re.compile(r"^https://github\.com/([^/]+/[^/]+)/pull/(\d+)")
PR_SHORT_RE = re.compile(r"^([^/\s]+/[^/#\s]+)#(\d+)$")
AGENT_RE = re.compile(r"\bAgent/(\S+)")
# The characters allowed in a cookie value by RFC 6265.
COOKIE_VALUE_RE = re.compile(r"[!#-+\--:<-\[\]-~]+")


def parse_pr_reference(reference, default_repo):
    if reference.isdigit():
        return default_repo, int(reference)
    for pattern in (PR_URL_RE, PR_SHORT_RE):
        match = pattern.match(reference)
        if match:
            return match.group(1), int(match.group(2))
    raise ValueError(
        f"Cannot parse pull request reference {reference!r}. Use a number, "
        "OWNER/REPO#NUMBER, or a pull request URL."
    )


def pr_url(repo, number):
    return f"https://github.com/{repo}/pull/{number}"


def classify(entry):
    """
    Return (kind, description) for an audit log entry.

    kind is one of "browser", "mobile", "gh-cli", "agent", "api", or "bot".
    Everything except "browser" and "mobile", which are a person using
    GitHub's website or mobile app, was opened through the API.
    """
    user_agent = entry.get("user_agent") or ""
    access_type = entry.get("programmatic_access_type")
    if entry.get("actor_is_bot") in (True, "true"):
        return "bot", "bot account"
    agent = AGENT_RE.search(user_agent)
    if agent:
        return "agent", f'AI agent "{agent.group(1)}" driving the GitHub CLI'
    if user_agent.startswith("GitHub CLI"):
        return "gh-cli", "GitHub CLI"
    mobile_app = GITHUB_MOBILE_OAUTH_APPS.get(str(entry.get("oauth_application_id")))
    if mobile_app:
        return "mobile", mobile_app
    if access_type:
        return "api", f"API client, {access_type}"
    if not user_agent.startswith("Mozilla/"):
        return "api", "API client"
    return "browser", "browser"


def describe_auth(entry):
    access_type = entry.get("programmatic_access_type")
    if not access_type:
        return "browser session"
    app_id = entry.get("oauth_application_id")
    if app_id is None:
        return access_type
    if str(app_id) == GITHUB_CLI_OAUTH_APP_ID:
        return f"{access_type} (GitHub CLI OAuth app)"
    if str(app_id) in GITHUB_MOBILE_OAUTH_APPS:
        return f"{access_type} ({GITHUB_MOBILE_OAUTH_APPS[str(app_id)]} OAuth app)"
    return f"{access_type} (OAuth app {app_id})"


def load_export(path):
    entries = {}
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("action") == "pull_request.create":
                entries[entry["pull_request_url"]] = entry
    return entries


def parse_entries(soup):
    for table in soup.select("table.entry-details-table"):
        entry = {}
        for row in table.select("tr"):
            header = row.find("th")
            cell = row.find("td")
            if header is None or cell is None:
                continue
            value = cell.get_text(strip=True)
            if value == "nil" and cell.find("em") is not None:
                value = None
            entry[header.get_text(strip=True)] = value
        yield entry


def older_cursor(soup):
    for link in soup.select(".pagination a[href]"):
        if link.get_text(strip=True) == "Older":
            return parse_qs(urlparse(link["href"]).query)["after"][0]
    return None


def iter_audit_entries(session, org, query):
    url = f"https://github.com/organizations/{org}/settings/audit-log/results"
    params = {"q": query}
    while True:
        response = session.get(url, params=params, allow_redirects=False, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(
                f"GitHub returned HTTP {response.status_code} for the {org} "
                "audit log. The stored session cookie is probably expired, "
                "or the account is not an owner of the organization. Run "
                "'pr-origin --from-browser' to import a fresh cookie."
            )
        soup = BeautifulSoup(response.text, "html.parser")
        entries = list(parse_entries(soup))
        if not entries and RESULTS_MARKER not in response.text:
            raise RuntimeError(
                "GitHub returned a page that does not look like audit log "
                "results. The audit log page layout may have changed, or "
                "GitHub may be asking the browser session to reauthenticate."
            )
        yield from entries
        after = older_cursor(soup)
        if after is None:
            return
        params = {"q": query, "after": after}


def make_session(cookies):
    session = requests.Session()
    session.headers["User-Agent"] = "pr-origin (audit log lookup script)"
    session.headers["Accept"] = "text/html,application/xhtml+xml"
    for name, value in cookies.items():
        session.cookies.set(name, value, domain="github.com", secure=True)
    session.cookies.set("logged_in", "yes", domain=".github.com", secure=True)
    return session


def get_pr_created_at(repo, number):
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/pulls/{number}", "--jq", ".created_at"],
        check=True, capture_output=True, text=True,
    )
    return datetime.datetime.fromisoformat(result.stdout.strip())


def find_entry_online(session, repo, number):
    created = get_pr_created_at(repo, number).date()
    day = datetime.timedelta(days=1)
    query = (
        f"action:pull_request.create repo:{repo} "
        f"created:{created - day}..{created + day}"
    )
    target = pr_url(repo, number)
    org = repo.split("/")[0]
    for entry in iter_audit_entries(session, org, query):
        if entry.get("pull_request_url") == target:
            return entry
    return None


def summarize(repo, number, entry):
    kind, description = classify(entry)
    return {
        "repo": repo,
        "number": number,
        "url": pr_url(repo, number),
        "actor": entry.get("actor"),
        "title": entry.get("pull_request_title"),
        "kind": kind,
        "via_api": kind not in ("browser", "mobile"),
        "description": description,
        "auth": describe_auth(entry),
        "user_agent": entry.get("user_agent"),
    }


def print_summary(summary):
    if summary["kind"] == "mobile":
        how = f"in {summary['description']}"
    elif summary["via_api"]:
        how = f"via the API ({summary['description']})"
    else:
        how = "in a browser"
    print(f"{summary['repo']}#{summary['number']} by {summary['actor']}: "
          f"opened {how}")
    print(f"  title:      {summary['title']}")
    print(f"  user agent: {summary['user_agent']}")
    print(f"  auth:       {summary['auth']}")


def store_cookies(cookies):
    for name, value in cookies.items():
        if not COOKIE_VALUE_RE.fullmatch(value):
            raise ValueError(
                f"The {name} cookie value is empty or contains characters "
                "that are not valid in a cookie."
            )
    keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, json.dumps(cookies))
    print("Stored the github.com session cookie in the system keychain.")


def set_cookie():
    cookie = getpass.getpass(
        f'Value of the github.com "{SESSION_COOKIE}" cookie (input is hidden): '
    ).strip()
    store_cookies({SESSION_COOKIE: cookie, SAME_SITE_COOKIE: cookie})


def read_browser_cookies(browser):
    return getattr(rookiepy, browser)(["github.com"])


def import_cookies_from_browser(browser):
    cookies = {}
    for cookie in read_browser_cookies(browser):
        if cookie["domain"].lstrip(".") != "github.com":
            continue
        if cookie["name"] in (SESSION_COOKIE, SAME_SITE_COOKIE):
            cookies[cookie["name"]] = cookie["value"]
    if SESSION_COOKIE not in cookies:
        raise RuntimeError(
            f"No github.com {SESSION_COOKIE} cookie was found in {browser}. "
            "Log in to github.com in that browser, name a different browser, "
            "or use --set-cookie."
        )
    cookies.setdefault(SAME_SITE_COOKIE, cookies[SESSION_COOKIE])
    store_cookies(cookies)


def get_cookies():
    stored = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    if stored is None:
        raise RuntimeError(
            "No session cookie is stored. Run 'pr-origin --from-browser' "
            "first, or pass --export with a downloaded audit log export."
        )
    return json.loads(stored)


def main():
    parser = argparse.ArgumentParser(
        description="Report which client opened a GitHub pull request, "
        "using the organization audit log.",
    )
    parser.add_argument("pull_requests", nargs="*", metavar="PR",
        help="pull request number, OWNER/REPO#NUMBER, or URL")
    parser.add_argument("--repo", default=DEFAULT_REPO,
        help="repository for bare PR numbers (default: %(default)s)")
    parser.add_argument("--export", metavar="FILE",
        help="read a JSON export downloaded from the audit log page instead "
        "of querying github.com")
    parser.add_argument("--json", action="store_true",
        help="print one JSON object per pull request")
    parser.add_argument("--from-browser", nargs="?", const=DEFAULT_BROWSER,
        choices=BROWSERS, metavar="BROWSER",
        help="copy the github.com session cookie from a browser that is "
        "logged in to GitHub into the system keychain. BROWSER is one of "
        "%(choices)s (default: %(const)s). The operating system may ask for "
        "permission to read the browser's cookie encryption key.")
    parser.add_argument("--set-cookie", action="store_true",
        help="prompt for the github.com user_session cookie and store it in "
        "the system keychain")
    parser.add_argument("--delete-cookie", action="store_true",
        help="remove the stored session cookie from the system keychain")
    args = parser.parse_args()

    if args.from_browser:
        import_cookies_from_browser(args.from_browser)
    if args.set_cookie:
        set_cookie()
    if args.delete_cookie:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
        print("Removed the session cookie from the system keychain.")
    if not args.pull_requests:
        if args.from_browser or args.set_cookie or args.delete_cookie:
            return 0
        parser.error("no pull requests given")

    references = [parse_pr_reference(r, args.repo) for r in args.pull_requests]

    if args.export:
        export = load_export(args.export)
        session = None
    else:
        session = make_session(get_cookies())

    missing = 0
    for repo, number in references:
        if session is None:
            entry = export.get(pr_url(repo, number))
        else:
            entry = find_entry_online(session, repo, number)
        if entry is None:
            missing += 1
            print(f"{repo}#{number}: no pull_request.create entry found in "
                  "the audit log", file=sys.stderr)
            continue
        summary = summarize(repo, number, entry)
        if args.json:
            print(json.dumps(summary))
        else:
            print_summary(summary)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
