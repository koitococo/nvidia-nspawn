"""machined identity and systemd-owned runtime device policy."""

import json
import os
from pathlib import Path

from ._worker import validate_user_namespace
from .common import Error, run


def properties(output: str) -> dict:
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def process_identity(pid: int) -> dict:
    proc = Path(f"/proc/{pid}")
    # comm can contain spaces and parentheses; fields after its final ')' are fixed.
    fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
    return {"starttime": fields[19], "mount_ns": os.readlink(proc / "ns/mnt"),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}


def discover(machine: str) -> dict:
    p = properties(run(["machinectl", "show", machine, "--no-pager"]))
    if p.get("Class") != "container" or p.get("Service") != "systemd-nspawn":
        raise Error(f"{machine!r} is not a running systemd-nspawn container")
    pid = int(p.get("Leader", "0"))
    unit = p.get("Unit") or p.get("Scope")
    if pid <= 1 or not unit:
        raise Error(f"{machine!r}: missing running leader or owning unit")
    if os.readlink(f"/proc/{pid}/ns/mnt") == os.readlink("/proc/self/ns/mnt"):
        raise Error("Refusing a target sharing the tool's mount namespace")
    cgroup = next((line[3:] for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
                   if line.startswith("0::")), None)
    if not cgroup:
        raise Error("A unified cgroup v2 hierarchy is required")
    result = {"machine": machine, "leader": pid, "unit": unit, "cgroup": cgroup,
              "root": f"/proc/{pid}/root", **process_identity(pid)}
    result["uid_map"] = Path(f"/proc/{pid}/uid_map").read_text()
    result["gid_map"] = Path(f"/proc/{pid}/gid_map").read_text()
    try:
        validate_user_namespace(
            os.readlink("/proc/self/ns/user"), os.readlink(f"/proc/{pid}/ns/user"),
            result["uid_map"], result["gid_map"])
    except ValueError as exc:
        raise Error(str(exc)) from exc
    result["selinux"] = (Path("/sys/fs/selinux/enforce").read_text().strip()
                         if Path("/sys/fs/selinux/enforce").exists() else "disabled")
    return result


def assert_identity(target: dict) -> None:
    current = discover(target["machine"])
    for key in ("leader", "starttime", "mount_ns", "boot_id", "unit", "cgroup"):
        if current[key] != target[key]:
            raise Error(f"Container instance changed ({key}); re-plan before proceeding")


def device_policy(unit: str) -> dict:
    # busctl JSON preserves whitespace in device paths and returns typed arrays.
    obj = json.loads(run(["busctl", "--json=short", "call", "org.freedesktop.systemd1",
                          "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
                          "GetUnit", "s", unit]))["data"][0]
    def get(name):
        return json.loads(run(["busctl", "--json=short", "get-property",
                               "org.freedesktop.systemd1", obj,
                               "org.freedesktop.systemd1.Service" if unit.endswith(".service")
                               else "org.freedesktop.systemd1.Scope", name]))["data"]
    return {"policy": get("DevicePolicy"), "allow": get("DeviceAllow")}


def merge_allow(existing: list, devices: list) -> list:
    """Preserve every existing allow and widen only explicitly requested paths."""
    merged = {path: set(perms) for path, perms in existing}
    for device in devices:
        merged.setdefault(device["source"], set()).update(device["permissions"])
    return [[path, "".join(p for p in "rwm" if p in perms)]
            for path, perms in merged.items()]


def set_allow(unit: str, entries: list) -> None:
    # Nonempty DeviceAllow arrays append/update, even over D-Bus. Reset before
    # adding the merged list in the same runtime SetUnitProperties request.
    argv = ["busctl", "call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager", "SetUnitProperties", "sba(sv)",
            unit, "true", "2", "DeviceAllow", "a(ss)", "0",
            "DeviceAllow", "a(ss)", str(len(entries))]
    for path, perms in entries:
        argv.extend([path, perms])
    run(argv)


def driver_version() -> str:
    versions = {line.strip() for line in run([
        "nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"
    ]).splitlines() if line.strip()}
    if len(versions) != 1:
        raise Error(f"Cannot determine one NVIDIA host driver version: {versions}")
    return versions.pop()
