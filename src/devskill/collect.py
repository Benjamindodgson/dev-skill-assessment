import datetime as dt
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

import requests
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
            resp.raise_for_status()
            try:
                return resp.json()
            except json.JSONDecodeError:
                # Transient truncated responses
                if attempt < retries - 1:
                    time.sleep(backoff * (2 ** attempt))
                    continue
                raise
        except (req_exc.ChunkedEncodingError, req_exc.ConnectionError, req_exc.ReadTimeout, req_exc.SSLError) as e:
            if attempt < retries - 1:
                time.sleep(backoff * (2 ** attempt))
                continue
            raise e
    # Should not reach here
    raise RuntimeError("Request failed after retries")


def _paginate_graphql(query: str, variables: Dict[str, Any], token: str, root_path: List[str]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    cursor = None
    while True:
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
        items.extend([edge["node"] for edge in edges])
        page_info = node["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
        time.sleep(0.2)
    return items


def _list_prs(owner: str, repo: str, token: str, since_iso: str, until_iso: str) -> List[Dict[str, Any]]:
    # GraphQL query for PRs with reviews and timeline counts
    query = """
    query($owner: String!, $repo: String!, $cursor: String) {
      repository(owner: $owner, name: $repo) {
        pullRequests(first: 50, after: $cursor, orderBy: {field: CREATED_AT, direction: DESC}, states: [OPEN, MERGED, CLOSED]) {
          edges {
            node {
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
          pageInfo { hasNextPage endCursor }
        }
      }
    }
    """

    # Paginate and then filter by timeframe client-side to avoid missing older pages if <90 days
    variables = {
        "owner": owner,
        "repo": repo,
    }
    items = _paginate_graphql(query, variables, token, ["data", "repository", "pullRequests"])
    since = dt.datetime.fromisoformat(since_iso)
    until = dt.datetime.fromisoformat(until_iso)
    filtered: List[Dict[str, Any]] = []
    for pr in items:
        created = dt.datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))
        if since <= created <= until:
            filtered.append(pr)
    return filtered


def _list_commits(owner: str, repo: str, token: str, since_iso: str, until_iso: str) -> List[Dict[str, Any]]:
    commits: List[Dict[str, Any]] = []
    page = 1
    per_page = 100
    while True:
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
        commits.extend(batch)
        page += 1
        time.sleep(0.2)
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


def collect_repository_data(
    owner: str,
    repo: str,
    since_iso: str,
    until_iso: str,
    token: str,
    bots: Iterable[str],
    cache_dir: Optional[str] = None,
    use_cache: bool = True,
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
        prs = _list_prs(owner, repo, token, since_iso, until_iso)
        if cache:
            _cache_set(cache, prs_key, {"items": prs})
    else:
        prs = cached_prs.get("items", [])

    if cached_commits is None:
        commits = _list_commits(owner, repo, token, since_iso, until_iso)
        if cache:
            _cache_set(cache, commits_key, {"items": commits})
    else:
        commits = cached_commits.get("items", [])

    prs = _filter_out_bots(prs, set(bots))
    commits = _filter_out_bots(commits, set(bots))

    return {
        "owner": owner,
        "repo": repo,
        "since": since_iso,
        "until": until_iso,
        "pull_requests": prs,
        "commits": commits,
    }



