import datetime as dt
import json
import os
import sys
import time
import random
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

import requests
import re
from . import ado
from requests import exceptions as req_exc


GITHUB_GRAPHQL = "https://api.github.com/graphql"
GITHUB_REST = "https://api.github.com"


def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-Github-Api-Version": "2022-11-28",
    }


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _cache_get(cache_dir: Path, key: str) -> Optional[Dict[str, Any]]:
    p = cache_dir / f"{key}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _cache_set(cache_dir: Path, key: str, value: Dict[str, Any]) -> None:
    p = cache_dir / f"{key}.json"
    p.write_text(json.dumps(value), encoding="utf-8")


def _request_json(method: str, url: str, token: str, retries: int = 5, backoff: float = 0.5, timeout: float = 30.0, **kwargs) -> Dict[str, Any]:
    """Perform an HTTP request returning JSON with robust retries for transient network/protocol errors and rate limits."""
    headers = kwargs.pop("headers", {})
    merged_headers = {**_headers(token), **headers}
    for attempt in range(retries):
        try:
            resp = requests.request(method, url, headers=merged_headers, timeout=timeout, **kwargs)
            # Handle rate limiting
            if resp.status_code in (429, 403):
                # If rate limited, wait until reset when provided
                reset = resp.headers.get("X-RateLimit-Reset")
                if reset and reset.isdigit():
                    now = int(time.time())
                    wait_s = max(0, int(reset) - now) + 1
                    time.sleep(min(wait_s, 60))
                    continue
            # Retry on 5xx responses as transient server errors
            if 500 <= resp.status_code < 600:
                if attempt < retries - 1:
                    # exponential backoff with jitter
                    sleep_s = backoff * (2 ** attempt)
                    jitter = random.uniform(0, sleep_s * 0.25)
                    time.sleep(sleep_s + jitter)
                    continue
            resp.raise_for_status()
            try:
                return resp.json()
            except json.JSONDecodeError:
                # Transient truncated responses
                if attempt < retries - 1:
                    sleep_s = backoff * (2 ** attempt)
                    jitter = random.uniform(0, sleep_s * 0.25)
                    time.sleep(sleep_s + jitter)
                    continue
                raise
        except (req_exc.ChunkedEncodingError, req_exc.ConnectionError, req_exc.ReadTimeout, req_exc.SSLError) as e:
            if attempt < retries - 1:
                sleep_s = backoff * (2 ** attempt)
                jitter = random.uniform(0, sleep_s * 0.25)
                time.sleep(sleep_s + jitter)
                continue
            raise e
    # Should not reach here
    raise RuntimeError("Request failed after retries")


