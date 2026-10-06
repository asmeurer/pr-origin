/*
 * Label pull requests, pull request reviews, and review comments on GitHub
 * with the client that created them: a browser, the GitHub CLI, an AI agent
 * driving the GitHub CLI, or some other API client. This is the same lookup
 * as ../pr_origin.py.
 *
 * The data comes from the organization audit log. The audit log results
 * page is fetched with the browser's own GitHub session, so this only works
 * in organizations that the logged in user is an owner of. The endpoint is
 * undocumented and may change.
 *
 * The audit log has no events for creating issues or issue comments, which
 * include the ordinary comments on pull requests, so those are never
 * labeled.
 */
"use strict";

const AGENT_RE = /\bAgent\/(\S+)/;
const GITHUB_CLI_OAUTH_APP_ID = "1157143";
const RESULTS_MARKER = "Filter audit logs";
const PR_URL_RE = /^https:\/\/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)(?:[/?#]|$)/;
const PR_PATH_RE = /^\/([^/]+\/[^/]+)\/pull\/(\d+)(?:\/|$)/;
const MINUTE = 60 * 1000;
const DAY = 24 * 60 * MINUTE;
// The most result pages (25 entries each) read for a single search.
const MAX_PAGES = 5;
// Reviews and comments are looked up by time. The audit log time can be
// several seconds later than the time shown on the page.
const TIME_WINDOW = 2 * MINUTE;
// Items closer together than this share a search, up to MAX_CLUSTER in all.
const CLUSTER_GAP = 15 * MINUTE;
const MAX_CLUSTER = 60 * MINUTE;
// The audit log keeps about six months of events. Items older than this are
// first checked against the start of the log all at once.
const PROBABLY_EXPIRED = 150 * DAY;
// Something that is still not in the audit log a day after it was created
// is assumed to be older than the log's retention period, and is not looked
// up again. Newer ones are retried, in case the log lags behind.
const LOG_DELAY = DAY;
const MISSING_RETRY = 10 * MINUTE;
const NO_ACCESS_RETRY = DAY;

// The kinds of items that get labels, and the audit log events that they
// are looked up in.
const ITEM_TYPES = {
  pr: {
    action: "pull_request.create",
    id: entry => entry.pull_request_url,
    verb: "Opened",
  },
  review: {
    action: "pull_request_review.submit",
    id: entry => entry.review_id,
    verb: "Submitted",
  },
  comment: {
    action: "pull_request_review_comment.create",
    id: entry => entry.comment_id,
    verb: "Posted",
  },
};

// recordKey(type, id) -> {kind, description, agent, auth, userAgent, actor}
// for an item that was found in the audit log, or {missing, checked, final}
// for one that was not. This mirrors chrome.storage.local.
const records = new Map();
const readFromStorage = new Set();
const pending = new Set();
// Organizations whose audit log is not queried again on this page.
const stoppedOrgs = new Set();

class AuditLogError extends Error {
  constructor(kind, message) {
    super(message);
    this.name = "AuditLogError";
    this.kind = kind;
  }
}

function recordKey(type, id) {
  return `${type}:${id}`;
}

function entryKey(entry) {
  for (const [type, { action, id }] of Object.entries(ITEM_TYPES)) {
    if (entry.action === action && id(entry)) {
      return recordKey(type, id(entry));
    }
  }
  return null;
}

function noAccessKey(org) {
  return `noaccess:${org}`;
}

function prNumber(url) {
  return Number(PR_URL_RE.exec(url ?? "")?.[2]);
}

function isoTime(milliseconds) {
  return new Date(milliseconds).toISOString().replace(/\.\d+Z$/, "Z");
}

function parseTime(datetime) {
  const time = datetime ? Date.parse(datetime) : NaN;
  return Number.isNaN(time) ? null : time;
}

// Return [kind, description] for an audit log entry. kind is one of
// "browser", "gh-cli", "agent", "api", or "bot". Everything except "browser"
// was created through the API.
function classify(entry) {
  const userAgent = entry.user_agent || "";
  const accessType = entry.programmatic_access_type;
  if (entry.actor_is_bot === "true") {
    return ["bot", "bot account"];
  }
  const agent = AGENT_RE.exec(userAgent);
  if (agent) {
    return ["agent", `AI agent "${agent[1]}" driving the GitHub CLI`];
  }
  if (userAgent.startsWith("GitHub CLI")) {
    return ["gh-cli", "GitHub CLI"];
  }
  if (accessType) {
    return ["api", `API client, ${accessType}`];
  }
  if (!userAgent.startsWith("Mozilla/")) {
    return ["api", "API client"];
  }
  return ["browser", "browser"];
}

function describeAuth(entry) {
  const accessType = entry.programmatic_access_type;
  if (!accessType) {
    return "browser session";
  }
  const appId = entry.oauth_application_id;
  if (appId == null) {
    return accessType;
  }
  if (appId === GITHUB_CLI_OAUTH_APP_ID) {
    return `${accessType} (GitHub CLI OAuth app)`;
  }
  return `${accessType} (OAuth app ${appId})`;
}

function summarize(entry) {
  const [kind, description] = classify(entry);
  return {
    kind,
    description,
    agent: AGENT_RE.exec(entry.user_agent || "")?.[1] ?? null,
    auth: describeAuth(entry),
    userAgent: entry.user_agent,
    actor: entry.actor,
  };
}

function parseEntries(doc) {
  const entries = [];
  for (const table of doc.querySelectorAll("table.entry-details-table")) {
    const entry = {};
    for (const row of table.querySelectorAll("tr")) {
      const header = row.querySelector("th");
      const cell = row.querySelector("td");
      if (!header || !cell) {
        continue;
      }
      let value = cell.textContent.trim();
      if (value === "nil" && cell.querySelector("em")) {
        value = null;
      }
      entry[header.textContent.trim()] = value;
    }
    entries.push(entry);
  }
  return entries;
}

async function fetchPage(org, query, after) {
  const params = new URLSearchParams({ q: query });
  if (after) {
    params.set("after", after);
  }
  const url = `${location.origin}/organizations/${org}/settings/audit-log/results?${params}`;
  const response = await fetch(url, { redirect: "manual" });
  if (response.status === 404) {
    throw new AuditLogError("no-access", `You are not an owner of ${org}.`);
  }
  if (response.type === "opaqueredirect") {
    throw new AuditLogError("reauth",
      `GitHub redirected the ${org} audit log request. Open the audit log, ` +
      "sign in or confirm access if asked, then reload this page.");
  }
  if (response.status !== 200) {
    throw new AuditLogError("http",
      `GitHub returned HTTP ${response.status} for the ${org} audit log. ` +
      "Reload this page to try again.");
  }
  const text = await response.text();
  const doc = new DOMParser().parseFromString(text, "text/html");
  const entries = parseEntries(doc);
  if (!entries.length && !text.includes(RESULTS_MARKER)) {
    throw new AuditLogError("reauth",
      `GitHub returned a page that does not look like ${org} audit log ` +
      "results. Open the audit log, confirm access if asked, then reload " +
      "this page. The audit log page layout may also have changed.");
  }
  let older = null;
  for (const link of doc.querySelectorAll(".pagination a[href]")) {
    if (link.textContent.trim() === "Older") {
      older = new URL(link.getAttribute("href"), location.origin).searchParams.get("after");
    }
  }
  return { entries, older };
}

function isFound(key) {
  const record = records.get(key);
  return record !== undefined && !record.missing;
}

function isFresh(record) {
  if (record === undefined) {
    return false;
  }
  return !record.missing || record.final || Date.now() - record.checked < MISSING_RETRY;
}

async function remember(entries) {
  const items = {};
  for (const entry of entries) {
    const key = entryKey(entry);
    if (key === null) {
      continue;
    }
    const record = summarize(entry);
    records.set(key, record);
    items[key] = record;
  }
  await chrome.storage.local.set(items);
  schedule();
}

// Read audit log results, newest first, until every target has been found.
// Return true if the targets that were not found cannot be in the results
// either, because there are no more results, or because isPast says that
// the results have gone past all of them. Return false if MAX_PAGES was
// reached first.
async function search(org, query, targets, isPast = () => false) {
  let after = null;
  for (let page = 0; page < MAX_PAGES; page++) {
    const results = await fetchPage(org, query, after);
    await remember(results.entries);
    const unknown = targets.filter(target => !isFound(target.key));
    if (!unknown.length || !results.older || isPast(results.entries, unknown)) {
      return true;
    }
    after = results.older;
  }
  return false;
}

// Pull request numbers increase with creation time, so once the results get
// below the lowest number still being looked for, the rest cannot match.
function belowRows(entries, rows) {
  const last = entries.at(-1);
  return last !== undefined &&
    prNumber(last.pull_request_url) < Math.min(...rows.map(row => row.number));
}

async function lookUpRows(org, repo, rows) {
  rows.sort((a, b) => b.number - a.number);
  const base = `action:pull_request.create repo:${repo}`;
  // An "opened" time is the creation time. A "merged" or "closed" time is
  // later than it, so it is only an upper bound.
  let range = "";
  if (rows.every(row => row.time != null)) {
    const times = rows.map(row => row.time);
    const upper = isoTime(Math.max(...times) + MINUTE);
    range = rows.every(row => row.opened)
      ? ` created:${isoTime(Math.min(...times) - MINUTE)}..${upper}`
      : ` created:<=${upper}`;
  }
  if (await search(org, base + range, rows, belowRows)) {
    return;
  }
  // The rows are spread over too much of the audit log to read all of it,
  // so look up the rest one at a time.
  for (const row of rows) {
    if (isFound(row.key) || row.time == null) {
      continue;
    }
    let qualifiers;
    if (row.opened) {
      qualifiers = `created:${isoTime(row.time - MINUTE)}..${isoTime(row.time + MINUTE)}`;
    } else {
      const actor = row.author ? `actor:${row.author} ` : "";
      qualifiers = `${actor}created:<=${isoTime(row.time + MINUTE)}`;
    }
    await search(org, `${base} ${qualifiers}`, [row], belowRows);
  }
}

// Look up items by the time shown on the page, which is when they were
// created. Each search reads every audit log event in the repository around
// that time.
async function lookUpByTime(org, repo, items) {
  let timed = items.filter(item => item.time != null).sort((a, b) => a.time - b.time);
  const old = timed.filter(item => Date.now() - item.time > PROBABLY_EXPIRED);
  if (old.length) {
    const cutoff = old.at(-1).time + TIME_WINDOW;
    const results = await fetchPage(org, `repo:${repo} created:<=${isoTime(cutoff)}`);
    await remember(results.entries);
    if (!results.entries.length) {
      // The audit log has nothing that old, so none of these can be found.
      timed = timed.filter(item => item.time > cutoff);
    }
  }
  const clusters = [];
  for (const item of timed) {
    const last = clusters.at(-1);
    if (last && item.time - last.end <= CLUSTER_GAP && item.time - last.start <= MAX_CLUSTER) {
      last.items.push(item);
      last.end = item.time;
    } else {
      clusters.push({ start: item.time, end: item.time, items: [item] });
    }
  }
  for (const { start, end, items: targets } of clusters) {
    if (targets.every(item => isFound(item.key))) {
      continue;
    }
    const range = `${isoTime(start - TIME_WINDOW)}..${isoTime(end + TIME_WINDOW)}`;
    await search(org, `repo:${repo} created:${range}`, targets);
  }
}

async function lookUp(org, repo, items) {
  const rows = items.filter(item => item.row);
  if (rows.length) {
    await lookUpRows(org, repo, rows);
  }
  await lookUpByTime(org, repo, items.filter(item => !item.row));

  const now = Date.now();
  const missing = {};
  for (const item of items) {
    if (isFound(item.key)) {
      continue;
    }
    const record = {
      missing: true,
      checked: now,
      final: item.time != null && now - item.time > LOG_DELAY,
    };
    records.set(item.key, record);
    missing[item.key] = record;
  }
  await chrome.storage.local.set(missing);
}

async function startLookUp(repo, items) {
  const org = repo.split("/")[0];
  for (const item of items) {
    pending.add(item.key);
  }
  try {
    const key = noAccessKey(org);
    const stored = await chrome.storage.local.get(key);
    if (stored[key] !== undefined && Date.now() - stored[key] < NO_ACCESS_RETRY) {
      stoppedOrgs.add(org);
      return;
    }
    await lookUp(org, repo, items);
  } catch (error) {
    stoppedOrgs.add(org);
    if (error instanceof AuditLogError && error.kind === "no-access") {
      await chrome.storage.local.set({ [noAccessKey(org)]: Date.now() });
      return;
    }
    showNotice(org, error instanceof AuditLogError
      ? error.message
      : `Looking up items in the ${org} audit log failed: ${error}`);
    throw error;
  } finally {
    for (const item of items) {
      pending.delete(item.key);
    }
    schedule();
  }
}

// Each item on the page that can get a label is an object with
//
//   key     its record key
//   type    a key of ITEM_TYPES
//   repo    "OWNER/REPO"
//   time    when it was created, in milliseconds, or null if unknown
//   anchor  the element that the label goes after, or at the end of if
//           append is true
//
// Rows of pull request lists also have row: true, number, author, and
// opened, which is false if time is when the pull request was merged or
// closed instead.

function listRows() {
  const rows = [];
  for (const link of document.querySelectorAll('a[data-testid="listitem-title-link"]')) {
    const match = PR_URL_RE.exec(link.href);
    const element = link.closest("li");
    if (!match || !element) {
      continue;
    }
    const stamp = element.querySelector('[data-testid="timestamp-container"]');
    const time = parseTime(stamp?.querySelector("relative-time[datetime]")?.getAttribute("datetime"));
    rows.push({
      key: recordKey("pr", `https://github.com/${match[1]}/pull/${match[2]}`),
      type: "pr",
      repo: match[1],
      time,
      anchor: stamp?.parentElement ?? element,
      append: true,
      row: true,
      number: Number(match[2]),
      author: stamp?.querySelector('[data-testid="author-filter-link"]')?.textContent.trim(),
      opened: time != null && /\bopened\b/.test(stamp.textContent),
    });
  }
  return rows;
}

function pageItem(element, repo, number) {
  // The pull request description, reviews, and review comments on the
  // Conversation tab. Each has a timestamp link with an id ending in
  // "-permalink". The same review can appear twice, with the same ids.
  let type, id, anchor;
  let match;
  if (/^pullrequest-\d+$/.test(element.id)) {
    type = "pr";
    id = `https://github.com/${repo}/pull/${number}`;
    anchor = [...element.querySelectorAll('a[id$="-permalink"]')]
      .find(link => /^issue-\d+-permalink$/.test(link.id));
  } else if ((match = /^(pullrequestreview-|discussion_r)(\d+)$/.exec(element.id))) {
    type = match[1] === "discussion_r" ? "comment" : "review";
    id = match[2];
    anchor = element.querySelector(`#${CSS.escape(element.id)}-permalink`);
  } else if ((match = /^r(\d+)$/.exec(element.id))) {
    // A review comment on the Files changed tab.
    const header = element.querySelector('[data-testid="comment-header"]');
    if (!header) {
      return null;
    }
    return {
      key: recordKey("comment", match[1]),
      type: "comment",
      repo,
      time: parseTime(header.querySelector("relative-time")?.getAttribute("datetime")),
      anchor: header.querySelector('[data-testid="comment-header-left-side-items"]') ?? header,
      append: true,
    };
  }
  if (!anchor) {
    return null;
  }
  return {
    key: recordKey(type, id),
    type,
    repo,
    time: parseTime(anchor.querySelector("relative-time")?.getAttribute("datetime")),
    anchor,
    append: false,
  };
}

function pageItems() {
  const match = PR_PATH_RE.exec(location.pathname);
  if (!match) {
    return [];
  }
  const selector = '[id^="pullrequest-"], [id^="pullrequestreview-"], [id^="discussion_r"], [id^="r"]';
  const items = [];
  for (const element of document.querySelectorAll(selector)) {
    const item = pageItem(element, match[1], match[2]);
    if (item !== null) {
      items.push(item);
    }
  }
  return items;
}

function labelFor(item, record) {
  if (record === undefined || (record.missing && !record.final)) {
    return null;
  }
  const { action, verb } = ITEM_TYPES[item.type];
  if (record.missing) {
    // Reviews and review comments only get a label when they are known to
    // have been created through the API. Pull requests are labeled either
    // way, so that a missing label means "opened in a browser".
    if (item.type !== "pr") {
      return null;
    }
    return {
      kind: "unknown",
      text: "origin unknown",
      title: `pr-origin: this pull request has no ${action} entry in the ` +
        "audit log. It is probably older than the audit log retention " +
        "period.",
    };
  }
  // Claude Code appends its version, as in "claude-code_2-1-278_agent".
  const text = {
    agent: `agent: ${record.agent?.split("_")[0]}`,
    "gh-cli": "GitHub CLI",
    api: "API",
    bot: "bot",
  }[record.kind];
  if (text === undefined) {
    return null;
  }
  return {
    kind: record.kind,
    text,
    title: `${verb} via the API: ${record.description}\n` +
      `User agent: ${record.userAgent}\n` +
      `Auth: ${record.auth}\n` +
      "(pr-origin, from the organization audit log)",
  };
}

function findLabel(item) {
  if (item.append) {
    return item.anchor.querySelector(":scope > .pr-origin-label");
  }
  const next = item.anchor.nextElementSibling;
  return next?.classList.contains("pr-origin-label") ? next : null;
}

// Only touch the DOM when something changed, since every change triggers
// the MutationObserver again.
function render(item) {
  const label = labelFor(item, records.get(item.key));
  let element = findLabel(item);
  if (label === null) {
    element?.remove();
    return;
  }
  if (element === null) {
    element = document.createElement("span");
    if (item.append) {
      item.anchor.append(element);
    } else {
      item.anchor.after(element);
    }
  }
  const className = `pr-origin-label pr-origin-${label.kind}`;
  if (element.className !== className) {
    element.className = className;
  }
  if (element.textContent !== label.text) {
    element.textContent = label.text;
  }
  if (element.title !== label.title) {
    element.title = label.title;
  }
}

function showNotice(org, message) {
  if (document.querySelector(".pr-origin-notice")) {
    return;
  }
  const notice = document.createElement("div");
  notice.className = "pr-origin-notice";
  notice.setAttribute("role", "alert");
  const text = document.createElement("span");
  text.textContent = `pr-origin: ${message} `;
  const link = document.createElement("a");
  link.href = `https://github.com/organizations/${org}/settings/audit-log`;
  link.target = "_blank";
  link.textContent = "Open the audit log";
  const close = document.createElement("button");
  close.type = "button";
  close.className = "pr-origin-notice-close";
  close.setAttribute("aria-label", "Dismiss");
  close.textContent = "×";
  close.addEventListener("click", () => notice.remove());
  notice.append(text, link, close);
  document.body.append(notice);
}

async function update() {
  const items = [...listRows(), ...pageItems()];
  const unread = [...new Set(items.map(item => item.key))]
    .filter(key => !readFromStorage.has(key));
  if (unread.length) {
    const stored = await chrome.storage.local.get(unread);
    for (const key of unread) {
      readFromStorage.add(key);
      if (stored[key] !== undefined && !records.has(key)) {
        records.set(key, stored[key]);
      }
    }
  }
  const stale = new Map();
  for (const item of items) {
    render(item);
    const org = item.repo.split("/")[0];
    if (isFresh(records.get(item.key)) || pending.has(item.key) || stoppedOrgs.has(org)) {
      continue;
    }
    if (!stale.has(item.repo)) {
      stale.set(item.repo, new Map());
    }
    stale.get(item.repo).set(item.key, item);
  }
  for (const [repo, repoItems] of stale) {
    startLookUp(repo, [...repoItems.values()]);
  }
}

let scheduled = false;

function schedule() {
  if (scheduled) {
    return;
  }
  scheduled = true;
  setTimeout(() => {
    scheduled = false;
    update();
  }, 200);
}

// GitHub swaps page content without full page loads, and Turbo replaces the
// body element itself, so watch the whole document.
new MutationObserver(schedule).observe(document.documentElement, {
  childList: true,
  subtree: true,
});
schedule();
