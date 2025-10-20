import datetime as dt
import json
import time
import random
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import requests


def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
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
    headers = kwargs.pop("headers", {})
    merged_headers = {**_headers(token), **headers}
    for attempt in range(retries):
        try:
            resp = requests.request(method, url, headers=merged_headers, timeout=timeout, **kwargs)
            if resp.status_code in (429, 503):
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    time.sleep(min(int(retry_after) + 1, 60))
                    continue
            if 500 <= resp.status_code < 600:
                if attempt < retries - 1:
                    sleep_s = backoff * (2 ** attempt)
                    jitter = random.uniform(0, sleep_s * 0.25)
                    time.sleep(sleep_s + jitter)
                    continue
            resp.raise_for_status()
            try:
                return resp.json()
            except json.JSONDecodeError:
                if attempt < retries - 1:
                    sleep_s = backoff * (2 ** attempt)
                    jitter = random.uniform(0, sleep_s * 0.25)
                    time.sleep(sleep_s + jitter)
                    continue
                raise
        except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout, requests.exceptions.SSLError) as e:
            if attempt < retries - 1:
                sleep_s = backoff * (2 ** attempt)
                jitter = random.uniform(0, sleep_s * 0.25)
                time.sleep(sleep_s + jitter)
                continue
            raise e
    raise RuntimeError("Request failed after retries")


def _list_test_runs(org: str, project: str, token: str, since_iso: str, until_iso: str, progress_callback: Optional[Callable[[str, int], None]] = None) -> List[Dict[str, Any]]:
    # Date filters use ISO; limit to top 1000 for performance
    url = (
        f"https://dev.azure.com/{org}/{project}/_apis/test/runs"
        f"?minLastUpdatedDate={since_iso}&maxLastUpdatedDate={until_iso}&$top=1000&api-version=7.1-preview.1"
    )
    if progress_callback:
        progress_callback("fetching", 0)
    payload = _request_json("GET", url, token)
    runs = payload.get("value", [])
    if progress_callback:
        progress_callback("complete", len(runs))
    # Minimal fields
    out: List[Dict[str, Any]] = []
    for r in runs:
        out.append({
            "id": r.get("id"),
            "name": r.get("name"),
            "state": r.get("state"),
            "startedDate": r.get("startedDate"),
            "completedDate": r.get("completedDate"),
            "isAutomated": r.get("isAutomated"),
        })
    return out


def _list_test_results_for_run(org: str, project: str, run_id: int, token: str) -> List[Dict[str, Any]]:
    # Note: results endpoint is preview; cap at 1000
    url = f"https://dev.azure.com/{org}/{project}/_apis/test/Runs/{run_id}/results?$top=1000&api-version=7.1-preview.6"
    payload = _request_json("GET", url, token)
    results = payload.get("value", [])
    out: List[Dict[str, Any]] = []
    for res in results:
        # Normalize tester identity
        tester = res.get("owner") or res.get("tester") or {}
        out.append({
            "id": res.get("id"),
            "outcome": res.get("outcome"),
            "durationInMs": res.get("durationInMs"),
            "startedDate": res.get("startedDate"),
            "completedDate": res.get("completedDate"),
            "testCaseTitle": res.get("testCaseTitle"),
            "automatedTestStorage": res.get("automatedTestStorage"),
            "runId": run_id,
            "tester": {
                "displayName": (tester or {}).get("displayName"),
                "uniqueName": (tester or {}).get("uniqueName"),
                "id": (tester or {}).get("id"),
            },
        })
    return out


def _wiql_query_bugs(org: str, project: str, token: str, since_iso: str, until_iso: str) -> List[int]:
    url = f"https://dev.azure.com/{org}/{project}/_apis/wit/wiql?api-version=7.1-preview.2"
    query = (
        "SELECT [System.Id] FROM WorkItems "
        "WHERE [System.WorkItemType] = 'Bug' "
        f"AND [System.CreatedDate] >= '{since_iso}' AND [System.CreatedDate] <= '{until_iso}' "
        "ORDER BY [System.ChangedDate] DESC"
    )
    payload = _request_json("POST", url, token, json={"query": query})
    work_items = payload.get("workItems", [])
    return [int(wi.get("id")) for wi in work_items if wi.get("id")]


