import sys

PROG = "cc-auth-switch"


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # Every wrapper runs `attribution` at launch, and Claude Code runs `session-start` on every
    # session start. Both skip argparse and import only their own module; yaml and the installer stay
    # unloaded on these paths.
    command = argv[0] if argv else None
    if command == "attribution":
        from claude_code_auth_switch import git_attribution

        return git_attribution.main([f"{PROG} attribution", *argv[1:]])
    if command == "session-start":
        from claude_code_auth_switch import session_start

        return session_start.main()

    return run_management_command(argv)


def run_management_command(argv: list[str]) -> int:
    import argparse
    from pathlib import Path

    def add_config_argument(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--config",
            type=Path,
            default=None,
            help="Path to config file (default: ~/.claude/auth-switch.yaml, falling back to ~/.claude-profiles/config.yaml)",
        )

    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Manage Claude Code profiles: isolated config directories and claude-<name> wrapper scripts",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    install_parser = commands.add_parser("install", help="Create the profile directories and install the wrapper scripts")
    add_config_argument(install_parser)
    install_parser.add_argument(
        "--sync",
        action="store_true",
        help="Force re-copy of mutable files (overwrite existing copies in profile dirs)",
    )
    install_parser.add_argument(
        "--force",
        action="store_true",
        help="Force overwrite of diverged profile files (use with --sync)",
    )
    commands.add_parser(
        "attribution",
        help="Print the attribution settings from git config for a directory (run by the wrappers)",
        usage=f"{PROG} attribution [DIRECTORY]",
    )
    commands.add_parser(
        "session-start",
        help="Record the profile of a starting session (run by the SessionStart hook)",
    )

    rollback_parser = commands.add_parser(
        "rollback-sessions",
        help="Give a profile back its own sessions/ directory after share_sessions",
        description="Replace the profile's sessions/ symlink with a real directory and move the records of its live sessions back into it.",
    )
    add_config_argument(rollback_parser)
    rollback_parser.add_argument("profile", help="Profile name from the config")

    args = parser.parse_args(argv)
    from claude_code_auth_switch import installer

    if args.command == "install":
        installer.install(args.config, args.sync, args.force)
    elif args.command == "rollback-sessions":
        installer.rollback_sessions(args.config, args.profile)
    return 0
