"""Container entry point with explicit, one-time persistent state initialization."""

import argparse
import sys
from pathlib import Path

from adapters.state.service_volume import (
    initialize_service_state,
    require_service_state,
)


def main():
    parser = argparse.ArgumentParser(
        description="Sky service: require initialized persistent state before starting.",
        epilog="Server options are forwarded to sky-platform; see sky-platform --help.",
        allow_abbrev=False,
    )
    parser.add_argument("--state-dir", type=Path, default=Path("/.sky"))
    parser.add_argument(
        "--initialize-state", action="store_true", help="Initialize an empty mounted directory and exit"
    )
    parser.add_argument(
        "--read-only-database",
        action="store_true",
        help="Read PostgreSQL records without a persistent state directory",
    )
    options, server_args = parser.parse_known_args()
    if options.read_only_database:
        if options.initialize_state:
            parser.error("--read-only-database cannot initialize local state")
        from interfaces.cli import main as run_platform

        sys.argv = [sys.argv[0], *server_args, "--read-only-database"]
        run_platform()
        return
    if options.initialize_state and server_args:
        parser.error("--initialize-state accepts only --state-dir; it does not start the server")
    try:
        if options.initialize_state:
            root = initialize_service_state(options.state_dir)
            print(f"Initialized Sky service state: {root}")
            return
        root = require_service_state(options.state_dir)
    except (OSError, ValueError) as error:
        parser.error(f"Sky service state: {error}")

    # Preserve the existing local CLI; the container always uses this guard.
    from interfaces.cli import main as run_platform

    # The validated path must win even over abbreviated flags in the old parser.
    sys.argv = [sys.argv[0], *server_args, "--state-dir", str(root)]
    run_platform()
