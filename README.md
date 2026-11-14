# claude-code-auth-switch

> **Note:** This has only been tested on Linux. macOS and Windows may not work as expected — macOS in particular has a [Keychain collision bug](https://github.com/anthropics/claude-code/issues/20553) that breaks multi-account OAuth isolation.

Run multiple [Claude Code](https://docs.anthropic.com/en/docs/claude-code) accounts side-by-side with isolated credentials, shared configuration, and zero Docker overhead.

Each profile gets its own `claude-<name>` command on your `$PATH`:

```
claude-personal     # Personal Anthropic subscription (OAuth)
claude-work-team    # Company team subscription (OAuth)
claude-work-vertex  # Company via Google Cloud Vertex AI
```

Run them in parallel across different project directories without credential collisions, file lock conflicts, or billing mix-ups.

## How it works

Claude Code's `CLAUDE_CONFIG_DIR` environment variable redirects all configuration and credential storage to a custom directory. We [verified in the binary](docs/Binary-Analysis-and-Design-Decisions.md) that this provides **full isolation** — including the `~/.claude.json` state file, which contrary to [some reports](docs/Gemini-Symlink-Behavior-Research.md) is **not** hardcoded to the home directory.

The install script reads `~/.claude/auth-switch.yaml`, creates a profile directory per account under `~/.claude-profiles/`, and generates thin wrapper scripts that set the right environment variables before `exec`-ing `claude`:

```
~/.claude/
├── auth-switch.yaml          # Your profile definitions
└── auth-switch.secrets.yaml  # Secret env values (gitignored, optional)

~/.claude-profiles/
├── personal/                # Profile: claude-personal
│   ├── settings.json        # Copied from ~/.claude/ (mutable)
│   ├── settings.local.json  # Copied from ~/.claude/ (mutable)
│   ├── CLAUDE.md            # Copied from ~/.claude/ (mutable, plus any other *.md files)
│   ├── keybindings.json     # Copied from ~/.claude/ (mutable)
│   ├── commands/ -> ~/.claude/commands/   # Symlinked (read-only)
│   ├── skills/   -> ~/.claude/skills/     # Symlinked (read-only)
│   ├── agents/   -> ~/.claude/agents/     # Symlinked (read-only)
│   ├── plugins/  -> ~/.claude/plugins/    # Symlinked (read-only)
│   ├── projects/ -> ~/.claude/projects/   # Symlinked (shared state)
│   ├── scripts/  -> ~/.claude/scripts/    # Symlinked (read-only)
│   ├── .credentials.json   # Created by Claude on first login
│   └── .claude.json         # Created by Claude on first login
├── work-team/               # Profile: claude-work-team
│   └── ...
└── work-vertex/             # Profile: claude-work-vertex
    └── ...
```

**Why copy some files and symlink others?** Claude Code uses atomic writes (write-to-temp + `fs.rename`) for config files like `settings.json`. This replaces symlinks with regular files, breaking the link. Directory symlinks are unaffected — files created inside a symlinked directory are real files in the target. See [design decisions](docs/Binary-Analysis-and-Design-Decisions.md) for details.

## Requirements

- Linux (macOS has [Keychain-specific issues](docs/Gemini-Managing-Multiple-Claude-Accounts.md) not addressed here)
- Python 3.10+ with [PyYAML](https://pypi.org/project/PyYAML/) (`pip install pyyaml`)
- Claude Code installed and available as `claude` on your `$PATH`
- `~/.local/bin` on your `$PATH` (or configure a different `bin_dir`)

## Installation

1. **Clone the repo:**

   ```bash
   git clone https://github.com/fprochazka/claude-code-auth-switch.git
   cd claude-code-auth-switch
   ```

2. **Create your config:**

   ```bash
   cp config.example.yaml ~/.claude/auth-switch.yaml
   ```

3. **Edit `~/.claude/auth-switch.yaml`** with your profiles:

   ```yaml
   source_dir: ~/.claude
   bin_dir: ~/.local/bin
   profiles_dir: ~/.claude-profiles

   profiles:
     personal:
       description: "Personal Anthropic account"

     work-team:
       description: "Company team subscription"
       model: sonnet
       add_dirs:
         - ~/devel/projects/company
       attribution:
         commit: "🤖 Generated with Claude Code\n\nCo-Authored-By: Claude <noreply@anthropic.com>"
         pr: "🤖 Generated with [Claude Code](https://claude.ai/code)"

     work-vertex:
       description: "Company via Google Cloud Vertex AI"
       add_dirs:
         - ~/devel/projects/company
       env:
         CLAUDE_CODE_USE_VERTEX: "1"
         ANTHROPIC_VERTEX_PROJECT_ID: "<your-project-id>"
         CLOUD_ML_REGION: "europe-west1"
   ```

4. **Run the install script:**

   ```bash
   python3 install.py
   ```

5. **Authenticate each profile** (first time only):

   ```bash
   # OAuth profiles — opens browser for login
   claude-personal
   claude-work-team

   # Vertex AI — requires gcloud auth first
   gcloud auth application-default login
   claude-work-vertex
   ```

## Usage

Use the profile commands exactly like `claude`:

```bash
claude-personal                          # Start a session
claude-work-team --resume                # Resume last session
claude-work-vertex -p "explain this"     # One-shot prompt
```

### Parallel execution

Run different profiles simultaneously in separate terminals — just use different project directories:

```
Terminal 1:  cd ~/personal-project && claude-personal
Terminal 2:  cd ~/work-project && claude-work-team
```

This works because each profile has its own `CLAUDE_CONFIG_DIR` (no credential collision) and different working directories mean different `.claude/history.jsonl.lock` files (no file lock collision).

### Additional directories

The `add_dirs` config option maps to Claude Code's `--add-dir` flag, giving the profile access to additional project directories:

```yaml
profiles:
  work-team:
    add_dirs:
      - ~/devel/projects/company
      - ~/devel/shared-libs
```

This replaces manual aliases like `alias claude-work='claude --add-dir ~/devel/projects/company'`.

### Pinned model versions

The `pinned_models` config option maps a model *class* — `sonnet`, `opus`, `haiku`, or `fable` — to a specific model version id, so the corresponding alias always resolves to that exact version instead of whatever the class currently points at:

```yaml
profiles:
  work-team:
    pinned_models:
      sonnet: claude-sonnet-4-5-20250929
      opus: claude-opus-4-8
```

Under the hood each entry becomes an `ANTHROPIC_DEFAULT_<CLASS>_MODEL` alias-resolution environment variable (`ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL`, `ANTHROPIC_DEFAULT_FABLE_MODEL`) exported by the generated wrapper. An explicit `env:` entry for the same variable wins over the `pinned_models`-derived value, so you can always override a single class by hand. Any class key outside the four valid ones, or an empty/non-string version, aborts the install with a clear error.

These variables only take effect when you launch through the generated `claude-<name>` wrapper — they are exported by the wrapper, not written into `settings.json`, so running plain `claude` is unaffected.

### Secrets

The main config is meant to be committed. An API key inside an `env:` entry is not. Put those values in a secrets file that sits next to the main config and carries the same stem: `~/.claude/auth-switch.secrets.yaml` for the default location, `config.secrets.yaml` for the legacy one. The installer loads it when it exists, and runs normally when it does not.

```yaml
# ~/.claude/auth-switch.secrets.yaml
profiles:
  work-team:
    env:
      OTEL_EXPORTER_OTLP_HEADERS: "dd-api-key=<your-api-key>"
```

The file has the same shape as the main config, restricted to one thing: `profiles.<name>.env` with string values. Any other top-level key, any other key inside a profile, a profile name that the main config does not define, or a non-string env value aborts the install with an error naming the offending key.

Secret env vars merge into the profile's `env` before the wrapper is written, and behave exactly like main-config env vars — they win over a value derived from `pinned_models`. Defining the same variable in both files is a fatal error instead of a silent precedence rule. The message names the profile and the variable, never the value.

The installer also refuses to use a secrets file that git could publish. Two checks run before the file is read:

1. The file is **tracked** in git → error. Remove it from the index with `git rm --cached`, then ignore it.
2. The file is inside a git repository and **not ignored** → error. Add its name to `.gitignore`.

A file outside any git repository passes both checks, and a missing `git` binary skips them.

Because `~/.claude` is a good candidate for version control, this matters most there. With an allowlist-style `.gitignore` — a leading `*` plus `!` entries for the files you want — the secrets file is ignored by default and needs no rule, while the main config needs an explicit `!/auth-switch.yaml` line to be committed at all.

## Configuration reference

### `auth-switch.yaml`

The default config path is `~/.claude/auth-switch.yaml`. When that file does not exist, the installer falls back to the legacy `~/.claude-profiles/config.yaml` and prints a note naming the preferred location. `--config PATH` overrides both.

| Field | Default | Description |
|-------|---------|-------------|
| `source_dir` | `~/.claude` | Directory to copy/symlink shared config from |
| `bin_dir` | `~/.local/bin` | Where to install wrapper scripts |
| `profiles_dir` | `~/.claude-profiles` | Where to create the per-profile config directories. `~` is expanded. |
| `profiles.<name>.description` | `""` | Human-readable profile description |
| `profiles.<name>.model` | | Default model to use (e.g., `sonnet`, `opus`). Overridden by `--model` on the command line. |
| `profiles.<name>.pinned_models` | `{}` | Map of model class (`sonnet`, `opus`, `haiku`, `fable`) to a specific version id. Emitted as `ANTHROPIC_DEFAULT_<CLASS>_MODEL` env vars in the wrapper. See [Pinned model versions](#pinned-model-versions). |
| `profiles.<name>.env` | `{}` | Environment variables to set before launching Claude |
| `profiles.<name>.add_dirs` | `[]` | Additional directories to pass via `--add-dir` |
| `profiles.<name>.attribution` | | Attribution settings object patched into the profile's `settings.json` on every install |
| `profiles.<name>.attribution.commit` | | Attribution text appended to git commit messages |
| `profiles.<name>.attribution.pr` | | Attribution text appended to pull request descriptions |
| `profiles.<name>.attribution.sessionUrl` | | Boolean. Keeps (`true`) or drops (`false`) the `Claude-Session: <url>` trailer that web/Remote Control sessions add to commits and PRs. Independent of `commit`/`pr`; omit to keep Claude Code's default. Set `false` to avoid leaking session links into public repo history. |
| `profiles.<name>.plansDirectory` | | Path written to the profile's `settings.json` as `plansDirectory`. `~` is expanded at install time. |

### `auth-switch.secrets.yaml`

| Field | Default | Description |
|-------|---------|-------------|
| `profiles.<name>.env` | `{}` | Secret environment variables, merged into the profile's `env`. The only key this file accepts. See [Secrets](#secrets). |

### `install.py`

| Flag | Description |
|------|-------------|
| `--config PATH` | Config file path (default: `~/.claude/auth-switch.yaml`, falling back to `~/.claude-profiles/config.yaml`) |
| `--sync` | Re-copy mutable files from source. If a profile file has diverged (contains changes not in source), shows a diff and exits — prompting you to merge first |
| `--sync --force` | Force overwrite of diverged profile files without prompting |

### Shared file handling

| Type | Files | Behavior |
|------|-------|----------|
| **Copied** | `settings.json`, `settings.local.json`, `keybindings.json`, and all top-level `*.md` files (e.g. `CLAUDE.md`) | Copied on first install. Use `--sync` to refresh from source. |
| **Symlinked** | `commands/`, `skills/`, `agents/`, `plugins/`, `projects/`, `scripts/` | Always symlinked. Changes are shared across all profiles. |
| **Patched** | `settings.json` (`attribution`, `plansDirectory`) | Set from profile config on every install. |
| **Selectively synced** | `.claude.json` (`mcpServers`) | Only the `mcpServers` key is synced from `~/.claude.json` on every install. Other keys in `.claude.json` are per-profile state. |
| **Per-profile** | `.credentials.json`, `.claude.json`, `teams/`, `history.jsonl` | Created automatically by Claude Code on first use. |

## Re-syncing after config changes

```bash
# Edited ~/.claude/auth-switch.yaml or ~/.claude/auth-switch.secrets.yaml → re-run install
# (also syncs mcpServers from ~/.claude.json into each profile)
python3 install.py

# Changed ~/.claude/settings.json and want profiles to pick it up
# (will show a diff and stop if any profile file has diverged)
python3 install.py --sync

# Force overwrite even if profile files have diverged
python3 install.py --sync --force
```

The install script is idempotent:
- Existing profile directories are never deleted
- Existing copied files are not overwritten (unless `--sync`)
- `--sync` detects diverged files (profile has changes not in source) and shows a diff before overwriting
- Symlinks are updated if their target changed
- `mcpServers` from `~/.claude.json` are synced into each profile on every run (not just `--sync`)
- Stale wrapper scripts (from removed profiles) are automatically cleaned up

## Vertex AI notes

When using a Vertex AI profile, the wrapper script injects the required environment variables (`CLAUDE_CODE_USE_VERTEX`, `ANTHROPIC_VERTEX_PROJECT_ID`, `CLOUD_ML_REGION`). Make sure these variables are **not** also set in your `~/.claude/settings.json` `env` section, or they will apply to all profiles (since `settings.json` is copied to each profile).

Authentication uses [Google Cloud Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials):

```bash
gcloud auth application-default login
```

## Environment variables

Each wrapper script exports the following environment variables before launching `claude`:

| Variable | Example | Description |
|----------|---------|-------------|
| `CLAUDE_CONFIG_DIR` | `~/.claude-profiles/personal` | Points Claude Code at the profile's isolated config directory |
| `FP_CC_AUTH_SWITCH_PROFILE` | `personal` | The active profile name — useful in [status line scripts](https://docs.anthropic.com/en/docs/claude-code/settings#status-bar) and hooks |

The `FP_CC_AUTH_SWITCH_PROFILE` variable is intentionally outside the `CLAUDE_*`/`ANTHROPIC_*` namespace so it is not cleared by the wrapper's env-unset logic when nesting sessions. You can use it in your `statusLine` command to display which profile is active:

```bash
# In your status line script:
if [ -n "${FP_CC_AUTH_SWITCH_PROFILE:-}" ]; then
    echo "Profile: $FP_CC_AUTH_SWITCH_PROFILE"
fi
```

## Limitations

- **Linux only.** macOS has a [Keychain collision bug](https://github.com/anthropics/claude-code/issues/20553) where multiple OAuth profiles overwrite each other's credentials regardless of `CLAUDE_CONFIG_DIR`.
- **Same-directory parallel execution** is not supported. Two profiles in the same project directory will fight over `.claude/history.jsonl.lock`. Use [git worktrees](https://git-scm.com/docs/git-worktree) if you need this.
- **Copied files can diverge.** When Claude Code modifies `settings.json` in a profile (e.g., accepting a permission), the change is local to that profile. Run `install.py --sync` to reset from source.

## Documentation

- [Binary Analysis and Design Decisions](docs/Binary-Analysis-and-Design-Decisions.md) — Local research verifying `CLAUDE_CONFIG_DIR` behavior, with corrections to prior Gemini research
- [Gemini: Symlink Behavior Research](docs/Gemini-Symlink-Behavior-Research.md) — Gemini Deep Research report on Claude Code's file I/O and symlink handling
- [Gemini: Managing Multiple Claude Accounts](docs/Gemini-Managing-Multiple-Claude-Accounts.md) — Gemini Deep Research report on multi-tenant Claude Code architecture

## Development

### Commit hooks

The research behind this tool comes from real machines and real accounts. Home paths, account names, cloud project IDs and company names show up in terminals and binary dumps, and from there they can slip into a commit. `scripts/hooks` holds a pre-commit and a commit-msg hook. They refuse a commit when a staged line or the commit message matches a private identifier. The identifiers live in a file outside the repository, because a list in the repository would publish the strings that the hooks keep out. Set it up once per clone:

```bash
git config core.hooksPath scripts/hooks
git config hooks.forbiddenPatternsFile ~/.config/git/forbidden-patterns.txt  # any path outside the repo
```

The file holds one extended regex per line, matched case-insensitively. Blank lines and `#` comments are ignored. Until `hooks.forbiddenPatternsFile` points at a file with at least one pattern, both hooks refuse every commit and print how to fix it. The hooks use `mapfile`, so they need bash 4 or newer. Docs and examples use placeholder names such as `<your-project-id>` and `~/devel/projects/company`.

## License

MIT
