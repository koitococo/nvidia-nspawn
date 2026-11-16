"""Short-lived namespace worker. Never import this in a long-lived process.

Detached host mount trees and hook FDs are acquired before setns/chroot.
A PID-namespace child gives hooks a valid container-relative /proc view.
"""

import ctypes
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

LIBC = ctypes.CDLL(None, use_errno=True)
LIBC.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                       ctypes.c_ulong, ctypes.c_char_p]
LIBC.umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]
LIBC.setns.argtypes = [ctypes.c_int, ctypes.c_int]


def syscall(result: int, action: str) -> None:
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, f"{action}: {os.strerror(code)}")


def mount(source: str, target: str, flags: int) -> None:
    syscall(LIBC.mount(os.fsencode(source), os.fsencode(target), None, flags, None),
            f"mount {source} -> {target}")


def clone_source(item: dict) -> int:
    if item.get("device"):
        return os.open(item["source"], os.O_PATH)
    LIBC.open_tree.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    flags = 1 | os.O_CLOEXEC  # OPEN_TREE_CLONE
    if "rbind" in item.get("options", []):
        flags |= 0x8000  # AT_RECURSIVE
    fd = LIBC.open_tree(-100, os.fsencode(item["source"]), flags)
    if fd < 0:
        syscall(-1, f"clone mount source {item['source']}")
    return fd


class MountAttributes(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
                ("attr_set", "attr_clr", "propagation", "userns_fd")]


def recursive_attributes(path: str, options: list[str]) -> None:
    attributes = MountAttributes()
    for name, flag in (("ro", 1), ("nosuid", 2), ("nodev", 4), ("noexec", 8)):
        if name in options:
            attributes.attr_set |= flag
    LIBC.mount_setattr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
                                  ctypes.POINTER(MountAttributes), ctypes.c_size_t]
    syscall(LIBC.mount_setattr(-100, os.fsencode(path), 0x8000,
                              ctypes.byref(attributes), ctypes.sizeof(attributes)),
            f"set recursive mount attributes on {path}")


def decode_mount_path(path: str) -> str:
    for code, value in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        path = path.replace(code, value)
    return path


def mount_table() -> list:
    result = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, after = line.split(" - ", 1)
        fields = before.split()
        result.append({"id": fields[0], "target": decode_mount_path(fields[4]),
                       "options": fields[5].split(","), "optional": fields[6:],
                       "filesystem": after.split()[0]})
    return result


def parent_mount(path: str, mounts: list) -> dict:
    candidates = [m for m in mounts if path == m["target"] or
                  path.startswith(m["target"].rstrip("/") + "/")]
    return max(candidates, key=lambda m: len(m["target"]))


def describe(path: str) -> dict:
    result = {"path": path, "resolved": os.path.realpath(path), "exists": os.path.lexists(path)}
    if result["exists"]:
        info = os.lstat(path)
        result.update(mode=info.st_mode, uid=info.st_uid, gid=info.st_gid,
                      inode=info.st_ino, dev=info.st_dev, rdev=info.st_rdev)
        if stat.S_ISLNK(info.st_mode):
            result["link"] = os.readlink(path)
    return result


def tracked_files() -> dict:
    paths = ["/etc/ld.so.conf.d", "/etc/nvidia/nvidia-application-profiles-rc.d",
             "/run/nvidia-ctk-hook"]
    result = {}
    for path in paths:
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                for name in dirs + files:
                    p = os.path.join(root, name)
                    result[p] = describe(p)
    return result


def make_parents(path: str, journal: dict, owner: list) -> None:
    missing = []
    parent = os.path.dirname(path)
    while not os.path.exists(parent):
        missing.append(parent)
        parent = os.path.dirname(parent)
    for directory in reversed(missing):
        os.mkdir(directory, 0o755)
        journal["created"].append(directory)
        os.chown(directory, *owner)


def cleanup(journal: dict) -> list:
    errors = []
    for target in reversed(journal.get("mounted", [])):
        try:
            syscall(LIBC.umount2(os.fsencode(target), 0), f"unmount {target}")
        except OSError as exc:
            errors.append(str(exc))
    for path in reversed(journal.get("created", [])):
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                os.rmdir(path)
            else:
                os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append(str(exc))
    return errors


