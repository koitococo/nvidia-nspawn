"""External-command boundary shared by the host CLI and verification."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


class Error(RuntimeError):
    """An actionable operation failure."""


def run(argv: list[str], *, input: str | None = None,
        env: dict | None = None, timeout: int = 60,
        merge_stderr: bool = True, pass_fds: tuple[int, ...] = ()) -> str:
    # A separate interpreter contains subreaper/signal state and cannot reap any
    # unrelated children belonging to the CLI (or to an embedding application).
    command = [sys.executable, str(Path(__file__).with_name("_supervisor.py").resolve()),
               json.dumps({"argv": argv, "pass_fds": pass_fds})]
    try:
        with subprocess.Popen(command, stdin=subprocess.PIPE if input is not None else None,
                              text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
                              env=env, pass_fds=pass_fds, start_new_session=True) as process:
            try:
                stdout, stderr = process.communicate(input, timeout=timeout)
            except BaseException:
                # Do not kill the supervisor: it must reap the entire tree before
                # this call fails, including grandchildren holding output pipes.
                process.terminate()
                process.communicate()
                raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Error(f"{shlex.join(argv)}: {exc}") from exc
    if process.returncode:
        raise Error(f"{shlex.join(argv)} exited {process.returncode}:\n"
                    f"{stdout}{stderr or ''}")
    return stdout


def require_root() -> None:
    if os.geteuid() != 0:
        raise Error("Run on the nspawn host as root; no container changes were made.")

