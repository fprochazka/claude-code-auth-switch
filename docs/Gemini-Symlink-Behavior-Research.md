# Comprehensive Analysis of Claude Code Configuration Management, Symlink Behavior, and Multi-Account Architecture

**Key Points**
*   **Atomic Writes Break File Symlinks:** Claude Code utilizes atomic write patterns (write-to-temp followed by rename) for configuration files like `settings.json`. This overwrites the symbolic link with a physical file, breaking the link to the shared source [cite: 1, 2].
*   **Leakage Outside `CLAUDE_CONFIG_DIR`:** The `CLAUDE_CONFIG_DIR` environment variable does not fully isolate the environment. Crucial authentication state and OAuth tokens are stored in a hardcoded `~/.claude.json` file in the user's home directory, ignoring the custom configuration path [cite: 3, 4].
*   **Symlink Reliability Issues:** Significant bugs exist regarding symlinks. A recent security patch (CVE-2026-25724) tightened symlink following, which has inadvertently caused performance degradation and permission recognition failures when `settings.json` is symlinked [cite: 5, 6]. Additionally, hooks and plugins often fail to execute or load correctly when hosted within symlinked directories [cite: 7, 8].
*   **Recommended Architecture:** Due to the persistence of `~/.claude.json` in the home directory and atomic write behaviors, a pure symlink-based multi-profile setup is fragile. The most robust approach involves using shell aliases to manipulate `CLAUDE_CONFIG_DIR` combined with a "dotfiles" synchronization script that copies (rather than symlinks) mutable config files, while symlinking read-only directories like `commands/` or `skills/` [cite: 9, 10, 11].

## 1. Introduction

The efficient management of AI-assisted coding tools in professional environments often requires rigorous separation of concerns—specifically, the isolation of personal and professional accounts. Users attempting to architect a multi-profile setup for **Claude Code** (the CLI tool distributed via `@anthropic-ai/claude-code`) frequently turn to the `CLAUDE_CONFIG_DIR` environment variable and Unix symbolic links (symlinks) to achieve this isolation while maintaining shared configurations.

However, the internal file I/O operations of the Claude Code binary (Node.js-based), specifically its reliance on atomic write patterns and hardcoded path fallbacks, present significant architectural challenges. This report provides an exhaustive technical analysis of how Claude Code handles configuration files, the specific mechanisms of its file operations, and the implications for multi-account environments.

## 2. File I/O Mechanics and Atomic Writes (Address Questions 1 & 3)

### 2.1. The Atomic Write Pattern
The stability of a symlink-based configuration strategy hinges on how the application saves data. Research confirms that Claude Code employs an **atomic write pattern** for its JSON configuration files, particularly `settings.json` and state files.

In a standard Node.js atomic write implementation, the process follows these steps:
1.  **Serialize:** The configuration object is serialized to a JSON string.
2.  **Write Temp:** The data is written to a temporary file in the same directory (e.g., `settings.json.tmp` or a UUID-based filename).
3.  **Rename (Replace):** The system calls `fs.rename` (or an equivalent) to move the temporary file over the target file (`settings.json`).

**Impact on Symlinks:** When `fs.rename` is executed over a target that is a symbolic link, the standard POSIX behavior (and Node.js implementation) is to **replace the symbolic link with the file**, rather than writing through the link to the target.

**Evidence:**
Debug logs from Claude Code explicitly show this behavior. One report details an `EACCES` error where Claude Code attempts to perform an atomic write through a symlink chain managed by Nix home-manager. The log reveals the tool "tries to create a temp file... Failed to write file atomically... Falling back to non-atomic write" [cite: 1].

Furthermore, users have reported that when `settings.json` is a symlink, Claude Code exhibits performance degradation and permission failures, leading to recommendations to replace the symlink with a physical copy [cite: 6]. This confirms that the application expects `settings.json` to be a writable file handle, not a pointer.

### 2.2. Node.js `fs.watch` and File Locking
Beyond simple writing, Claude Code actively watches configuration files for changes to enable hot-reloading of settings. This introduces further complications with symlinks.
*   **Watcher Instability:** Reports indicate that the internal `FSWatcher` (likely utilizing `chokidar` or native `fs.watch`) throws `EINVAL` or unhandled promise rejections when watching files inside symlinked directories, particularly on Linux kernels 6.14+ [cite: 2].
*   **Race Conditions:** The atomic write process (delete/rename) can trigger file watchers explicitly. If the watcher is bound to the symlink, the momentary disappearance of the file during the rename operation can cause the watcher to detach or throw errors.

