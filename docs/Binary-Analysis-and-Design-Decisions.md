# Binary Analysis and Design Decisions

Local research conducted on 2026-02-25 against Claude Code v2.1.52 (`/home/fprochazka/.local/share/claude/versions/2.1.52`), a compiled ELF binary (Bun-bundled Node.js).

## 1. Does `CLAUDE_CONFIG_DIR` fully isolate `~/.claude.json`?

### Gemini's claim (WRONG)

Both Gemini deep research reports claimed that `~/.claude.json` is **hardcoded to the home directory** and does not respect `CLAUDE_CONFIG_DIR`. The symlink research report (Section 3) states:

> "This file path is hardcoded to the user's home directory. It does not respect CLAUDE_CONFIG_DIR."

This was presented as a critical architectural flaw requiring file-swapping hacks or Docker containerization to work around.

### What we actually found

By extracting and analyzing strings from the compiled binary, we identified the two relevant functions:

**Config directory function (`JL()`):**
```javascript
function JL() {
  return (process.env.CLAUDE_CONFIG_DIR ?? path.join(os.homedir(), ".claude")).normalize("NFC")
}
```

**Global config file path (`RW`):**
```javascript
RW = memoize(() => {
  // New-style: check if .config.json exists inside the config dir
  if (fs.existsSync(path.join(JL(), ".config.json")))
    return path.join(JL(), ".config.json");

  // Legacy fallback
  let filename = `.claude${suffix()}.json`;  // suffix() returns "" in prod
  return path.join(process.env.CLAUDE_CONFIG_DIR || os.homedir(), filename);
})
```

**Both code paths respect `CLAUDE_CONFIG_DIR`.** In the legacy fallback, the code is `path.join(process.env.CLAUDE_CONFIG_DIR || os.homedir(), ".claude.json")`. When `CLAUDE_CONFIG_DIR` is set, the config file becomes `$CLAUDE_CONFIG_DIR/.claude.json`, not `~/.claude.json`.

### Conclusion

**Gemini was wrong.** `CLAUDE_CONFIG_DIR` provides full isolation of all config and credential state, including `~/.claude.json`. No file-swapping hack or Docker containerization is needed for account isolation on Linux.

## 2. Atomic writes and symlinks

### Gemini's claim (CORRECT)

Claude Code uses atomic write patterns (write to temp file, then `fs.rename` over target) for config files like `settings.json`. This replaces symlinks with regular files.

### Our verification

We did not independently verify this in the binary, but the claim is consistent with standard Node.js patterns and community reports. We accepted this as likely correct and designed around it.

### Design decision

**Copy mutable files, don't symlink them.** Files that Claude Code writes to (`settings.json`, `settings.local.json`, `CLAUDE.md`) are copied from the source directory into each profile directory. Re-running the install script with `--sync` refreshes these copies.

## 3. Directory symlinks vs file symlinks

### Gemini's claim (MISLEADING)

The symlink research report (Section 4) claimed that symlinking directories like `plugins/` and `projects/` is "not reliable" and causes state mismatches and loading failures.

### Our analysis

Gemini conflated two different things:

1. **Symlink to a FILE** — atomic write (write temp + rename) replaces the symlink with a real file. This genuinely breaks things.
2. **Symlink to a DIRECTORY** — files created inside a symlinked directory are real files in the target directory. The OS follows the symlink transparently. This works fine.

When `plugins/` is a symlink pointing to `~/.claude/plugins/`, and Claude Code creates or modifies a file inside `plugins/`, the operation happens in the real `~/.claude/plugins/` directory. The symlink is never replaced because no one is renaming over the `plugins/` entry itself.

The specific issues Gemini cited (CVE-2026-25724 path resolution, plugin state mismatches) relate to the *working directory* or *config root* being a symlink, not to subdirectories within the config tree being symlinks.

### Design decision

**Symlink directories, copy files.** We symlink `commands/`, `skills/`, `agents/`, `plugins/`, and `projects/` from the source directory. This keeps all sessions, plugins, and customizations in one shared location. Only the three mutable files are copied.

## 4. macOS Keychain entry per config directory

### Gemini's claim (OUTDATED)

The first Gemini report extensively documented Issue #20553 where Claude Code hardcodes the macOS Keychain service name, causing credential collisions between profiles.

### What we found

Checked on 2026-10-06 against Claude Code 2.1.289 (`~/.local/share/claude/versions/2.1.289`, the Linux x86-64 build). 2.1.284 has the same code. The Linux build drops the macOS-only branches at build time, but the shared module that builds the Keychain service name is still in it (minified names as in 2.1.289):