def _paginate_graphql(
    query: str, 
    variables: Dict[str, Any], 
    token: str, 
    root_path: List[str],
    progress_callback: Optional[Callable[[str, int], None]] = None,
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    cursor = None
    page_num = 0
    while True:
        page_num += 1
        if progress_callback:
            progress_callback("fetching", len(items))
        vars_with_cursor = dict(variables)
        vars_with_cursor["cursor"] = cursor
        payload = _request_json(
            "POST",
            GITHUB_GRAPHQL,
            token,
            json={"query": query, "variables": vars_with_cursor},
        )
        # Graceful error handling
        if isinstance(payload, dict) and "errors" in payload and payload.get("errors"):
            messages = "; ".join(str(e.get("message", e)) for e in payload.get("errors", []))
            raise RuntimeError(
                f"GitHub GraphQL error: {messages}. Ensure token has repo access and org SSO is authorized (gh auth status)."
            )
        if not isinstance(payload, dict) or "data" not in payload or payload.get("data") is None:
            raise RuntimeError(
                "GitHub GraphQL returned no data. Check token scopes and organization SSO authorization."
            )
        node = payload
        for key in root_path:
            if key not in node:
                raise RuntimeError(
                    f"Unexpected GraphQL response shape; missing '{key}'. Response keys: {list(node.keys())}"
                )
            node = node[key]
        edges = node["edges"]
        for edge in edges:
            items.append(edge["node"])
            if progress_callback:
                progress_callback("fetching", len(items))
        page_info = node["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
        time.sleep(0.2)
    if progress_callback:
        progress_callback("complete", len(items))
    return items


def _list_prs(
    owner: str, 
    repo: str, 
    token: str, 
    since_iso: str, 
    until_iso: str,
    progress_callback: Optional[Callable[[str, int], None]] = None,
) -> List[Dict[str, Any]]:
    # Use GraphQL search with created date range to avoid over-fetching
    # GitHub search syntax: type:pr repo:owner/repo created:YYYY-MM-DD..YYYY-MM-DD
    # We page through search results and then hydrate minimal PR fields via node selection
    # Normalize ISO strings to date portion (UTC) for the search range
    since_day = since_iso[:10]
    until_day = until_iso[:10]

    search_query = f"type:pr repo:{owner}/{repo} created:{since_day}..{until_day}"
    query = """
    query($q: String!, $cursor: String) {
      search(query: $q, type: ISSUE, first: 50, after: $cursor) {
        edges {
          node {
            ... on PullRequest {
              number
              title
              state
              isDraft
              createdAt
              mergedAt
              closedAt
              author { login __typename }
              additions
              deletions
              changedFiles
              commits(first: 1) { totalCount }
              reviews(first: 100) {
                totalCount
                nodes {
                  author { login __typename }
                  state
                  submittedAt
                }
              }
              comments(first: 1) { totalCount }
              reviewRequests(first: 10) { totalCount }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
    """

    variables = {"q": search_query}
    items = _paginate_graphql(query, variables, token, ["data", "search"], progress_callback)

    # Nodes can include non-PRs in theory, but we only selected PullRequest in fragment; filter defensively
    prs: List[Dict[str, Any]] = []
    for node in items:
        if node and isinstance(node, dict) and (node.get("__typename") == "PullRequest" or "createdAt" in node):
            prs.append(node)
    if progress_callback:
        progress_callback("complete", len(prs))
    return prs


def _list_commits(
    owner: str, 
    repo: str, 
    token: str, 
    since_iso: str, 
    until_iso: str,
    progress_callback: Optional[Callable[[str, int], None]] = None,
) -> List[Dict[str, Any]]:
    commits: List[Dict[str, Any]] = []
    page = 1
    per_page = 100
    while True:
        if progress_callback:
            progress_callback("fetching", len(commits))
        url = f"{GITHUB_REST}/repos/{owner}/{repo}/commits"
        params = {
            "since": since_iso,
            "until": until_iso,
            "per_page": per_page,
            "page": page,
        }
        payload = _request_json("GET", url, token, params=params)
        batch = payload
        if not batch:
            break
        for commit in batch:
            commits.append(commit)
            if progress_callback:
                progress_callback("fetching", len(commits))
        page += 1
        time.sleep(0.2)
    if progress_callback:
        progress_callback("complete", len(commits))
    return commits


def _filter_out_bots(items: Iterable[Dict[str, Any]], bots: Set[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for it in items:
        author = None
        if "author" in it and isinstance(it["author"], dict):
            author = it["author"].get("login")
        elif "commit" in it and isinstance(it["commit"], dict):
            author = (it.get("author") or {}).get("login") or (it["commit"].get("author") or {}).get("email")
        if author and author in bots:
            continue
        out.append(it)
    return out


def _extract_bug_ids_from_prs(prs: Iterable[Dict[str, Any]]) -> Dict[int, List[int]]:
    """Return mapping of PR number -> list of unique ADO bug IDs referenced as AB#123."""
    pr_to_bugs: Dict[int, List[int]] = {}
    pattern = re.compile(r"AB#(\d+)", re.IGNORECASE)
    for pr in prs:
        title = str(pr.get("title") or "")
        numbers = []
        for match in pattern.finditer(title):
            try:
                n = int(match.group(1))
                if n not in numbers:
                    numbers.append(n)
            except ValueError:
                continue
        if numbers and pr.get("number") is not None:
            pr_to_bugs[int(pr["number"])] = numbers
    return pr_to_bugs


def collect_repository_data(
    owner: str,
    repo: str,
    since_iso: str,
    until_iso: str,
    token: str,
    bots: Iterable[str],
    cache_dir: Optional[str] = None,
    use_cache: bool = True,
    on_prs_progress: Optional[Callable[[str, int], None]] = None,
    on_commits_progress: Optional[Callable[[str, int], None]] = None,
    on_ado_progress: Optional[Callable[[str, int], None]] = None,
    ado_org: Optional[str] = None,
    ado_project: Optional[str] = None,
    ado_ready_states: Optional[Iterable[str]] = None,
    ado_resolved_states: Optional[Iterable[str]] = None,
    ado_qa_failed_states: Optional[Iterable[str]] = None,
    ado_disable: bool = False,
    ado_skip_lookup: bool = False,
) -> Dict[str, Any]:
    cache = Path(cache_dir) if cache_dir else None
    if cache:
        _ensure_dir(cache)

    cache_key_base = f"{owner}_{repo}_{since_iso[:10]}_{until_iso[:10]}"

    prs_key = f"prs_{cache_key_base}"
    commits_key = f"commits_{cache_key_base}"

    if use_cache and cache:
        cached_prs = _cache_get(cache, prs_key)
        cached_commits = _cache_get(cache, commits_key)
    else:
        cached_prs = None
        cached_commits = None

    if cached_prs is None:
        prs = _list_prs(owner, repo, token, since_iso, until_iso, on_prs_progress)
        if cache:
            _cache_set(cache, prs_key, {"items": prs})
    else:
        prs = cached_prs.get("items", [])
        if on_prs_progress:
            on_prs_progress("cached", len(prs))

    if cached_commits is None:
        commits = _list_commits(owner, repo, token, since_iso, until_iso, on_commits_progress)
        if cache:
            _cache_set(cache, commits_key, {"items": commits})
    else:
        commits = cached_commits.get("items", [])
        if on_commits_progress:
            on_commits_progress("cached", len(commits))

    prs = _filter_out_bots(prs, set(bots))
    commits = _filter_out_bots(commits, set(bots))

    pr_bug_map = _extract_bug_ids_from_prs(prs)
    pr_bug_ids: List[int] = []
    seen_pr_ids: Set[int] = set()
    for bug_ids in pr_bug_map.values():
        for bid in bug_ids:
            if bid not in seen_pr_ids:
                seen_pr_ids.add(bid)
                pr_bug_ids.append(bid)

    # Azure DevOps project collection (optional)
    azure_data: Dict[str, Any] = {}
    if ado_skip_lookup:
        if on_ado_progress:
            on_ado_progress("skipped", 0)
    elif not ado_disable and ado_org and ado_project and pr_bug_ids:
        try:
            azure_data = ado.collect_work_items_by_ids(
                org=ado_org,
                project=ado_project,
                ids=pr_bug_ids,
                since_iso=since_iso,
                until_iso=until_iso,
                cache_dir=cache_dir,
                use_cache=use_cache,
                ready_states=ado_ready_states,
                resolved_states=ado_resolved_states,
                qa_failed_states=ado_qa_failed_states,
                progress_callback=on_ado_progress,
            )
        except Exception as e:
            # Fallback for ADO failures (e.g. azure-cli bugs, auth issues)
            # We print to stderr to avoid breaking json output if used elsewhere, 
            # though here it's inside CLI.
            print(f"\n[Warning] ADO collection failed: {e}", file=sys.stderr)
            if on_ado_progress:
                on_ado_progress("disabled", 0)
            azure_data = {}
    elif on_ado_progress:
        on_ado_progress("disabled", 0)

    return {
        "owner": owner,
        "repo": repo,
        "since": since_iso,
        "until": until_iso,
        "pull_requests": prs,
        "commits": commits,
        "ado": {
            "org": ado_org,
            "project": ado_project,
            "ready_states": list(ado_ready_states) if ado_ready_states else [],
            "resolved_states": list(ado_resolved_states) if ado_resolved_states else [],
            "qa_failed_states": list(ado_qa_failed_states) if ado_qa_failed_states else [],
        },
        "ado_bug_items": [],
        "ado_pr_bugs": pr_bug_map,
        "azure": azure_data,
    }



