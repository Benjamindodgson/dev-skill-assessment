# Dev Skill Assessment CLI

Run a 90-day GitHub developer assessment against any repository and export Markdown and JSON reports.

## Install

```bash
pip install git+https://github.com/Benjamindodgson/dev-skill-assessment.git
```

## Usage

### Interactive Mode (Recommended)

Simply run devskill without any arguments for an interactive experience:

```bash
devskill
```

The tool will guide you through:
1. **Authentication** - Automatically checks if you're already authenticated with GitHub CLI or `GITHUB_TOKEN`
2. **Repository Selection** - Lists all repositories you have access to (owned, collaborated, or organization repos)
3. **Analysis Period** - Prompts for number of days to analyze (default: 90)
4. **Output Directory** - Where to save reports (default: `reports`)
5. **Config File** - Optional configuration file path

### CLI Mode (For Automation)

For scripting and CI/CD, use command-line arguments:

```bash
devskill \
  --repo-url https://github.com/owner/repo \
  --days 90 \
  --outdir reports \
  --config config.yml
```

**Authentication:** The tool uses GitHub CLI authentication by default. For CI/automation, you can set `GITHUB_TOKEN` environment variable instead. Run `gh auth login` to authenticate if needed.

**CLI Arguments:**
- `--repo-url`: accepts `owner/repo`, HTTPS, or SSH GitHub URLs
- `--days`: time window (default 90)
- `--outdir`: output directory (default `reports`)
- `--config`: optional YAML for aliases/bots/weights (see `config.yml`)
- `--no-cache`: disable local API response caching

Outputs include repository and date in filenames, e.g. `dev-skill-assessment-OWNER-REPO-YYYY-MM-DD.md` and `dev-skill-assessment-insights-OWNER-REPO-YYYY-MM-DD.md`.

### QA Assessment (Azure DevOps) — qaskill

Run a 90-day QA assessment using Azure DevOps test runs/results and bugs. Requires Azure CLI and Azure AD login (no PAT needed):

```bash
qaskill
```

The tool will guide you through:
1. Organization prompt (e.g., `YourOrg` or `https://dev.azure.com/YourOrg`)
2. Azure AD sign-in if needed (`az login`)
3. Project selection from your accessible projects
4. Days to analyze (default: 90), QA users confirm, and output directory

CLI mode:

```bash
qaskill \
  --az-org YourOrg \
  --az-project MyProject \
  --days 90 \
  --qa-users "qa1@company.com,qa2@company.com" \
  --outdir reports \
  --config config.yml
```

Authentication:
- Uses Azure AD access token from `az account get-access-token` (resource `Azure DevOps`).
- For automation, you can set `AZDO_AUTH_TOKEN` to an existing Azure AD bearer token.

Outputs include project and date in filenames, e.g. `qa-skill-raw-ORG-PROJECT-YYYY-MM-DD.json`, `qa-skill-scores-*.json`, `qa-skill-assessment-*.md`, and `qa-skill-assessment-insights-*.md`.

Rerun from last raw data:

```bash
qaskill rerun
qaskill rerun --outdir reports
qaskill rerun --raw reports/qa-skill-raw-ORG-PROJECT-YYYY-MM-DD.json --anonymous
qaskill rerun --tag custom-tag
```

### Rerun from last raw data

You can regenerate reports from the most recent saved raw JSON without calling GitHub again:

```bash
# Regenerate from the latest dev-skill-raw-*.json under ./reports (if present)
devskill rerun

# Search within a specific directory for the latest raw JSON
devskill rerun --outdir reports

# Use a specific raw file
devskill rerun --raw reports/dev-skill-raw-OWNER-REPO-YYYY-MM-DD.json

# Apply a config (aliases, bots, weights) and anonymize names
devskill rerun --config config.yml --anonymous

# Override the output tag used in filenames
devskill rerun --tag custom-tag
```

### Fork this repo (recommended)

To keep your results organized in this project and save outputs under `./reports`:

```bash
gh repo fork Benjamindodgson/dev-skill-assessment --clone
cd dev-skill-assessment
pip install -e .
devskill
```

The default output directory is `./reports`. You can override with `--outdir`.

## Config YAML (optional)

See `config.yml` for a complete configuration example. Basic structure:

```yaml
# config.yml
repo: owner/repo            # optional override; CLI --repo-url has precedence
aliases:                    # map alternate logins to canonical names
  jdoe-alt: jdoe
exclude:                    # developer logins to exclude from scoring
  - bot-account
bots:                       # logins treated as bots and filtered out
  - dependabot
weights:                    # area weights (0..1)
  delivery: 0.30
  collaboration: 0.25
  hygiene: 0.15
  stability: 0.30
hygiene:
  small_pr_lines_threshold: 300

# QA assessment configuration (Azure DevOps)
qa:
  users:
    - qa1@company.com
    - qa2@company.com
  aliases:
    qa1@company.com: qa1
    "Smith, Alex": alex.smith@company.com
  exclude:
    - service.account@company.com
  weights:
    testing: 0.30
    defects: 0.30
    hygiene: 0.20
    responsiveness: 0.20
```

## Example Reports

See the `examples/` directory for sample output reports generated from a real repository.

## License

MIT


