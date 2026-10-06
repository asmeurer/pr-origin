# pr-origin

Report which client opened a GitHub pull request: a browser, the GitHub CLI,
an AI agent driving the GitHub CLI, or some other API client.

GitHub does not expose this on the pull request itself. It is only recorded
in the organization audit log, where every `pull_request.create` event carries
the `user_agent` and `programmatic_access_type` of the request. The audit log
REST and GraphQL APIs require GitHub Enterprise Cloud, so on other plans this
tool reads the HTML fragment that the audit log web page loads:

    https://github.com/organizations/ORG/settings/audit-log/results?q=...

That endpoint is undocumented and may change without notice. It only works
with the browser session cookie of an organization owner.

## Installation

The script is self-contained and runs under [uv](https://docs.astral.sh/uv/),
which installs its dependencies on first use. Symlink it onto your `PATH`:

    ln -s "$PWD/pr_origin.py" ~/.local/bin/pr-origin

The [GitHub CLI](https://cli.github.com/) must be installed and logged in. It
is used to look up the creation date of each pull request.

## Usage

Import your GitHub session cookie once, from a browser that is logged in to
github.com as an owner of the organization:

    pr-origin --from-browser

This reads Chrome by default. Name another browser to use it instead, for
example `pr-origin --from-browser firefox`. The supported names are listed in
`pr-origin --help`. The browser can stay open. The cookie is copied into the
system keychain and is never printed.

Chromium-based browsers encrypt their cookies with a key that is kept in the
macOS keychain, so macOS asks for your login password before the tool can read
it. Choosing "Allow" grants access for that one run. "Always Allow" lets the
Python interpreter read the browser's cookie key without asking again. Firefox
stores cookies unencrypted and does not prompt.

Run the same command again whenever the tool reports that the cookie has
expired.

Alternatively, `pr-origin --set-cookie` prompts for the value of the
`user_session` cookie for github.com, which you can copy from the browser
developer tools under Application > Cookies.

Then look up pull requests by number, `OWNER/REPO#NUMBER`, or URL:

    pr-origin 30548 30566
    pr-origin sympy/live#123
    pr-origin --json https://github.com/sympy/sympy/pull/30548

Bare numbers refer to `sympy/sympy` unless `--repo` is given. The exit status
is 1 if any pull request has no audit log entry.

`pr-origin --delete-cookie` removes the cookie from the keychain.

The session cookie grants full access to your GitHub account, including
without two-factor authentication. Treat it like a password.

### Offline mode

The audit log page has an Export button. With `--export`, the tool reads a
downloaded JSON export instead of querying github.com, and needs no cookie:

    pr-origin --export export-sympy-1790100396.json 30548

## Browser extension

The `extension/` directory is a Chrome extension that labels the pull
requests in GitHub's pull request lists, such as
https://github.com/sympy/sympy/pulls, with the client that opened them.
Pull requests opened via the API get a label such as `GitHub CLI` or
`agent: claude-code`. Hover over a label to see the user agent and the
authentication type. Pull requests opened in a browser get no label, and ones
with no audit log entry, usually because they are older than the audit log
retention period, are labeled `origin unknown`.

On a pull request page, the extension also labels the pull request
description, reviews, and review comments (the comments on lines of code, on
both the Conversation and Files changed tabs). Reviews and review comments are
only labeled when the audit log shows that they were created through the API.
An unlabeled review or review comment was usually posted in a browser, but
not always: GitHub logs no review submission for a review that is created
through the API together with its comments, and it can leave some of the
comments in a large API review out of the log. In one such review, only 24 of
the 49 comments were logged.

Issues and ordinary comments, on issues or on pull requests, are never
labeled. The audit log has no events for creating them.

To install it, open `chrome://extensions`, turn on Developer mode, click
"Load unpacked", and select the `extension` directory. When loaded unpacked,
the extension checks its files every second, and when they change it reloads
itself and any open github.com tabs, so edits take effect without another
visit to `chrome://extensions`. The extension needs no cookie setup. It reads the same audit log page as the command line tool,
using the GitHub session of the browser, so it only labels pull requests in
organizations that you are an owner of. It skips other organizations, and
checks them again a day later.

Each page of the list usually takes one or two audit log requests, and a pull
request page takes about one request per burst of review activity. Results
are cached in the extension's local storage, so anything that has already
been looked up is labeled immediately. If GitHub asks you to confirm access to
the audit log, the extension shows a notice with a link to the audit log page.
Confirm access there, then reload the page.

## Classification

| Kind      | Meaning                                                                 |
|-----------|-------------------------------------------------------------------------|
| `browser` | Browser user agent with a normal web session                            |
| `gh-cli`  | GitHub CLI                                                              |
| `agent`   | GitHub CLI run by an AI agent (the user agent has an `Agent/...` suffix) |
| `api`     | Any other token-authenticated or non-browser client                     |
| `bot`     | Bot account such as Dependabot                                          |

Everything except `browser` is reported as opened via the API. A request that
uses a token is classified as `api` even if it sends a browser user agent.

The `Agent/...` suffix comes from the GitHub CLI itself, which detects agents
such as Claude Code, Codex, and OpenCode from their environment variables and
appends the agent name to its user agent.

## Tests

    uv run --with pytest --with requests --with beautifulsoup4 --with keyring --with rookiepy pytest

The tests use mocked audit log pages and a mocked browser and keychain. They
do not contact GitHub or read any real cookies.