def apply(request: dict, fds: list[int]) -> dict:
    journal = {"mounted": [], "created": []}
    owner = request["owner"]
    try:
        for item, fd in zip(request["resources"], fds):
            path = item["target"]
            make_parents(path, journal, owner)
            if item.get("device"):
                os.mknod(path, stat.S_IFCHR | item["node_mode"], item["rdev"])
                journal["created"].append(path)
                os.chown(path, *item["node_owner"])
                os.chmod(path, item["node_mode"])
                continue
            if not os.path.lexists(path):
                if item["directory"]:
                    os.mkdir(path, 0o755)
                else:
                    with open(path, "xb"):
                        pass
                journal["created"].append(path)
                os.chown(path, *owner)
            options = item.get("options", [])
            recursive = "rbind" in options
            # A /proc/self/fd path to a host mount cannot be bind-mounted from
            # another mount namespace. Transfer a detached host mount instead.
            LIBC.move_mount.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                                       ctypes.c_char_p, ctypes.c_uint]
            syscall(LIBC.move_mount(fd, b"", -100, os.fsencode(path), 4),
                    f"attach detached host mount to {path}")
            journal["mounted"].append(path)
            mount("", path, (1 << 18) | (16384 if "rprivate" in options else 0))
            flags = 4096 | 32  # MS_BIND | MS_REMOUNT
            for option, bit in (("ro", 1), ("nosuid", 2), ("nodev", 4), ("noexec", 8),
                                ("sync", 16), ("dirsync", 128), ("noatime", 1024),
                                ("nodiratime", 2048), ("relatime", 1 << 21),
                                ("strictatime", 1 << 24)):
                if option in options:
                    flags |= bit
            mount("", path, flags)
            if recursive:
                recursive_attributes(path, options)
        for link in request["links"]:
            path = link["link"]
            if os.path.islink(path) and os.readlink(path) == link["target"]:
                continue
            if os.path.lexists(path):
                raise RuntimeError(f"Refusing to replace existing symlink/file: {path}")
            make_parents(path, journal, owner)
            os.symlink(link["target"], path)
            journal["created"].append(path)
            os.chown(path, *owner, follow_symlinks=False)
        if request["ldconfig_folders"]:
            path = "/etc/ld.so.conf.d/nvidia-nspawn.conf"
            make_parents(path, journal, owner)
            with open(path, "x") as stream:
                journal["created"].append(path)
                stream.write("# Managed by nvidia-nspawn\n" + "\n".join(request["ldconfig_folders"]) + "\n")
            os.chown(path, *owner)
        return {"journal": journal}
    except Exception as exc:
        return {"error": str(exc), "journal": journal, "cleanup_errors": cleanup(journal)}


def dispatch(request: dict, fds: list[int]) -> dict:
    operation = request["operation"]
    if operation == "inspect":
        table = mount_table()
        return {"mounts": table, "paths": [describe(p) for p in request["paths"]],
                "tracked_files": tracked_files(),
                "ldconfig": next((p for p in ("/sbin/ldconfig", "/usr/sbin/ldconfig")
                                  if os.access(p, os.X_OK)), None),
                "setpriv": next((p for p in ("/usr/bin/setpriv", "/bin/setpriv")
                                 if os.access(p, os.X_OK)), None),
                "recursive_mount_attributes": hasattr(LIBC, "mount_setattr")}
    if operation == "apply":
        return apply(request, fds)
    if operation == "cleanup":
        return {"errors": cleanup(request["journal"])}
    if operation == "hook":
        state = request["state"]
        state.update(root="/proc/1/root", pid=1,
                     bundle=f"/proc/self/fd/{request['bundle_fd']}")
        stdin = os.memfd_create("nvidia-nspawn-hook-state")
        os.write(stdin, json.dumps(state).encode())
        os.lseek(stdin, 0, os.SEEK_SET)
        os.dup2(stdin, 0)
        os.execve(request["hook_fd"], request["hook"]["args"], request["env"])
    if operation == "exec":
        command = request["argv"]
        env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
               "LANG": "C", **request.get("env", {})}
        if request.get("user"):
            binary = next((p for p in ("/usr/bin/setpriv", "/bin/setpriv")
                           if os.access(p, os.X_OK)), None)
            if not binary:
                raise RuntimeError("Container setpriv is required for ordinary-user verification")
            # Resolve NSS identity with container executables, not host libc/NSS
            # modules loaded into this worker. Extra CDI groups are process-local.
            identity_env = {"PATH": env["PATH"], "LANG": "C"}
            def identity(option: str) -> str:
                result = subprocess.run(["id", option, "--", request["user"]],
                                        text=True, capture_output=True, env=identity_env)
                if result.returncode:
                    raise RuntimeError(result.stderr)
                return result.stdout.strip()
            gid = identity("-g")
            groups = sorted(set(map(int, identity("-G").split()))
                            | set(request.get("additional_gids", [])))
            command = [binary, "--reuid", request["user"], "--regid", gid,
                       "--groups", ",".join(map(str, groups)), "--", *command]
        os.execvpe(command[0], command, env)
    raise RuntimeError(f"Unknown namespace worker operation: {operation}")


