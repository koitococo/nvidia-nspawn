"""Normalize NVIDIA CDI edits without trimming driver resources.

CLI/hook contracts are verified against NVIDIA/nvidia-container-toolkit v1.20.2:
cmd/nvidia-ctk/cdi/generate/generate.go, internal/discover/hooks.go, and
cmd/nvidia-cdi-hook/{create-symlinks,update-ldcache,cudacompat,apply-cuda-memory-limits}.
No hook is executed here: consumers must adapt actions to the container root.
"""

from __future__ import annotations

import json
import posixpath
import re

from .common import Error, run


_SUPPORTED_HOOKS = {
    "create-symlinks": {"link"},
    "update-ldcache": {"folder", "ldconfig-path"},
    "enable-cuda-compat": {
        "host-driver-version", "host-cuda-version", "cuda-compat-container-root"
    },
    "disable-device-node-modification": set(),
    "update-application-profile": set(),
    "apply-cuda-memory-limits": {"driver-root", "gpu-id"},
}
_MOUNT_OPTIONS = {
    "bind", "rbind", "ro", "nosuid", "nodev", "noexec", "relatime", "strictatime",
    "noatime", "nodiratime", "sync", "async", "dirsync", "private", "rprivate",
}


def _object(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise Error(f"CDI {label} must be an object")
    return value


def _list(value: object, label: str) -> list:
    if not isinstance(value, list):
        raise Error(f"CDI {label} must be an array")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise Error(f"CDI {label} must be a nonempty string without NUL")
    return value


def _path(value: object, label: str) -> str:
    value = _text(value, label)
    if not value.startswith("/") or ".." in value.split("/") or value == "/":
        raise Error(f"CDI {label} must be an absolute non-root path: {value!r}")
    return posixpath.normpath(value)


def _fields(value: dict, allowed: set[str], label: str) -> None:
    unknown = value.keys() - allowed
    if unknown:
        raise Error(f"Unsupported CDI {label} fields: {', '.join(sorted(unknown))}")


def _unique(items: dict, key: str, value: dict, label: str) -> None:
    if key in items and items[key] != value:
        raise Error(f"Conflicting CDI {label} for {key}")
    items[key] = value


def _hook(hook: dict) -> dict:
    _fields(hook, {"hookName", "path", "args", "env", "timeout"}, "hook")
    path = _path(hook.get("path"), "hook path")
    binary = posixpath.basename(path)
    if binary not in {"nvidia-cdi-hook", "nvidia-ctk"}:
        raise Error(f"Unsupported CDI hook executable: {path}")
    args = _list(hook.get("args"), "hook args")
    if any(not isinstance(arg, str) or "\x00" in arg for arg in args):
        raise Error("CDI hook arguments must be strings without NUL")
    if not args or args[0] not in {path, binary}:
        raise Error(f"CDI hook argv[0] does not match executable: {args!r}")
    offset = 1
    if binary == "nvidia-ctk":
        if args[1:2] != ["hook"]:
            raise Error("nvidia-ctk CDI hooks require the hook subcommand")
        offset = 2
    if len(args) <= offset or args[offset] not in _SUPPORTED_HOOKS:
        raise Error(f"Unsupported NVIDIA CDI hook: {args!r}")
    kind = args[offset]
    lifecycle = "createRuntime" if kind == "apply-cuda-memory-limits" else "createContainer"
    if hook.get("hookName") != lifecycle:
        raise Error(f"Unsupported CDI hook lifecycle for {kind}: {hook.get('hookName')!r}")
    for entry in _list(hook.get("env", []), "hook env"):
        # Other environment can change hook semantics; never silently discard it.
        if entry not in {"NVIDIA_CTK_DEBUG=true", "NVIDIA_CTK_DEBUG=false"}:
            raise Error(f"Unsupported CDI hook environment: {entry!r}")
    if "timeout" in hook and (type(hook["timeout"]) is not int or hook["timeout"] <= 0):
        raise Error("CDI hook timeout must be a positive integer")
    options: dict[str, list[str]] = {}
    tokens = iter(args[offset + 1:])
    for token in tokens:
        token = _text(token, "hook argument")
        flag, sep, value = token.partition("=")
        if not flag.startswith("--") or flag[2:] not in _SUPPORTED_HOOKS[kind]:
            raise Error(f"Unsupported {kind} hook argument: {token!r}")
        if not sep:
            value = next(tokens, None)
        name = flag[2:]
        if name != "driver-root" or value != "":
            value = _text(value, f"{kind} {flag}")
        if name in options and name not in {"link", "folder", "gpu-id"}:
            raise Error(f"Duplicate {kind} hook option: {flag}")
        options.setdefault(name, []).append(value)
    normalized = {key: values if key in {"link", "folder", "gpu-id"} else values[0]
                  for key, values in options.items()}
    if kind == "create-symlinks":
        links: dict[str, dict] = {}
        if not options.get("link"):
            raise Error("create-symlinks requires at least one --link")
        for value in options["link"]:
            parts = value.split("::")
            if len(parts) != 2:
                raise Error(f"Invalid CDI symlink: {value!r}")
            target = _text(parts[0], "symlink target")
            link = _path(parts[1], "symlink path")
            # Relative symlink text is meaningful relative to the link parent.
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(link), target))
            if not resolved.startswith("/") or resolved == "/":
                raise Error(f"Invalid CDI symlink target: {target!r}")
            _unique(links, link, {"target": target, "link": link}, "symlink")
        return {"kind": kind, "options": normalized, "links": list(links.values())}
    if kind == "update-ldcache":
        folders = [_path(p, "ldcache folder") for p in options.get("folder", [])]
        ldconfig = options.get("ldconfig-path", ["/sbin/ldconfig"])[0]
        _path(ldconfig.removeprefix("@"), "ldconfig executable")
        return {"kind": kind, "options": normalized,
                "folders": list(dict.fromkeys(folders)), "ldconfig_path": ldconfig}
    if kind == "apply-cuda-memory-limits":
        if not options.get("gpu-id") or any(not value.strip() for value in options["gpu-id"]):
            raise Error("apply-cuda-memory-limits requires nonempty --gpu-id values")
    if "cuda-compat-container-root" in normalized:
        _path(normalized["cuda-compat-container-root"], "CUDA compat root")
    for key in ("host-driver-version", "host-cuda-version"):
        if key in normalized and not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", normalized[key]):
            raise Error(f"Invalid CDI {key}: {normalized[key]!r}")
    return {"kind": kind, "options": normalized}


