# claude-code-auth-switch

> **Note:** This has only been tested on Linux. Nobody has tested it on macOS or WSL yet. Reading the Claude Code binary suggests that macOS should work, because each profile directory gets its own Keychain entry, but that is not a tested result. Native Windows is not supported. See [macOS and WSL notes](#macos-and-wsl-notes).

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

The `cc-auth-switch install` command reads `~/.claude/auth-switch.yaml`, creates a profile directory per account under `~/.claude-profiles/`, and generates thin wrapper scripts that set the right environment variables before `exec`-ing `claude`:

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

- Linux. macOS and WSL are untested, see [macOS and WSL notes](#macos-and-wsl-notes).
- [uv](https://docs.astral.sh/uv/). It installs the tool with its own Python 3.10+ and PyYAML, so the system Python does not matter. This includes macOS, where `/usr/bin/python3` is 3.9.
- Claude Code installed and available as `claude` on your `$PATH`
- `~/.local/bin` on your `$PATH`. `uv tool install` puts the tool's commands there, and the wrappers go there unless you configure a different `bin_dir`.

## Installation

1. **Install the tool:**

   ```bash
   uv tool install git+https://github.com/fprochazka/claude-code-auth-switch.git
   ```

   This puts one command into `~/.local/bin`: `cc-auth-switch`. The wrappers run `cc-auth-switch attribution`, and the profiles' `settings.json` runs `cc-auth-switch session-start`, both by the absolute path that `cc-auth-switch` had at install time.

   If you used the older `python3 install.py`, it left a copy of the attribution helper in `bin_dir` as `claude-auth-switch-attribution`. `cc-auth-switch install` removes it when its content matches a version that `install.py` wrote, and otherwise warns and leaves it.

2. **Create your config** from [`config.example.yaml`](config.example.yaml):

   ```bash
   curl -o ~/.claude/auth-switch.yaml https://raw.githubusercontent.com/fprochazka/claude-code-auth-switch/master/config.example.yaml
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

4. **Create the profiles and wrappers:**

   ```bash
   cc-auth-switch install
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

### Attribution from git config

The commit and PR attribution can also come from git config, so it follows the directory you work in instead of the profile. Put a `[claude-code "attribution"]` section into a gitconfig that applies to a directory, for example a company gitconfig included by `includeIf`:

```ini
# ~/.gitconfig
[includeIf "gitdir:~/devel/projects/company/"]
    path = ~/devel/projects/company/.gitconfig
```

```ini
# ~/devel/projects/company/.gitconfig
[claude-code "attribution"]
    commit = "🤖 Generated with Claude Code\n\nCo-Authored-By: Claude <noreply@anthropic.com>"
    pr = "🤖 Generated with [Claude Code](https://claude.ai/code)"
    sessionUrl = false
```

At launch, every wrapper runs `cc-auth-switch attribution "$PWD"`, by the absolute path that `cc-auth-switch` had at install time. When the command finds any of the keys, the wrapper passes them as `claude --settings '{"attribution": {...}}'`. The keys are the same as in the profile's `attribution`: `commit` and `pr` are strings, where an empty string turns the attribution off, and `sessionUrl` is a git boolean (`true`/`yes`/`on`/`1`, `false`/`no`/`off`/`0`). An invalid boolean or an unknown key prints a warning and is ignored. A key that is not set stays unset, so Claude Code falls back to the next source for it.

Which config applies depends on where you start Claude:

- **Inside a git repository** git reads the config itself, so every `include` and `includeIf` works as it does for `git config`, including globs, `gitdir/i:`, `onbranch:` and `hasconfig:`. The repository's own `.git/config` can set the section too.
- **Outside a git repository** git ignores every `gitdir:` condition. The script reads the global config and evaluates only `gitdir:` and `gitdir/i:` patterns that start with `~/`, `./` or `/`, end with `/`, and contain no `*`, `?` or `[`. Such a pattern matches the start directory and everything under it, by its path as given and by its resolved path. Other patterns are skipped. Starting in `~/devel/projects/company`, a directory that holds the company's repositories but is not one itself, uses the company attribution.

Precedence, per key: git config wins over the profile's `attribution` in `auth-switch.yaml`, which wins over `~/.claude/settings.json`. Claude Code deep-merges settings sources, so a gitconfig that sets only `commit` keeps `pr` and `sessionUrl` from the profile.

The attribution is fixed when Claude starts. A session started in a personal repository keeps the personal attribution even when it later works in a company directory added with `--add-dir`. Start Claude in the directory whose attribution you want.

Subcommands such as `claude-personal mcp list` do not get the setting. If you pass your own `--settings` to a wrapper, the wrapper does not add its own, because Claude Code keeps only the last `--settings` flag instead of merging them. Put the attribution into your settings file when you need both.

If `cc-auth-switch` is missing or the command fails, the wrapper prints a one-line warning and starts Claude without the git config attribution.

### Plugins

`plugins/` is a symlink to `~/.claude/plugins`, so the installed plugins and the known marketplaces are shared by all profiles. Claude Code loads a plugin only when `enabledPlugins` in the profile's own `settings.json` sets it to `true`. Every `cc-auth-switch install`, with or without `--sync`, therefore copies `enabledPlugins` and `extraKnownMarketplaces` whole from `source_dir`'s `settings.json` into each profile. A key that the source does not have is removed from the profiles.

To turn plugins on or off in one profile, list them under `plugins:`. The installer applies these entries on top of the copied `enabledPlugins`:

```yaml
profiles:
  personal:
    plugins:
      company-tools@company-marketplace: false
      notes@personal-marketplace: true
```

A key is `<plugin>@<marketplace>` and a value is `true` or `false`; anything else stops the install. When an entry enables a plugin whose marketplace is neither in `extraKnownMarketplaces` nor in `plugins/known_marketplaces.json`, the installer warns, because Claude Code cannot install that plugin.

A `/plugin enable` or `/plugin disable` inside one profile writes only that profile's `settings.json`, and the next install resets it. Put the change into `plugins:` to keep it.

### Profile start log

Claude Code records nothing about the profile in a session's files. To let other tools tell which profile started or resumed a session, the installer registers `cc-auth-switch session-start` as a `SessionStart` hook in every profile's `settings.json`. It never adds the hook to `source_dir`'s `settings.json`. It replaces its own entry, also the `claude-auth-switch-session-start` entry of an earlier version, and keeps all other `SessionStart` hooks, because Claude Code runs every hook from every settings source. `--sync` does not report the hook as a divergence.

On every start, resume, `/clear`, compaction and fork, the hook appends one line to `auth-switch-profile-starts.jsonl` in the session's directory, `projects/<project>/<session-id>/`:

```json
{"v": 1, "ts": 1763290000000, "session_id": "<uuid>", "pid": 12345, "source": "startup", "profile": "work-team", "config_dir": "/home/you/.claude-profiles/work-team", "cwd": "/home/you/devel/projects/company/service"}
```

- `ts` is the time in epoch milliseconds.
- `pid` is the Claude Code process, the one that writes `sessions/<pid>.json`. It is `null` when the hook cannot find it.
- `source` is `startup`, `resume`, `clear`, `compact` or `fork`.
- `profile` and `config_dir` come from `FP_CC_AUTH_SWITCH_PROFILE` and `CLAUDE_CONFIG_DIR`, and are `null` when a session starts without a wrapper.

[cc-blackbox](https://github.com/fprochazka/cc-blackbox) reads this file. Treat the format as a contract: a change needs a new `v`.

Claude Code puts the stdout of a `SessionStart` hook into the model context, so the hook prints nothing and always exits 0. It writes errors to `auth-switch-session-start.log` in the profile directory.

Claude Code deletes the session directory, and the log with it, when it cleans up old transcripts. A session quit before its first prompt has no transcript, and the cleanup leaves its directory behind, because it does not delete files it does not know.

## Configuration reference

### `auth-switch.yaml`

The default config path is `~/.claude/auth-switch.yaml`. When that file does not exist, the installer falls back to the legacy `~/.claude-profiles/config.yaml` and prints a note naming the preferred location. `--config PATH` overrides both.

| Field | Default | Description |
|-------|---------|-------------|
| `source_dir` | `~/.claude` | Directory to copy/symlink shared config from |
| `bin_dir` | `~/.local/bin` | Where to install the wrapper scripts |
| `profiles_dir` | `~/.claude-profiles` | Where to create the per-profile config directories. `~` is expanded. | Only the profiles listed in the config are managed: the installer never reads for writing, changes or deletes anything in another directory under `profiles_dir`.
| `profiles.<name>.description` | `""` | Human-readable profile description |
| `profiles.<name>.model` | | Default model to use (e.g., `sonnet`, `opus`). Overridden by `--model` on the command line. |
| `profiles.<name>.pinned_models` | `{}` | Map of model class (`sonnet`, `opus`, `haiku`, `fable`) to a specific version id. Emitted as `ANTHROPIC_DEFAULT_<CLASS>_MODEL` env vars in the wrapper. See [Pinned model versions](#pinned-model-versions). |
| `profiles.<name>.env` | `{}` | Environment variables to set before launching Claude |
| `profiles.<name>.add_dirs` | `[]` | Additional directories to pass via `--add-dir` |
| `profiles.<name>.attribution` | | Attribution settings object patched into the profile's `settings.json` on every install. A `[claude-code "attribution"]` git config section overrides it per key, see [Attribution from git config](#attribution-from-git-config). |
| `profiles.<name>.attribution.commit` | | Attribution text appended to git commit messages |
| `profiles.<name>.attribution.pr` | | Attribution text appended to pull request descriptions |
| `profiles.<name>.attribution.sessionUrl` | | Boolean. Keeps (`true`) or drops (`false`) the `Claude-Session: <url>` trailer that web/Remote Control sessions add to commits and PRs. Independent of `commit`/`pr`; omit to keep Claude Code's default. Set `false` to avoid leaking session links into public repo history. |
| `profiles.<name>.plansDirectory` | | Path written to the profile's `settings.json` as `plansDirectory`. `~` is expanded at install time. |
| `profiles.<name>.plugins` | `{}` | Map of `"<plugin>@<marketplace>"` to `true` or `false`, applied on top of the `enabledPlugins` copied from `source_dir`. See [Plugins](#plugins). |

### `auth-switch.secrets.yaml`

| Field | Default | Description |
|-------|---------|-------------|
| `profiles.<name>.env` | `{}` | Secret environment variables, merged into the profile's `env`. The only key this file accepts. See [Secrets](#secrets). |

### `cc-auth-switch install`

| Flag | Description |
|------|-------------|
| `--config PATH` | Config file path (default: `~/.claude/auth-switch.yaml`, falling back to `~/.claude-profiles/config.yaml`) |
| `--sync` | Re-copy mutable files from source. If a profile file has diverged (contains changes not in source), shows a diff and exits — prompting you to merge first |
| `--sync --force` | Force overwrite of diverged profile files without prompting. Each replaced file is backed up first and kept until you delete it, as with every file that an install changes, see [Re-syncing after config changes](#re-syncing-after-config-changes) |

### Shared file handling

| Type | Files | Behavior |
|------|-------|----------|
| **Copied** | `settings.json`, `settings.local.json`, `keybindings.json`, and all top-level `*.md` files (e.g. `CLAUDE.md`) | Copied on first install. Use `--sync` to refresh from source. |
| **Symlinked** | `commands/`, `skills/`, `agents/`, `plugins/`, `projects/`, `scripts/` | Always symlinked. Changes are shared across all profiles. |
| **Patched** | `settings.json` (`attribution`, `plansDirectory`, `hooks.SessionStart`) | Set from profile config on every install. The installer creates the file when `source_dir` has none. See [Profile start log](#profile-start-log) for the hook. |
| **Per launch** | `--settings` (`attribution`) | Read from git config for the start directory every time a wrapper starts Claude. Never written to disk. See [Attribution from git config](#attribution-from-git-config). |
| **Synced on every install** | `settings.json` (`enabledPlugins`, `extraKnownMarketplaces`) | Copied whole from `source_dir`'s `settings.json`, then the profile's `plugins:` overrides are applied. See [Plugins](#plugins). |
| **Selectively synced** | `.claude.json` (`mcpServers`) | Only the `mcpServers` key is synced from `~/.claude.json` on every install. Other keys in `.claude.json` are per-profile state. |
| **Per-profile** | `.credentials.json`, `.claude.json`, `teams/`, `history.jsonl` | Created automatically by Claude Code on first use. |

## Re-syncing after config changes

```bash
# Edited ~/.claude/auth-switch.yaml or ~/.claude/auth-switch.secrets.yaml → re-run install
# (also syncs mcpServers from ~/.claude.json into each profile)
cc-auth-switch install

# Changed ~/.claude/settings.json and want profiles to pick it up
# (will show a diff and stop if any profile file has diverged)
cc-auth-switch install --sync

# Force overwrite even if profile files have diverged
cc-auth-switch install --sync --force
```

Before any `cc-auth-switch install`, with or without `--sync` and `--force`, changes an existing file in a profile's root, it copies the old file to `<profile>/.auth-switch-backups/<UTC timestamp>/` and prints one line with the backup path. That covers the files that `--sync` replaces (`settings.json`, `settings.local.json`, `keybindings.json`, a top-level `*.md`), the patches to `settings.json` (`attribution`, `plansDirectory`, the plugin keys, the hook) and the `mcpServers` sync into `.claude.json`. When a run writes a file twice, the backup holds the content from before the run. Files that the run creates, and the shared symlinked directories, are never backed up. The installer writes the new content to a temporary file next to the target and compares the bytes. When they are equal, it deletes the temporary file and leaves the target alone, without a backup. Otherwise it backs up the target and replaces it atomically, keeping the target's file mode. A run that changes nothing therefore prints no line per profile and creates no backup directory. That makes `--sync --force` safe to run unattended, for example from an upgrade script.

The tool never deletes a backup. Remove the directories with `rm` whenever you no longer need them, for example `rm -r ~/.claude-profiles/*/.auth-switch-backups`. A run that backed something up says so in its last line.

The installer is idempotent:
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

## macOS and WSL notes

Nobody has run this tool on macOS or WSL. Everything in this section comes from reading the Claude Code 2.1.289 binary, so treat it as expected behavior, not a tested result. If you try it, please open an issue with what you saw.

### macOS

Claude Code on macOS stores the OAuth login in the Keychain and falls back to `.credentials.json` in the config directory. When `CLAUDE_CONFIG_DIR` is set, the Keychain service name gets a suffix: `Claude Code-credentials-<hash>`, where `<hash>` is the first 8 hex characters of the SHA-256 of the `CLAUDE_CONFIG_DIR` value. Without the variable, the name is plain `Claude Code-credentials`. Each wrapper sets a different `CLAUDE_CONFIG_DIR`, so each profile should get its own Keychain entry, and profiles should not overwrite each other's logins. The [Binary Analysis](docs/Binary-Analysis-and-Design-Decisions.md#4-macos-keychain-entry-per-config-directory) doc has the code.

[Issue #20553](https://github.com/anthropics/claude-code/issues/20553) reported that all profiles shared one Keychain entry. That was true in early 2026. A user in that issue confirmed per-directory entries on macOS in Claude Code 2.1.56, but the issue is still open.

To see which entry a profile uses:

```bash
hash=$(printf '%s' "$HOME/.claude-profiles/personal" | shasum -a 256 | cut -c1-8)
security find-generic-password -s "Claude Code-credentials-$hash" -a "$USER"
```

Gotchas:

- **The hash covers the exact string.** Claude Code does not expand `~`, strip a trailing slash or resolve symlinks before hashing. If you move `profiles_dir`, or point it at the same directory through a different path, every profile reads as logged out and the old Keychain entries stay behind. The installer writes the expanded absolute path, so re-running it with the same config keeps the same entries.
- **Never export `CLAUDE_CONFIG_DIR=~/.claude`.** The explicit default path produces a hashed name, not the plain `Claude Code-credentials` entry that your existing login uses, so plain `claude` reads as logged out. The wrappers unset every inherited `CLAUDE_*` variable, so they are not affected. [cenv](https://github.com/lukabudik/cenv) documents the same gotcha.
- **A login in `~/.claude` does not carry over.** Log in once in every profile, as on Linux.
- **Re-login after a reboot.** One user in issue #20553 reported that macOS blocked reads of the per-directory entries after a restart, because of the entry's access control list. The binary reads the Keychain by running the `security` command (`/usr/bin/security`), which that list allows, so this may not apply. Nobody has checked.

Other macOS differences:

- The wrappers are bash scripts. They run under the bash 3.2 that macOS ships, which was checked in a `bash:3.2` container, not on a Mac.
- `~/.local/bin` is not on the macOS `$PATH` by default. Add it, or set `bin_dir` to a directory that is.

### WSL

Claude Code under WSL is the Linux build. The Linux build stores the login only in `.credentials.json` in the profile directory, and does not use the Windows Credential Manager. The isolation works the same as on Linux.

For the browser login, Claude Code runs `$BROWSER`, or `xdg-open` when `$BROWSER` is unset. The binary has no WSL-specific browser opener. If no browser opens, Claude Code prints the login URL, and you open it in Windows by hand.

### Native Windows

Not supported. The wrappers are bash scripts.

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

- **Tested on Linux only.** macOS and WSL should work based on the binary, but nobody has tested them. See [macOS and WSL notes](#macos-and-wsl-notes).
- **Same-directory parallel execution** is not supported. Two profiles in the same project directory will fight over `.claude/history.jsonl.lock`. Use [git worktrees](https://git-scm.com/docs/git-worktree) if you need this.
- **Copied files can diverge.** When Claude Code modifies `settings.json` in a profile (e.g., accepting a permission), the change is local to that profile. Run `cc-auth-switch install --sync` to reset from source.

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

### Tests

The tests use real git with a throwaway home directory and never touch your own config. Run them with [uv](https://docs.astral.sh/uv/):

```bash
uv run pytest
```

`uv run` installs the package into the project environment, so the tests find its commands on `PATH`. To try a local checkout as the installed tool, run `uv tool install --force --editable .`.

## License

MIT
