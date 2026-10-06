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

Store your session cookie once. This is the value of the `user_session`
cookie for github.com, which you can copy from the browser developer tools
under Application > Cookies. It is kept in the system keychain.

    pr-origin --set-cookie

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

    uv run --with pytest --with requests --with beautifulsoup4 --with keyring pytest

The tests use mocked audit log pages and do not contact GitHub.
