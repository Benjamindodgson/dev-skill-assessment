import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.panel import Panel
from rich.box import ROUNDED


AAD_RESOURCE_DEVOPS = "499b84ac-1321-427f-aa17-267ca6975798"


def _get_ado_access_token() -> str:
    env_token = os.environ.get("AZDO_AUTH_TOKEN", "").strip()
    if env_token:
        return env_token
    # Ask az for an AAD token for Azure DevOps resource (service GUID)
    import subprocess

    proc = subprocess.run(
        ["az", "account", "get-access-token", "--resource", AAD_RESOURCE_DEVOPS],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError("Azure login required. Run `az login` and retry.")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("Failed to parse Azure access token response. Ensure `az` is installed and you're logged in.")
    token = payload.get("accessToken", "").strip()
    if not token:
        raise RuntimeError("No access token returned. Ensure `az login` succeeded for your account.")
    return token


def _ado_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _list_projects(org: str, token: str) -> List[Dict[str, Any]]:
    url = f"https://dev.azure.com/{quote(org, safe='')}/_apis/projects?stateFilter=all&$top=1000&api-version=7.1-preview.4"
    resp = requests.get(url, headers=_ado_headers(token), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def _prompt_org() -> str:
    try:
        s = input("Organization (e.g., dev.azure.com/YourOrg or YourOrg): ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        sys.exit(1)
    if not s:
        print("Organization is required.")
        return _prompt_org()
    # normalize input like https://dev.azure.com/YourOrg
    s = s.replace("https://", "").replace("http://", "")
    s = s.replace("dev.azure.com/", "")
    return s


def _prompt_project_choice(projects: List[Dict[str, Any]]) -> Tuple[str, str]:
    if not projects:
        print("No projects available in this organization.")
        sys.exit(1)
    print(f"\nFound {len(projects)} projects:")
    print("-" * 80)
    for idx, p in enumerate(projects, 1):
        print(f"{idx:4d}. {p.get('name','')}  [{p.get('id','')[:8]}…]")
    print("-" * 80)
    while True:
        try:
            sel = input(f"Select project number (1-{len(projects)}): ").strip()
            i = int(sel) - 1
            if 0 <= i < len(projects):
                choice = projects[i]
                return choice.get("name", ""), choice.get("id", "")
            print("Out of range.")
        except ValueError:
            print("Enter a number.")
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            sys.exit(1)


def _prompt_days() -> int:
    while True:
        try:
            s = input("\nNumber of days to analyze [90]: ").strip()
            if not s:
                return 90
            v = int(s)
            if v > 0:
                return v
            print("Please enter a positive number.")
        except ValueError:
            print("Please enter a valid number.")
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            sys.exit(1)


def _prompt_output_dir(default: str) -> str:
    try:
        s = input(f"\nOutput directory [{default}]: ").strip()
        return s if s else default
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        sys.exit(1)


def _prompt_qa_users(defaults: List[str]) -> List[str]:
    print("\nKnown QA users (comma-separated emails or display names). Press Enter to accept.")
    try:
        s = input(f"Users [{', '.join(defaults)}]: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        sys.exit(1)
    if not s:
        return defaults
    return [x.strip() for x in s.split(",") if x.strip()]


def _load_config(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        p = Path.cwd() / "config.yml"
        if not p.exists():
            return {}
    else:
        p = Path(path)
        if not p.exists():
            return {}
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _print_terminal_summary(console: Console, scores: Dict[str, Any], org: str, project: str, since_iso: str, until_iso: str, named: bool = True) -> None:
    team = scores.get("team", {})
    people = scores.get("people", []) or scores.get("qa", []) or scores.get("developers", [])

    title = f"{org}/{project} — {since_iso[:10]} → {until_iso[:10]}"
    team_score = float(team.get("score", 0.0))
    console.print(
        Panel.fit(
            f"Team Score: {team_score:.2f}",
            title="[bold]QASkill Assessment[/bold]",
            subtitle=title,
            border_style="magenta",
            box=ROUNDED,
        )
    )

    from rich.table import Table

    table = Table(box=ROUNDED, header_style="bold magenta", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("QA", justify="left")
    table.add_column("Score", justify="right")
    table.add_column("Testing", justify="right")
    table.add_column("Defects", justify="right")
    table.add_column("Hygiene", justify="right")
    table.add_column("Responsiveness", justify="right")

    for idx, row in enumerate(people, 1):
        name = row.get("person") or row.get("qa") or row.get("developer") or "unknown"
        name = name if named else "qa-***"
        subs = row.get("subscores", {})
        table.add_row(
            str(idx),
            str(name),
            f"{float(row.get('score', 0.0)):.2f}",
            f"{float(subs.get('testing', 0.0)):.2f}",
            f"{float(subs.get('defects', 0.0)):.2f}",
            f"{float(subs.get('hygiene', 0.0)):.2f}",
            f"{float(subs.get('responsiveness', 0.0)):.2f}",
        )

    console.print(table)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser("qaskill")
    subparsers = parser.add_subparsers(dest="command")

    rerun_p = subparsers.add_parser("rerun", help="Regenerate QA reports from last saved raw data")
    rerun_p.add_argument("--raw", default=None, help="Path to qa-skill-raw-*.json (optional)")
    rerun_p.add_argument("--tag", default=None, help="Tag to use for regenerated reports (optional)")
    rerun_p.add_argument("--anonymous", action="store_true", help="Generate anonymized QA names")
    parser.add_argument("--az-org", required=False, help="Azure DevOps org (e.g., YourOrg or dev.azure.com/YourOrg)")
    parser.add_argument("--az-project", required=False, help="Azure DevOps project name")
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--qa-users", default=None, help="Comma-separated list of QA users")
    parser.add_argument("--outdir", default=None, help="Output directory (default: ./reports)")
    parser.add_argument("--config", default=None)
    parser.add_argument("--debug-users", action="store_true", help="Include identity matching debug in raw output")
    parser.add_argument("--debug-api", action="store_true", help="Include API request parameters in raw output")
    parser.add_argument("--no-user-filter", action="store_true", help="Do not filter by qa users; include all testers and authors")
    parser.add_argument("--run-id", action="append", type=int, help="Force inclusion of specific Test Run ID(s); can be passed multiple times")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args(argv)

    # Rerun subcommand
    if getattr(args, "command", None) == "rerun":
        console = Console()
        console.print("[bold magenta]Rerunning QA from last raw data[/bold magenta]")
        raw_path: Optional[Path]
        if getattr(args, "raw", None):
            raw_path = Path(args.raw)
            if not raw_path.exists():
                console.print(f"[red]Raw file not found:[/red] {raw_path}")
                return 2
        else:
            # Find most recent qa-skill-raw-*.json under reports/
            search_root = Path.cwd() / "reports"
            candidates = sorted(search_root.rglob("qa-skill-raw-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            raw_path = candidates[0] if candidates else None
            if not raw_path:
                console.print("[red]No qa-skill-raw-*.json found.[/red]")
                return 2
        try:
            data = json.loads(raw_path.read_text(encoding="utf-8"))
        except Exception as e:
            console.print(f"[red]Failed to read raw JSON:[/red] {e}")
            return 2

        org = data.get("org") or data.get("owner") or ""
        project = data.get("project") or data.get("repo") or ""
        since_iso = data.get("since")
        until_iso = data.get("until")
        if not all([org, project, since_iso, until_iso]):
            console.print("[red]Raw JSON missing required fields (org, project, since, until).[/red]")
            return 2

        cfg = _load_config(args.config)

        outdir_path = Path(args.outdir) if args.outdir else raw_path.parent
        outdir_path.mkdir(parents=True, exist_ok=True)

        tag = args.tag or f"{org}-{project}-{dt.datetime.utcnow():%Y-%m-%d}"

        # Compute metrics and generate reports
        from . import metrics as qa_metrics
        from . import report as qa_report
        from . import insights as qa_insights

        console.print("\n[bold yellow]Computing QA metrics and scores...[/bold yellow]")
        scores = qa_metrics.compute_scores(data=data, org=org, project=project, since_iso=since_iso, until_iso=until_iso, config=cfg.get("qa") or {})

        report_subdir = outdir_path / f"{project}, {dt.datetime.fromisoformat(until_iso.replace('Z','+00:00')).strftime('%B %d, %Y')}"
        report_subdir.mkdir(parents=True, exist_ok=True)

        console.print("[bold magenta]Generating QA reports...[/bold magenta]")
        qa_report.generate_reports(scores=scores, outdir=str(outdir_path), org=org, project=project, since_iso=since_iso, until_iso=until_iso, named=not getattr(args, "anonymous", False), tag=tag)
        qa_insights.generate_insights(scores=scores, data=data, outdir=str(outdir_path), org=org, project=project, since_iso=since_iso, until_iso=until_iso, tag=tag)
        _print_terminal_summary(console=console, scores=scores, org=org, project=project, since_iso=since_iso, until_iso=until_iso, named=not getattr(args, "anonymous", False))
        console.print(f"\n[bold green]✓ QA reports regenerated in {report_subdir}[/bold green]")
        return 0

    # Interactive/CLI mode
    org = (args.az_org or "").strip() or _prompt_org()

    try:
        token = _get_ado_access_token()
        # Probe by listing projects (also used for interactive selection)
        projects = _list_projects(org, token)
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 2

    if args.az_project:
        project_name = args.az_project
        project_id = next((p.get("id") for p in projects if p.get("name") == project_name), "")
        if not project_id:
            print(f"Project '{project_name}' not found in org '{org}'.")
            return 2
    else:
        project_name, project_id = _prompt_project_choice(projects)

    days = args.days if args.days is not None else _prompt_days()
    now = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
    since = now - dt.timedelta(days=days)
    since_iso, until_iso = since.isoformat(), now.isoformat()

    default_outdir = str(Path.cwd() / "reports")
    outdir = args.outdir if args.outdir is not None else _prompt_output_dir(default_outdir)
    cfg = _load_config(args.config)
    qa_cfg = cfg.get("qa") or {}
    default_users = list(qa_cfg.get("users", []))
    if args.qa_users:
        default_users = [x.strip() for x in args.qa_users.split(",") if x.strip()]
    qa_users = _prompt_qa_users(default_users)
    bug_types = qa_cfg.get("bug_types") or ["Bug", "Defect"]
    story_types = qa_cfg.get("story_types") or ["Product Backlog Item", "Bug", "Feature"]
    qa_states = qa_cfg.get("qa_states") or ["QA Failed", "Ready for Test", "Ready for QA Build", "In Test"]
    include_comments = bool(qa_cfg.get("include_comments", True))
    include_updates = bool(qa_cfg.get("include_updates", True))
    alias_map = qa_cfg.get("aliases") or {}

    # Ensure cache/out dirs
    outdir_path = Path(outdir)
    outdir_path.mkdir(parents=True, exist_ok=True)
    cache_dir = Path.cwd() / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    console = Console()
    console.print(f"[bold magenta]Collecting Azure DevOps QA data for {org}/{project_name}[/bold magenta]")
    console.print(f"[dim]Date range: {since_iso[:10]} to {until_iso[:10]}[/dim]\n")

    # Progress UI
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        runs_task = progress.add_task("[cyan]Fetching test runs/results...", total=None)
        bugs_task = progress.add_task("[green]Fetching bugs...", total=None)

        def on_runs_progress(status: str, count: int):
            if status == "fetching":
                progress.update(runs_task, description=f"[cyan]Fetching test runs/results... ({count} fetched)")
            elif status == "complete":
                progress.update(runs_task, completed=100, total=100, description=f"[cyan]✓ Test runs/results fetched ({count} total)")
            elif status == "cached":
                progress.update(runs_task, completed=100, total=100, description=f"[cyan]✓ Test runs/results (cached: {count} total)")

        def on_bugs_progress(status: str, count: int):
            if status == "fetching":
                progress.update(bugs_task, description=f"[green]Fetching bugs... ({count} fetched)")
            elif status == "complete":
                progress.update(bugs_task, completed=100, total=100, description=f"[green]✓ Bugs fetched ({count} total)")
            elif status == "cached":
                progress.update(bugs_task, completed=100, total=100, description=f"[green]✓ Bugs (cached: {count} total)")

        from . import collect as qa_collect

        data = qa_collect.collect_qa_data(
            org=org,
            project=project_name,
            project_id=project_id,
            since_iso=since_iso,
            until_iso=until_iso,
            token=token,
            qa_users=qa_users,
            bug_types=bug_types,
            story_types=story_types,
            aliases=alias_map,
            disable_user_filter=getattr(args, "no_user_filter", False),
            debug_users=getattr(args, "debug_users", False),
            debug_api=getattr(args, "debug_api", False),
            force_run_ids=getattr(args, "run_id", None),
            cache_dir=str(cache_dir),
            use_cache=not args.no_cache,
            include_comments=include_comments,
            include_updates=include_updates,
            on_runs_progress=on_runs_progress,
            on_bugs_progress=on_bugs_progress,
        )

    tag = f"{org}-{project_name}-{now:%Y-%m-%d}"
    folder_name = f"{project_name}, {dt.datetime.fromisoformat(until_iso.replace('Z','+00:00')).strftime('%B %d, %Y')}"
    report_subdir = outdir_path / folder_name
    report_subdir.mkdir(parents=True, exist_ok=True)

    raw_path = report_subdir / f"qa-skill-raw-{tag}.json"
    raw_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    console.print(f"[dim]Wrote QA raw data → {raw_path}[/dim]")

    from . import metrics as qa_metrics
    from . import report as qa_report
    from . import insights as qa_insights

    console.print("\n[bold yellow]Computing QA metrics and scores...[/bold yellow]")
    scores = qa_metrics.compute_scores(
        data=data,
        org=org,
        project=project_name,
        since_iso=since_iso,
        until_iso=until_iso,
        config=qa_cfg,
    )

    console.print("[bold magenta]Generating QA reports...[/bold magenta]")
    qa_report.generate_reports(
        scores=scores,
        outdir=str(outdir_path),
        org=org,
        project=project_name,
        since_iso=since_iso,
        until_iso=until_iso,
        named=True,
        tag=tag,
    )
    qa_insights.generate_insights(
        scores=scores,
        data=data,
        outdir=str(outdir_path),
        org=org,
        project=project_name,
        since_iso=since_iso,
        until_iso=until_iso,
        tag=tag,
    )
    _print_terminal_summary(console=console, scores=scores, org=org, project=project_name, since_iso=since_iso, until_iso=until_iso, named=True)
    console.print(f"\n[bold green]✓ QA reports written to {report_subdir}[/bold green]")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