def parse(spec: dict, gpu_uuid: str) -> dict:
    """Merge common and UUID-selected device edits; perform no host I/O."""
    if not isinstance(gpu_uuid, str) or not re.fullmatch(r"GPU-[A-Za-z0-9-]+", gpu_uuid):
        raise Error("Select a full GPU by its GPU-UUID, not an index or MIG UUID")
    spec = _object(spec, "specification")
    version = _text(spec.get("cdiVersion"), "version")
    if not re.fullmatch(r"(?:0|1)\.[0-9]+\.[0-9]+", version):
        raise Error(f"Unsupported CDI version: {version}")
    if spec.get("kind") != "nvidia.com/gpu":
        raise Error(f"Unsupported CDI kind: {spec.get('kind')!r}")
    selected = []
    for device in _list(spec.get("devices"), "devices"):
        device = _object(device, "device")
        if device.get("name") == gpu_uuid:
            selected.append(device)
    if len(selected) != 1:
        raise Error(f"Expected one CDI device named {gpu_uuid}; found {len(selected)}")
    mounts: dict[str, dict] = {}
    devices: dict[str, dict] = {}
    env: dict[str, str] = {}
    additional_gids: dict[int, None] = {}
    hooks = []
    actions = []
    links: dict[str, dict] = {}
    for edits in (spec.get("containerEdits", {}), selected[0].get("containerEdits", {})):
        edits = _object(edits, "containerEdits")
        _fields(edits, {"mounts", "deviceNodes", "env", "hooks", "additionalGids"}, "containerEdits")
        for gid in _list(edits.get("additionalGids", []), "additionalGids"):
            if type(gid) is not int or gid < 0:
                raise Error(f"Invalid CDI additionalGids value: {gid!r}")
            additional_gids[gid] = None
        for mount in _list(edits.get("mounts", []), "mounts"):
            mount = _object(mount, "mount")
            _fields(mount, {"hostPath", "containerPath", "options", "type"}, "mount")
            if mount.get("type", "bind") != "bind":
                raise Error(f"Unsupported CDI mount type: {mount.get('type')}")
            options = _list(mount.get("options", []), "mount options")
            if any(not isinstance(o, str) or o not in _MOUNT_OPTIONS for o in options):
                raise Error(f"Unsupported CDI mount options: {options!r}")
            if not {"bind", "rbind"} & set(options):
                raise Error("CDI mounts must explicitly request bind or rbind")
            options = list(dict.fromkeys(options))
            if "ro" not in options:
                options.append("ro")
            item = {"source": _path(mount.get("hostPath"), "mount source"),
                    "target": _path(mount.get("containerPath"), "mount target"),
                    "options": options}
            previous = mounts.get(item["target"])
            if previous and set(previous["options"]) == set(options):
                item["options"] = previous["options"]
            _unique(mounts, item["target"], item, "mount")
        for node in _list(edits.get("deviceNodes", []), "deviceNodes"):
            node = _object(node, "device node")
            _fields(node, {"path", "hostPath", "permissions", "type", "major", "minor",
                           "fileMode", "uid", "gid"}, "device node")
            if node.get("type", "c") != "c":
                raise Error("Only character CDI device nodes are supported")
            target = _path(node.get("path"), "device path")
            permissions = node.get("permissions", "rwm")
            if not isinstance(permissions, str) or not permissions or set(permissions) - set("rwm"):
                raise Error(f"Invalid CDI device permissions: {permissions!r}")
            item = {"source": _path(node.get("hostPath") or target, "device host path"),
                    "target": target, "permissions": "".join(p for p in "rwm" if p in permissions)}
            for field in ("major", "minor", "fileMode", "uid", "gid"):
                if field in node:
                    value = node[field]
                    if type(value) is not int or value < 0:
                        raise Error(f"Invalid CDI device {field}: {value!r}")
                    if field == "fileMode" and value > 0o7777:
                        raise Error(f"Unsupported CDI device fileMode: {value!r}")
                    item[field] = value
            _unique(devices, target, item, "device")
        for entry in _list(edits.get("env", []), "env"):
            entry = _text(entry, "environment entry")
            name, sep, value = entry.partition("=")
            if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise Error(f"Invalid CDI environment entry: {entry!r}")
            # The official hook reads these from bundle process.env. GPU memory
            # quotas are outside this consumer's contract, even for empty values.
            if name in {"NVIDIA_GPU_MEMORY_REQUEST", "NVIDIA_GPU_MEMORY_LIMIT"}:
                raise Error(f"Unsupported CDI GPU memory quota environment: {name}")
            if name in env and env[name] != value:
                raise Error(f"Conflicting CDI environment values for {name}")
            env[name] = value
        for hook in _list(edits.get("hooks", []), "hooks"):
            hook = _object(hook, "hook")
            action = _hook(hook)
            for link in action.get("links", []):
                _unique(links, link["link"], link, "symlink")
            if hook not in hooks:
                hooks.append(hook)
                actions.append(action)
    for target in devices.keys() & mounts.keys():
        raise Error(f"CDI target is both mount and device: {target}")
    for target in links.keys() & (devices.keys() | mounts.keys()):
        raise Error(f"CDI symlink conflicts with mounted resource: {target}")
    return {"gpu_uuid": gpu_uuid, "cdi_version": version, "cdi_kind": spec["kind"],
            "cdi_device": gpu_uuid, "mounts": list(mounts.values()),
            "devices": list(devices.values()), "additional_gids": list(additional_gids),
            "env": env, "hooks": hooks,
            "hook_actions": actions, "links": list(links.values()),
            "ldconfig_folders": list(dict.fromkeys(
                folder for action in actions for folder in action.get("folders", [])))}


def generate(gpu_uuid: str) -> dict:
    """Discover the complete NVIDIA CDI specification with stable UUID names."""
    output = run(["nvidia-ctk", "cdi", "generate", "--format=json", "--device-name-strategy=uuid"],
                 merge_stderr=False)
    try:
        spec = json.loads(output)
    except json.JSONDecodeError as exc:
        raise Error(f"nvidia-ctk returned invalid CDI JSON: {exc}") from exc
    return parse(spec, gpu_uuid)
