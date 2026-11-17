"""Private Linux command-tree supervisor; invoked only by common.run."""

import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys


def supervise(argv: list[str], pass_fds: tuple[int, ...]) -> int:
    """Own and reap one command tree without changing the caller's process state."""
    libc = ctypes.CDLL(None, use_errno=True)
    # PR_SET_CHILD_SUBREAPER is local to this dedicated supervisor process.
    if libc.prctl(36, 1, 0, 0, 0):
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    process = None
    group = None
    stopping = False

    def stop(_signum=None, _frame=None):
        nonlocal stopping, group
        stopping = True
        if group is not None:
            # Never signal this group again after its leader can be reaped and
            # its numeric ID reused. Escaped/new generations are handled below.
            owned_group, group = group, None
            try:
                os.killpg(owned_group, signal.SIGKILL)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, stop)
    try:
        process = subprocess.Popen(argv, pass_fds=pass_fds, start_new_session=True)
        group = process.pid
        if stopping:
            stop()
        # Keep the leader waitable until its process group has been killed.
        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
    finally:
        if process is not None:
            stop()
            children_path = Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children")
            while True:
                # A descendant may have started its own session. Once its parent
                # dies it is adopted here, so kill/reap each new generation too.
                for child in children_path.read_text().split():
                    try:
                        os.kill(int(child), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                try:
                    pid, status = os.waitpid(-1, 0)
                except ChildProcessError:
                    break
                if pid == process.pid:
                    process.returncode = os.waitstatus_to_exitcode(status)
    return process.returncode


if __name__ == "__main__":
    try:
        request = json.loads(sys.argv[1])
        returncode = supervise(request["argv"], tuple(request["pass_fds"]))
    except Exception as exc:
        print(f"Command supervision failed: {exc}", file=sys.stderr)
        sys.exit(125)
    if returncode < 0:
        signum = -returncode
        if signum != signal.SIGKILL:
            signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    sys.exit(returncode)
