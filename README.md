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

The `cc-auth-switch install` command reads `~/.claude/auth-switch.yaml`, creates a profile directory per account under `~/.claude-profiles/`, and puts a `claude-<name>` launcher for each profile into `~/.local/bin`. A launcher is one line, `exec /home/you/.local/bin/cc-auth-switch start --config /home/you/.claude/auth-switch.yaml <name> "$@"`, with the paths that `install` saw. On every launch, `cc-auth-switch start` reads the config and the secrets file, sets the environment and the arguments for the profile, and replaces itself with `claude`:

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

**Why read the config on every launch?** A change to `env`, `model`, `add_dirs`, `pinned_models`, `default_role`, `refuse_home_cwd`, `capture_rate_limits`, `roles:` or the secrets file applies at the next launch without a reinstall, and the launch logic lives in Python where it can be tested. It costs two things. Each launch takes about 10 ms longer than the bash wrappers of earlier versions, measured on Linux. A broken config or a broken tool install now stops every `claude-*` command, where static wrappers kept working. `cc-auth-switch start` never falls back to a plain `claude`, because that would start Claude under the wrong account: it prints the config path and the error and exits with a non-zero status. See [`cc-auth-switch start`](#cc-auth-switch-start-profile).

**Why copy some files and symlink others?** Claude Code uses atomic writes (write-to-temp + `fs.rename`) for config files like `settings.json`. This replaces symlinks with regular files, breaking the link. Directory symlinks are unaffected — files created inside a symlinked directory are real files in the target. See [design decisions](docs/Binary-Analysis-and-Design-Decisions.md) for details.

## Requirements

- Linux. macOS and WSL are untested, see [macOS and WSL notes](#macos-and-wsl-notes).
- [uv](https://docs.astral.sh/uv/). It installs the tool with its own Python 3.12+ and PyYAML, so the system Python does not matter. This includes macOS, where `/usr/bin/python3` is 3.9.
- Claude Code installed and available as `claude` on your `$PATH`
- `~/.local/bin` on your `$PATH`. `uv tool install` puts the tool's commands there, and the launchers go there unless you configure a different `bin_dir`.

## Installation

1. **Install the tool:**

   ```bash
   uv tool install git+https://github.com/fprochazka/claude-code-auth-switch.git
   ```

   This puts one command into `~/.local/bin`: `cc-auth-switch`. The launchers run `cc-auth-switch start`, and the profiles' `settings.json` runs `cc-auth-switch session-start`, both by the absolute path that `cc-auth-switch` had at install time.

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

4. **Create the profiles and launchers:**

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
claude-personal --agent-role dispatcher  # Start with a role, see Roles
```

### Parallel execution

Run different profiles simultaneously in separate terminals — just use different project directories:

```
Terminal 1:  cd ~/personal-project && claude-personal
Terminal 2:  cd ~/work-project && claude-work-team
```

This works because each profile has its own `CLAUDE_CONFIG_DIR` (no credential collision) and different working directories mean different `.claude/history.jsonl.lock` files (no file lock collision).

### Session names in tmux

Inside tmux, the launcher names a new session after the tmux session and pane: `--name "<tmux session>-p<pane number>"`, for example `api-service-p12` for pane `%12`. Other sessions can then address it by that name with `SendMessage`, which is useful together with [shared sessions](#sharing-sessions-between-profiles).

The launcher adds no name when you pass `-n`/`--name` yourself, and none when you resume or continue a session (`-r`/`--resume`, `-c`/`--continue`, `--session-id`, `--fork-session`, `--from-pr`). A name given at launch replaces the saved title of a resumed session, including one set with `/rename`. When `tmux display-message` fails or does not answer within 2 seconds, the session starts without a name. Subcommands such as `claude-work-team mcp list` never get a name, because they reject the option.

### Starting in the home directory

`start` refuses to start a session when the start directory is your home directory. Claude Code reads `.claude/settings.json` of the start directory as project settings, and in `~` that file is `~/.claude/settings.json`, the source whose `enabledPlugins` the installer copies into every profile. A session started in `~` therefore installs every enabled plugin at project scope for your home directory, and those installs stay after the session ends. `start` compares resolved paths, so `~/.` and a symlink to `~` count as the home directory too, and a directory below `~` does not.

`start` prints why and exits with status 2. The refusal also covers `-p`, `--resume` and `--continue`, because Claude Code loads the project settings for them as for a new session. Subcommands such as `mcp list`, and `-v`/`--version` or `-h`/`--help` before a `--`, still run in `~`, because they start no session.

To start in `~` once, set `CC_AUTH_SWITCH_ALLOW_HOME=1` for the launch: `CC_AUTH_SWITCH_ALLOW_HOME=1 claude-personal`. `start` does not pass the variable on to Claude Code, so a launcher run from inside that session is refused again. To turn the check off, set `refuse_home_cwd: false` at the top level of the config for every profile, or in one profile. A profile's value wins over the top-level one:

```yaml
refuse_home_cwd: true

profiles:
  sandbox:
    refuse_home_cwd: false
```

### Additional directories

The `add_dirs` config option maps to Claude Code's `--add-dir` flag, giving the profile access to additional project directories:

```yaml
profiles:
  work-team:
    add_dirs:
      - ~/devel/projects/company
      - ~/devel/shared-libs
```

This replaces manual aliases like `alias claude-work='claude --add-dir ~/devel/projects/company'`. `start` passes each directory as `--add-dir=<dir>` before your own arguments. With `--add-dir <dir>`, Claude Code would also take the words after the directory as more directories, your prompt included.

### Pinned model versions

The `pinned_models` config option maps a model *class* — `sonnet`, `opus`, `haiku`, or `fable` — to a specific model version id, so the corresponding alias always resolves to that exact version instead of whatever the class currently points at:

```yaml
profiles:
  work-team:
    pinned_models:
      sonnet: claude-sonnet-4-5-20250929
      opus: claude-opus-4-8
```

Under the hood each entry becomes an `ANTHROPIC_DEFAULT_<CLASS>_MODEL` alias-resolution environment variable (`ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL`, `ANTHROPIC_DEFAULT_FABLE_MODEL`) that `cc-auth-switch start` sets. An explicit `env:` entry for the same variable wins over the `pinned_models`-derived value, so you can always override a single class by hand. Any class key outside the four valid ones, or an empty/non-string version, stops the install and every launch of the profile with an error.

These variables only take effect when you launch through the `claude-<name>` launcher. They are set at launch, not written into `settings.json`, so running plain `claude` is unaffected.

### Roles

A role is a named set of launch options: system prompt files, a model, tools, settings and more. Roles are a top-level `roles:` section, not part of a profile, so `claude-personal --agent-role dispatcher` and `claude-work-team --agent-role dispatcher` start the same role under different accounts.

```yaml
roles:
  dispatcher:
    description: "Runs worker sessions in sibling tmux windows"
    prompt:
      file: /home/you/devel/claude-code-plugins/plugins/agent-roles/roles/dispatcher.md
      mode: append
    model: opus
    effort: high
    permission_mode: auto
    autocompact: 250000
    settings:
      autoCompactEnabled: true
    allowed_tools: ["Bash(tmux:*)"]

  worker:
    prompt: { file: /home/you/devel/claude-code-plugins/plugins/agent-roles/roles/worker.md, mode: append }

profiles:
  work-team:
    default_role: worker
```

Pick a role with `--agent-role <name>` or `--agent-role=<name>`, right after the profile and before the arguments for `claude`: `claude-personal --agent-role dispatcher -p hi`. `start` consumes the flag and never passes it to `claude`. Further on, before a `--`, the flag is an error, because `claude` would reject it; inside a subcommand's arguments it belongs to the subcommand. A profile's `default_role` applies when the command line has no `--agent-role`, and `--agent-role none` starts without a role even then. A resumed session gets the role it ran as, see [Resuming a session with a role](#resuming-a-session-with-a-role). A role that the config does not define, in the flag or in `default_role`, stops the launch with an error. Subcommands such as `mcp list`, and the options `-v`/`--version` or `-h`/`--help` before a `--`, never get a role, so a missing prompt file does not break `claude-personal --version`.

`none` is the reserved name for "no role". `--agent-role none` starts without a role also when the profile has a `default_role`, and `default_role: none` makes that the profile's default. Such a launch sets `FP_CC_AUTH_SWITCH_ROLE=none` and none of the other role variables: no `FP_CC_AUTH_SWITCH_ROLE_PROMPT`, no `FP_CC_AUTH_SWITCH_ROLE_PROMPT_MODE`, no role `env`. A hook that injects a default role of its own, such as the `SessionStart` hook of the agent-roles plugin, reads `none` as "inject nothing". A launch where nothing chose a role, because the profile has no `default_role`, leaves `FP_CC_AUTH_SWITCH_ROLE` unset, and such a hook may apply its own default. The [start log](#profile-start-log) records the launch with `"role": "none"`, so a resume keeps it.

Every key maps to one `claude` flag, see the [configuration reference](#auth-switchyaml). Precedence is profile, then role, then your own flags, the last one winning:

- A single-value flag that you pass yourself, such as `--model`, `--effort` or `--permission-mode`, replaces the role's value. Your `--model` also drops the profile's `model`.
- List flags add up: the role's `--allowed-tools`, `--add-dir` and the others come next to the profile's and yours. `start` passes each list item as `--flag=value`, because Claude Code's option parser lets `--flag value` take the following words too, including your prompt.
- The role's `settings`, the [git config attribution](#attribution-from-git-config) and your own `--settings` are deep-merged into one `--settings` JSON, the role over the attribution and yours over both. Lists inside settings, such as `permissions.allow`, are concatenated.
- `extra_args` go in as written, after the role's other flags and before yours, for any `claude` flag that the role keys do not cover. Because your arguments follow right after them, every item must be an option that cannot take your first word as its value: an option without a value, such as `--verbose`, or a long option with its value in the same item, `--flag=value`, such as `--add-dir=/a` or `--debug=api`. The config check refuses a separate value item, an option that takes a value written without `=`, and an unknown option without `=`, naming the role and the item. Which options take no value comes from the option table of Claude Code 2.1.325, see [`cc-auth-switch start`](#cc-auth-switch-start-profile). A hidden option with a value, such as `--max-turns=3`, works in the `=` form.

No role string that reaches the `claude` command line or the environment may contain a NUL character. The prompt is one file in `prompt.file`, or several in the list `prompt.files`, never both. Each path must be absolute (`~` is expanded), and `prompt.files` may not name a file twice. When `start` cannot read a prompt file, because it does not exist, is not a regular file or lacks read permission, `start` stops with the path and the reason instead of starting without the role. This holds for a profile's `default_role` and a resumed session's role too. Several files reach the session as one file that joins them, see [Several prompt files](#several-prompt-files). `prompt.mode` decides how the text gets into the session:

- `append` (the default) passes `--append-system-prompt-file` with the prompt file. The text comes after Claude Code's own system prompt.
- `replace` passes `--system-prompt-file` with the prompt file. The text replaces Claude Code's system prompt.
- `none` passes no flag. A `SessionStart` hook of your own injects the text, reading the path of the prompt file from `FP_CC_AUTH_SWITCH_ROLE_PROMPT`.

The system prompt flags are used instead of a hook by default because Claude Code moves a hook's `additionalContext` above 10,000 characters into a file and keeps only a short preview in context. The system prompt has no such limit, and it survives compaction. If you pass the matching system prompt flag yourself, yours wins: `start` drops the role's flag, sets the prompt mode to `none` so that a hook can inject the role's text, and prints a note.

`start` clears the role variables that it inherits, then sets them for the role it applies, see [Environment variables](#environment-variables). The role's `env` is merged over the profile's. `start` lists the names it set in `FP_CC_AUTH_SWITCH_ROLE_ENV_KEYS`, each with a hash of its value, and a launch from inside that session clears a listed variable while it still holds the role's value. A worker started from a dispatcher's Bash therefore does not inherit the dispatcher role's env, and a value that you set yourself since, as in `FOO=mine claude-personal`, stays. The list carries hashes, not values, because every process started from the session inherits it, a tmux server included. A role cannot set the variables that the profile or `start` own, or that a launch from inside the session needs: `CLAUDE_CONFIG_DIR`, `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `PWD`, `OLDPWD`, `SHLVL`, `LANG`, `TERM`, `DISPLAY`, `WAYLAND_DISPLAY`, and every name that starts with `FP_CC_AUTH_SWITCH_`, `LC_`, `TMUX`, `XDG_`, `SSH_` or `LD_`.

#### Several prompt files

`prompt.files` lets a role combine a shared prompt with your own additions, for example a public `dispatcher.md` and a private file next to your config:

```yaml
roles:
  dispatcher:
    prompt:
      files:
        - /home/you/devel/claude-code-plugins/plugins/agent-roles/roles/dispatcher.md
        - ~/.claude/roles/dispatcher-private.md
      mode: append
```

Claude Code 2.1.325 keeps only the last `--append-system-prompt-file`, and the last `--system-prompt-file`, so `start` cannot pass the flag once per file. On every launch it reads the files in order and joins them into one file: each file as it is, with a newline added when it does not end with one, and a blank line between files. It writes the result to `${XDG_CACHE_HOME:-~/.cache}/cc-auth-switch/role-prompts/<role>-<hash>.md`, where `<hash>` is the start of the SHA-256 of the joined text, and passes that path everywhere a single prompt file goes: the flag, `FP_CC_AUTH_SWITCH_ROLE_PROMPT` and `role_prompt` in the [start log](#profile-start-log). A role with one file, in `file` or in a one-item `files`, gets the path of that file, as before.

- **Private.** The file has mode `0600`, and a directory that `start` creates has mode `0700`, because the text may be private.
- **Never changed under a session.** An edited source gives a new hash and a new file, so a running session keeps reading the text it started with, also when a hook re-reads the file on `/clear` or compaction. A launch with unchanged sources reuses the file and only updates its modification time.
- **Cleaned up after 30 days.** After `start` writes a new file, it deletes the files in that directory that no launch has used for 30 days. A session that runs longer and re-reads the file in mode `none` finds it gone, and `role_prompt` in an old start log may name a deleted file.
- **Stops the launch on failure.** When `start` cannot write the file, it stops with the error instead of starting without the role.

`FP_CC_AUTH_SWITCH_ROLE_PROMPT_FILES` and `role_prompt_files` in the start log list the source files.

#### Resuming a session with a role

A resume restores neither the role's tool rules nor its model unless they are passed again, so `start` looks up the role that the session ran as. It reads every [profile start log](#profile-start-log) of the session, `projects/*/<session-id>/auth-switch-profile-starts.jsonl` under the profile directory, takes the newest line by `ts`, and applies that role. When no line has a `ts`, or several share the newest one, the log in the start directory's project directory wins, then the log written last. A profile's `default_role` does not apply to a resume. What counts as a resume, and what `start` does:

- **`--resume <id>`, `-r <id>`, or `--session-id <id>` of a session that has a start log, without `--agent-role`:** `start` applies the recorded role. A session recorded with `"role": "none"` resumes without a role and with `FP_CC_AUTH_SWITCH_ROLE=none`, also when the profile has a `default_role`. A session without a recorded role, or without a readable log, resumes without a role and without the variable, silently, also when the profile has a `default_role`. A recorded role that the config no longer defines prints a warning, and the session resumes without a role.
- **The same with `--agent-role`:** your role applies. Claude Code records the system prompt on a conversation's first request and sends that copy again on every resume until the conversation compacts, so a new role prompt would not reach the model. When your role differs from the recorded one, `start` adds `--system-prompt-snapshot off` and prints a note. That run reuses less of the prompt cache. `--agent-role none` counts as a role here: it resumes a dispatcher without its role.
- **`-c`/`--continue`:** Claude Code picks the session, so `start` applies no role. When the newest session in the start directory ran with a role, `start` prints a notice that names it. With `--agent-role`, `start` compares your role with that newest session.
- **The picker:** `--resume` without a value, a value that is not a session id (a session name or a search term), and `--from-pr` get no role and no notice, because `start` cannot know which session you pick. With `--agent-role`, `start` always adds `--system-prompt-snapshot off`.

A `--session-id` with a new id starts a new session and gets the profile's `default_role` as usual. If you pass `--system-prompt-snapshot` yourself, `start` does not add its own.

### Secrets

The main config is meant to be committed. An API key inside an `env:` entry is not. Put those values in a secrets file that sits next to the main config and carries the same stem: `~/.claude/auth-switch.secrets.yaml` for the default location, `config.secrets.yaml` for the legacy one. The installer and every launch read it when it exists, and run normally when it does not.

```yaml
# ~/.claude/auth-switch.secrets.yaml
profiles:
  work-team:
    env:
      OTEL_EXPORTER_OTLP_HEADERS: "dd-api-key=<your-api-key>"
```

The file has the same shape as the main config, restricted to one thing: `profiles.<name>.env` with string values. Any other top-level key, any other key inside a profile, a profile name that the main config does not define, or a non-string env value stops the install and every launch with an error naming the offending key.

Secret env vars merge into the profile's `env` at every launch, and behave exactly like main-config env vars — they win over a value derived from `pinned_models`. Defining the same variable in both files is a fatal error instead of a silent precedence rule. The message names the profile and the variable, never the value.

The installer and `cc-auth-switch start` also refuse to use a secrets file that git could publish. Two checks run before the file is read:

1. The file is **tracked** in git → error. Remove it from the index with `git rm --cached`, then ignore it.
2. The file is inside a git repository and **not ignored** → error. Add its name to `.gitignore`.

A file outside any git repository passes both checks, and a missing `git` binary skips them. A git call that does not answer within 5 seconds is an error, because the file cannot be shown safe without it.

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

At launch, `cc-auth-switch start` reads the section for the start directory, `$PWD`. When it finds any of the keys, it passes them as `claude --settings '{"attribution": {...}}'`. To see what a directory gets, run `cc-auth-switch attribution <directory>`. The keys are the same as in the profile's `attribution`: `commit` and `pr` are strings, where an empty string turns the attribution off, and `sessionUrl` is a git boolean (`true`/`yes`/`on`/`1`, `false`/`no`/`off`/`0`). An invalid boolean or an unknown key prints a warning and is ignored. A key that is not set stays unset, so Claude Code falls back to the next source for it.

Which config applies depends on where you start Claude:

- **Inside a git repository** git reads the config itself, so every `include` and `includeIf` works as it does for `git config`, including globs, `gitdir/i:`, `onbranch:` and `hasconfig:`. The repository's own `.git/config` can set the section too.
- **Outside a git repository** git ignores every `gitdir:` condition. The script reads the global config and evaluates only `gitdir:` and `gitdir/i:` patterns that start with `~/`, `./` or `/`, end with `/`, and contain no `*`, `?` or `[`. Such a pattern matches the start directory and everything under it, by its path as given and by its resolved path. Other patterns are skipped. Starting in `~/devel/projects/company`, a directory that holds the company's repositories but is not one itself, uses the company attribution.

Precedence, per key: git config wins over the profile's `attribution` in `auth-switch.yaml`, which wins over `~/.claude/settings.json`. Claude Code deep-merges settings sources, so a gitconfig that sets only `commit` keeps `pr` and `sessionUrl` from the profile.

The attribution is fixed when Claude starts. A session started in a personal repository keeps the personal attribution even when it later works in a company directory added with `--add-dir`. Start Claude in the directory whose attribution you want.

Subcommands such as `claude-personal mcp list` do not get the setting. Claude Code keeps only the last `--settings` flag instead of merging them, so `start` passes a single `--settings` JSON, which also holds the [status line](#rate-limits-from-the-status-line). When you pass your own `--settings` to a launcher, as inline JSON or as the path of a JSON file, `start` reads it and deep-merges it last, so your keys win per key. Lists are concatenated without duplicates, as Claude Code merges its own settings sources. When you pass several, `start` uses the last one, as Claude Code would. A file that cannot be read or is not a JSON object stops the launch with the path and the error. When `start` has no settings of its own for the launch, it passes your `--settings` unchanged. It still reads the file to find your status line, see [Rate limits from the status line](#rate-limits-from-the-status-line); a file that it cannot read then only costs the status line tap, and Claude Code reports the file.

The two cases behave differently in Claude Code. Which one applies depends on whether the start directory has a git config attribution, whether the role has `settings`, and whether `start` puts its status line tap in front of a status line of yours, which it does by default when you have one, so the second case is the usual one:

- **Nothing to merge:** Claude Code gets your file path, as with plain `claude`. A relative path to the file resolves against the start directory, and Claude Code can see later changes to the file.
- **Something to merge:** Claude Code gets inline JSON with your file's content, not the file. It cannot see later changes to the file. `start` copies the values as they are, so a relative path inside the file, such as a `statusLine` command `./status.sh`, no longer resolves the way it would for the file. Write absolute paths in a settings file that you pass to a launcher.

If git cannot read the config, or a git call does not answer within 5 seconds, the launcher prints a one-line warning and starts Claude without the git config attribution.

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
{"v": 1, "ts": 1763290000000, "session_id": "<uuid>", "pid": 12345, "source": "startup", "profile": "work-team", "config_dir": "/home/you/.claude-profiles/work-team", "cwd": "/home/you/devel/projects/company/service", "role": "dispatcher", "role_prompt": "/home/you/devel/claude-code-plugins/plugins/agent-roles/roles/dispatcher.md", "role_prompt_mode": "append", "role_prompt_files": ["/home/you/devel/claude-code-plugins/plugins/agent-roles/roles/dispatcher.md"]}
```

- `ts` is the time in epoch milliseconds.
- `pid` is the Claude Code process, the one that writes `sessions/<pid>.json`. It is `null` when the hook cannot find it.
- `source` is `startup`, `resume`, `clear`, `compact` or `fork`.
- `profile` and `config_dir` come from `FP_CC_AUTH_SWITCH_PROFILE` and `CLAUDE_CONFIG_DIR`, and are `null` when a session starts without a launcher.
- `role`, `role_prompt`, `role_prompt_mode` and `role_prompt_files` come from `FP_CC_AUTH_SWITCH_ROLE`, `FP_CC_AUTH_SWITCH_ROLE_PROMPT`, `FP_CC_AUTH_SWITCH_ROLE_PROMPT_MODE` and `FP_CC_AUTH_SWITCH_ROLE_PROMPT_FILES`, see [Environment variables](#environment-variables). Each is `null` when its variable is unset, and `role_prompt_files` also when its value is not a JSON array of strings. `role_prompt` is the file whose text the session got: the role's only prompt file, or the file that joins [several prompt files](#several-prompt-files), which the cleanup may have deleted since. `role_prompt_files` lists the role's prompt files. `role` is `"none"` when the launch explicitly asked for no role, and `null` when nothing chose one or the session started without a launcher. Lines written before these fields existed lack them; read a missing field as `null`.

[cc-blackbox](https://github.com/fprochazka/cc-blackbox) reads this file. Treat the format as a contract: a change needs a new `v`.

Claude Code puts the stdout of a `SessionStart` hook into the model context, so the hook prints nothing and always exits 0. It writes errors to `auth-switch-session-start.log` in the profile directory.

Claude Code deletes the session directory, and the log with it, when it cleans up old transcripts. A session quit before its first prompt has no transcript, and the cleanup leaves its directory behind, because it does not delete files it does not know.

### Rate limits from the status line

Claude Code passes the account's rate limits only to the [status line](https://code.claude.com/docs/en/statusline) command, as `rate_limits` in the JSON on its stdin: the five-hour and weekly windows of a claude.ai subscription, each with `used_percentage` and `resets_at`, and the spend limit of a Claude apps gateway. Reading them needs no credentials and no API call. To keep them for other tools, `start` passes its own status line in the `--settings` JSON of every session it launches: `cc-auth-switch statusline --delegate '<your status line command>'`. On every status line update it:

1. merges `rate_limits` into `auth-switch-rate-limits.json` in the profile directory, writing a temporary file next to it and renaming it into place, with mode `0600`;
2. replaces itself with `/bin/sh -c '<your command>'`, with the same bytes on stdin. Your command runs in the same process, directory and environment as when Claude Code runs it, and its output and exit status are its own. Before that, the tap undoes what Python changed in its own process: it stops ignoring `SIGPIPE` and `SIGXFSZ`, and puts `LC_CTYPE` back to its value at start, which Python may have set to coerce a C locale to UTF-8.

The tap is a Python program. A `PYTHONHOME` or `PYTHONPATH` in the `env` of your settings, or in the environment you start the launcher in, reaches it too and can stop it from starting; the status line then shows nothing.

**Only with a status line of your own.** Without one, `start` passes no status line and the profile records no rate limits. Any configured status line changes Claude Code's screen: it hides the hint row under the prompt, and in a folder you have not trusted it asks for workspace trust first. A session without a status line therefore keeps having none. To record the limits of such a profile, configure a status line, even one that prints nothing.

A payload without `rate_limits`, as before a session's first response or for an API key or Vertex AI account, writes nothing. An error never reaches the status line: it goes to stderr, which Claude Code shows only in `claude --debug`. On this machine the tap adds about 10 ms to each status line run (`uv run pytest -m perf -s` measures it).

**Which status line is yours.** `start` resolves it when it launches the session, as Claude Code would without the launcher: the `statusLine` of the profile's `settings.json`, then the start directory's `.claude/settings.json`, then `.claude/settings.local.json`, then your own `--settings`, each one over the one before. Claude Code 2.1.325 reads `settings.local.json` from the main checkout of the git repository that the start directory is in, also from a subdirectory or a linked worktree, and `start` does the same. `--setting-sources` leaves out the files it leaves out. The tap copies `padding`, `refreshInterval` and `hideVimModeIndicator` from your status line. A statusLine command that you change in a settings file during a session applies at its next launch, because Claude Code sees only the launcher's command; changes to your script apply at once, as before.

`start` passes no status line when managed settings set a `statusLine` or `allowManagedHooksOnly`, because Claude Code would ignore it, and when `capture_rate_limits: false` is set at the top level of the config or in the profile, the profile's value winning. Turn it off for a profile that never gets rate limits, such as a Vertex AI one, to save the few milliseconds per update.

The file holds one entry per window, merged from every session of the profile:

```json
{"v": 1, "profile": "work-team", "written_at": 1763290000000, "session_id": "<uuid>", "model": "claude-opus-5-5", "claude_version": "2.1.325", "windows": {"five_hour": {"used_percentage": 23.5, "resets_at": 1763300000, "observed_at": 1763290000000, "changed_at": 1763289000000, "session_id": "<uuid>"}, "seven_day": {"used_percentage": 41.2, "resets_at": 1763800000, "observed_at": 1763290000000, "changed_at": 1763200000000, "session_id": "<uuid>"}}}
```

- `written_at`, `observed_at` and `changed_at` are epoch milliseconds; `resets_at` is Claude Code's epoch seconds. Claude Code's other keys of a window, such as a spend limit's `used_usd`, `limit_usd` and `period`, are kept as given.
- `observed_at` is the last time a session reported the stored value, `changed_at` when the value last changed, and `session_id` the session that reported it. The top-level `session_id`, `model` and `claude_version` are those of the session that wrote the file last.
- **The merge.** A session reports the values of its own last response, so an idle one sends numbers hours old. Within a window the usage only grows, and the next window resets at least one window length later, so the tap keeps the higher value of a window, takes a window that resets later, ignores one that resets earlier, and keeps a window that a payload no longer has: Claude Code drops a window once its reset time has passed. A spend limit, whose length the tap does not know, counts as the same period while its reset time moves by less than five minutes. Unchanged values are written again at most once a minute. A session holds an `flock` on `auth-switch-rate-limits.lock` while it reads, merges and replaces the file, and writes without it when another session holds it for more than 50 ms.
- **What it cannot see.** A real decrease within a window, such as a `/login` to another account in the same profile, shows only when the window rolls over or after you delete the file. Usage from other machines or claude.ai shows with the next response of a session here.

[cc-blackbox](https://github.com/fprochazka/cc-blackbox) reads this file. Treat the format as a contract: a change needs a new `v`.

### Sharing sessions between profiles

Claude Code finds other local sessions, for `ListAgents` and for `SendMessage` by name, only in `sessions/` of its own config directory. Sessions of different profiles therefore cannot see each other. Set `share_sessions: true` on a profile to point its `sessions/` at `~/.claude/sessions`, the directory of plain `claude`. Every sharing profile, and plain `claude`, then sees the sessions of all the others. The flag is off by default.

```yaml
profiles:
  work-team:
    share_sessions: true
```

What sharing exposes:

- **The accounts are no longer isolated from each other.** A session of one profile can list the sessions of another and send them messages. The receiving session acts on such a message with its own account. On Linux, the message socket does not check the sender at all, so sharing changes discovery, not who can deliver a message.
- **Session names become machine-wide.** Claude Code keeps names unique among the live sessions of one `sessions/` directory. A second session with a taken name gets a suffix, also across profiles.
- **FleetView affects every sharing profile.** While FleetView watches the directory, every session that writes its record there also writes its state classification. One heartbeat file turns that on for all sharing profiles at once.

The installer migrates a profile whose `sessions/` is a real directory while its sessions run:

1. It copies every file into `~/.claude/sessions`, keeping the modification time, and keeps a newer copy that is already there. Each copy goes to a temporary file `.<name>.<random>.cc-auth-switch-tmp` and is then renamed into place. Every copy pass first deletes such files that an interrupted run left behind.
2. It swaps the directory for the symlink in one system call: `renameat2(RENAME_EXCHANGE)` on Linux and `renamex_np(RENAME_SWAP)` on macOS. Where neither works, it renames twice in a row.
3. It keeps the old directory as `sessions.bak-<timestamp>` in the profile directory.
4. After 1.5 seconds it copies the files that are newer in the backup, for writes that reached the old directory just before the swap. It also copies a backup file whose content differs when the copy in `~/.claude/sessions` is still exactly what step 1 put there, whatever the modification times say. A session rewrites its record by emptying it and writing it again, and both writes can carry the same modification time, so step 1 may have copied the empty file. It repeats this once and warns when the second pass still finds something.
5. It prints the backup path and the rollback command.

A profile whose `sessions/` already points at `~/.claude/sessions` is left alone. A `sessions/` symlink to anywhere else is skipped with a warning. A leftover `sessions.swap` from an interrupted run stops the installer, unless it is a symlink to `~/.claude/sessions`.

To undo the sharing, set `share_sessions: false` and run:

```bash
cc-auth-switch rollback-sessions work-team
```

The rollback replaces the symlink with a real directory and moves back the records of the profile's live sessions. It finds the owner of a record in the [profile start log](#profile-start-log), then in `/proc/<pid>/environ` on Linux, then in the migration backups. It deletes the records of ended sessions that belong to the profile and leaves everything else in `~/.claude/sessions`. Turning the flag off does not roll back by itself: the installer only warns that `sessions/` is still shared.

The migration has run on Linux for four profiles while about 30 Claude Code sessions were running in them. Afterwards every session still had its record, `ListAgents` listed the sessions of all four profiles, and a message to a session of another profile got a reply. The rollback has run only against simulated writers in a scratch directory, not under live sessions. The macOS `renamex_np` branch has not run on a Mac. Keep the backup until you have checked that every session still lists and receives messages, then delete it by hand.

## Configuration reference

### `auth-switch.yaml`

The default config path is `~/.claude/auth-switch.yaml`. When that file does not exist, the installer falls back to the legacy `~/.claude-profiles/config.yaml` and prints a note naming the preferred location. `--config PATH` overrides both.

| Field | Default | Description |
|-------|---------|-------------|
| `source_dir` | `~/.claude` | Directory to copy/symlink shared config from |
| `bin_dir` | `~/.local/bin` | Where to install the `claude-<name>` launchers |
| `refuse_home_cwd` | `true` | Boolean. Refuse to start a session in the home directory. See [Starting in the home directory](#starting-in-the-home-directory). |
| `capture_rate_limits` | `true` | Boolean. Put the status line tap in front of your status line, to record the rate limits. See [Rate limits from the status line](#rate-limits-from-the-status-line). |
| `profiles_dir` | `~/.claude-profiles` | Where to create the per-profile config directories. `~` is expanded. | Only the profiles listed in the config are managed: the installer never reads for writing, changes or deletes anything in another directory under `profiles_dir`.
| `profiles.<name>.description` | `""` | Human-readable profile description |
| `profiles.<name>.model` | | Default model to use (e.g., `sonnet`, `opus`). Overridden by `--model` on the command line. |
| `profiles.<name>.pinned_models` | `{}` | Map of model class (`sonnet`, `opus`, `haiku`, `fable`) to a specific version id. Set as `ANTHROPIC_DEFAULT_<CLASS>_MODEL` env vars at launch. See [Pinned model versions](#pinned-model-versions). |
| `profiles.<name>.env` | `{}` | Environment variables to set before launching Claude. A name must be a valid shell variable name: letters, digits and `_`, not starting with a digit. Values are passed as written, without shell expansion of `$` or backticks, and must not contain a NUL character. |
| `profiles.<name>.add_dirs` | `[]` | Additional directories to pass via `--add-dir` |
| `profiles.<name>.attribution` | | Attribution settings object patched into the profile's `settings.json` on every install. A `[claude-code "attribution"]` git config section overrides it per key, see [Attribution from git config](#attribution-from-git-config). |
| `profiles.<name>.attribution.commit` | | Attribution text appended to git commit messages |
| `profiles.<name>.attribution.pr` | | Attribution text appended to pull request descriptions |
| `profiles.<name>.attribution.sessionUrl` | | Boolean. Keeps (`true`) or drops (`false`) the `Claude-Session: <url>` trailer that web/Remote Control sessions add to commits and PRs. Independent of `commit`/`pr`; omit to keep Claude Code's default. Set `false` to avoid leaking session links into public repo history. |
| `profiles.<name>.plansDirectory` | | Path written to the profile's `settings.json` as `plansDirectory`. `~` is expanded at install time. |
| `profiles.<name>.plugins` | `{}` | Map of `"<plugin>@<marketplace>"` to `true` or `false`, applied on top of the `enabledPlugins` copied from `source_dir`. See [Plugins](#plugins). |
| `profiles.<name>.share_sessions` | `false` | Boolean. Point the profile's `sessions/` at `~/.claude/sessions`, so that its sessions and those of other sharing profiles and plain `claude` can list and message each other. See [Sharing sessions between profiles](#sharing-sessions-between-profiles). |
| `profiles.<name>.refuse_home_cwd` | top-level `refuse_home_cwd` | Boolean. Wins over the top-level `refuse_home_cwd` for this profile. |
| `profiles.<name>.capture_rate_limits` | top-level `capture_rate_limits` | Boolean. Wins over the top-level `capture_rate_limits` for this profile. |
| `profiles.<name>.default_role` | | Name of a role under `roles:` that applies when the command line has no `--agent-role`, or `none` for no role. See [Roles](#roles). |
| `roles.<name>.description` | | Human-readable role description |
| `roles.<name>.prompt.file` | | Absolute path of the role's prompt file. `~` is expanded. A file that `start` cannot read stops the launch. |
| `roles.<name>.prompt.files` | | List of absolute paths of prompt files, instead of `prompt.file`. `~` is expanded. `start` joins them in this order into one file, see [Several prompt files](#several-prompt-files). |
| `roles.<name>.prompt.mode` | `append` | `append` passes `--append-system-prompt-file`, `replace` passes `--system-prompt-file`, `none` passes nothing and leaves the text to a hook. |
| `roles.<name>.model` | | `--model`. Replaces the profile's `model`; your own `--model` replaces both. |
| `roles.<name>.effort` | | `--effort` |
| `roles.<name>.permission_mode` | | `--permission-mode` |
| `roles.<name>.autocompact` | | `--autocompact`, a number of tokens or `auto` |
| `roles.<name>.settings` | `{}` | Settings object, deep-merged into the one `--settings` JSON that `start` passes |
| `roles.<name>.tools` | `[]` | `--tools`. An empty list passes no flag. |
| `roles.<name>.allowed_tools` | `[]` | `--allowed-tools` |
| `roles.<name>.disallowed_tools` | `[]` | `--disallowed-tools` |
| `roles.<name>.add_dirs` | `[]` | `--add-dir`, added to the profile's `add_dirs`. `~` is expanded. |
| `roles.<name>.plugin_dirs` | `[]` | `--plugin-dir`. `~` is expanded. |
| `roles.<name>.mcp_config` | `[]` | `--mcp-config`, each a JSON file path or inline JSON |
| `roles.<name>.strict_mcp_config` | `false` | `--strict-mcp-config` |
| `roles.<name>.agents` | | `--agents`: a mapping of agents, a JSON string, or the path of a JSON file |
| `roles.<name>.env` | `{}` | Environment variables, merged over the profile's `env`. Names and values follow the rules of `profiles.<name>.env`. Reserved names are refused, see [Roles](#roles). |
| `roles.<name>.extra_args` | `[]` | More `claude` options, passed as written after the role's other flags. Each item is an option without a value or `--flag=value`. |

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

### `cc-auth-switch start <profile>`

```bash
cc-auth-switch start [--config PATH] <profile> [claude arguments...]
```

The `claude-<name>` launchers run this command with the `--config` path that `install` used. Everything after the profile goes to `claude`, except `--agent-role` and a `--settings` that `start` merges into its own. In this order, `start`:

1. Reads the config and the secrets file, and resolves the profile's `env`, `pinned_models`, `model`, `add_dirs`, `default_role`, `refuse_home_cwd` and `capture_rate_limits`, and the `roles:` section.
2. Removes `--agent-role` from the start of the arguments.
3. Drops every inherited `CLAUDE_*` and `ANTHROPIC_*` variable, the `FP_CC_AUTH_SWITCH_ROLE*` variables, and every variable that `FP_CC_AUTH_SWITCH_ROLE_ENV_KEYS` names while it still holds the role's value, so a launch from inside another Claude Code session does not inherit its account or its role. Then it sets `CLAUDE_CONFIG_DIR`, `FP_CC_AUTH_SWITCH_PROFILE` and the profile's `env`. It also drops `CC_AUTH_SWITCH_ALLOW_HOME`.
4. Passes the arguments through when the first one is a Claude Code subcommand such as `mcp`, `auth` or `plugin`, because subcommands reject the session options. It does the same when the option `-v`, `--version`, `-h` or `--help` comes before any `--`, because `claude` then prints and exits without a session. The value of another option, as in `--name -v`, does not count.
5. Otherwise stops when the start directory is the home directory, unless `refuse_home_cwd` is off for the profile or `CC_AUTH_SWITCH_ALLOW_HOME=1` is set. See [Starting in the home directory](#starting-in-the-home-directory).
6. Otherwise puts its own arguments before yours, and picks the [role](#roles), also the role a [resumed session](#resuming-a-session-with-a-role) ran as, sets its `env` and the role variables, and adds `--add-dir=<dir>` for each `add_dirs` entry of the profile and the role, `--settings` with the [attribution from git config](#attribution-from-git-config), the role's `settings` and your own `--settings` merged into it and the [status line tap](#rate-limits-from-the-status-line) in place of every `statusLine`, the [tmux session name](#session-names-in-tmux), the role's flags, `--system-prompt-snapshot off` when a resume switches the role, and `--model`. It adds the name, the model and the role's single-value flags only when you did not pass the same option yourself.
7. Prints its notes to stderr, and replaces itself with `claude`.

A config that is missing, cannot be parsed, or fails validation stops the launch. So does a profile that the config no longer defines, a secrets file that is invalid, unreadable or not ignored by git, a start in the home directory, an unknown `--agent-role`, a role whose prompt file `start` cannot read, a joined prompt file that `start` cannot write, and a `--settings` file that `start` cannot read. `start` then prints the config path and the error and exits with a non-zero status, without starting Claude. Any other error in `start` also stops the launch, with one line that names the profile, the config path and the error. Fix the config and launch again; a reinstall is only needed for the settings that `install` writes into the profile directory.

`start` reads your arguments with the option table of `claude --help` from Claude Code 2.1.325, the way Claude Code's option parser does, so it tells an option from the value of another one. An option with a value takes the next argument even when that looks like an option: in `--name --model`, `--model` is the session name, and `start` still adds the profile's model. An option with an optional value, such as `--resume` or `--debug`, takes the next argument only when it does not start with `-`. A list option, such as `--add-dir`, takes the arguments up to the next one that starts with `-`. An option that the table does not know, such as a hidden one, is read as taking no value. A newer Claude Code may add options that the table does not know yet.

| Flag | Description |
|------|-------------|
| `--config PATH` | Config file path, as for `install` |
| `--agent-role NAME`, `--agent-role=NAME` | Apply the role `NAME` from `roles:`, or no role with `none`. Goes right after the profile, before the `claude` arguments. See [Roles](#roles). |

### `cc-auth-switch rollback-sessions <profile>`

| Flag | Description |
|------|-------------|
| `--config PATH` | Config file path, as for `install` |

### Shared file handling

| Type | Files | Behavior |
|------|-------|----------|
| **Copied** | `settings.json`, `settings.local.json`, `keybindings.json`, and all top-level `*.md` files (e.g. `CLAUDE.md`) | Copied on first install. Use `--sync` to refresh from source. |
| **Symlinked** | `commands/`, `skills/`, `agents/`, `plugins/`, `projects/`, `scripts/` | Always symlinked. Changes are shared across all profiles. |
| **Patched** | `settings.json` (`attribution`, `plansDirectory`, `hooks.SessionStart`) | Set from profile config on every install. The installer creates the file when `source_dir` has none. See [Profile start log](#profile-start-log) for the hook. |
| **Per launch** | `--settings` (`attribution`, the role's `settings`, `statusLine`) | Read from git config for the start directory, from the role and from your status line every time a launcher starts Claude. Never written to disk. See [Attribution from git config](#attribution-from-git-config), [Roles](#roles) and [Rate limits from the status line](#rate-limits-from-the-status-line). |
| **Written by the status line** | `auth-switch-rate-limits.json` | The rate limits that the profile's sessions reported. See [Rate limits from the status line](#rate-limits-from-the-status-line). |
| **Synced on every install** | `settings.json` (`enabledPlugins`, `extraKnownMarketplaces`) | Copied whole from `source_dir`'s `settings.json`, then the profile's `plugins:` overrides are applied. See [Plugins](#plugins). |
| **Selectively synced** | `.claude.json` (`mcpServers`) | Only the `mcpServers` key is synced from `~/.claude.json` on every install. Other keys in `.claude.json` are per-profile state. |
| **Shared on request** | `sessions/` | A real per-profile directory by default. With `share_sessions: true`, a symlink to `~/.claude/sessions`. |
| **Per-profile** | `.credentials.json`, `.claude.json`, `teams/`, `history.jsonl` | Created automatically by Claude Code on first use. |

## Re-syncing after config changes

```bash
# Added or removed a profile, or changed attribution, plansDirectory, plugins or share_sessions → re-run install
# (also syncs mcpServers from ~/.claude.json into each profile).
# env, model, add_dirs, pinned_models, default_role, refuse_home_cwd, capture_rate_limits, roles and the secrets file apply at the next launch without it.
cc-auth-switch install

# Changed ~/.claude/settings.json and want profiles to pick it up
# (will show a diff and stop if any profile file has diverged)
cc-auth-switch install --sync

# Force overwrite even if profile files have diverged
cc-auth-switch install --sync --force
```

Before any `cc-auth-switch install`, with or without `--sync` and `--force`, changes an existing file in a profile's root, it copies the old file to `<profile>/.auth-switch-backups/<UTC timestamp>/` and prints one line with the backup path. That covers the files that `--sync` replaces (`settings.json`, `settings.local.json`, `keybindings.json`, a top-level `*.md`), the patches to `settings.json` (`attribution`, `plansDirectory`, the plugin keys, the hook) and the `mcpServers` sync into `.claude.json`. When a run writes a file twice, the backup holds the content from before the run. Files that the run creates, and the shared symlinked directories, are never backed up. The installer writes the new content to a temporary file next to the target and compares the bytes. When they are equal, it deletes the temporary file and leaves the target alone, without a backup. Otherwise it backs up the target and replaces it atomically, keeping the target's file mode. A run that changes nothing therefore creates no backup directory, and prints only `in sync, nothing changed` under each profile and `nothing to clean up` under `Cleanup:`. That makes `--sync --force` safe to run unattended, for example from an upgrade script.

The tool never deletes a backup. Remove the directories with `rm` whenever you no longer need them, for example `rm -r ~/.claude-profiles/*/.auth-switch-backups`. A run that backed something up says so in its last line.

The installer is idempotent:
- Existing profile directories are never deleted
- Existing copied files are not overwritten (unless `--sync`)
- `--sync` detects diverged files (profile has changes not in source) and shows a diff before overwriting
- Symlinks are updated if their target changed
- `mcpServers` from `~/.claude.json` are synced into each profile on every run (not just `--sync`)
- Launchers of removed profiles, and the bash wrappers that earlier versions generated, are removed

## Vertex AI notes

When using a Vertex AI profile, the launcher sets the required environment variables (`CLAUDE_CODE_USE_VERTEX`, `ANTHROPIC_VERTEX_PROJECT_ID`, `CLOUD_ML_REGION`). Make sure these variables are **not** also set in your `~/.claude/settings.json` `env` section, or they will apply to all profiles (since `settings.json` is copied to each profile).

Authentication uses [Google Cloud Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials):

```bash
gcloud auth application-default login
```

## macOS and WSL notes

Nobody has run this tool on macOS or WSL. Everything in this section comes from reading the Claude Code 2.1.289 binary, so treat it as expected behavior, not a tested result. If you try it, please open an issue with what you saw.

### macOS

Claude Code on macOS stores the OAuth login in the Keychain and falls back to `.credentials.json` in the config directory. When `CLAUDE_CONFIG_DIR` is set, the Keychain service name gets a suffix: `Claude Code-credentials-<hash>`, where `<hash>` is the first 8 hex characters of the SHA-256 of the `CLAUDE_CONFIG_DIR` value. Without the variable, the name is plain `Claude Code-credentials`. Each launcher sets a different `CLAUDE_CONFIG_DIR`, so each profile should get its own Keychain entry, and profiles should not overwrite each other's logins. The [Binary Analysis](docs/Binary-Analysis-and-Design-Decisions.md#4-macos-keychain-entry-per-config-directory) doc has the code.

[Issue #20553](https://github.com/anthropics/claude-code/issues/20553) reported that all profiles shared one Keychain entry. That was true in early 2026. A user in that issue confirmed per-directory entries on macOS in Claude Code 2.1.56, but the issue is still open.

To see which entry a profile uses:

```bash
hash=$(printf '%s' "$HOME/.claude-profiles/personal" | shasum -a 256 | cut -c1-8)
security find-generic-password -s "Claude Code-credentials-$hash" -a "$USER"
```

Gotchas:

- **The hash covers the exact string.** Claude Code does not expand `~`, strip a trailing slash or resolve symlinks before hashing. If you move `profiles_dir`, or point it at the same directory through a different path, every profile reads as logged out and the old Keychain entries stay behind. The installer writes the expanded absolute path, so re-running it with the same config keeps the same entries.
- **Never export `CLAUDE_CONFIG_DIR=~/.claude`.** The explicit default path produces a hashed name, not the plain `Claude Code-credentials` entry that your existing login uses, so plain `claude` reads as logged out. The launchers drop every inherited `CLAUDE_*` variable, so they are not affected. [cenv](https://github.com/lukabudik/cenv) documents the same gotcha.
- **A login in `~/.claude` does not carry over.** Log in once in every profile, as on Linux.
- **Re-login after a reboot.** One user in issue #20553 reported that macOS blocked reads of the per-directory entries after a restart, because of the entry's access control list. The binary reads the Keychain by running the `security` command (`/usr/bin/security`), which that list allows, so this may not apply. Nobody has checked.

Other macOS differences:

- The launchers are one-line bash scripts that `exec` `cc-auth-switch start`, so the bash 3.2 that macOS ships runs them.
- `~/.local/bin` is not on the macOS `$PATH` by default. Add it, or set `bin_dir` to a directory that is.

### WSL

Claude Code under WSL is the Linux build. The Linux build stores the login only in `.credentials.json` in the profile directory, and does not use the Windows Credential Manager. The isolation works the same as on Linux.

For the browser login, Claude Code runs `$BROWSER`, or `xdg-open` when `$BROWSER` is unset. The binary has no WSL-specific browser opener. If no browser opens, Claude Code prints the login URL, and you open it in Windows by hand.

### Native Windows

Not supported. The launchers are bash scripts.

## Environment variables

`cc-auth-switch start` sets the following environment variables before it runs `claude`:

| Variable | Example | Description |
|----------|---------|-------------|
| `CLAUDE_CONFIG_DIR` | `~/.claude-profiles/personal` | Points Claude Code at the profile's isolated config directory |
| `FP_CC_AUTH_SWITCH_PROFILE` | `personal` | The active profile name — useful in [status line scripts](https://docs.anthropic.com/en/docs/claude-code/settings#status-bar) and hooks |
| `FP_CC_AUTH_SWITCH_ROLE` | `dispatcher` | The [role](#roles) of this start. `none` when `--agent-role none`, `default_role: none` or a resumed session's start log asked for no role. Unset when nothing chose a role. |
| `FP_CC_AUTH_SWITCH_ROLE_PROMPT` | `/home/you/roles/dispatcher.md` | The absolute path of the role's prompt file, or of the file that joins its [several prompt files](#several-prompt-files). Unset when the role has no prompt. |
| `FP_CC_AUTH_SWITCH_ROLE_PROMPT_FILES` | `["/home/you/roles/dispatcher.md"]` | The role's `prompt.file` or `prompt.files`, as a JSON array of absolute paths. Unset when the role has no prompt. |
| `FP_CC_AUTH_SWITCH_ROLE_ENV_KEYS` | `DISPATCH_LEVEL=6b86b273ff34fce1,TEAM=…` | Each variable that the role's `env` set, as `NAME=<first 16 hex characters of the SHA-256 of the value>`, comma-separated. The next launch from inside the session drops a listed variable while its value still matches. Unset when the role sets none. |
| `FP_CC_AUTH_SWITCH_ROLE_PROMPT_MODE` | `append` | `append` or `replace`: the prompt is already in the system prompt. `none`: a hook should inject it. Unset when the role has no prompt. |

The `FP_CC_AUTH_SWITCH_PROFILE` variable is intentionally outside the `CLAUDE_*`/`ANTHROPIC_*` namespace so a nested launch does not drop it together with the inherited `CLAUDE_*`/`ANTHROPIC_*` variables. You can use it in your `statusLine` command to display which profile is active:

```bash
# In your status line script:
if [ -n "${FP_CC_AUTH_SWITCH_PROFILE:-}" ]; then
    echo "Profile: $FP_CC_AUTH_SWITCH_PROFILE"
fi
```

`start` always clears the role variables before it sets them, so a launcher run from another session's Bash does not inherit that session's role. Hooks should read these variables to learn the role of the current start: Claude Code does not order `SessionStart` hooks from different sources, so another hook may run before the [profile start log](#profile-start-log) has the line for this start. The start log is the place to read a session's role afterwards, as a resume does. Without a launcher, you can set the variables by hand before you run `claude`.

## Limitations

- **Tested on Linux only.** macOS and WSL should work based on the binary, but nobody has tested them. See [macOS and WSL notes](#macos-and-wsl-notes).
- **Same-directory parallel execution** is not supported. Two profiles in the same project directory will fight over `.claude/history.jsonl.lock`. Use [git worktrees](https://git-scm.com/docs/git-worktree) if you need this.
- **Copied files can diverge.** When Claude Code modifies `settings.json` in a profile (e.g., accepting a permission), the change is local to that profile. Run `cc-auth-switch install --sync` to reset from source.

## Documentation

- [Binary Analysis and Design Decisions](docs/Binary-Analysis-and-Design-Decisions.md) — Local research verifying `CLAUDE_CONFIG_DIR` behavior, with corrections to prior Gemini research
- [Gemini: Symlink Behavior Research](docs/Gemini-Symlink-Behavior-Research.md) — Gemini Deep Research report on Claude Code's file I/O and symlink handling
- [Gemini: Managing Multiple Claude Accounts](docs/Gemini-Managing-Multiple-Claude-Accounts.md) — Gemini Deep Research report on multi-tenant Claude Code architecture

## Developing

The tool is a Python package in `src/claude_code_auth_switch/`, built with hatchling. [uv](https://docs.astral.sh/uv/) manages the environment, and `uv.lock` pins the dependencies. GitHub Actions runs `.github/workflows/ci.yml` on every push to `master` and on every pull request. It runs ruff and mypy, and the tests on Python 3.12 to 3.15 on Linux, and on Python 3.12 and 3.15 on macOS. The macOS jobs cover the tests, not a real Claude Code login, so the [macOS notes](#macos-and-wsl-notes) still apply.

### Commit hooks

The research behind this tool comes from real machines and real accounts. Home paths, account names, cloud project IDs and company names show up in terminals and binary dumps, and from there they can slip into a commit. `scripts/hooks` holds a pre-commit and a commit-msg hook. They refuse a commit when a staged line or the commit message matches a private identifier. The identifiers live in a file outside the repository, because a list in the repository would publish the strings that the hooks keep out. Set it up once per clone:

```bash
git config core.hooksPath scripts/hooks
git config hooks.forbiddenPatternsFile ~/.config/git/forbidden-patterns.txt  # any path outside the repo
```

The file holds one extended regex per line, matched case-insensitively. Blank lines and `#` comments are ignored. Until `hooks.forbiddenPatternsFile` points at a file with at least one pattern, both hooks refuse every commit and print how to fix it. The hooks use `mapfile`, so they need bash 4 or newer. Docs and examples use placeholder names such as `<your-project-id>` and `~/devel/projects/company`.

### Tests

The tests use real git with a throwaway home directory and never touch your own config. Run them with uv:

```bash
uv run pytest
```

`uv run` installs the package into the project environment, so the tests find `cc-auth-switch` on `PATH`.

The package supports Python 3.12 and newer. uv runs the tests on the newest Python it finds, and Python 3.14 evaluates annotations lazily, so code that fails to import on an older Python can still pass there. CI runs every supported version. To catch such a failure before you push, run the tests on the oldest one too:

```bash
uv run --isolated --python 3.12 pytest
```

### Linters

CI fails when any of these fails:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

`uv run ruff format .` and `uv run ruff check --fix .` fix most findings. mypy runs in strict mode on `src/` and `tests/`. Test functions and fixtures may stay unannotated, through an override in `pyproject.toml` that lists each test module by name, because `tests/` has no `__init__.py`. When you add a test file, add its module name to that list, or mypy reports every unannotated function in it.

### Trying a change locally

To run a checkout as the installed tool, install it as an editable uv tool:

```bash
uv tool install --force --editable .
```

An editable install runs whatever the checkout holds at the moment, with no reinstall. Every launcher and every Claude Code session that you start through one uses it at once, also while the checkout is on a half-finished branch. Make changes in a separate git worktree when your own sessions run from the installed checkout.

To see what `cc-auth-switch install` writes without touching your profiles, run it with `--config` pointing at a temporary `auth-switch.yaml` whose `bin_dir` and `profiles_dir` are temporary directories. Leave `share_sessions` off in that config, because it links `sessions/` to `~/.claude/sessions`.

To see what a launch runs, put a fake `claude` that prints its arguments and environment first on `PATH`, then run `start` with the same temporary config. It needs no `install` before, and it replaces itself with the fake. Inside tmux the output also has the `--name` of your pane.

```bash
PATH=/tmp/fake-claude-dir:$PATH uv run cc-auth-switch start --config /tmp/auth-switch.yaml work -p hello
```

An unexpected error in `start` prints one line instead of a traceback. To get the traceback, set `CC_AUTH_SWITCH_DEBUG=1` for the launch, for example `CC_AUTH_SWITCH_DEBUG=1 claude-work -p hello`. The name is outside `CLAUDE_*`, so `start` does not drop it.

## Releasing

The tool has no releases: no version bumps, no tags, no PyPI package and no GitHub releases. The version in `pyproject.toml` stays at `0.1.0`. A change reaches users when it lands on `master`:

1. Run the tests on the default Python and on Python 3.12, see [Tests](#tests), and the [linters](#linters).
2. Push the commit to `master`.
3. Check that the CI run of the commit is green, for example with `gh run list --branch master`. A red run on `master` is the next thing to fix.

Users then update in two steps:

1. Update the tool. An install from GitHub updates with `uv tool upgrade claude-code-auth-switch`, or with `uv tool install --reinstall git+https://github.com/fprochazka/claude-code-auth-switch.git`. An editable install updates when you pull its checkout. For an editable install, `uv tool upgrade` only refreshes the dependencies.
2. Run `cc-auth-switch install`. It regenerates the launchers and patches each profile's `settings.json`, so they match the new code. Every launcher runs `cc-auth-switch start`, and the `SessionStart` hook runs `cc-auth-switch session-start`, so a change to either takes effect at the next launch without this step. That covers how `start` reads the config and builds the environment and the arguments: `env`, secrets, `pinned_models`, `model`, `add_dirs`, roles, the git config attribution, the status line tap and the tmux name. A change to what `install` writes still needs this step: the launcher line, the patches to the profiles' `settings.json` (`attribution`, `plansDirectory`, the plugin keys, the hook), the copied and symlinked files, and `sessions/` sharing.

Check the output of `cc-auth-switch install`: it lists what changed under each profile and the path of every backup it made, see [Re-syncing after config changes](#re-syncing-after-config-changes).

## License

MIT
