#!/usr/bin/env python3
"""The installer moved into the claude-code-auth-switch package; see the README for `uv tool install`."""

import sys

print("install.py is replaced by the cc-auth-switch command.", file=sys.stderr)
print("Install the tool with `uv tool install`, see the README, then run:", file=sys.stderr)
print("  cc-auth-switch install [--sync] [--force]", file=sys.stderr)
sys.exit(1)