def _work_items_batch(org: str, project: str, token: str, ids: List[int]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    # Batch up to 200 ids per request
    for i in range(0, len(ids), 200):
        chunk = ids[i : i + 200]
        if not chunk:
            continue
        joined = ",".join(str(x) for x in chunk)
        url = (
            f"https://dev.azure.com/{org}/{project}/_apis/wit/workitems"
            f"?ids={joined}&$expand=Relations&api-version=7.1-preview.3"
        )
        payload = _request_json("GET", url, token)
        out.extend(payload.get("value", []))
        time.sleep(0.2)
    return out


def _identity_matches(user: Dict[str, Any], candidates: Iterable[str], aliases: Dict[str, str]) -> bool:
    display = str(user.get("displayName") or "").lower()
    unique = str(user.get("uniqueName") or "").lower()
    uid = str(user.get("id") or "").lower()
    names: List[str] = [display, unique, uid]
    # apply aliases mapping
    lower_alias = {k.lower(): v.lower() for k, v in aliases.items()}
    for name in list(names):
        if name in lower_alias:
            names.append(lower_alias[name])
    cand_lower = {str(c).lower() for c in candidates}
    return any(n and n in cand_lower for n in names)


def collect_qa_data(
    org: str,
    project: str,
    project_id: str,
    since_iso: str,
    until_iso: str,
    token: str,
    qa_users: Iterable[str],
    cache_dir: Optional[str] = None,
    use_cache: bool = True,
    on_runs_progress: Optional[Callable[[str, int], None]] = None,
    on_bugs_progress: Optional[Callable[[str, int], None]] = None,
) -> Dict[str, Any]:
    cache = Path(cache_dir) if cache_dir else None
    if cache:
        _ensure_dir(cache)

    cache_key_base = f"{org}_{project}_{since_iso[:10]}_{until_iso[:10]}"
    runs_key = f"qa_runs_{cache_key_base}"
    results_key = f"qa_results_{cache_key_base}"
    bugs_key = f"qa_bugs_{cache_key_base}"

    if use_cache and cache:
        cached_runs = _cache_get(cache, runs_key)
        cached_results = _cache_get(cache, results_key)
        cached_bugs = _cache_get(cache, bugs_key)
    else:
        cached_runs = None
        cached_results = None
        cached_bugs = None

    if cached_runs is None:
        runs = _list_test_runs(org, project, token, since_iso, until_iso, on_runs_progress)
        if cache:
            _cache_set(cache, runs_key, {"items": runs})
    else:
        runs = cached_runs.get("items", [])
        if on_runs_progress:
            on_runs_progress("cached", len(runs))

    # Collect results for each run
    results: List[Dict[str, Any]]
    if cached_results is None:
        results = []
        count = 0
        if on_runs_progress:
            on_runs_progress("fetching", 0)
        for r in runs:
            run_id = r.get("id")
            if run_id is None:
                continue
            res = _list_test_results_for_run(org, project, int(run_id), token)
            results.extend(res)
            count += len(res)
            if on_runs_progress:
                on_runs_progress("fetching", count)
            time.sleep(0.2)
        if on_runs_progress:
            on_runs_progress("complete", count)
        if cache:
            _cache_set(cache, results_key, {"items": results})
    else:
        results = cached_results.get("items", [])
        if on_runs_progress:
            on_runs_progress("cached", len(results))

    # Bugs
    if cached_bugs is None:
        if on_bugs_progress:
            on_bugs_progress("fetching", 0)
        bug_ids = _wiql_query_bugs(org, project, token, since_iso, until_iso)
        bugs = _work_items_batch(org, project, token, bug_ids)
        if on_bugs_progress:
            on_bugs_progress("complete", len(bugs))
        if cache:
            _cache_set(cache, bugs_key, {"items": bugs})
    else:
        bugs = cached_bugs.get("items", [])
        if on_bugs_progress:
            on_bugs_progress("cached", len(bugs))

    # Filter to known QA users for results and bugs
    aliases = {}
    qa_list = list(qa_users)

    def result_belongs_to_known(res: Dict[str, Any]) -> bool:
        tester = res.get("tester") or {}
        return _identity_matches(tester, qa_list, aliases)

    def bug_belongs_to_known(bug: Dict[str, Any]) -> bool:
        fields = bug.get("fields", {})
        created_by = fields.get("System.CreatedBy") or {}
        return _identity_matches(created_by, qa_list, aliases)

    filtered_results = [r for r in results if result_belongs_to_known(r)] if qa_list else results
    filtered_bugs = [b for b in bugs if bug_belongs_to_known(b)] if qa_list else bugs

    return {
        "org": org,
        "project": project,
        "projectId": project_id,
        "since": since_iso,
        "until": until_iso,
        "test_runs": runs,
        "test_results": filtered_results,
        "bugs": filtered_bugs,
        "qa_users": qa_list,
    }


