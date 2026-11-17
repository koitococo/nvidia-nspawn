"""Run shared-user-namespace commands as children of the guest system manager.

The host only executes systemd clients. The guest manager, already inside
nspawn's security boundary, forks and executes every container program.
"""

from pathlib import Path
import re
import uuid

from . import host
from .common import Error, run

# Bound both the guest's SIGTERM grace period and host transport operations.
_STOP_SECONDS = 5
_TRANSPORT_SECONDS = 5


class CleanupError(Error):
    """Guest processes may remain; retain any resources they can still use."""

    safe_to_cleanup = False


def preflight(target: dict) -> None:
    """Check native execution support without creating a unit or changing policy.

    Call before changing DeviceAllow, mounts, or persistent attach state. This
    checks host client support and a live guest system bus/manager; it executes
    no guest program and does not attempt to emulate nspawn's security settings.
    """
    host.assert_identity(target)
    version = run(["systemd-run", "--version"], timeout=_TRANSPORT_SECONDS,
                  merge_stderr=False)
    match = re.match(r"systemd\s+(\d+)", version)
    if not match or int(match[1]) < 257:
        raise Error("Native container execution requires host systemd 257 or newer")
    socket = Path(target["root"]) / "run/dbus/system_bus_socket"
    if not socket.is_socket():
        raise Error(f"{target['machine']}: native execution requires a running guest system bus")
    manager = run(["systemctl", f"--machine={target['machine']}", "--no-pager",
                   "--no-ask-password", "show", "--property=Version"],
                  timeout=_TRANSPORT_SECONDS, merge_stderr=False)
    if not manager.startswith("Version=") or not manager.removeprefix("Version=").strip():
        raise Error(f"{target['machine']}: guest system manager did not report its version")


def _unit_state(output: str, unit: str) -> str | None:
    """Extract one exact unit's active state from plain list-units output."""
    states = []
    for line in output.splitlines():
        fields = line.split()
        if fields and fields[0] == unit:
            if len(fields) < 4:
                raise Error(f"Cannot determine guest unit state: {line!r}")
            states.append(fields[2])
        elif fields:
            raise Error(f"Unexpected guest unit listing: {line!r}")
    if len(states) > 1:
        raise Error(f"Ambiguous guest unit state for {unit}")
    return states[0] if states else None


def _stop(machine: str, unit: str) -> None:
    """Synchronously stop the guest cgroup, tolerating an already collected unit."""
    client = ["systemctl", f"--machine={machine}", "--no-pager", "--no-ask-password"]
    stop_error = None
    try:
        run([*client, "stop", unit], timeout=_STOP_SECONDS + _TRANSPORT_SECONDS,
            merge_stderr=False)
    except Error as exc:
        # --collect normally removes the unit before this request arrives. Do
        # not confuse that expected case with a failed stop or broken bus.
        stop_error = exc
    output = run([*client, "list-units", "--all", "--full", "--plain",
                  "--no-legend", unit], timeout=_TRANSPORT_SECONDS,
                 merge_stderr=False)
    state = _unit_state(output, unit)
    if state is not None and (stop_error is not None or state not in ("inactive", "failed")):
        raise Error(f"Guest unit {machine}/{unit} was not confirmed stopped "
                    f"(state={state}): {stop_error or 'stop returned without deactivation'}")


def execute(target: dict, argv: list[str], *, user: str | None = None,
            env: dict | None = None, timeout: int = 60,
            additional_gids: list[int] | None = None, input: str | None = None) -> str:
    """Execute argv through the guest manager and return merged command output.

    User/group names are resolved by the guest manager, never by a host helper.
    Explicit environment assignments and argv are passed without a shell or
    argument environment expansion. ``input`` supplies the command's stdin.

    ``timeout`` bounds the requested active runtime. The host client gets ten
    additional seconds for startup/termination transport. A finally block then
    spends at most ten seconds stopping the entire guest unit and five seconds
    confirming its state. Thus failure handling has a fixed 25-second allowance,
    not an unbounded wait. A guest RuntimeMaxSec also survives client death.
    Any cleanup/verification failure raises Error, including after command
    failure or interruption: callers must not assume it is safe to unmount hook
    resources when that error is reported.
    """
    if not argv:
        raise Error("Native container execution requires a command")
    if timeout <= 0:
        raise Error("Native container execution requires a positive timeout")
    gids = additional_gids or []
    if any(type(gid) is not int or gid < 0 for gid in gids):
        raise Error("Supplementary groups must be non-negative numeric GIDs")
    preflight(target)
    machine = target["machine"]
    unit = f"nvidia-nspawn-exec-{uuid.uuid4().hex}.service"
    command = ["systemd-run", f"--machine={machine}", f"--unit={unit}",
               "--quiet", "--wait", "--pipe", "--collect", "--service-type=exec",
               "--no-ask-password", "--expand-environment=no",
               f"--uid={user if user is not None else '0'}",
               f"--property=RuntimeMaxSec={timeout}s",
               f"--property=TimeoutStartSec={timeout}s",
               f"--property=TimeoutStopSec={_STOP_SECONDS}s",
               "--property=KillMode=control-group", "--property=SendSIGKILL=yes"]
    if gids:
        command.append("--property=SupplementaryGroups=" + " ".join(str(gid) for gid in gids))
    for key, value in (env or {}).items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise Error(f"Invalid environment variable name: {key!r}")
        command.append(f"--setenv={key}={value}")
    command.extend(["--", *argv])
    try:
        return run(command, input=input, timeout=timeout + _STOP_SECONDS + _TRANSPORT_SECONDS)
    finally:
        try:
            _stop(machine, unit)
        except BaseException as exc:
            raise CleanupError(f"Failed to stop/verify guest execution unit {machine}/{unit}: {exc}. "
                               "Container processes may remain; do not remove their mounted resources.") from exc
