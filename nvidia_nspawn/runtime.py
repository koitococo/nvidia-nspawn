"""Plan and apply live mounts, hooks, and systemd runtime permissions."""

import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile

from . import cdi, host
from .common import Error, require_root, run
from ._worker import mount_table, parent_mount

STATE_DIR = Path("/run/nvidia-nspawn")
WORKER = str(Path(__file__).with_name("_worker.py").resolve())
MANAGED_CONF = "/etc/ld.so.conf.d/nvidia-nspawn.conf"
PROFILE = "/etc/nvidia/nvidia-application-profiles-rc.d/10-container.conf"


def assert_identity(target: dict) -> None:
    host.assert_identity(target)


def worker(target: dict, operation: str, *, timeout: int = 60, **kwargs) -> str:
    assert_identity(target)
    return run([sys.executable, WORKER], input=json.dumps({
        "target": target, "operation": operation, **kwargs,
    }), timeout=timeout)


def exec_container(target: dict, argv: list[str], *, user: str | None = None,
                   env: dict | None = None, timeout: int = 60,
                   additional_gids: list[int] | None = None) -> str:
    return worker(target, "exec", argv=argv, user=user, env=env or {}, timeout=timeout,
                  additional_gids=additional_gids or [])


def state_path(machine: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", machine) or machine in (".", ".."):
        raise Error(f"Invalid machine name: {machine!r}")
    return STATE_DIR / f"{machine}.json"


def load_state(machine: str) -> dict | None:
    path = state_path(machine)
    return json.loads(path.read_text()) if path.exists() else None


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_path(state["target"]["machine"]).write_text(json.dumps(state, indent=2) + "\n")


def mapped_id(mapping: str, value: int = 0) -> int:
    for line in mapping.splitlines():
        inside, outside, count = map(int, line.split())
        if inside <= value < inside + count:
            return outside + value - inside
    raise Error(f"Container UID/GID {value} is not mapped")


def fingerprint(path: str) -> dict:
    info = os.stat(path)
    return {"dev": info.st_dev, "inode": info.st_ino, "mode": info.st_mode,
            "rdev": info.st_rdev, "size": info.st_size, "mtime_ns": info.st_mtime_ns}


def resources(plan: dict) -> list:
    result = []
    for item in plan["mounts"]:
        result.append({**item, "directory": os.path.isdir(item["source"])})
    for item in plan["devices"]:
        result.append({**item, "options": [], "directory": False})
    targets = [r["target"] for r in result]
    if len(targets) != len(set(targets)):
        raise Error("CDI mounts and devices overlap at a container destination")
    return result


def inspect(target: dict, paths: list[str]) -> dict:
    result = json.loads(worker(target, "inspect", paths=list(dict.fromkeys(paths))))
    result["host_shared_groups"] = sorted({tag for m in mount_table() for tag in m["optional"]
                                           if tag.startswith("shared:")})
    return result


def plan(machine: str, gpu: str) -> dict:
    require_root()
    for binary in ("machinectl", "busctl", "nvidia-ctk", "nvidia-smi"):
        if not shutil.which(binary):
            raise Error(f"Required host executable is missing: {binary}")
    state_path(machine)  # Validate before using the name in state storage.
    target = host.discover(machine)
    edits = cdi.generate(gpu)
    items = resources(edits)
    for item in items:
        item["fingerprint"] = fingerprint(item["source"])
        if "permissions" in item:
            info = os.stat(item["source"])
            item.update(device=True, rdev=info.st_rdev,
                        node_mode=item.get("fileMode", stat.S_IMODE(info.st_mode)),
                        node_owner=[mapped_id(target["uid_map"], item.get("uid", 0)),
                                    mapped_id(target["gid_map"], item.get("gid", 0))])
    for device in edits["devices"]:
        info = os.stat(device["source"])
        if not stat.S_ISCHR(info.st_mode):
            raise Error(f"Expected NVIDIA character device: {device['source']}")
        for key, value in (("major", os.major(info.st_rdev)), ("minor", os.minor(info.st_rdev))):
            if key in device and device[key] != value:
                raise Error(f"CDI {key} is stale for {device['source']}; regenerate CDI")
    paths = [i["target"] for i in items] + [l["link"] for l in edits.get("links", [])]
    paths += [MANAGED_CONF, PROFILE, "/proc/driver/nvidia/params"]
    observation = inspect(target, paths)
    previous = load_state(machine)
    same_instance = previous and all(previous["target"][k] == target[k]
                                     for k in ("boot_id", "leader", "starttime", "mount_ns"))
    if same_instance:
        if previous["gpu_uuid"] != gpu:
            raise Error("This container already has another GPU attachment; replacement is unsupported")
        if previous["phase"] != "attached":
            raise Error("Previous attach failed; inspect its reported residual changes before retrying")
    else:
        validate_destinations(items, edits, observation)
    policy = host.device_policy(target["unit"])
    return {"target": target, "gpu_uuid": gpu, "driver_version": host.driver_version(),
            "plan": edits, "resources": items, "before": observation,
            "device_policy": policy,
            "requested_device_allow": host.merge_allow(policy["allow"], edits["devices"]),
            "owner": [mapped_id(target["uid_map"]), mapped_id(target["gid_map"])],
            "existing_attachment": bool(same_instance),
            "notes": ["Runtime only; container restart requires another attach.",
                      "Parent/descendant cgroup filters and SELinux are not bypassed; verify real device opens.",
                      "Device nodes are created with CDI mode and mapped ownership; host DAC is not modified."]}


def shared_with_host(parent: dict, host_groups: list[str]) -> bool:
    return bool(set(parent["optional"]) & set(host_groups))


def validate_destinations(items: list, edits: dict, observation: dict) -> None:
    by_path = {p["path"]: p for p in observation["paths"]}
    for item in items:
        info = by_path[item["target"]]
        if info["exists"]:
            raise Error(f"Refusing to cover existing container resource: {item['target']}")
        parent = parent_mount(info["resolved"], observation["mounts"])
        if shared_with_host(parent, observation["host_shared_groups"]):
            raise Error(f"Shared mount propagation to host at {parent['target']}; refusing live injection")
    for link in edits.get("links", []):
        info = by_path[link["link"]]
        if info["exists"] and info.get("link") != link["target"]:
            raise Error(f"CDI symlink would replace a user resource: {link['link']}")
    for path in (MANAGED_CONF, PROFILE):
        if by_path[path]["exists"]:
            raise Error(f"Existing unmanaged NVIDIA configuration conflicts: {path}")
    if not observation["ldconfig"] or not observation["setpriv"]:
        raise Error("Container requires ldconfig and setpriv before attach")
    if any("rbind" in item["options"] for item in items) and not observation["recursive_mount_attributes"]:
        raise Error("Host libc requires mount_setattr support for recursive CDI mounts")
    for hook in edits["hooks"]:
        if not os.access(hook["path"], os.X_OK):
            raise Error(f"CDI hook executable is missing: {hook['path']}")


def apply_hooks(state: dict, stage: str) -> None:
    target, edits = state["target"], state["plan"]
    # Supply the standard bundle/config.json fallback for older Toolkit versions
    # as well as the root extension understood by recent versions.
    with tempfile.TemporaryDirectory(prefix="nvidia-nspawn-") as bundle:
        hook_root = "/proc/1/root" if stage == "createContainer" else target["root"]
        Path(bundle, "config.json").write_text(json.dumps({
            "ociVersion": "1.0.2", "root": {"path": hook_root},
            "process": {"env": [f"{k}={v}" for k, v in edits["env"].items()]},
        }))
        stdin = json.dumps({"ociVersion": "1.0.2", "id": target["machine"],
                            "status": "running", "pid": target["leader"],
                            "bundle": bundle, "root": target["root"]})
        for hook, action in zip(edits["hooks"], edits["hook_actions"]):
            if hook["hookName"] != stage:
                continue
            # These two are adapted explicitly by the namespace worker: links
            # never overwrite user files; ldconfig uses our own named config.
            if action["kind"] in ("create-symlinks", "update-ldcache"):
                continue
            assert_identity(target)
            env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"}
            env.update(dict(v.split("=", 1) for v in hook.get("env", [])))
            args = hook.get("args") or [hook["path"]]
            command = [hook["path"], *args[1:]]
            if stage == "createContainer":
                worker(target, "hook", hook=hook, bundle=bundle, state=json.loads(stdin),
                       env=env, timeout=hook.get("timeout") or 60)
            else:
                run(command, input=stdin, env=env, timeout=hook.get("timeout") or 60)
    if stage == "createContainer":
        exec_container(target, [state["before"]["ldconfig"]])


def observe_changes(state: dict) -> None:
    current = inspect(state["target"], [i["target"] for i in state["resources"]])
    old_ids = {m["id"] for m in state["before"]["mounts"]}
    mounted = [m["target"] for m in current["mounts"] if m["id"] not in old_ids]
    # Scope hook cleanup to known NVIDIA paths; never claim unrelated new mounts.
    known = {i["target"] for i in state["resources"]}
    state["journal"]["mounted"] = list(dict.fromkeys(state["journal"]["mounted"] + [
        p for p in mounted if p in known or p == "/proc/driver/nvidia/params"
        or p.startswith("/run/nvidia-ctk-hook/")]))
    old_files = state["before"]["tracked_files"]
    for path in sorted(current["tracked_files"], key=lambda p: (p.count("/"), p)):
        hook_owned = (path == PROFILE or path.startswith("/run/nvidia-ctk-hook/")
                      or (path.startswith("/etc/ld.so.conf.d/00-compat-") and path.endswith(".conf")))
        if hook_owned and path not in old_files and path not in state["journal"]["created"]:
            state["journal"]["created"].append(path)
    state["observed"] = current


def attach(machine: str, gpu: str) -> dict:
    prepared = plan(machine, gpu)
    if prepared["existing_attachment"]:
        current = status(machine)
        if not current["healthy"]:
            raise Error("Existing attachment has drifted: " + json.dumps(current))
        return current
    state = {**prepared, "phase": "applying", "journal": {"mounted": [], "created": []}}
    assert_identity(state["target"])
    if host.driver_version() != state["driver_version"]:
        raise Error("Host driver changed during planning; re-plan")
    for item in state["resources"]:
        if fingerprint(item["source"]) != item["fingerprint"]:
            raise Error(f"Host resource changed during planning: {item['source']}")
    save_state(state)
    policy_changed = False
    try:
        policy = state["device_policy"]
        # auto with an empty allow-list already permits all devices. Adding the
        # first DeviceAllow would implicitly restrict it, so leave it untouched.
        if not (policy["policy"] == "auto" and not policy["allow"]):
            host.set_allow(state["target"]["unit"], state["requested_device_allow"])
            policy_changed = True
        apply_hooks(state, "createRuntime")
        result = json.loads(worker(state["target"], "apply", resources=state["resources"],
                                   links=state["plan"].get("links", []), owner=state["owner"],
                                   ldconfig_folders=state["plan"].get("ldconfig_folders", [])))
        if "error" in result:
            # The worker already attempted its own rollback.
            if result["cleanup_errors"]:
                state["journal"] = result["journal"]
            raise Error(json.dumps(result))
        state["journal"] = result["journal"]
        save_state(state)
        apply_hooks(state, "createContainer")
        observe_changes(state)
        assert_identity(state["target"])
        if host.driver_version() != state["driver_version"]:
            raise Error("Host driver changed during attach")
        state["phase"] = "attached"
        save_state(state)
        result = status(machine)
        if not result["healthy"]:
            raise Error("Post-attach resources do not match: " + json.dumps(result))
        return result
    except Exception as exc:
        cleanup_errors = []
        for action in (
            lambda: observe_changes(state),
            lambda: cleanup_errors.extend(json.loads(worker(
                state["target"], "cleanup", journal=state["journal"]))["errors"]),
            lambda: exec_container(state["target"], [state["before"]["ldconfig"]]),
        ):
            try:
                action()
            except Exception as cleanup_error:
                cleanup_errors.append(str(cleanup_error))
        if policy_changed:
            try:
                assert_identity(state["target"])
                host.set_allow(state["target"]["unit"], state["device_policy"]["allow"])
            except Exception as cleanup_error:
                cleanup_errors.append(str(cleanup_error))
        state.update(phase="failed", error=str(exc), cleanup_errors=cleanup_errors)
        save_state(state)
        raise Error(f"Attach failed: {exc}\nCleanup errors: {cleanup_errors}\n"
                    f"Change journal: {json.dumps(state['journal'])}\n"
                    f"State: {state_path(machine)}") from exc


def status(machine: str) -> dict:
    require_root()
    target = host.discover(machine)
    state = load_state(machine)
    if not state:
        return {"target": target, "phase": "not-attached", "healthy": False}
    if any(state["target"][k] != target[k] for k in ("boot_id", "leader", "starttime", "mount_ns")):
        return {"target": target, "phase": "stale", "healthy": False,
                "recorded_target": state["target"]}
    current = inspect(target, [r["target"] for r in state["resources"]]
                      + [l["link"] for l in state["plan"].get("links", [])])
    by_path = {p["path"]: p for p in current["paths"]}
    mounted = {m["target"]: m for m in current["mounts"]}
    checks = []
    for item in state["resources"]:
        path = item["target"]
        actual = by_path[path]
        source = item["fingerprint"]
        if item.get("device"):
            okay = (stat.S_ISCHR(actual.get("mode", 0)) and actual.get("rdev") == item["rdev"]
                    and stat.S_IMODE(actual["mode"]) == item["node_mode"]
                    and [actual["uid"], actual["gid"]] == item["node_owner"])
        else:
            okay = (actual.get("inode") == source["inode"] and actual.get("dev") == source["dev"]
                    and actual["resolved"] in mounted)
            if "ro" in item["options"]:
                okay = okay and "ro" in mounted.get(actual["resolved"], {}).get("options", [])
        checks.append({"path": path, "present_and_matching": bool(okay)})
    for link in state["plan"].get("links", []):
        checks.append({"path": link["link"],
                       "present_and_matching": by_path[link["link"]].get("link") == link["target"]})
    driver_matches = host.driver_version() == state["driver_version"]
    return {"target": target, "gpu_uuid": state["gpu_uuid"], "phase": state["phase"],
            "healthy": state["phase"] == "attached" and driver_matches
                       and all(c["present_and_matching"] for c in checks),
            "driver_matches": driver_matches, "resources": checks,
            "device_policy": host.device_policy(target["unit"]),
            "note": "Mount/driver health only; use verify for real ordinary-user device access and CUDA execution."}
