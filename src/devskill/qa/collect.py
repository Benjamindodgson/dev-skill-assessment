import datetime as dt
import json
import time
import random
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import quote

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


def _normalize_ado_datetime(dt_str: str) -> str:
    """Normalize various ISO8601 inputs to Azure DevOps-friendly format.

    Azure DevOps APIs expect UTC timestamps like YYYY-MM-DDTHH:MM:SSZ.
    This function parses common ISO8601 variants (including "+00:00" tz and
    microseconds) and returns a compact Zulu-time string without microseconds.
    """
    try:
        s = (dt_str or "").strip()
        if not s:
            return s
        # Support both trailing 'Z' and explicit timezone offset
        s = s.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(s)
        parsed_utc = parsed.astimezone(dt.timezone.utc).replace(microsecond=0)
        return parsed_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        # If parsing fails, fall back to original string
        return dt_str


def _list_test_runs(org: str, project: str, token: str, since_iso: str, until_iso: str, progress_callback: Optional[Callable[[str, int], None]] = None) -> List[Dict[str, Any]]:
    # Date filters use ISO; limit to top 1000 for performance
    url = f"https://dev.azure.com/{quote(org, safe='')}/{quote(project, safe='')}/_apis/test/runs"
    params = {
        "minLastUpdatedDate": _normalize_ado_datetime(since_iso),
        "maxLastUpdatedDate": _normalize_ado_datetime(until_iso),
        "$top": "1000",
        "api-version": "7.1-preview.1",
    }
    if progress_callback:
        progress_callback("fetching", 0)
    payload = _request_json("GET", url, token, params=params)
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
    url = f"https://dev.azure.com/{quote(org, safe='')}/{quote(project, safe='')}/_apis/test/Runs/{run_id}/results"
    params = {"$top": "1000", "api-version": "7.1-preview.6"}
    payload = _request_json("GET", url, token, params=params)
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


def _wiql_query_bugs(org: str, project: str, token: str, since_iso: str, until_iso: str, work_item_types: Optional[List[str]] = None) -> List[int]:
    url = f"https://dev.azure.com/{quote(org, safe='')}/{quote(project, safe='')}/_apis/wit/wiql"
    types = [t for t in (work_item_types or ["Bug"]) if t]
    if not types:
        types = ["Bug"]
    types_clause = ", ".join(f"'{t}'" for t in types)
    query = (
        "SELECT [System.Id] FROM WorkItems "
        f"WHERE [System.WorkItemType] IN ({types_clause}) "
        f"AND [System.CreatedDate] >= '{_normalize_ado_datetime(since_iso)}' AND [System.CreatedDate] <= '{_normalize_ado_datetime(until_iso)}' "
        "ORDER BY [System.ChangedDate] DESC"
    )
    payload = _request_json("POST", url, token, params={"api-version": "7.1-preview.2"}, json={"query": query})
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
        url = f"https://dev.azure.com/{quote(org, safe='')}/{quote(project, safe='')}/_apis/wit/workitems"
        params = {"ids": joined, "$expand": "Relations", "api-version": "7.1-preview.3"}
        payload = _request_json("GET", url, token, params=params)
        out.extend(payload.get("value", []))
        time.sleep(0.2)
    return out


def _work_item_comments(org: str, project: str, token: str, work_item_id: int) -> List[Dict[str, Any]]:
    url = f"https://dev.azure.com/{quote(org, safe='')}/{quote(project, safe='')}/_apis/wit/workItems/{work_item_id}/comments"
    params = {"api-version": "7.1-preview.3"}
    payload = _request_json("GET", url, token, params=params)
    comments = payload.get("value", [])
    out: List[Dict[str, Any]] = []
    for c in comments:
        author = c.get("revisedBy") or c.get("createdBy") or {}
        out.append({
            "id": c.get("id"),
            "text": c.get("text"),
            "createdDate": c.get("createdDate") or c.get("publishedDate") or c.get("revisedDate"),
            "revisedDate": c.get("revisedDate"),
            "author": {
                "displayName": (author or {}).get("displayName"),
                "uniqueName": (author or {}).get("uniqueName") or (author or {}).get("uniqueName"),
                "id": (author or {}).get("id"),
            },
        })
    return out


