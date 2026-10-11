"""Local-only administration for the isolated Stage 15B.3B auth store."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import sys

from prototypes.admin_auth import (
    AdminAuthStore,
    AuthFailure,
    AuthUnavailable,
    BootstrapComplete,
    bootstrap_from_console,
    reset_password_from_console,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Operate the isolated NEXO admin-auth store locally.")
    parser.add_argument("--db", required=True, help="dedicated auth SQLite path (never the NEXO database)")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("bootstrap", "status", "reset-password", "revoke-sessions"):
        commands.add_parser(name)
    return parser


def _private_parent(database: Path, *, create: bool) -> None:
    parent = database.parent.resolve()
    if create:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent.exists():
        if create:
            raise ValueError("administrative directory is unavailable")
        return
    info = parent.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o700 != 0o700 or info.st_mode & 0o022):
        raise ValueError("administrative directory permissions are unsafe")


def _require_tty() -> bool:
    return bool(sys.stdin.isatty() and sys.stderr.isatty())


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database = Path(args.db).expanduser().resolve()
    try:
        auth = AdminAuthStore(database)
        if args.command != "status" and not _require_tty():
            print("this operation requires a local interactive TTY", file=sys.stderr)
            return 2
        _private_parent(database, create=args.command == "bootstrap")
        if args.command == "status":
            current = auth.status()
            state = "initialized" if current["initialized"] else "uninitialized"
            print(f"state={state} active_sessions={current['active_sessions']}")
            return 0
        current = auth.status()
        if args.command == "bootstrap":
            if current["initialized"]:
                raise BootstrapComplete()
            bootstrap_from_console(auth)
            print("administrator_bootstrapped")
        elif not current["initialized"]:
            raise BootstrapComplete()
        elif args.command == "reset-password":
            reset_password_from_console(auth)
            print("password_reset_sessions_revoked")
        else:
            if input("Type REVOKE to invalidate all admin sessions: ") != "REVOKE":
                print("cancelled", file=sys.stderr)
                return 2
            auth.revoke_all()
            print("sessions_revoked")
        return 0
    except BootstrapComplete:
        print("administrative state does not allow this operation", file=sys.stderr)
        return 4
    except AuthUnavailable:
        print("administrative storage unavailable", file=sys.stderr)
        return 3
    except AuthFailure:
        print("administrative operation failed", file=sys.stderr)
        return 3
    except (OSError, ValueError):
        print("invalid administrative store or input", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
