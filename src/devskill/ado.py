import json
import os
import subprocess
import sys
import datetime as dt
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


def _normalize_org(org: str) -> str:
    """Return an org URL acceptable to Azure DevOps CLI."""
    if not org:
        return org
    org = org.strip()
    if org.startswith("http://"):
        org = org.replace("http://", "https://", 1)
    if org.startswith("https://"):
        return org.rstrip("/")
    # Accept bare org name
    return f"https://dev.azure.com/{org}"


def _run_az(args: List[str], env: Optional[Dict[str, str]] = None) -> Dict:
    """Run az CLI and return parsed JSON, raising on failure."""
    cmd = ["az"] + args + ["-o", "json"]
    merged_env = os.environ.copy()
    # Avoid writing Azure CLI command logs to locations that may be unwritable in sandboxes.
    merged_env.setdefault("AZURE_CORE_NO_LOG_FILE", "1")
    if env:
        merged_env.update(env)
    proc = subprocess.run(cmd, capture_output=True, text=True, env=merged_env)
    if proc.returncode != 0:
        raise RuntimeError(f"Azure DevOps CLI failed: {' '.join(cmd)}\n{proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as e:
        output = proc.stdout or ""
        if "<html" in output.lower() or "sign in" in output.lower():
            raise RuntimeError(
                "Azure DevOps CLI returned a sign-in page. "
                "Please authenticate using `az login` or check your ADO credentials/permissions."
            ) from e
        # Include the stdout in the error message for debugging
        raise RuntimeError(f"Failed to parse az CLI JSON response: {e}\nOutput was: {output!r}") from e


_DEFAULT_API_VERSIONS = ("6.0",)
# Azure DevOps resource ID for az rest so we get the correct AAD token
_ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"


def _fetch_work_item_updates(org_url: str, work_item_id: str) -> List[Dict]:
    """Fetch work item updates via az rest (bypassing az devops invoke issues)."""
    # Use api-version 6.0 as a stable baseline
    url = f"{org_url}/_apis/wit/workitems/{work_item_id}/updates?api-version=6.0"
    try:
        payload = _run_az(
            ["rest", "--method", "get", "--url", url, "--resource", _ADO_RESOURCE]
        )
        return payload.get("value", []) or []
    except RuntimeError as e:
        # If 404 or similar, just return empty updates?
        # The original code caught Exception and warned.
        print(f"Warning: failed to fetch updates for work item {work_item_id}: {e}", file=sys.stderr)
        return []


def _fetch_work_item(org_url: str, work_item_id: str) -> Dict[str, Any]:
    """Fetch a work item with relations via az rest."""
    url = f"{org_url}/_apis/wit/workitems/{work_item_id}?$expand=Relations&api-version=6.0"
    return _run_az(["rest", "--method", "get", "--url", url, "--resource", _ADO_RESOURCE])


def _cache_path(cache_dir: Path, org: str, project: str) -> Path:
    safe_org = org.replace("/", "_").replace(":", "_")
    safe_project = project.replace("/", "_").replace(":", "_")
    return cache_dir / f"ado_workitems_{safe_org}_{safe_project}.json"


def _ids_cache_path(cache_dir: Path, org: str, project: str, since: str, until: str) -> Path:
    safe_org = org.replace("/", "_").replace(":", "_")
    safe_project = project.replace("/", "_").replace(":", "_")
    safe_since = since.replace(":", "_")
    safe_until = until.replace(":", "_")
    return cache_dir / f"ado_workitem_ids_{safe_org}_{safe_project}_{safe_since}_{safe_until}.json"


def _load_cache(cache_dir: Optional[str], org: str, project: str) -> Dict[str, Dict]:
    if not cache_dir:
        return {}
    p = _cache_path(Path(cache_dir), org, project)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache_dir: Optional[str], org: str, project: str, data: Dict[str, Dict]) -> None:
    if not cache_dir:
        return
    p = _cache_path(Path(cache_dir), org, project)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load_ids_cache(cache_dir: Optional[str], org: str, project: str, since: str, until: str) -> List[int]:
    if not cache_dir:
        return []
    p = _ids_cache_path(Path(cache_dir), org, project, since, until)
    if not p.exists():
        return []
    try:
        payload = json.loads(p.read_text(encoding="utf-8")) or {}
        return [int(x) for x in payload.get("ids", [])]
    except Exception:
        return []


