# claude-code-auth-switch

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

The install script creates a profile directory per account under `~/.claude-profiles/` and generates thin wrapper scripts that set the right environment variables before `exec`-ing `claude`:

```
~/.claude-profiles/
├── config.yaml              # Your profile definitions
├── personal/                # Profile: claude-personal
│   ├── settings.json        # Copied from ~/.claude/ (mutable)
│   ├── settings.local.json  # Copied from ~/.claude/ (mutable)
│   ├── CLAUDE.md            # Copied from ~/.claude/ (mutable)
│   ├── commands/ -> ~/.claude/commands/   # Symlinked (read-only)
│   ├── skills/   -> ~/.claude/skills/     # Symlinked (read-only)
│   ├── agents/   -> ~/.claude/agents/     # Symlinked (read-only)
│   ├── plugins/  -> ~/.claude/plugins/    # Symlinked (read-only)
│   ├── projects/ -> ~/.claude/projects/   # Symlinked (shared state)
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
   mkdir -p ~/.claude-profiles
   cp config.example.yaml ~/.claude-profiles/config.yaml
   ```

3. **Edit `~/.claude-profiles/config.yaml`** with your profiles:

   ```yaml
   source_dir: ~/.claude
   bin_dir: ~/.local/bin

   profiles:
     personal:
       description: "Personal Anthropic account"

     work-team:
       description: "Company team subscription"
       add_dirs:
         - ~/devel/projects/company

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

## Configuration reference

### `config.yaml`

| Field | Default | Description |
|-------|---------|-------------|
| `source_dir` | `~/.claude` | Directory to copy/symlink shared config from |
| `bin_dir` | `~/.local/bin` | Where to install wrapper scripts |
| `profiles.<name>.description` | `""` | Human-readable profile description |
| `profiles.<name>.env` | `{}` | Environment variables to set before launching Claude |
| `profiles.<name>.add_dirs` | `[]` | Additional directories to pass via `--add-dir` |

### `install.py`

| Flag | Description |
|------|-------------|
| `--config PATH` | Config file path (default: `~/.claude-profiles/config.yaml`) |
| `--sync` | Force re-copy of mutable files, overwriting existing copies in profile dirs |

### Shared file handling

| Type | Files | Behavior |
|------|-------|----------|
| **Copied** | `settings.json`, `settings.local.json`, `CLAUDE.md` | Copied on first install. Use `--sync` to refresh from source. |
| **Symlinked** | `commands/`, `skills/`, `agents/`, `plugins/`, `projects/` | Always symlinked. Changes are shared across all profiles. |
| **Per-profile** | `.credentials.json`, `.claude.json`, `teams/`, `history.jsonl` | Created automatically by Claude Code on first use. |

## Re-syncing after config changes

```bash
# Add/remove profiles, update env vars → re-run install
python3 install.py

# Changed ~/.claude/settings.json and want profiles to pick it up
python3 install.py --sync
```

The install script is idempotent:
- Existing profile directories are never deleted
- Existing copied files are not overwritten (unless `--sync`)
- Symlinks are updated if their target changed
- Stale wrapper scripts (from removed profiles) are automatically cleaned up

## Vertex AI notes

When using a Vertex AI profile, the wrapper script injects the required environment variables (`CLAUDE_CODE_USE_VERTEX`, `ANTHROPIC_VERTEX_PROJECT_ID`, `CLOUD_ML_REGION`). Make sure these variables are **not** also set in your `~/.claude/settings.json` `env` section, or they will apply to all profiles (since `settings.json` is copied to each profile).

Authentication uses [Google Cloud Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials):

```bash
gcloud auth application-default login
```

## Limitations

- **Linux only.** macOS has a [Keychain collision bug](https://github.com/anthropics/claude-code/issues/20553) where multiple OAuth profiles overwrite each other's credentials regardless of `CLAUDE_CONFIG_DIR`.
- **Same-directory parallel execution** is not supported. Two profiles in the same project directory will fight over `.claude/history.jsonl.lock`. Use [git worktrees](https://git-scm.com/docs/git-worktree) if you need this.
- **Copied files can diverge.** When Claude Code modifies `settings.json` in a profile (e.g., accepting a permission), the change is local to that profile. Run `install.py --sync` to reset from source.

## Documentation

- [Binary Analysis and Design Decisions](docs/Binary-Analysis-and-Design-Decisions.md) — Local research verifying `CLAUDE_CONFIG_DIR` behavior, with corrections to prior Gemini research
- [Gemini: Symlink Behavior Research](docs/Gemini-Symlink-Behavior-Research.md) — Gemini Deep Research report on Claude Code's file I/O and symlink handling
- [Gemini: Managing Multiple Claude Accounts](docs/Gemini-Managing-Multiple-Claude-Accounts.md) — Gemini Deep Research report on multi-tenant Claude Code architecture

## License

MIT