```javascript
var kCn = "-credentials";

// Directory that holds .credentials.json
function HS() {
  let e = process.env.CLAUDE_SECURESTORAGE_CONFIG_DIR;
  if (e !== void 0) return (e || join(homedir(), ".claude")).normalize("NFC");
  return we();
}

// Keychain service name
function tGe(e = "") {
  let n = process.env.CLAUDE_SECURESTORAGE_CONFIG_DIR,
      t = n !== void 0 ? !n : !process.env.CLAUDE_CONFIG_DIR,
      r = n !== void 0 ? n.normalize("NFC") : we(),
      c = t ? "" : `-${createHash("sha256").update(r).digest("hex").substring(0, 8)}`;
  return `Claude Code${pn().OAUTH_FILE_SUFFIX}${e}${c}`;
}
```

The config directory function is the same as in section 1:

```javascript
var we = memoize(() => (process.env.CLAUDE_CONFIG_DIR ?? join(homedir(), ".claude")).normalize("NFC"));
```

`OAUTH_FILE_SUFFIX` is `""` in the production OAuth config, and the credentials item is called with the `-credentials` suffix. The service name for the OAuth login is therefore:

- `Claude Code-credentials` when `CLAUDE_CONFIG_DIR` is unset.
- `Claude Code-credentials-<first 8 hex of sha256(CLAUDE_CONFIG_DIR)>` when it is set.

The Keychain account is `$USER`, or the OS user name when `$USER` is unset. The binary reads the item with `security find-generic-password -a <user> -w -s <service>`. The storage modes include `keychain-with-plaintext-fallback`, and the plaintext file is `.credentials.json` in `HS()`, which is also the profile directory. On the Linux build the storage mode function returns `"file"`.

Consequences:

