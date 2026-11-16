"""External-command boundary shared by the host CLI and verification."""

import os
import shlex
import subprocess


class Error(RuntimeError):
    """An actionable operation failure."""


def run(argv: list[str], *, input: str | None = None,
        env: dict | None = None, timeout: int = 60,
        merge_stderr: bool = True) -> str:
    try:
        result = subprocess.run(argv, input=input, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
                                env=env, timeout=timeout,
                                check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Error(f"{shlex.join(argv)}: {exc}") from exc
    if result.returncode:
        raise Error(f"{shlex.join(argv)} exited {result.returncode}:\n"
                    f"{result.stdout}{result.stderr or ''}")
    return result.stdout


def require_root() -> None:
    if os.geteuid() != 0:
        raise Error("Run on the nspawn host as root; no container changes were made.")
