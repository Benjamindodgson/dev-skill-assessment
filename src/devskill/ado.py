import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple


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
        raise RuntimeError(f"Failed to parse az CLI JSON response: {e}") from e


def _fetch_work_item_updates(org_url: str, work_item_id: str) -> List[Dict]:
    """Fetch work item updates via az devops invoke (works with PAT or AAD auth)."""
    api_versions = ["7.1-preview.3", "7.1", "7.0"]
    last_exc: Optional[Exception] = None

    for api_ver in api_versions:
        try:
            payload = _run_az(
                [
                    "devops",
                    "invoke",
                    "--area",
                    "wit",
                    "--resource",
                    f"workitems/{work_item_id}/updates",
                    "--api-version",
                    api_ver,
                    "--organization",
                    org_url,
                ]
            )
            return payload.get("value", []) or []
        except RuntimeError as exc:
            last_exc = exc
            msg = str(exc).lower()
            # Azure CLI can choke on preview API versions when parsing as floats.
            if "could not convert string to float" in msg and api_ver != api_versions[-1]:
                continue
            raise

    if last_exc:
        raise last_exc
    return []


def _cache_path(cache_dir: Path, org: str, project: str) -> Path:
    safe_org = org.replace("/", "_").replace(":", "_")
    safe_project = project.replace("/", "_").replace(":", "_")
    return cache_dir / f"ado_workitems_{safe_org}_{safe_project}.json"


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
            item = _run_az(
                [
                    "boards",
                    "work-item",
                    "show",
                    "--id",
                    wid,
                    "--organization",
                    org_url,
                ]
            )
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

