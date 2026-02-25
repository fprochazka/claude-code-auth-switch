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

## 4. macOS Keychain collision

### Gemini's claim (CORRECT, but irrelevant)

The first Gemini report extensively documented Issue #20553 where Claude Code hardcodes the macOS Keychain service name, causing credential collisions between profiles.

### Our situation

We're on Linux. Credentials are stored in flat files (`.credentials.json`), not in a system Keychain. The `CLAUDE_CONFIG_DIR` approach gives each profile its own `.credentials.json` automatically. This entire class of problems doesn't apply.

## 5. Machine ID telemetry bans

### Gemini's claim (OVERBLOWN)

The first Gemini report warned extensively about hardware fingerprinting and subscription revocation when running multiple paid subscriptions from the same machine.

### Our situation

Our three profiles use three different auth backends:
- **personal**: Personal Anthropic OAuth subscription
- **work-team**: Team/org Anthropic OAuth subscription
- **work-vertexgcp**: Google Cloud Vertex AI (no Anthropic billing at all)

These aren't three competing personal subscriptions on the same hardware. A personal account, a team account, and an enterprise cloud account are expected to coexist. The Docker-based Machine ID spoofing recommended by Gemini is unnecessary.

## Summary of design decisions

| Decision | Rationale |
|----------|-----------|
| Use `CLAUDE_CONFIG_DIR` for isolation | Binary analysis confirmed full isolation including `~/.claude.json` |
| Copy `settings.json`, `settings.local.json`, `CLAUDE.md` | Atomic writes would break symlinks to these files |
| Symlink `commands/`, `skills/`, `agents/`, `plugins/`, `projects/` | Directory symlinks are transparent; files inside are real |
| No Docker containerization | Unnecessary on Linux with `CLAUDE_CONFIG_DIR` providing full isolation |
| No `~/.claude.json` file swapping | `CLAUDE_CONFIG_DIR` handles this correctly (Gemini was wrong) |
| No Machine ID spoofing | Three different auth backends, not three competing subscriptions |
| Wrapper scripts with `exec claude` | Simple, debuggable, no runtime overhead |
| Install script with `--sync` | Idempotent by default, force-refresh when needed |
