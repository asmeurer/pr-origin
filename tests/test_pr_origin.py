import datetime
import json

import pytest

import pr_origin


def row(key, value):
    cell = "<em>nil</em>" if value is None else value
    return (
        f'<tr class="entry-details-tr"> <th class="entry-details-th" tabindex="0">{key}</th> '
        f'<td class="entry-details-td" tabindex="0">\n            {cell}\n        </td> </tr>'
    )


def entry_html(number, actor, user_agent, access_type=None, app_id=None):
    rows = [
        row("@timestamp", "2026-09-16 14:48:24 -0600"),
        row("action", "pull_request.create"),
        row("actor", f'<a href="/organizations/sympy/settings/audit-log?q=actor%3A{actor}">{actor}</a>'),
        row("actor_is_bot", "false"),
        row("pull_request_title", "Some title"),
        row("pull_request_url", f"https://github.com/sympy/sympy/pull/{number}"),
        row("request_access_security_header", None),
        row("user_agent", user_agent),
    ]
    if access_type:
        rows.append(row("programmatic_access_type", access_type))
    if app_id:
        rows.append(row("oauth_application_id", app_id))
    return (
        '<div class="TableObject-item audit-log-details-cont"><div class="entry-details">'
        '<table class="entry-details-table"> <tbody>' + "".join(rows) + "</tbody></table></div></div>"
    )


HEADER = "<div>Filters Filter audit logs</div>"
PAGE1 = HEADER + entry_html(101, "alice", "Mozilla/5.0 (Macintosh) Chrome/152") + (
    '<div class="paginate-container"><div class="pagination">'
    '<span class="disabled">Newer</span>'
    '<a href="/organizations/sympy/settings/audit-log?after=CURSOR%3D%3D&amp;q=action%3Apull_request.create">Older</a>'
    "</div></div>"
)
PAGE2 = HEADER + entry_html(100, "bob", "GitHub CLI 2.93.0 Agent/codex", "OAuth access token", "1157143")
EMPTY = HEADER + "<p>We couldn't find any events</p>"


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


class FakeSession:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, params, allow_redirects, timeout):
        self.calls.append((url, dict(params)))
        assert allow_redirects is False
        return self.pages[params.get("after")]


