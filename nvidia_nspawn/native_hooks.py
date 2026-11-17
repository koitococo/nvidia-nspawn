"""Expose Toolkit hooks to a shared-userns guest without executing guest code on host."""

import json
from pathlib import Path
import shutil
import tempfile
import uuid

from . import native_execution, runtime
from .common import Error


def _cleanup(state: dict, temporary: dict, before: dict, paths: list[str]) -> None:
    """Only remove confirmed temporary changes; preserve uncertain backing files."""
    target = state["target"]
    try:
        current = runtime.inspect(target, paths)
        mounts = {item["target"] for item in current["mounts"]}
        existing = {item["path"] for item in current["paths"] if item["exists"]}
        journal = temporary["journal"]
        remaining = {"mounted": [p for p in journal["mounted"] if p in mounts],
                     "created": [p for p in journal["created"] if p in existing]}
        if any(remaining.values()):
            temporary["cleanup_errors"] = json.loads(runtime.worker(
                target, "cleanup", journal=remaining))["errors"]
        else:
            temporary["cleanup_errors"] = []
        current = runtime.inspect(target, paths)
        temporary["possible_residuals"] = runtime.residual_candidates(before, current, paths)
        known = set(paths)
        for mount in current["mounts"]:
            path = mount["target"]
            if path.startswith(temporary["guest_root"] + "/") and path not in known:
                temporary["possible_residuals"].append({
                    "path": path, "reason": "Unjournaled mount under temporary hook directory"})
    except Exception as exc:
        temporary.setdefault("cleanup_errors", []).append(str(exc))
        temporary.setdefault("possible_residuals", []).append({
            "path": temporary["guest_root"],
            "reason": "Temporary hook cleanup could not be confirmed"})
    if not temporary["cleanup_errors"] and not temporary["possible_residuals"]:
        try:
            shutil.rmtree(temporary["host_bundle"])
        except OSError as exc:
            temporary["cleanup_errors"].append(str(exc))
        else:
            state["temporary_hooks"].remove(temporary)
            if not state["temporary_hooks"]:
                del state["temporary_hooks"]
    runtime.save_state(state)


def execute_hook(state: dict, hook: dict) -> None:
    """Mount a trusted hook and OCI bundle read-only, run via guest PID 1, then clean."""
    target = state["target"]
    runtime.assert_identity(target)
    guest_root = "/run/nvidia-nspawn-hooks/" + uuid.uuid4().hex
    guest_binary = guest_root + "/hook"
    guest_bundle = guest_root + "/bundle"
    bundle = tempfile.mkdtemp(prefix="nvidia-nspawn-hook-")
    temporary = {"guest_root": guest_root, "host_bundle": bundle,
                 "journal": {"mounted": [], "created": []},
                 "cleanup_errors": [], "possible_residuals": []}
    resources = [
        {"source": hook["path"], "target": guest_binary, "directory": False,
         "options": ["ro", "nosuid", "nodev"]},
        {"source": bundle, "target": guest_bundle, "directory": True,
         "options": ["ro", "nosuid", "nodev", "noexec"]},
    ]
    paths = runtime.apply_paths(resources, [], False)
    before = {}
    error = None
    safe_to_cleanup = True
    try:
        Path(bundle, "config.json").write_text(json.dumps({
            "ociVersion": "1.0.2", "root": {"path": "/"},
            "process": {"env": [f"{k}={v}" for k, v in state["plan"]["env"].items()]},
        }))
        before = runtime.inspect(target, paths)
        for item in before["paths"]:
            if item["path"] == guest_root and item["exists"]:
                raise Error(f"Temporary hook destination already exists: {guest_root}")
            if item["path"] in (guest_binary, guest_bundle):
                parent = runtime.parent_mount(item["resolved"], before["mounts"])
                if runtime.shared_with_host(parent, before["host_shared_groups"]):
                    raise Error(f"Shared mount propagation to host at {parent['target']}; "
                                "refusing temporary hook mounts")
        state.setdefault("temporary_hooks", []).append(temporary)
        runtime.save_state(state)
        try:
            result = json.loads(runtime.worker(
                target, "apply", resources=resources, links=[], owner=state["owner"],
                ldconfig_folders=[]))
        except runtime.WorkerError as exc:
            temporary["journal"] = exc.journal
            temporary["journal_errors"] = exc.journal_errors
            temporary["possible_residuals"] = [
                {"path": path, "reason": "Interrupted temporary mount apply"}
                for path in exc.possible_residuals]
            raise Error(f"Temporary hook mount apply failed: {exc}; "
                        f"progress errors: {exc.journal_errors}") from exc
        temporary["journal"] = result["journal"]
        if "error" in result:
            # A complete result already rolled back. Do not replay paths that
            # are no longer mounted; _cleanup observes the remaining journal.
            raise Error("Temporary hook mount apply failed: " + json.dumps(result))
        runtime.save_state(state)
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"}
        env.update(dict(value.split("=", 1) for value in hook.get("env", [])))
        args = hook.get("args") or [hook["path"]]
        oci_state = {"ociVersion": "1.0.2", "id": target["machine"],
                     "status": "running", "pid": 1, "root": "/", "bundle": guest_bundle}
        try:
            native_execution.execute(
                target, [guest_binary, *args[1:]], env=env,
                input=json.dumps(oci_state), timeout=hook.get("timeout") or 60)
        except BaseException as exc:
            safe_to_cleanup = getattr(exc, "safe_to_cleanup", True)
            raise
    except BaseException as exc:
        error = exc
    finally:
        if temporary in state.get("temporary_hooks", []):
            if safe_to_cleanup:
                _cleanup(state, temporary, before, paths)
            else:
                temporary["cleanup_errors"].append(
                    "Guest hook unit could not be stopped; temporary mounts and bundle retained")
                runtime.save_state(state)
        else:
            shutil.rmtree(bundle)
    if not safe_to_cleanup:
        raise native_execution.CleanupError(
            f"Native hook may still be running: {error}; "
            f"temporary resources retained: {json.dumps(temporary)}") from error
    if temporary.get("cleanup_errors") or temporary.get("possible_residuals"):
        raise Error(f"Native hook failed: {error or 'temporary cleanup incomplete'}; "
                    f"temporary resources: {json.dumps(temporary)}") from error
    if error is not None:
        raise error