def main() -> None:
    request = json.load(sys.stdin)
    pid = request["target"]["leader"]
    # Acquire all host handles before changing namespaces or root.
    fds = [clone_source(item) for item in request.get("resources", [])]
    if request["operation"] == "hook":
        request["hook_fd"] = os.open(request["hook"]["path"], os.O_RDONLY)
        request["bundle_fd"] = os.open(request["bundle"], os.O_RDONLY | os.O_DIRECTORY)
        os.set_inheritable(request["bundle_fd"], True)
    root = os.open(f"/proc/{pid}/root", os.O_RDONLY | os.O_DIRECTORY)
    mount_ns = os.open(f"/proc/{pid}/ns/mnt", os.O_RDONLY)
    pid_ns = os.open(f"/proc/{pid}/ns/pid", os.O_RDONLY)
    user_ns = None
    if os.readlink(f"/proc/{pid}/ns/user") != os.readlink("/proc/self/ns/user"):
        user_ns = os.open(f"/proc/{pid}/ns/user", os.O_RDONLY)
    if request["operation"] == "exec":
        # Verification must exercise the container's device BPF policy, not
        # silently inherit the host CLI's unrestricted cgroup.
        group = request["target"]["cgroup"].lstrip("/")
        Path("/sys/fs/cgroup", group, "cgroup.procs").write_text(str(os.getpid()))
        if request["target"].get("selinux") != "disabled":
            label = Path(f"/proc/{pid}/attr/current").read_bytes().rstrip(b"\n\x00")
            selinux = ctypes.CDLL("libselinux.so.1", use_errno=True)
            selinux.setexeccon.argtypes = [ctypes.c_char_p]
            syscall(selinux.setexeccon(label), "set container SELinux exec context")
        for namespace in ("ipc", "uts", "net", "cgroup"):
            fd = os.open(f"/proc/{pid}/ns/{namespace}", os.O_RDONLY)
            syscall(LIBC.setns(fd, 0), f"enter {namespace} namespace")
            os.close(fd)
    syscall(LIBC.setns(mount_ns, 0), "enter mount namespace")
    syscall(LIBC.setns(pid_ns, 0), "enter PID namespace")
    if request["operation"] == "exec" and user_ns is not None:
        syscall(LIBC.setns(user_ns, 0), "enter user namespace")
        os.setgroups([])
        os.setresgid(0, 0, 0)
        os.setresuid(0, 0, 0)
    child = os.fork()
    if child:
        _, status = os.waitpid(child, 0)
        sys.exit(os.waitstatus_to_exitcode(status) if os.WIFEXITED(status) else 1)
    try:
        os.fchdir(root)
        os.chroot(".")
        os.chdir("/")
        result = dispatch(request, fds)
        print(json.dumps(result), flush=True)
    except Exception as exc:
        print(f"namespace worker: {exc}", file=sys.stderr, flush=True)
        os._exit(1)
    os._exit(0)


if __name__ == "__main__":
    main()