### 2.3. Conclusion on Atomic Writes
**Answer to Q1 & Q3:** Yes, Claude Code uses atomic write patterns. It writes to a temporary file and renames it over the target. This **will destroy** a symlink pointing to `settings.json` upon the first save operation (e.g., changing a setting via the CLI or accepting a permission that gets persisted). The symlink will be replaced by a standard file containing the new configuration, breaking the link to your shared "source of truth."

## 3. The `CLAUDE_CONFIG_DIR` Environment Variable and Leakage (Address Question 6)

A critical architectural flaw in the current version of Claude Code is the incomplete isolation provided by `CLAUDE_CONFIG_DIR`. While this variable redirects the storage of *most* configuration data, it fails to capture the most sensitive element: authentication state.

### 3.1. The Hardcoded `~/.claude.json`
Research indicates the existence of a file named `~/.claude.json` (note: this is a file in the home root, distinct from the `~/.claude/` directory). This file is responsible for storing:
*   OAuth session tokens.
*   MCP server states.
*   Feature flag caches.
*   Project state [cite: 12].

**The Leakage:**
This file path is **hardcoded to the user's home directory**. It does *not* respect `CLAUDE_CONFIG_DIR`.
*   [cite: 3] states explicitly: "Exception: `~/.claude.json` is hardcoded to the home directory and is not affected by `CLAUDE_CONFIG_DIR`."
*   [cite: 4] corroborates this, noting that in containerized environments where `~/.claude/` is mounted but the home dir is ephemeral, users lose authentication state because the tool attempts to write to `~/.claude.json` (or `/.claude.json` in root) rather than the configured directory.
*   [cite: 13] highlights a conflict where environment variables (`CLAUDE_CODE_OAUTH_TOKEN`) interact poorly with this hardcoded credential file, causing billing issues where usage is charged to the wrong account (API vs. Max subscription).

### 3.2. Plugin Directory Leakage
In addition to the credential file, there are reports that the plugin marketplace directory does not correctly adhere to the configuration path.
*   [cite: 14] reports that even when `CLAUDE_CONFIG_DIR` is set to a custom path (e.g., `~/.claude-af`), the application still creates and uses `~/.claude/plugins/marketplaces/` in the default home location.

### 3.3. Implications for Multi-Account Setup
**Answer to Q6:** `CLAUDE_CONFIG_DIR` does **not** fully replace `~/.claude/`. The persistence of `~/.claude.json` in the home directory means that if you swap `CLAUDE_CONFIG_DIR` to switch profiles, the application will still read the *same* auth token from the hardcoded file in your home directory. You cannot achieve true account isolation (e.g., Work Account vs. Personal Account) solely by changing `CLAUDE_CONFIG_DIR` because they will fight over the single `~/.claude.json` file.

## 4. Symlink Reliability and Known Bugs (Address Questions 2 & 4)

Using symlinks for the internal structure of the configuration directory (e.g., linking `plugins/` or `projects/`) is fraught with stability issues in the current version of Claude Code.

### 4.1. Security Vulnerabilities and Patch Side Effects
A significant recent vulnerability, **CVE-2026-25724**, involved "Permission Deny Bypass Through Symbolic Links" [cite: 5, 15, 16]. Attackers could bypass deny rules (e.g., "deny access to `/etc`") by creating a symlink to the target.
*   **The Fix:** Anthropic patched this in version 2.1.7 by strictly enforcing path resolution.
*   **The Consequence:** This security hardening likely makes the application aggressive about resolving real paths (`realpath`). If you symlink your `projects/` directory, the application may resolve the files to their physical location, which can break path-based permission rules defined in `settings.json` that expect the symlinked path [cite: 6].

### 4.2. Specific Symlink Failures
1.  **Hooks Execution:** Hooks (scripts triggered on events like `PostToolUse`) fail to execute or hang indefinitely if they are located in a symlinked directory. The workaround is to use absolute paths in `settings.json` rather than paths through the symlink [cite: 7].
2.  **Plugin State Mismatch:** Plugins may appear as "installed" in the discovery list but remain non-functional if the `.claude` directory is a symlink. This is caused by a state mismatch between where the plugin files physically reside and where the CLI expects to find them [cite: 8].
3.  **Project Context Loss:** When starting Claude Code from a directory that is a symlink to a project, the tool resolves the path to the physical location. This breaks the inheritance of `CLAUDE.md` memory files located in parent directories of the symlink [cite: 17].
4.  **`history.jsonl` Issues:** There are specific reports of issues with `history.jsonl` not persisting correctly or being misread when the environment involves symlinks, particularly in VS Code extensions that interact with the CLI data [cite: 18].