@pytest.fixture(autouse=True)
def fixed_created_at(monkeypatch):
    created = datetime.datetime(2026, 9, 16, 20, 48, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr(pr_origin, "get_pr_created_at", lambda repo, number: created)


def two_page_session():
    return FakeSession({None: FakeResponse(PAGE1), "CURSOR==": FakeResponse(PAGE2)})


def test_find_entry_follows_older_cursor():
    session = two_page_session()
    entry = pr_origin.find_entry_online(session, "sympy/sympy", 100)
    assert entry["user_agent"] == "GitHub CLI 2.93.0 Agent/codex"
    assert entry["actor"] == "bob"
    assert entry["request_access_security_header"] is None
    assert session.calls == [
        (
            "https://github.com/organizations/sympy/settings/audit-log/results",
            {"q": "action:pull_request.create repo:sympy/sympy created:2026-09-15..2026-09-17"},
        ),
        (
            "https://github.com/organizations/sympy/settings/audit-log/results",
            {
                "q": "action:pull_request.create repo:sympy/sympy created:2026-09-15..2026-09-17",
                "after": "CURSOR==",
            },
        ),
    ]
    summary = pr_origin.summarize("sympy/sympy", 100, entry)
    assert summary["kind"] == "agent"
    assert summary["via_api"] is True
    assert summary["auth"] == "OAuth access token (GitHub CLI OAuth app)"


def test_find_entry_stops_at_first_match():
    session = two_page_session()
    entry = pr_origin.find_entry_online(session, "sympy/sympy", 101)
    assert len(session.calls) == 1
    assert pr_origin.classify(entry) == ("browser", "browser")
    assert pr_origin.summarize("sympy/sympy", 101, entry)["via_api"] is False


def test_find_entry_missing():
    session = FakeSession({None: FakeResponse(EMPTY)})
    assert pr_origin.find_entry_online(session, "sympy/sympy", 1) is None


@pytest.mark.parametrize("response", [
    FakeResponse("", 404),
    FakeResponse("", 302),
    FakeResponse("<html>Confirm access</html>"),
])
def test_unexpected_response_raises(response):
    session = FakeSession({None: response})
    with pytest.raises(RuntimeError):
        pr_origin.find_entry_online(session, "sympy/sympy", 1)


IOS_USER_AGENT = "GitHub/1.275.0 (com.github.stormbreaker.prod; build:203825250; iOS 26.6.0; iPhone12,1)"
ANDROID_USER_AGENT = "GitHub/1.269.0 (com.github.android; build:939; Android 16; moto g - 2026)"


@pytest.mark.parametrize("entry, kind", [
    ({"user_agent": "Mozilla/5.0 (X11; Linux x86_64) Firefox/152"}, "browser"),
    ({"user_agent": "GitHub CLI 2.93.0", "programmatic_access_type": "OAuth access token"}, "gh-cli"),
    ({"user_agent": "GitHub CLI 2.98.0 Agent/claude-code_2-1-246_agent"}, "agent"),
    ({"user_agent": "Mozilla/5.0", "programmatic_access_type": "Personal access token (classic)"}, "api"),
    ({"user_agent": "curl/7.81.0"}, "api"),
    ({"user_agent": "dependabot-api", "actor_is_bot": "true"}, "bot"),
    ({"user_agent": "Faraday v2.14.1", "actor_is_bot": True}, "bot"),
    ({"user_agent": IOS_USER_AGENT, "programmatic_access_type": "OAuth access token",
      "oauth_application_id": "1079828"}, "mobile"),
    ({"user_agent": ANDROID_USER_AGENT, "programmatic_access_type": "OAuth access token",
      "oauth_application_id": 1119845}, "mobile"),
    # A script can send the mobile app's user agent, but not use its token.
    ({"user_agent": IOS_USER_AGENT, "programmatic_access_type": "Personal access token (classic)"}, "api"),
])
def test_classify(entry, kind):
    assert pr_origin.classify(entry)[0] == kind


def test_mobile_app_is_not_reported_as_api(capsys):
    entry = {"actor": "alice", "user_agent": IOS_USER_AGENT,
             "programmatic_access_type": "OAuth access token", "oauth_application_id": "1079828"}
    summary = pr_origin.summarize("sympy/sympy", 1, entry)
    assert summary["kind"] == "mobile"
    assert summary["via_api"] is False
    assert summary["auth"] == "OAuth access token (GitHub Mobile for iOS OAuth app)"
    pr_origin.print_summary(summary)
    assert "sympy/sympy#1 by alice: opened in GitHub Mobile for iOS" in capsys.readouterr().out


@pytest.mark.parametrize("reference, expected", [
    ("123", ("sympy/sympy", 123)),
    ("sympy/live#12", ("sympy/live", 12)),
    ("https://github.com/sympy/sympy.github.com/pull/205", ("sympy/sympy.github.com", 205)),
    ("https://github.com/sympy/sympy/pull/30548/files", ("sympy/sympy", 30548)),
])
def test_parse_pr_reference(reference, expected):
    assert pr_origin.parse_pr_reference(reference, "sympy/sympy") == expected


def test_parse_pr_reference_invalid():
    with pytest.raises(ValueError):
        pr_origin.parse_pr_reference("foo", "sympy/sympy")


def test_load_export(tmp_path):
    export = tmp_path / "export.json"
    entries = [
        {"action": "pull_request.create", "actor": "bob", "oauth_application_id": 1157143,
         "programmatic_access_type": "OAuth access token",
         "pull_request_url": "https://github.com/sympy/sympy/pull/100",
         "user_agent": "GitHub CLI 2.93.0"},
        {"action": "repo.create", "actor": "alice"},
    ]
    export.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    loaded = pr_origin.load_export(export)
    assert list(loaded) == ["https://github.com/sympy/sympy/pull/100"]
    summary = pr_origin.summarize("sympy/sympy", 100, loaded["https://github.com/sympy/sympy/pull/100"])
    assert summary["kind"] == "gh-cli"
    assert summary["auth"] == "OAuth access token (GitHub CLI OAuth app)"


class FakeKeyring:
    def __init__(self):
        self.passwords = {}

    def set_password(self, service, username, password):
        self.passwords[service, username] = password

    def get_password(self, service, username):
        return self.passwords.get((service, username))


@pytest.fixture
def fake_keyring(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(pr_origin, "keyring", fake)
    return fake


def browser_cookie(name, value, domain="github.com"):
    return {"name": name, "value": value, "domain": domain, "path": "/",
            "secure": True, "http_only": True, "expires": None}


def test_import_cookies_from_browser(monkeypatch, fake_keyring):
    requested = []

    def read(browser):
        requested.append(browser)
        return [
            browser_cookie("logged_in", "yes", ".github.com"),
            browser_cookie("user_session", "wrong-site", "gist.github.com"),
            browser_cookie("user_session", "abc_DEF-123"),
            browser_cookie("__Host-user_session_same_site", "same_SITE-456"),
        ]

    monkeypatch.setattr(pr_origin, "read_browser_cookies", read)
    pr_origin.import_cookies_from_browser("firefox")
    assert requested == ["firefox"]
    assert pr_origin.get_cookies() == {
        "user_session": "abc_DEF-123",
        "__Host-user_session_same_site": "same_SITE-456",
    }


def test_import_cookies_without_same_site_cookie(monkeypatch, fake_keyring):
    monkeypatch.setattr(pr_origin, "read_browser_cookies",
                        lambda browser: [browser_cookie("user_session", "abc")])
    pr_origin.import_cookies_from_browser("chrome")
    assert pr_origin.get_cookies() == {
        "user_session": "abc",
        "__Host-user_session_same_site": "abc",
    }


def test_import_cookies_not_logged_in(monkeypatch, fake_keyring):
    monkeypatch.setattr(pr_origin, "read_browser_cookies",
                        lambda browser: [browser_cookie("logged_in", "no", ".github.com")])
    with pytest.raises(RuntimeError, match="No github.com user_session cookie"):
        pr_origin.import_cookies_from_browser("chrome")
    assert fake_keyring.passwords == {}


@pytest.mark.parametrize("value", ["", "\x8f\x02garbage", "has space", 'quoted"value', "semi;colon"])
def test_invalid_cookie_value_is_not_stored(monkeypatch, fake_keyring, value):
    monkeypatch.setattr(pr_origin, "read_browser_cookies",
                        lambda browser: [browser_cookie("user_session", value)])
    with pytest.raises(ValueError):
        pr_origin.import_cookies_from_browser("chrome")
    assert fake_keyring.passwords == {}


def test_set_cookie_prompts_and_stores(monkeypatch, fake_keyring):
    monkeypatch.setattr(pr_origin.getpass, "getpass", lambda prompt: "  abc123\n")
    pr_origin.set_cookie()
    assert pr_origin.get_cookies() == {
        "user_session": "abc123",
        "__Host-user_session_same_site": "abc123",
    }


def test_get_cookies_missing(fake_keyring):
    with pytest.raises(RuntimeError, match="--from-browser"):
        pr_origin.get_cookies()


def test_make_session_sends_stored_cookies():
    session = pr_origin.make_session({"user_session": "abc", "__Host-user_session_same_site": "def"})
    sent = {c.name: (c.value, c.domain) for c in session.cookies}
    assert sent["user_session"] == ("abc", "github.com")
    assert sent["__Host-user_session_same_site"] == ("def", "github.com")
    assert sent["logged_in"] == ("yes", ".github.com")
