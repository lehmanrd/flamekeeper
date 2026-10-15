"""Admin commands run inside the controller container (the installer and the
`flamekeeper` host CLI wrap them with `docker exec`):

    python -m app.cli admin-exists          exit 0 if the admin account exists, 1 if not
    python -m app.cli set-admin USERNAME    create the admin account, replacing any
                                            existing one and signing everyone out; reads
                                            the password from stdin (not an argument, so
                                            it never shows in the process list)
    python -m app.cli selfcheck             run the startup self-check
"""

from __future__ import annotations

import asyncio
import logging
import sys

from app.config import Settings, get_settings
from app.db import init_engine, run_migrations
from app.services.auth import AuthError, AuthService

COMMANDS = ("admin-exists", "set-admin", "selfcheck")


def main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    if command not in COMMANDS or (command == "set-admin" and len(argv) != 2):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    logging.disable(logging.WARNING)  # print results only, not the app's log lines
    try:
        return _run(command, argv[1:])
    finally:
        logging.disable(logging.NOTSET)


def _run(command: str, args: list[str]) -> int:
    settings = get_settings()
    if command == "selfcheck":
        return asyncio.run(_selfcheck(settings))
    run_migrations(settings.db_url)
    init_engine(settings.db_url)
    auth = AuthService(settings.data_dir)
    if command == "admin-exists":
        return 1 if auth.setup_required() else 0
    password = sys.stdin.readline().rstrip("\n")
    try:
        user = auth.set_admin(args[0], password)
    except AuthError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"Admin account {user.username!r} is ready.")
    return 0


async def _selfcheck(settings: Settings) -> int:
    from app.services.docker_adapter import RealDockerAdapter
    from app.services.selfcheck import run_selfcheck

    try:
        docker = RealDockerAdapter()
    except Exception as e:  # no socket: run_selfcheck reports it, but needs an adapter
        print(f"  [fail] Docker: {e}")
        return 1
    result = await run_selfcheck(settings, docker)
    for check in result.checks:
        print(f"  [{check.status}] {check.label}: {check.detail}")
    return 1 if result.fatal_error else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