- Each profile directory gets its own Keychain entry, so the collision from #20553 should not happen. A user in that issue reported the same per-directory entries on macOS in 2.1.56. The issue is still open.
- The hash covers the raw variable value after Unicode NFC normalization only. No `~` expansion, no trailing-slash removal, no symlink resolution. A different spelling of the same directory is a different Keychain entry.
- Setting `CLAUDE_CONFIG_DIR` to the default `~/.claude` path still adds the hash, so it does not find the login stored under the plain name. [cenv](https://github.com/lukabudik/cenv) documents this gotcha.
- `CLAUDE_SECURESTORAGE_CONFIG_DIR` overrides the directory for both the Keychain name and `.credentials.json`. An empty value means `~/.claude` with the plain name. The wrappers unset it with all other inherited `CLAUDE_*` variables.

### Not verified

Nobody has run this on a Mac. The binary analyzed is the Linux build, so the macOS read and write code itself was removed at build time. Only the shared name-building code and the `security` call sites are visible.

## 5. Machine ID telemetry bans

### Gemini's claim (OVERBLOWN)

The first Gemini report warned extensively about hardware fingerprinting and subscription revocation when running multiple paid subscriptions from the same machine.

### Our situation

Our three profiles use three different auth backends:
- **personal**: Personal Anthropic OAuth subscription
- **work-team**: Team/org Anthropic OAuth subscription
- **work-vertexgcp**: Google Cloud Vertex AI (no Anthropic billing at all)

These aren't three competing personal subscriptions on the same hardware. A personal account, a team account, and an enterprise cloud account are expected to coexist. The Docker-based Machine ID spoofing recommended by Gemini is unnecessary.

## 6. A `SessionStart` hook that records the profile

Checked on 2026-11-16 against Claude Code 2.1.325, by reading the binary and by running a scratch config directory with a probe hook that dumped its stdin and environment.

### What we found

- The payload on stdin has `session_id`, `transcript_path`, `cwd`, `hook_event_name` and `source`. `source` is one of `startup`, `resume`, `clear`, `compact` and `fork`, and an empty `matcher` matches all of them.
- The hook inherits the environment of the claude process, so the wrapper's `FP_CC_AUTH_SWITCH_PROFILE` and `CLAUDE_CONFIG_DIR` arrive. Claude Code adds `CLAUDE_PID`, the pid of the process that writes `sessions/<pid>.json`. `$PPID` can be an intermediate shell.
- `transcript_path` is `<config dir>/projects/<slug>/<session-id>.jsonl`. On resume it is the resumed transcript, which stays in its original project directory. The slug rules live in the binary, so the hook derives the session directory from this path and never from `cwd`.
- The `<session-id>/` directory does not exist yet at `SessionStart`. Claude Code creates it the first time a tool result or a subagent needs it. `transcript_path` is empty in remote and storage modes.
- Stdout of a `SessionStart` hook that exits 0 goes into the model context. With any other exit code, Claude Code shows stderr to the user. The default timeout of a command hook is 10 minutes.
- Hooks from all settings sources run, concatenated, not overridden per event. There is no deduplication of identical commands across sources.
- Transcript discovery, resume listings and the cleanup only read `projects/<slug>/<id>.jsonl` and named children of `<id>/`, such as `subagents/` and `tool-results/`. An unknown file in `<id>/` is ignored. When the cleanup deletes an old transcript, it deletes `<id>/` recursively. When `<id>/` has no transcript next to it, the cleanup deletes only the file names it knows and then fails to `rmdir` the directory.

### Design decision

The installer registers one hook group per profile, `cc-auth-switch session-start`, recognizes its own entry by the command, and leaves every other `SessionStart` entry alone. The hook is a subcommand of the one console script. The command imports only the hook module, not yaml or the installer, because it runs on every session start. The hook writes a single `O_APPEND` line into `<session-id>/auth-switch-profile-starts.jsonl` and creates the directory first. It prints nothing, exits 0 and logs errors to a file. A session that quits before its first prompt leaves a directory that holds only this log. We accept that leak.

## 7. Sharing `sessions/` between profiles

Checked on 2026-11-16 against Claude Code 2.1.325, by reading the binary, by sampling the `sessions/` directories of live sessions for 12 minutes, and with scratch config directories that were not logged in.

### What we found

- Discovery for `ListAgents` and `SendMessage` by name reads only `$CLAUDE_CONFIG_DIR/sessions/<pid>.json`. Each call runs a fresh `readdir`. There is no watcher, no cache and no heartbeat. The message sockets already share `$XDG_RUNTIME_DIR/cc-socks/` across profiles.
- A symlinked `sessions/` works: `mkdir -p` and `chmod 0700` go through the symlink without replacing it. Symlinked files inside the directory are skipped.
- A record carries nothing about the profile. A session rewrites its record in place with `writeFile`, and only when its status, name, session or working directory changes. An idle session never rewrites it. In the sample, no record of 34 live sessions changed in 12 minutes.
- The key file `<pid>.<hash>.key` is written with a temp file and a rename, once per session start.
- On Linux an inbox accepts a message without a valid auth line, so a raw `uds:` send across profiles works even without sharing.
- Session names are unique among the live records of one `sessions/` directory. A later session with a taken name gets `<name>-<adjective>-<noun>`.
- While `sessions/.fleetview-heartbeat` is younger than 5 seconds, every session that registers there also writes its state classification.

### Design decision

The flag is per profile and off by default, because sharing removes the isolation between accounts. The target is `~/.claude/sessions`, the directory of plain `claude`.

The migration copies the files, swaps the directory and the symlink atomically, and keeps the old directory as a backup. After a pause, it copies files that are newer in the backup. A prototype with simulated writers compared this with hard links. Hard links lost up to 160 of 200 writes of the temp-and-rename writer: the backup shares inodes with the live files, so an "inode differs" check cannot tell which side is newer, and stale data overwrote newer files. The copy variant lost none of the in-place writes and 2 of 200 writes of a temp-and-rename writer in a tight loop. Claude Code writes its key file only once per start, so that case does not apply.

The rollback takes the owner of a record from the profile start log of [section 6](#6-a-sessionstart-hook-that-records-the-profile), then from `/proc/<pid>/environ`, then from the migration backups.

### Not verified

The migration has run on Linux for four profiles with about 30 live Claude Code sessions, and every session kept its record and could list and message the sessions of the other profiles. The rollback ran only against simulated writers, not against live sessions. The `renamex_np(RENAME_SWAP)` branch for macOS follows the documented API and has not run on a Mac.

## Summary of design decisions

| Decision | Rationale |
|----------|-----------|
| Use `CLAUDE_CONFIG_DIR` for isolation | Binary analysis confirmed full isolation including `~/.claude.json` |
| Copy `settings.json`, `settings.local.json`, `CLAUDE.md` | Atomic writes would break symlinks to these files |
| Symlink `commands/`, `skills/`, `agents/`, `plugins/`, `projects/` | Directory symlinks are transparent; files inside are real |
| No Docker containerization | Unnecessary on Linux with `CLAUDE_CONFIG_DIR` providing full isolation |
| No Keychain workaround for macOS | The Keychain service name includes a hash of `CLAUDE_CONFIG_DIR` (seen in the 2.1.289 binary, reported on macOS in 2.1.56, untested here) |
| No `~/.claude.json` file swapping | `CLAUDE_CONFIG_DIR` handles this correctly (Gemini was wrong) |
| No Machine ID spoofing | Three different auth backends, not three competing subscriptions |
| Wrapper scripts with `exec claude` | Simple, debuggable, no runtime overhead |
| Installer with `--sync` | Idempotent by default, force-refresh when needed |
| `share_sessions` symlinks `sessions/` to `~/.claude/sessions`, migrated by copy and atomic swap | Discovery reads only `sessions/`; copying keeps a backup whose files never change under the live sessions |
| `SessionStart` hook writes the profile into the session directory | Claude Code's own session files carry no profile; the hook gets the wrapper's environment and the transcript path |
