import argparse
from pathlib import Path

from claude_code_auth_switch import installer


def add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Path to config file (default: {installer.DEFAULT_CONFIG_PATH}, falling back to {installer.LEGACY_CONFIG_PATH})",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="claude-code-auth-switch",
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

    args = parser.parse_args(argv)
    if args.command == "install":
        installer.install(args.config, args.sync, args.force)
    return 0