### 4.3. Reliability of Symlinking Subdirectories
**Answer to Q4:** Symlinking directories like `plugins/` and `projects/` is **not reliable**.
*   **Plugins:** The plugin loader seems to rely on exact path matching for state verification. Symlinking this directory leads to "installed but broken" states [cite: 8].
*   **Projects:** Symlinking `projects/` (where session history is stored) is risky because Claude Code uses this directory for active state management (`session-id.jsonl`). Given the file watcher issues and path resolution strictness, you risk corrupting session history or breaking the "Resume" functionality.

## 5. Alternative Approaches and Recommendations (Address Question 5)

Given the findings that `CLAUDE_CONFIG_DIR` leaks auth data and atomic writes break file symlinks, a purely environment-variable-driven or symlink-driven setup is insufficient.

### 5.1. The "Dotfiles" Copy Strategy (Recommended)
Instead of symlinking the *active* configuration files, use a "source of truth" repository and *copy* the files into place.

**Architecture:**
1.  **Source Directory:** Create a directory (e.g., `~/dotfiles/claude-shared/`) containing `settings.base.json`, `commands/`, `skills/`, and `agents/`.
2.  **Profile Directories:** Create physical directories for each profile: `~/.claude-work/` and `~/.claude-personal/`.
3.  **Setup Script:** Write a script that:
    *   Copies `settings.base.json` to `~/.claude-work/settings.json`.
    *   Symlinks the *read-only* directories (`commands/`, `skills/`) from the source to the profile. (Symlinking these directories is generally safer than symlinking the config root, as Claude reads them but rarely writes *to* the directory structure itself, unlike `projects/` or `settings.json`).
    *   **Crucial:** Handles the `~/.claude.json` leakage.

### 5.2. Handling the Authentication Leak
Since `~/.claude.json` is hardcoded to home, you must swap this file when switching profiles.

**Solution: The "Profile Switcher" Function**
You can implement a shell function to swap the auth file physically.

```bash
function claude-work() {
    # 1. Swap the auth file
    if [ -f ~/.claude.json.work ]; then
        cp ~/.claude.json ~/.claude.json.backup
        cp ~/.claude.json.work ~/.claude.json
    fi

    # 2. Set the config dir for non-auth data
    export CLAUDE_CONFIG_DIR=~/.claude-work

    # 3. Run Claude
    claude "$@"

    # 4. Save state back (optional but recommended)
    cp ~/.claude.json ~/.claude.json.work
}
```

This ensures that when you run `claude-work`, it uses the correct permissions/settings (from `CLAUDE_CONFIG_DIR`) and the correct authenticated user (by swapping the hardcoded file).

### 5.3. Isolation via Containers (Most Robust)
If the file swapping hack is too fragile, the only 100% reliable method to isolate accounts currently is **Docker**.
*   Mount the source code as a volume.
*   Mount a profile-specific volume to `/root/.claude` (or `/home/user/.claude`).
*   This creates a completely contained filesystem where atomic writes and hardcoded paths function as intended without conflict [cite: 9, 19].

## 6. Summary of Answers

1.  **Atomic Writes?** **Yes.** Claude Code writes to a temp file and renames it, which will overwrite/break any symlink pointing to `settings.json` [cite: 1].
2.  **Known Symlink Bugs?** **Yes.** High severity. Includes hooks failing to execute [cite: 7], plugins showing false status [cite: 8], and security patches causing path resolution issues [cite: 16].
3.  **Node.js I/O Mechanism?** It uses `fs.writeFile` (atomic pattern) and `fs.rename`. It also uses `fs.watch` which is unstable on symlinks in some Linux kernels [cite: 2].
4.  **Symlinking Directories Work?** **No, not reliably.** Symlinking `.claude` or `plugins` causes state mismatches and plugin loading failures [cite: 8, 14].
5.  **Alternative Approaches?**
    *   **Shell Alias + File Swapping:** Use aliases to set `CLAUDE_CONFIG_DIR` *and* physically swap the `~/.claude.json` file to handle the auth leakage.
    *   **Docker:** Run distinct containers for work/personal to enforce total isolation.
