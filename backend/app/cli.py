"""Admin commands run inside the controller container (the `flamekeeper` host CLI wraps
them with `docker exec`):

    python -m app.cli setup-code     show the first-setup code
    python -m app.cli reset-admin    forgot the password: remove the admin account and
                                     sessions, and print a new setup code
    python -m app.cli selfcheck      run the startup self-check (for `flamekeeper doctor`)
"""

from __future__ import annotations

import asyncio
import logging
import sys

from app.config import Settings, get_settings
from app.db import init_engine, run_migrations
from app.services.auth import AuthService


def main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    if command not in ("setup-code", "reset-admin", "selfcheck"):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    logging.disable(logging.WARNING)  # print results only, not the app's log lines
    try:
        return _run(command)
    finally:
        logging.disable(logging.NOTSET)


def _run(command: str) -> int:
    settings = get_settings()
    if command == "selfcheck":
        return asyncio.run(_selfcheck(settings))
    run_migrations(settings.db_url)
    init_engine(settings.db_url)
    auth = AuthService(settings.data_dir)
    if command == "reset-admin":
        code = auth.reset()
        print("The admin account was removed and everyone was signed out.")
        print(f"Open FlameKeeper and create a new admin account with setup code: {code}")
        return 0
    if not auth.setup_required():
        print("FlameKeeper is already set up. Forgot the password? Run: flamekeeper reset-admin")
        return 1
    print(auth.ensure_setup_code())
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
