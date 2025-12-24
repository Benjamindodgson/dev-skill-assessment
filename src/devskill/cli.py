import argparse
import datetime as dt
import json
import os
import re
import time
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional, Set

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table
from rich.panel import Panel
from rich.box import ROUNDED

from . import collect, metrics, report, insights, azure_metrics, ado

REPO_RE = re.compile(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$")
 


def parse_repo_input(s: str) -> Tuple[str, str]:
    m = REPO_RE.search(s)
    if m:
        return m.group("owner"), m.group("repo")
    if "/" in s:
        owner, repo = s.split("/", 1)
        return owner, repo
    raise ValueError("Expected 'owner/repo' or a GitHub URL")


def _parse_csv_arg(value: Optional[str]) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _load_config(path: Any, verbose: bool = True) -> Dict[str, Any]:
    # Auto-discover config.yml if not specified
    auto_discovered = False
    if not path:
        default_config = Path.cwd() / "config.yml"
        if default_config.exists():
            path = default_config
            auto_discovered = True
        else:
            return {}
    p = Path(path)
    if not p.exists():
        return {}
    if yaml is None:  # pragma: no cover
        print("PyYAML not installed; ignoring --config", file=sys.stderr)
        return {}
    with p.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
        if verbose and auto_discovered:
            print(f"[config] Auto-discovered and loaded: {p}")
        return config


def _get_github_token() -> str:
    """Get GitHub token from gh CLI or GITHUB_TOKEN env var."""
    # Try gh CLI first
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    
    # Fall back to environment variable
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    
    raise RuntimeError(
        "No GitHub authentication found. Either:\n"
        "  1. Install and authenticate with GitHub CLI: gh auth login\n"
        "  2. Set GITHUB_TOKEN environment variable"
    )


def _get_github_username(token: str) -> str:
    """Get the authenticated user's GitHub username."""
    query = """
    query {
        viewer {
            login
        }
    }
    """
    data = collect._request_json(
        "POST",
        collect.GITHUB_GRAPHQL,
        token,
        timeout=10,
        json={"query": query},
    )
    return data["data"]["viewer"]["login"]


def _list_user_repos(token: str) -> List[Dict[str, Any]]:
    """Fetch all repositories the authenticated user has access to via GitHub GraphQL API."""
    repos = []
    cursor = None
    
    while True:
        after_clause = f', after: "{cursor}"' if cursor else ""
        query = f"""
        query {{
            viewer {{
                repositories(first: 100{after_clause}, affiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {{
                    nodes {{
                        owner {{
                            login
                        }}
                        name
                        description
                    }}
                    pageInfo {{
                        hasNextPage
                        endCursor
                    }}
                }}
            }}
        }}
        """
        data = collect._request_json(
            "POST",
            collect.GITHUB_GRAPHQL,
            token,
            timeout=30,
            json={"query": query},
        )
        
        nodes = data["data"]["viewer"]["repositories"]["nodes"]
        repos.extend(nodes)
        
        page_info = data["data"]["viewer"]["repositories"]["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
        # Gentle pacing to avoid secondary rate limits
        time.sleep(0.2)
    
    return repos


def _prompt_authentication() -> str:
    """Prompt for GitHub authentication, checking if already authenticated."""
    try:
        token = _get_github_token()
        username = _get_github_username(token)
        print(f"✓ Already authenticated as {username}")
        return token
    except RuntimeError:
        print("\n⚠️  GitHub authentication required")
        print("Please authenticate using one of these methods:")
        print("  1. GitHub CLI: gh auth login")
        print("  2. Environment variable: export GITHUB_TOKEN=your_token")
        print("\nPress Enter after authenticating...")
        input()
        # Retry after user authenticates
        return _get_github_token()


def _prompt_repo_selection(token: str) -> List[Tuple[str, str]]:
    """Display available repos and prompt user to select one or more."""
    print("\nFetching your repositories...")
    repos = _list_user_repos(token)
    
    if not repos:
        print("No repositories found.")
        sys.exit(1)
    
    print(f"\nFound {len(repos)} repositories:")
    print("-" * 80)
    
    for idx, repo in enumerate(repos, 1):
        owner = repo["owner"]["login"]
        name = repo["name"]
        description = repo["description"] or "No description"
        # Truncate description if too long
        if len(description) > 60:
            description = description[:57] + "..."
        print(f"{idx:4d}. {owner}/{name:30s} - {description}")
    
    print("-" * 80)
    
    while True:
        try:
            selection = input(f"\nSelect repository number(s) (1-{len(repos)}), separated by commas or spaces: ").strip()
            parts = [p for p in re.split(r"[\\s,]+", selection) if p]
            if not parts:
                print("Please enter at least one number.")
                continue
            try:
                indices = [int(p) for p in parts]
            except ValueError:
                print("Invalid input. Please enter numbers separated by commas or spaces.")
                continue
            if any(idx < 1 or idx > len(repos) for idx in indices):
                print(f"Please enter numbers between 1 and {len(repos)}")
                continue
            selections: List[Tuple[str, str]] = []
            seen: set[int] = set()
            for idx in indices:
                if idx in seen:
                    continue
                seen.add(idx)
                selected = repos[idx - 1]
                owner = selected["owner"]["login"]
                name = selected["name"]
                selections.append((owner, name))
            if len(selections) == 1:
                owner, name = selections[0]
                print(f"✓ Selected: {owner}/{name}")
            else:
                joined = ", ".join(f"{o}/{n}" for o, n in selections)
                print(f"✓ Selected: {joined}")
            return selections
        except KeyboardInterrupt:
            print("\nInvalid input. Please enter a number.")
        except EOFError:
            print("\nAborted.")
            sys.exit(1)


def _prompt_days() -> int:
    """Prompt for number of days to analyze."""
    while True:
        try:
            response = input("\nNumber of days to analyze [90]: ").strip()
            if not response:
                return 90
            days = int(response)
            if days > 0:
                return days
            print("Please enter a positive number.")
        except ValueError:
            print("Please enter a valid number.")
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            sys.exit(1)


def _prompt_output_dir(default: str) -> str:
    """Prompt for output directory."""
    try:
        response = input(f"\nOutput directory [{default}]: ").strip()
        return response if response else default
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        sys.exit(1)


def _prompt_config_file() -> Any:
    """Prompt for config file path."""
    try:
        response = input("\nConfig file path (press Enter to skip): ").strip()
        return response if response else None
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        sys.exit(1)


def _discover_latest_raw(search_root: Optional[str]) -> Optional[Path]:
    """Find the most recent dev-skill-raw-*.json under the given root or defaults.

    Search order when search_root is None:
      1) ./reports (if exists)
    Select by newest modification time.
    """
    candidates: List[Path] = []
    roots: List[Path] = []
    if search_root:
        roots.append(Path(search_root))
    else:
        default_reports = Path.cwd() / "reports"
        if default_reports.exists():
            roots.append(default_reports)

    for root in roots:
        try:
            for p in root.rglob("dev-skill-raw-*.json"):
                if p.is_file():
                    candidates.append(p)
        except Exception:
            # Ignore permission or traversal errors and continue
            continue

    if not candidates:
        return None
    # Pick latest by mtime
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _derive_tag_from_raw_filename(p: Path) -> Optional[str]:
    name = p.name
    prefix = "dev-skill-raw-"
    suffix = ".json"
    if name.startswith(prefix) and name.endswith(suffix):
        return name[len(prefix) : -len(suffix)]
    return None


def _print_terminal_summary(
    console: Console,
    scores: Dict[str, Any],
    owner: str,
    repo: str,
    since_iso: str,
    until_iso: str,
    named: bool = True,
) -> None:
    team = scores.get("team", {})
    devs = scores.get("developers", [])

    title = f"{owner}/{repo} — {since_iso[:10]} → {until_iso[:10]}"
    team_score = float(team.get("score", 0.0))
    console.print(
        Panel.fit(
            f"Team Score: {team_score:.2f}",
            title="[bold]DevSkill Assessment[/bold]",
            subtitle=title,
            border_style="cyan",
            box=ROUNDED,
        )
    )

    table = Table(box=ROUNDED, header_style="bold cyan", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("Developer", justify="left")
    table.add_column("Score", justify="right")
    table.add_column("Delivery", justify="right")
    table.add_column("Collaboration", justify="right")
    table.add_column("Hygiene", justify="right")
    table.add_column("Stability", justify="right")

    for idx, row in enumerate(devs, 1):
        name = row.get("developer", "unknown") if named else "dev-***"
        subs = row.get("subscores", {})
        table.add_row(
            str(idx),
            str(name),
            f"{float(row.get('score', 0.0)):.2f}",
            f"{float(subs.get('delivery', 0.0)):.2f}",
            f"{float(subs.get('collaboration', 0.0)):.2f}",
            f"{float(subs.get('hygiene', 0.0)):.2f}",
            f"{float(subs.get('stability', 0.0)):.2f}",
        )

    console.print(table)


def _print_azure_summary(console: Console, assessment: Optional[Dict[str, Any]]) -> None:
    if not assessment:
        return
    team = assessment.get("team", {})
    metrics_map = team.get("metrics", {})
    pillars = team.get("pillars", {})
    counts = assessment.get("counts", {})
    table = Table(box=ROUNDED, header_style="bold magenta", show_lines=False)
    table.add_column("Metric", justify="left")
    table.add_column("Value", justify="right")
    table.add_column("Score", justify="right")

    def fmt_pct(v: float) -> str:
        return f"{100.0 * v:.1f}%"

    def fmt_duration_hours(hours: Any) -> str:
        """Format hours as days and remaining hours for readability."""
        try:
            total_hours = float(hours or 0.0)
        except Exception:
            total_hours = 0.0
        sign = "-" if total_hours < 0 else ""
        total_hours = abs(total_hours)
        days = int(total_hours // 24)
        rem_hours = round(total_hours - days * 24, 1)
        if rem_hours >= 24.0:  # handle rounding edge case
            days += 1
            rem_hours = 0.0
        return f"{sign}{days}d {rem_hours:.1f}h"

    table.add_row(
        "Resolution (median)",
        fmt_duration_hours(metrics_map.get("resolution_hours_median", 0.0)),
        f"{float(pillars.get('resolution', 0.0)):.1f}",
    )
    table.add_row(
        "Predictability (resolved rate)",
        fmt_pct(metrics_map.get("predictability_rate", 0.0)),
        f"{float(pillars.get('predictability', 0.0)):.1f}",
    )
    table.add_row(
        "Velocity (pts/wk)",
        f"{metrics_map.get('velocity_points_per_week', 0.0):.2f}",
        f"{float(pillars.get('velocity', 0.0)):.1f}",
    )
    table.add_row(
        "Quality (QA pass rate)",
        fmt_pct(metrics_map.get("quality_pass_rate", 0.0)),
        f"{float(pillars.get('quality', 0.0)):.1f}",
    )

    subtitle = f"{team.get('org','')}/{team.get('project','')} — {team.get('since','')[:10]} → {team.get('until','')[:10]}"
    console.print(
        Panel.fit(
            table,
            title=f"[bold magenta]Azure Assessment[/bold magenta] Score: {float(team.get('score', 0.0)):.2f}",
            subtitle=subtitle,
            border_style="magenta",
            box=ROUNDED,
        )
    )
    console.print(
        f"[dim]Items analyzed: {counts.get('considered', 0)} | Resolved: {counts.get('resolved', 0)} | "
        f"Excluded (unassigned/emails): {counts.get('excluded', 0)}[/dim]"
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser("devskill")
    subparsers = p.add_subparsers(dest="command")
    # Rerun subcommand (re-generate from existing raw JSON)
    rerun_p = subparsers.add_parser("rerun", help="Regenerate reports from last saved raw data")
    rerun_p.add_argument("--raw", default=None, help="Path to dev-skill-raw-*.json (optional)")
    rerun_p.add_argument("--tag", default=None, help="Tag to use for regenerated reports (optional)")
    rerun_p.add_argument("--anonymous", action="store_true", help="Generate anonymized developer names")
    p.add_argument("--repo-url", required=False, help="owner/repo or GitHub URL")
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--outdir", default=None, help="Output directory (default: ./reports)")
    p.add_argument("--config", default=None)
    p.add_argument("--no-cache", action="store_true")
    # Azure DevOps / Azure assessment
    p.add_argument("--ado-org", default=None, help="Azure DevOps org (e.g., dev.azure.com/YourOrg or YourOrg)")
    p.add_argument("--ado-project", default=None, help="Azure DevOps project name")
    p.add_argument("--ado-ready-states", default=None, help="Comma-separated Ready state names")
    p.add_argument("--ado-resolved-states", default=None, help="Comma-separated Resolved/Done state names")
    p.add_argument("--ado-qa-failed-states", default=None, help="Comma-separated QA Failed state names")
    p.add_argument("--ado-disable", action="store_true", help="Skip Azure DevOps enrichment/assessment")
    p.add_argument("--azure-exclude-emails", default=None, help="Comma-separated emails to exclude from Azure assessment")
    args = p.parse_args(argv)

    # Handle rerun subcommand: reuse last raw JSON, recompute metrics, regenerate reports/insights
    if getattr(args, "command", None) == "rerun":
        console = Console()
        console.print("[bold cyan]Rerunning from last raw data[/bold cyan]")

        raw_path: Optional[Path]
        if getattr(args, "raw", None):
            raw_path = Path(args.raw)
            if not raw_path.exists():
                console.print(f"[red]Raw file not found:[/red] {raw_path}")
                return 2
        else:
            # Search using provided outdir as root if given; else defaults
            raw_path = _discover_latest_raw(args.outdir)
            if not raw_path:
                console.print("[red]No dev-skill-raw-*.json found.[/red]")
                console.print("Hint: pass --raw PATH or --outdir DIR to search in.")
                return 2

        try:
            with raw_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            console.print(f"[red]Failed to read raw JSON:[/red] {e}")
            return 2

        owner = data.get("owner")
        repo = data.get("repo")
        since_iso = data.get("since")
        until_iso = data.get("until")
        if not all([owner, repo, since_iso, until_iso]):
            console.print("[red]Raw JSON missing required fields (owner, repo, since, until).[/red]")
            return 2

        cfg = _load_config(args.config)
        azure_cfg = cfg.get("azure") or {}
        azure_assessment = None

        # Determine output directory and tag
        outdir_path = Path(args.outdir) if args.outdir else raw_path.parent
        outdir_path.mkdir(parents=True, exist_ok=True)

        tag = args.tag or _derive_tag_from_raw_filename(raw_path) or f"{owner}-{repo}-{dt.datetime.utcnow():%Y-%m-%d}"

        console.print(f"[dim]Using raw:[/dim] {raw_path}")
        console.print(f"[dim]Output dir:[/dim] {outdir_path}")
        console.print("\n[bold yellow]Computing metrics and scores...[/bold yellow]")

        scores = metrics.compute_scores(
            data=data,
            owner=owner,
            repo=repo,
            since_iso=since_iso,
            until_iso=until_iso,
            config=cfg,
        )
        if data.get("azure"):
            azure_assessment = azure_metrics.compute_scores(
                data=data.get("azure") or {},
                config=azure_cfg,
            )

        # Create dated subfolder for reports
        folder_name = report._fmt_folder_name(repo, until_iso)
        report_subdir = outdir_path / folder_name
        report_subdir.mkdir(parents=True, exist_ok=True)

        console.print("[bold magenta]Generating reports...[/bold magenta]")
        report.generate_reports(
            scores=scores,
            outdir=str(outdir_path),
            owner=owner,
            repo=repo,
            since_iso=since_iso,
            until_iso=until_iso,
            named=not getattr(args, "anonymous", False),
            tag=tag,
        )
        insights.generate_insights(
            scores=scores,
            data=data,
            outdir=str(outdir_path),
            owner=owner,
            repo=repo,
            since_iso=since_iso,
            until_iso=until_iso,
            small_pr_threshold=float(((cfg.get("hygiene") or {}).get("small_pr_lines_threshold")) or 300),
            tag=tag,
        )
        _print_terminal_summary(
            console=console,
            scores=scores,
            owner=owner,
            repo=repo,
            since_iso=since_iso,
            until_iso=until_iso,
            named=not getattr(args, "anonymous", False),
        )
        _print_azure_summary(console=console, assessment=azure_assessment)
        console.print(f"\n[bold green]✓ Reports regenerated in {report_subdir}[/bold green]")
        return 0

    # Interactive mode: if no repo-url is provided
    if args.repo_url is None:
        print("=" * 80)
        print("DevSkill Assessment - Interactive Mode")
        print("=" * 80)
        
        # Prompt for authentication
        try:
            token = _prompt_authentication()
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 2
        
        # Prompt for repository selection
        selections = _prompt_repo_selection(token)
        
        # Prompt for other parameters
        days = _prompt_days()
        
        # Default output directory inside the repository workspace
        now = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
        default_outdir = str(Path.cwd() / "reports")
        outdir = _prompt_output_dir(default_outdir)
        config_path = _prompt_config_file()
        
        print("\n" + "=" * 80)
        print("Starting assessment...")
        print("=" * 80 + "\n")
    else:
        # CLI mode: use provided arguments
        owner, repo = parse_repo_input(args.repo_url.strip())
        selections = [(owner, repo)]
        try:
            token = _get_github_token()
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 2
        
        days = args.days if args.days is not None else 90
        
        # Default output directory inside the repository workspace
        now = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
        default_outdir = str(Path.cwd() / "reports")
        outdir = args.outdir if args.outdir is not None else default_outdir
        config_path = args.config

    # Calculate date range (reuse 'now' from interactive mode if already set)
    if 'now' not in locals():
        now = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
    since = now - dt.timedelta(days=days)
    since_iso, until_iso = since.isoformat(), now.isoformat()

    cfg = _load_config(config_path)
    bots = list(cfg.get("bots", []))
    ado_cfg = cfg.get("ado") or {}
    azure_cfg = cfg.get("azure") or {}

    # Azure/Ado config resolution
    ado_org = args.ado_org or ado_cfg.get("org")
    ado_project = args.ado_project or ado_cfg.get("project")
    ado_ready_states = _parse_csv_arg(args.ado_ready_states) or list(ado_cfg.get("ready_states") or [])
    ado_resolved_states = _parse_csv_arg(args.ado_resolved_states) or list(ado_cfg.get("resolved_states") or [])
    ado_qa_failed_states = _parse_csv_arg(args.ado_qa_failed_states) or list(ado_cfg.get("qa_failed_states") or [])
    ado_disable = bool(args.ado_disable or not (ado_org and ado_project))

    azure_org = ado_org or azure_cfg.get("org")
    azure_project = ado_project or azure_cfg.get("project")
    azure_ready_states = _parse_csv_arg(args.ado_ready_states) or list(azure_cfg.get("ready_states") or ado_ready_states)
    azure_resolved_states = _parse_csv_arg(args.ado_resolved_states) or list(azure_cfg.get("resolved_states") or ado_resolved_states)
    azure_qa_failed_states = _parse_csv_arg(args.ado_qa_failed_states) or list(azure_cfg.get("qa_failed_states") or ado_qa_failed_states)
    azure_exclude_emails = {e.lower() for e in (_parse_csv_arg(args.azure_exclude_emails) or azure_cfg.get("exclude_emails") or [])}
    azure_enabled = bool(not args.ado_disable and azure_org and azure_project)

    outdir_path = Path(outdir)
    outdir_path.mkdir(parents=True, exist_ok=True)

    cache_dir = Path.cwd() / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    console = Console()

    combined_prs: List[Dict[str, Any]] = []
    combined_commits: List[Dict[str, Any]] = []
    combined_bug_ids: Set[int] = set()
    combined_pr_bug_map: Dict[str, Dict[int, List[int]]] = {}
    selected_labels = [f"{o}/{r}" for o, r in selections]

    for owner, repo in selections:
        console.print(f"[bold cyan]Collecting GitHub data for {owner}/{repo}[/bold cyan]")
        console.print(f"[dim]Date range: {since_iso[:10]} to {until_iso[:10]}[/dim]\n")
        
        # Set up Rich progress display for this repo
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            pr_task = progress.add_task(f"[cyan]{owner}/{repo}: Fetching pull requests...", total=None)
            commit_task = progress.add_task(f"[green]{owner}/{repo}: Fetching commits...", total=None)
            
            def on_prs_progress(status: str, count: int):
                if status == "fetching":
                    progress.update(pr_task, description=f"[cyan]{owner}/{repo}: Fetching pull requests... ({count} fetched)")
                elif status == "complete":
                    progress.update(pr_task, completed=100, total=100, description=f"[cyan]{owner}/{repo}: ✓ Pull requests fetched ({count} total)")
                elif status == "cached":
                    progress.update(pr_task, completed=100, total=100, description=f"[cyan]{owner}/{repo}: ✓ Pull requests (cached: {count} total)")
            
            def on_commits_progress(status: str, count: int):
                if status == "fetching":
                    progress.update(commit_task, description=f"[green]{owner}/{repo}: Fetching commits... ({count} fetched)")
                elif status == "complete":
                    progress.update(commit_task, completed=100, total=100, description=f"[green]{owner}/{repo}: ✓ Commits fetched ({count} total)")
                elif status == "cached":
                    progress.update(commit_task, completed=100, total=100, description=f"[green]{owner}/{repo}: ✓ Commits (cached: {count} total)")

            # Collect per-repo GitHub data; defer Azure fetch until after all repos are processed.
            data = collect.collect_repository_data(
                owner=owner,
                repo=repo,
                since_iso=since_iso,
                until_iso=until_iso,
                token=token,
                bots=bots,
                cache_dir=str(cache_dir),
                use_cache=not args.no_cache,
                on_prs_progress=on_prs_progress,
                on_commits_progress=on_commits_progress,
                on_ado_progress=None,
                ado_org=ado_org,
                ado_project=ado_project,
                ado_ready_states=ado_ready_states,
                ado_resolved_states=ado_resolved_states,
                ado_qa_failed_states=ado_qa_failed_states,
                ado_disable=True,  # defer Azure fetch
            )
        
        console.print()

        combined_prs.extend(data.get("pull_requests", []))
        combined_commits.extend(data.get("commits", []))

        pr_bug_map = data.get("ado_pr_bugs") or {}
        combined_pr_bug_map[f"{owner}/{repo}"] = pr_bug_map
        for bug_ids in pr_bug_map.values():
            for bid in bug_ids:
                try:
                    combined_bug_ids.add(int(bid))
                except Exception:
                    continue

    # After all repos are processed, fetch Azure items once using the combined AB# set.
    azure_data: Dict[str, Any] = {}
    if azure_enabled and combined_bug_ids:
        console.print(f"[bold magenta]Fetching Azure work items for combined AB# IDs ({len(combined_bug_ids)})[/bold magenta]")
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            azure_task = progress.add_task("[magenta]Fetching Azure work items...", total=None)
            azure_total: Optional[int] = None

            def on_azure_progress(status: str, count: int):
                nonlocal azure_total
                if status == "ids":
                    azure_total = count
                if status in ("skipped", "disabled"):
                    progress.update(
                        azure_task,
                        completed=100,
                        total=100,
                        description=f"[magenta]Azure work items {status}",
                    )
                    return
                total_known = azure_total is not None and azure_total > 0
                display_current = count + 1 if status not in ("complete",) else count
                if total_known and display_current > azure_total:
                    display_current = azure_total
                count_part = f"{display_current}/{azure_total}" if total_known else f"{display_current}"
                desc = "[magenta]Fetching Azure work items..."
                if status == "fetching-ids":
                    desc = f"[magenta]Fetching Azure work item IDs... ({count_part})"
                elif status in ("fetching", "complete", "cached", "ids"):
                    desc = f"[magenta]Fetching Azure work items... ({count_part})"
                if status in ("complete", "cached"):
                    progress.update(
                        azure_task,
                        completed=100,
                        total=100,
                        description=f"[magenta]✓ Azure work items ({count} total)",
                    )
                else:
                    progress.update(azure_task, description=desc)

            azure_data = ado.collect_work_items_by_ids(
                org=azure_org,
                project=azure_project,
                ids=combined_bug_ids,
                since_iso=since_iso,
                until_iso=until_iso,
                cache_dir=str(cache_dir),
                use_cache=not args.no_cache,
                ready_states=azure_ready_states,
                resolved_states=azure_resolved_states,
                qa_failed_states=azure_qa_failed_states,
                progress_callback=on_azure_progress,
            )
        console.print()
    elif azure_enabled:
        console.print("[magenta]No AB# IDs found across selected repos; skipping Azure fetch.[/magenta]")

    # Build combined dataset
    combined_data: Dict[str, Any] = {
        "owner": "combined",
        "repo": "combined",
        "since": since_iso,
        "until": until_iso,
        "repos": selected_labels,
        "pull_requests": combined_prs,
        "commits": combined_commits,
        "ado_pr_bugs": combined_pr_bug_map,
        "azure": azure_data,
        "ado": {
            "org": ado_org,
            "project": ado_project,
            "ready_states": list(ado_ready_states),
            "resolved_states": list(ado_resolved_states),
            "qa_failed_states": list(ado_qa_failed_states),
        },
    }

    azure_assessment: Optional[Dict[str, Any]] = None
    if azure_enabled and azure_data:
        azure_assessment = azure_metrics.compute_scores(
            data=azure_data,
            config={**azure_cfg, "exclude_emails": list(azure_exclude_emails)},
        )
        combined_data["azure_assessment"] = azure_assessment

    tag = f"combined-{now:%Y-%m-%d}"
    folder_name = report._fmt_folder_name("combined", until_iso)
    report_subdir = outdir_path / folder_name
    report_subdir.mkdir(parents=True, exist_ok=True)

    raw_path = report_subdir / f"dev-skill-raw-{tag}.json"
    with raw_path.open("w", encoding="utf-8") as f:
        json.dump(combined_data, f, indent=2, sort_keys=True)
    console.print(f"[dim]Wrote combined raw data → {raw_path}[/dim]")

    console.print("\n[bold yellow]Computing combined metrics and scores...[/bold yellow]")
    scores = metrics.compute_scores(
        data=combined_data,
        owner="combined",
        repo="combined",
        since_iso=since_iso,
        until_iso=until_iso,
        config=cfg,
    )

    console.print("[bold magenta]Generating combined reports...[/bold magenta]")
    report.generate_reports(
        scores=scores,
        outdir=str(outdir_path),
        owner="combined",
        repo="combined",
        since_iso=since_iso,
        until_iso=until_iso,
        named=True,
        tag=tag,
    )
    insights.generate_insights(
        scores=scores,
        data=combined_data,
        outdir=str(outdir_path),
        owner="combined",
        repo="combined",
        since_iso=since_iso,
        until_iso=until_iso,
        small_pr_threshold=float(((cfg.get("hygiene") or {}).get("small_pr_lines_threshold")) or 300),
        tag=tag,
    )
    _print_terminal_summary(
        console=console,
        scores=scores,
        owner="combined",
        repo="combined",
        since_iso=since_iso,
        until_iso=until_iso,
        named=True,
    )
    _print_azure_summary(console=console, assessment=azure_assessment)
    console.print(f"\n[bold green]✓ Combined reports written to {report_subdir}[/bold green]\n")
    return 0


