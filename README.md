# Dev Skill Assessment CLI

Run a 90-day GitHub developer assessment against any repository and export Markdown, CSV, and JSON reports.

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
```

## Example Reports

See the `examples/` directory for sample output reports generated from a real repository.

## License

MIT