6.  **`CLAUDE_CONFIG_DIR` Replacement?** **Partial only.** It replaces storage for history, projects, and settings, but **fails** to replace the authentication state file (`~/.claude.json`), which remains hardcoded in the home directory [cite: 3, 4].

## 7. Strategic Recommendation

For your specific plan: **Do not symlink `settings.json`.**
Instead, treat your shared config as a template. Use a startup script or alias that copies your shared `settings.json` into the target `CLAUDE_CONFIG_DIR` before launch. This allows Claude Code to perform its atomic writes on the physical file without breaking your setup. For `commands/` and `skills/`, symlinks are acceptable as they are primarily read-only, but avoid symlinking `plugins/` or `projects/` due to the state management bugs identified. Finally, you must implement a mechanism to swap `~/.claude.json` if you intend to use different Anthropic accounts (email addresses) for the different profiles.

**Sources:**
1. [github.com](https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQGQYmmtudsarEgbQ3A40Bz3bpDOxYQDmQ_2ICHLh4PZE13hM5-SJAUbpe12OYNplapby3CjN5qjYrairMhWCuNxNDqLl4kC2lazSPtHROGAQCbXLpaJJfG7Utw705uzEhXCiLuPF1gmV81I8DU=)
2. [github.com](https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQHMQV471cp9F4MyehfY59zA4MFMsXPa0muH9Z-wbI70qZX3Q_1zUR3qB10iIWQCapCtVQQHP3krjqs1-i4RacWXdkJfvnReBjWSfuh9h-FH3hio8_CgnEAOVUrsKABsyVROzb0BJ1nyoGjxnb0=)
3. [github.com](https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQEYQz3HE_Ij-yidkrnkvTuHHtiNUtkTFFUzn5yOACrJ8-qUCs_2iRpb7ywNXvMNtbEpADkWPWML-RcXaFRIgwTVys5I7TXfrr58GYgXHJwA1gNrjBtsBZEajEQGF3ELkW675t7Yu1GKIb3eGg==)
4. [github.com](https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQEO2UeZyKNMzLRVRL7davaMVAq5hdf4gYUJw_zzPtv2gJwweNq9dSW0pTUukLGrN1_Rj6SqG0M-EdfOgLjjcjgYyZr-nDjUAXGZkyqE0xiyo13IcIU86C9Ul_PIZ_CUQv8qQcxgQyrK9sYzIdk=)
5. [github.com](https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQGtlFigk1s6RMFj7MCEZfiPtUJ-cik0EKA_qKTGKkGip8UpGnKLgoHJGZ2opJH3E5Txq-wr5Ucs5NZuM0_Tc3tWzuZHeC7o5WScpAX0WhugMDNodkVJ6X0D49SnJhAUC3f-_ENfJlJH)
6. [github.com](https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQHNUOibPprVmFUeDq1331_31PVXKs-m5yaPrJ5sunXEQMiTwLxWRBl_PAAhuALLhCxm8hS6bgAFl657cEfmteblT0YjaRky8sbCWvW-iheWJ3jilZEk_pXxaEhmxkCawovAvao5dr1sVE4mwg==)
7. [github.com](https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQGDjYrzY6eYJ_lvTz-uwquQrZzw5fIm5pILm3n4Fe-vBXf9WUzFSKPbVu0FdUz8ftYYRXUwyGnmC-ZQ_WH3pW0-wLM1IChAGqlDlCuyQ13TCG5ZdsLTM615mrqiR3uUUDG1QlG_DFuy3RzzqQ==)
8. [github.com](https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQFSRigCUL1tQF1qUOD_YiD7Z38C8YRB6T9NoIfzCyttcIZbSMhybFCc18Ceq3j4KVvl6seDOaOx2gdEjdo1agbPQIefE4YiKUHQyT56b4-WMI6lZvFzBAffdw7eRcWXA6-qkiGY6Wdp8MQLXBA=)
9. [yieldcode.blog](https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQHU8zhzfk_AyttxiJXFVwdbl5WXag99bjac4j3zyfdprk9aH6l4p0jwY-TtNNG62OeksRrxpx4CbyBvGSaxG3lXnf2qTAYTxNLbXVeXJK5uYebwQsnfjifpnvQ5A3N14xrs4oQBxPMnaQ==)
10. [github.com](https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQH1zGkpxrg0fO8HsctJyQmicCcmnVM0Pb8TfG6tsbM-znxFU2zkaUKFgSVPITvPTzyvHZFg7WJMf4EZNnkEkYgn71ZfgK9Fr6Wo2-p6v9V5o96MyfcFI8uEmO0ygSOdeMIFl5jYzIv1wpnsLMavOxo90yxKYi4c)
11. [medium.com](https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQHhcrCmstbAUChh2rPuKaT68kcHXI36k9PZ6GWL_dx-DANl6obc2h5Nm2Ao1a4wiujqA0hgYCMEbbtA_OvBoItUm2vxlL80gfKkOAUxAFTeG56XPVuzLVpimS_63ipTEzpxiu_t5FOkdvxrnNaHaZ4VpjN0Fw89cCOnQE-nDY2Q-3TszRrISsQxTc6UVWeU)
12. [github.com](https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQGGuetuNJWa2tFRRjkB521GZZZcpK4q73JWsprAuB39agBckKyKeoQCauoloqphNXgadKvdtVfUaqUHtijRuEDBb-BXYUx__lK9z34nBO5lxN3YxT57bXGp3Lvbswo88yc7BhelxVkwiJTJe9peozen6c6UCFCQ)
13. [github.com](https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQHgQAKUvrjPK3HvqIIQwgAfIeI29rSc6UGKluQ6HS23-HqzpxcLBdGpaIKZuL7PlgJN1y7ao08TyYju9WHc9CnPOoUPc7V-gWoaKTFKiEtTkQS_Xbdy08spSwATvoHUA8K4kWDDIib3dOmdjL8=)
14. [github.com](https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQF_BemniiQr-WIeb-hDhjzmKv-oju96vyOfwPjsSmKsoqBO_Hl3zeBlvmXq0icnBzUPLGyMxfwS_v1wtPSnxulyBB70XCC8A4YnTGiZ8Z4AgUzMrVWlJsWUEj_JpXD1dGpsSjRHzjr665P5K-I=)
15. [snyk.io](https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQH1RBOeCcP0mcwnM-0N9XrTCb2W5AIAJh-Xuwj0YBjG_RGlwJMfcZusURvs8MxC57Ned6fwbvfTbdfMZ4PCfZYMUAh3u3LqhycEDXRpxOVSe4UAy8X4EP1yCSASbh1zOw8DDjJA9uYWCGle_S4xrOhimP52gW9W8T9CfCIoxCOh)
16. [sentinelone.com](https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQEj8WbRHJFHL7LXNutUI3vBkxgFm17qrJ5PAYV2naTlJFk7vaDd4TdDfN-5Qob7adYJyT3tUNQzMiXqqFGMp169K-1qqfqPrpgbi4VViM0S4OD4XFfvjbzDj6QkhD-337D33d3nAQv6ENBVxBxLI9OMtAIlDJug0lc=)
17. [github.com](https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQFGDPptoTLCX-g-nN8a9dAnppxfNG61DHfOCDBol-CK3zHha94cBAzBOgZysN_V8avSp0lMoY-Fk-wY4F6XBft8CTQqMz7FoaRBElZtsp6JVzp1gwAgFr-zD17AFi8g3X8WW2ZzlQwZ2NiEYnE=)
18. [github.com](https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQFQa0iDXuZ2S6gvnp1eZ6U4AVWj1ysx2M0qlHEcvcC5PKJejCbvR9uoMhiPRPcvQBTlEqZwNJmGm3GCWeExnkhPEyH-s7DJY0jQ_EgGAfOukN6u2zc101l3cfZLxsXBb5zT_Viti6wWmbSgRw==)
19. [reddit.com](https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQFL7Wen4VI8YahUdsv49kN_bSXzwa0AMugy0x638aqXz0O2XBM2_7dCVb6kxpQgQpqhaOEGmSkoAlcpFXbCayNrgntBC2Gtzxd9MctkiW2keHBmkQrND-XSz9QmCJob-H2OBq_BrURdmO7lLbfKtA5ODAmn8uOl47XMVRVWc_B5JcLDPkay93aD2GHkIXNOqNugcngYCgfzL4_d)