def _work_item_updates(org: str, project: str, token: str, work_item_id: int) -> List[Dict[str, Any]]:
    url = f"https://dev.azure.com/{quote(org, safe='')}/{quote(project, safe='')}/_apis/wit/workitems/{work_item_id}/updates"
    params = {"api-version": "7.1-preview.3"}
    payload = _request_json("GET", url, token, params=params)
    updates = payload.get("value", [])
    out: List[Dict[str, Any]] = []
    for u in updates:
        by = u.get("revisedBy") or {}
        out.append({
            "id": u.get("id"),
            "revisedDate": u.get("revisedDate"),
            "revisedBy": {
                "displayName": (by or {}).get("displayName"),
                "uniqueName": (by or {}).get("uniqueName"),
                "id": (by or {}).get("id"),
            },
            "fields": u.get("fields", {}),
        })
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
    bug_types: Optional[Iterable[str]] = None,
    story_types: Optional[Iterable[str]] = None,
    aliases: Optional[Dict[str, str]] = None,
    disable_user_filter: bool = False,
    debug_users: bool = False,
    debug_api: bool = False,
    force_run_ids: Optional[Iterable[int]] = None,
    cache_dir: Optional[str] = None,
    use_cache: bool = True,
    include_comments: bool = True,
    include_updates: bool = True,
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
    stories_key = f"qa_stories_{cache_key_base}"
    comments_key = f"qa_comments_{cache_key_base}"
    updates_key = f"qa_updates_{cache_key_base}"

    if use_cache and cache:
        cached_runs = _cache_get(cache, runs_key)
        cached_results = _cache_get(cache, results_key)
        cached_bugs = _cache_get(cache, bugs_key)
        cached_stories = _cache_get(cache, stories_key)
        cached_comments = _cache_get(cache, comments_key)
        cached_updates = _cache_get(cache, updates_key)
    else:
        cached_runs = None
        cached_results = None
        cached_bugs = None
        cached_stories = None
        cached_comments = None
        cached_updates = None

    project_for_api = project_id or project
    api_debug: Dict[str, Any] = {"project_attempts": []} if debug_api else {}

    if cached_runs is None:
        # Initialize defaults for both forced and discovered run flows
        runs: List[Dict[str, Any]] = []
        last_err: Optional[Exception] = None
        # If explicit run IDs are provided, honor them and skip listing runs
        if force_run_ids:
            runs = [{"id": int(rid)} for rid in force_run_ids if rid is not None]
            if on_runs_progress:
                on_runs_progress("complete", len(runs))
            if debug_api:
                api_debug["runs_forced"] = {"ids": [r.get("id") for r in runs]}
            if cache:
                _cache_set(cache, runs_key, {"items": runs})
        else:
            # Prefer project GUID when available, but some ADO Test APIs 404 on GUID path.
            # In that case, retry with the human-readable project name.
            attempts = []
            primary = project_id or project
            if primary:
                attempts.append(primary)
            if project_id and project and project != project_id:
                attempts.append(project)

            for idx, candidate in enumerate(attempts or [project]):
                try:
                    runs = _list_test_runs(org, candidate, token, since_iso, until_iso, on_runs_progress)
                    # If this candidate returned non-empty results, or this is the last attempt, accept it.
                    project_for_api = candidate
                    last_err = None
                    if runs or idx == (len(attempts or [project]) - 1):
                        break
                    # Otherwise, continue to try the next candidate (e.g., switch from GUID to name)
                    continue
                except requests.exceptions.HTTPError as e:  # type: ignore[attr-defined]
                    # Only fall back on 404; other errors should surface.
                    status = getattr(e, "response", None).status_code if getattr(e, "response", None) is not None else None
                    if status == 404:
                        last_err = e
                        continue
                    raise
                except Exception as e:
                    last_err = e
                    break

        if last_err:
            # If all attempts returned 404, treat as no Test Runs available for this project
            # (e.g., Test Plans disabled). Continue to collect bugs.
            status = getattr(last_err, "response", None).status_code if getattr(last_err, "response", None) is not None else None
            if status == 404:
                runs = []
                if on_runs_progress:
                    on_runs_progress("complete", 0)
            else:
                raise last_err

        if debug_api and not force_run_ids:
            api_debug["runs_request"] = {
                "org": org,
                "project_used": project_for_api,
                "minLastUpdatedDate": _normalize_ado_datetime(since_iso),
                "maxLastUpdatedDate": _normalize_ado_datetime(until_iso),
                "count": len(runs),
            }
        if cache and not force_run_ids:
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
            # Try with the chosen project identifier; on 404, retry with project name
            try:
                res = _list_test_results_for_run(org, project_for_api, int(run_id), token)
            except requests.exceptions.HTTPError as e:  # type: ignore[attr-defined]
                status = getattr(e, "response", None).status_code if getattr(e, "response", None) is not None else None
                if status == 404 and project and project != project_for_api:
                    res = _list_test_results_for_run(org, project, int(run_id), token)
                    project_for_api = project
                else:
                    raise
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
        # Similar to Test APIs, WIQL can behave differently for GUID vs name. Try both.
        bug_ids: List[int] = []
        wit_attempts: List[str] = []
        primary_wit = project_for_api or project
        if primary_wit:
            wit_attempts.append(primary_wit)
        if project and project != primary_wit:
            wit_attempts.append(project)

        wit_last_err: Optional[Exception] = None
        for idx, candidate in enumerate(wit_attempts or [project]):
            try:
                bug_ids = _wiql_query_bugs(org, candidate, token, since_iso, until_iso, list(bug_types or []))
                project_for_api = candidate
                wit_last_err = None
                # Accept if we found ids or this is the last attempt; otherwise try next candidate
                if bug_ids or idx == (len(wit_attempts or [project]) - 1):
                    break
                continue
            except requests.exceptions.HTTPError as e:  # type: ignore[attr-defined]
                status = getattr(e, "response", None).status_code if getattr(e, "response", None) is not None else None
                if status in (400, 404):
                    wit_last_err = e
                    continue
                raise
            except Exception as e:
                wit_last_err = e
                break

        if wit_last_err:
            status = getattr(wit_last_err, "response", None).status_code if getattr(wit_last_err, "response", None) is not None else None
            if status in (400, 404):
                bugs = []
                if on_bugs_progress:
                    on_bugs_progress("complete", 0)
                if cache:
                    _cache_set(cache, bugs_key, {"items": bugs})
            else:
                raise wit_last_err
        else:
            bugs = _work_items_batch(org, project_for_api, token, bug_ids) if bug_ids else []
            if on_bugs_progress:
                on_bugs_progress("complete", len(bugs))
            if debug_api:
                api_debug["bugs_request"] = {
                    "org": org,
                    "project_used": project_for_api,
                    "types": list(bug_types or []),
                    "created_from": _normalize_ado_datetime(since_iso),
                    "created_to": _normalize_ado_datetime(until_iso),
                    "ids_found": len(bug_ids),
                    "fetched": len(bugs),
                }
            if cache:
                _cache_set(cache, bugs_key, {"items": bugs})
    else:
        bugs = cached_bugs.get("items", [])
        if on_bugs_progress:
            on_bugs_progress("cached", len(bugs))

    # Stories (User Stories/PBIs/Features)
    if cached_stories is None:
        story_ids: List[int] = []
        stories: List[Dict[str, Any]] = []
        types_in = list(story_types or [])
        if types_in:
            attempts_s: List[str] = []
            primary_wit = project_for_api or project
            if primary_wit:
                attempts_s.append(primary_wit)
            if project and project != primary_wit:
                attempts_s.append(project)
            last_err_st: Optional[Exception] = None
            for idx, candidate in enumerate(attempts_s or [project]):
                try:
                    story_ids = _wiql_query_bugs(org, candidate, token, since_iso, until_iso, types_in)
                    project_for_api = candidate
                    last_err_st = None
                    if story_ids or idx == (len(attempts_s or [project]) - 1):
                        break
                    continue
                except requests.exceptions.HTTPError as e:  # type: ignore[attr-defined]
                    status = getattr(e, "response", None).status_code if getattr(e, "response", None) is not None else None
                    if status in (400, 404):
                        last_err_st = e
                        continue
                    raise
                except Exception as e:
                    last_err_st = e
                    break
            if last_err_st:
                status = getattr(last_err_st, "response", None).status_code if getattr(last_err_st, "response", None) is not None else None
                if status in (400, 404):
                    stories = []
                else:
                    raise last_err_st
            else:
                stories = _work_items_batch(org, project_for_api, token, story_ids) if story_ids else []
        else:
            stories = []
        if cache:
            _cache_set(cache, stories_key, {"items": stories})
    else:
        stories = cached_stories.get("items", [])

    # Comments and Updates for Bugs + Stories
    wi_ids_for_extras: List[int] = []
    if include_comments or include_updates:
        wi_ids_for_extras = []
        for b in bugs:
            if b.get("id") is not None:
                wi_ids_for_extras.append(int(b.get("id")))
        for s in stories:
            if s.get("id") is not None:
                wi_ids_for_extras.append(int(s.get("id")))
        # de-dup
        wi_ids_for_extras = sorted({int(x) for x in wi_ids_for_extras})

    work_item_comments: Dict[str, List[Dict[str, Any]]] = {}
    work_item_updates: Dict[str, List[Dict[str, Any]]] = {}

    if include_comments:
        if cached_comments is not None:
            work_item_comments = {str(k): v for k, v in (cached_comments.get("items", {}) or {}).items()}
        else:
            for wid in wi_ids_for_extras:
                try:
                    comments = _work_item_comments(org, project_for_api, token, wid)
                except requests.exceptions.HTTPError as e:  # type: ignore[attr-defined]
                    status = getattr(e, "response", None).status_code if getattr(e, "response", None) is not None else None
                    if status == 404 and project and project != project_for_api:
                        comments = _work_item_comments(org, project, token, wid)
                        project_for_api = project
                    else:
                        raise
                # Filter to window
                since_norm = _normalize_ado_datetime(since_iso)
                until_norm = _normalize_ado_datetime(until_iso)
                def within_window(d: str) -> bool:
                    try:
                        if not d:
                            return False
                        return since_norm <= _normalize_ado_datetime(d) <= until_norm
                    except Exception:
                        return True
                filtered = [c for c in comments if within_window(c.get("createdDate") or c.get("revisedDate") or "")]
                work_item_comments[str(wid)] = filtered
                time.sleep(0.1)
            if cache:
                _cache_set(cache, comments_key, {"items": work_item_comments})

    if include_updates:
        if cached_updates is not None:
            work_item_updates = {str(k): v for k, v in (cached_updates.get("items", {}) or {}).items()}
        else:
            for wid in wi_ids_for_extras:
                try:
                    updates = _work_item_updates(org, project_for_api, token, wid)
                except requests.exceptions.HTTPError as e:  # type: ignore[attr-defined]
                    status = getattr(e, "response", None).status_code if getattr(e, "response", None) is not None else None
                    if status == 404 and project and project != project_for_api:
                        updates = _work_item_updates(org, project, token, wid)
                        project_for_api = project
                    else:
                        raise
                # Filter to window
                since_norm = _normalize_ado_datetime(since_iso)
                until_norm = _normalize_ado_datetime(until_iso)
                def within_window_u(d: str) -> bool:
                    try:
                        if not d:
                            return False
                        return since_norm <= _normalize_ado_datetime(d) <= until_norm
                    except Exception:
                        return True
                filtered_u = [u for u in updates if within_window_u(u.get("revisedDate") or "")]
                work_item_updates[str(wid)] = filtered_u
                time.sleep(0.1)
            if cache:
                _cache_set(cache, updates_key, {"items": work_item_updates})

    # Filter to known QA users for results and bugs
    alias_map = {k.lower(): v for k, v in (aliases or {}).items()}
    qa_list = list(qa_users)

    def result_belongs_to_known(res: Dict[str, Any]) -> bool:
        tester = res.get("tester") or {}
        return _identity_matches(tester, qa_list, alias_map)

    def bug_belongs_to_known(bug: Dict[str, Any]) -> bool:
        fields = bug.get("fields", {})
        created_by = fields.get("System.CreatedBy") or {}
        return _identity_matches(created_by, qa_list, alias_map)

    # Build debug identity summaries before filtering
    identity_debug: Dict[str, Any] = {}
    if debug_users:
        seen_testers = {}
        for r in results:
            t = r.get("tester") or {}
            key = (str(t.get("uniqueName") or "").lower() or str(t.get("displayName") or "").lower() or str(t.get("id") or "").lower())
            if key not in seen_testers:
                seen_testers[key] = {
                    "displayName": t.get("displayName"),
                    "uniqueName": t.get("uniqueName"),
                    "id": t.get("id"),
                    "matches": _identity_matches(t, qa_list, alias_map),
                }
        seen_authors = {}
        for b in bugs:
            fields = b.get("fields", {})
            u = fields.get("System.CreatedBy") or {}
            key = (str(u.get("uniqueName") or "").lower() or str(u.get("displayName") or "").lower() or str(u.get("id") or "").lower())
            if key not in seen_authors:
                seen_authors[key] = {
                    "displayName": u.get("displayName"),
                    "uniqueName": u.get("uniqueName"),
                    "id": u.get("id"),
                    "matches": _identity_matches(u, qa_list, alias_map),
                }
        identity_debug = {
            "qa_users": list(qa_list),
            "aliases": alias_map,
            "testers": list(seen_testers.values()),
            "bug_authors": list(seen_authors.values()),
        }

    filtered_results = (
        results if disable_user_filter or not qa_list else [r for r in results if result_belongs_to_known(r)]
    )
    filtered_bugs = (
        bugs if disable_user_filter or not qa_list else [b for b in bugs if bug_belongs_to_known(b)]
    )

    return {
        "org": org,
        "project": project,
        "projectId": project_id,
        "since": since_iso,
        "until": until_iso,
        "test_runs": runs,
        "test_results": filtered_results,
        "bugs": filtered_bugs,
        "stories": stories,
        "work_item_comments": work_item_comments if include_comments else {},
        "work_item_updates": work_item_updates if include_updates else {},
        "qa_users": qa_list,
        **({"identity_debug": identity_debug} if debug_users else {}),
        **({"api_debug": api_debug} if debug_api else {}),
    }