def _save_ids_cache(cache_dir: Optional[str], org: str, project: str, since: str, until: str, ids: List[int]) -> None:
    if not cache_dir:
        return
    p = _ids_cache_path(Path(cache_dir), org, project, since, until)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"ids": ids}, indent=2), encoding="utf-8")


def _parse_iso_dt(value: str) -> dt.datetime:
    if not value:
        return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    normalized = value.replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(normalized)
    except Exception:
        return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def _list_iterations(org_url: str, project: str) -> List[Dict]:
    try:
        payload = _run_az(
            [
                "boards",
                "iteration",
                "project",
                "list",
                "--project",
                project,
                "--organization",
                org_url,
            ]
        )
        if isinstance(payload, list):
            return payload
        return payload.get("value", []) or []
    except Exception:
        return []


def _query_work_item_ids(org_url: str, project: str, since_iso: str, until_iso: str) -> List[int]:
    """Query work item IDs in the project changed between since/until (chunked to avoid WIQL caps)."""
    since_dt = _parse_iso_dt(since_iso)
    until_dt = _parse_iso_dt(until_iso)
    if since_dt > until_dt:
        since_dt, until_dt = until_dt, since_dt

    ids: List[int] = []
    seen = set()

    chunk_days = 21
    cursor = since_dt
    while cursor <= until_dt:
        chunk_end = min(cursor + dt.timedelta(days=chunk_days), until_dt)
        # Azure WIQL date precision rejects timestamps; use date-only strings.
        cursor_date = cursor.date().isoformat()
        chunk_end_date = chunk_end.date().isoformat()
        query = (
            "Select [System.Id] From WorkItems "
            f"Where [System.TeamProject] = '{project}' "
            f"AND [System.CreatedDate] >= '{cursor_date}' "
            f"AND [System.CreatedDate] <= '{chunk_end_date}' "
            "Order By [System.CreatedDate]"
        )
        try:
            payload = _run_az(
                [
                    "boards",
                    "query",
                    "--wiql",
                    query,
                    "--organization",
                    org_url,
                    "--project",
                    project,
                ]
            )
            work_items = []
            if isinstance(payload, dict):
                work_items = payload.get("workItems", []) or []
            elif isinstance(payload, list):
                work_items = payload
            for wi in work_items:
                try:
                    wid = int(wi.get("id"))
                    if wid not in seen:
                        seen.add(wid)
                        ids.append(wid)
                except Exception:
                    continue
        except RuntimeError as exc:
            msg = str(exc).lower()
            # If WIQL caps are hit, shorten chunk size and retry.
            if "top 200" in msg or "work items limit" in msg:
                chunk_days = max(7, chunk_days // 2)
                continue
            raise

        # Bump to next calendar day to avoid overlapping date-bounded queries.
        cursor = chunk_end + dt.timedelta(days=1)

    return ids


def collect_project_items(
    org: str,
    project: str,
    since_iso: str,
    until_iso: str,
    cache_dir: Optional[str] = None,
    use_cache: bool = True,
    ready_states: Optional[Iterable[str]] = None,
    resolved_states: Optional[Iterable[str]] = None,
    qa_failed_states: Optional[Iterable[str]] = None,
    progress_callback: Optional[Callable[[str, int], None]] = None,
) -> Dict[str, Any]:
    """Collect all project work items (with updates) and iterations for the window."""
    org_url = _normalize_org(org)

    if progress_callback:
        progress_callback("fetching-ids", 0)
    ids = _load_ids_cache(cache_dir, org_url, project, since_iso, until_iso) if use_cache else []
    if not ids:
        ids = _query_work_item_ids(org_url, project, since_iso, until_iso)
        _save_ids_cache(cache_dir, org_url, project, since_iso, until_iso, ids)
    if progress_callback:
        progress_callback("ids", len(ids))

    items = fetch_work_items(
        ids=ids,
        org=org,
        project=project,
        cache_dir=cache_dir,
        use_cache=use_cache,
        progress_callback=progress_callback,
    ) if ids else []

    iterations = _list_iterations(org_url, project)

    since_dt = _parse_iso_dt(since_iso)
    until_dt = _parse_iso_dt(until_iso)
    filtered_items: List[Dict[str, Any]] = []
    for item in items:
        created_raw = (item.get("fields") or {}).get("System.CreatedDate") or ""
        created_dt = _parse_iso_dt(str(created_raw))
        if since_dt <= created_dt <= until_dt:
            filtered_items.append(item)
    items = filtered_items

    return {
        "org": org,
        "project": project,
        "since": since_iso,
        "until": until_iso,
        "ready_states": list(ready_states) if ready_states else [],
        "resolved_states": list(resolved_states) if resolved_states else [],
        "qa_failed_states": list(qa_failed_states) if qa_failed_states else [],
        "work_items": items,
        "iterations": iterations,
        "count": len(items),
    }


def collect_work_items_by_ids(
    org: str,
    project: str,
    ids: Iterable[int],
    since_iso: str,
    until_iso: str,
    cache_dir: Optional[str] = None,
    use_cache: bool = True,
    ready_states: Optional[Iterable[str]] = None,
    resolved_states: Optional[Iterable[str]] = None,
    qa_failed_states: Optional[Iterable[str]] = None,
    progress_callback: Optional[Callable[[str, int], None]] = None,
) -> Dict[str, Any]:
    """Collect specific work items (and updates) by explicit IDs, plus iterations."""
    org_url = _normalize_org(org)
    project_name = project

    # Deduplicate and normalize IDs
    unique_ids: List[int] = []
    seen: set[int] = set()
    for raw in ids:
        try:
            wid = int(raw)
        except Exception:
            continue
        if wid not in seen:
            seen.add(wid)
            unique_ids.append(wid)

    if progress_callback:
        progress_callback("ids", len(unique_ids))

    items = fetch_work_items(
        ids=unique_ids,
        org=org,
        project=project,
        cache_dir=cache_dir,
        use_cache=use_cache,
        progress_callback=progress_callback,
    ) if unique_ids else []

    iterations = _list_iterations(org_url, project_name)

    return {
        "org": org,
        "project": project,
        "since": since_iso,
        "until": until_iso,
        "ready_states": list(ready_states) if ready_states else [],
        "resolved_states": list(resolved_states) if resolved_states else [],
        "qa_failed_states": list(qa_failed_states) if qa_failed_states else [],
        "work_items": items,
        "iterations": iterations,
        "count": len(items),
    }


def fetch_work_items(
    ids: Iterable[int],
    org: str,
    project: str,
    cache_dir: Optional[str] = None,
    use_cache: bool = True,
    progress_callback: Optional[Callable[[str, int], None]] = None,
) -> List[Dict]:
    """Fetch work items and updates via Azure DevOps CLI."""
    org_url = _normalize_org(org)
    project_name = project
    cache = _load_cache(cache_dir, org_url, project_name) if use_cache else {}

    results: List[Dict] = []
    dirty = False
    if progress_callback:
        progress_callback("fetching", 0)
    all_cached = True
    for raw_id in ids:
        wid = str(raw_id)
        if use_cache and wid in cache:
            results.append(cache[wid])
            if progress_callback:
                progress_callback("cached", len(results))
            continue

        all_cached = False
        try:
            item = _fetch_work_item(org_url=org_url, work_item_id=wid)
        except RuntimeError as exc:
            msg = str(exc)
            if "does not exist" in msg or "do not have permissions" in msg:
                print(f"Warning: skipping Azure DevOps work item {wid}: {msg}", file=sys.stderr)
                continue
            raise

        try:
            updates = _fetch_work_item_updates(org_url=org_url, work_item_id=wid)
        except Exception as exc:
            print(f"Warning: failed to fetch updates for work item {wid}: {exc}", file=sys.stderr)
            updates = []

        payload = {"id": item.get("id"), "fields": item.get("fields", {}), "relations": item.get("relations", []), "updates": updates}
        cache[wid] = payload
        results.append(payload)
        dirty = True
        if progress_callback:
            progress_callback("fetching", len(results))

    if dirty:
        _save_cache(cache_dir, org_url, project_name, cache)
    if progress_callback:
        status = "cached" if all_cached else "complete"
        progress_callback(status, len(results))
    return results


def ensure_cli_latest() -> None:
    """Ensure Azure CLI is up-to-date."""
    try:
        subprocess.run(["az", "upgrade", "--yes"], check=True, capture_output=True)
    except Exception as e:
        # Many environments (Homebrew, etc.) manage az externally and 'az upgrade' fails.
        # We warn but don't hard-fail.
        print(f"Warning: Failed to auto-update Azure CLI: {e}", file=sys.stderr)


def ensure_logged_in() -> None:
    """Ensure we have an active Azure CLI session, prompting login if needed."""
    try:
        # Check if we have an account
        subprocess.run(["az", "account", "show"], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        print("Azure CLI not logged in. Launching browser login...", file=sys.stderr)
        # Interactive login
        subprocess.run(["az", "login"], check=True)

