"""Real, unprivileged GPU acceptance checks and driver-loader evidence."""

from __future__ import annotations

import csv
import re
from pathlib import PurePosixPath

from . import runtime
from .common import Error, run


_QUERY = ["nvidia-smi", "--query-gpu=uuid,driver_version", "--format=csv,noheader"]
_IDENTITY_FIELDS = ("machine", "leader", "starttime", "unit", "mount_ns", "cgroup")
_LOADER_LINE = re.compile(r"^\s*\d+:\s")
_INIT_LINE = re.compile(r"^\s*\d+:\s+calling init:\s+(/\S+)\s*$")
_LIBRARY_NAMES = {
    "nvml": re.compile(r"libnvidia-ml\.so(?:\.[0-9]+)*"),
    "cuda": re.compile(r"libcuda\.so(?:\.[0-9]+)*"),
}
# These are source references for output interpretation, not a claim that an
# arbitrary user-supplied executable's source/build provenance is known.
_SAMPLE_SOURCES = {
    "Test PASSED": (
        "https://github.com/NVIDIA/cuda-samples/blob/v12.8/"
        "Samples/0_Introduction/vectorAdd/vectorAdd.cu"
    ),
    "Unified Memory: CPU and GPU answers match": (
        "https://github.com/NVIDIA/cuda-samples/blob/"
        "5443602d89ed99aede2e4b7bf329daddeadb320e/"
        "cpp/0_Introduction/vectorAdd/vectorAdd.cu"
    ),
}


def _program_lines(output: str) -> list[str]:
    """Remove glibc LD_DEBUG records, not arbitrary diagnostic lines."""
    return [
        line.strip()
        for line in output.splitlines()
        if line.strip() and not _LOADER_LINE.match(line)
    ]


def _parse_gpu_query(output: str) -> tuple[str, str]:
    lines = _program_lines(output)
    try:
        rows = list(csv.reader(lines, strict=True))
    except csv.Error as exc:
        raise Error(f"Invalid nvidia-smi query output: {exc}\n{output}") from exc
    if len(rows) != 1 or len(rows[0]) != 2:
        raise Error(f"Expected exactly one GPU UUID and driver version\n{output}")
    uuid, version = (part.strip() for part in rows[0])
    if not re.fullmatch(r"GPU-[A-Za-z0-9-]+", uuid) or not re.fullmatch(
        r"[0-9]+(?:\.[0-9]+)+", version
    ):
        raise Error(f"Invalid GPU UUID or driver version\n{output}")
    return uuid, version


def _parse_gpu_list(output: str) -> str:
    lines = _program_lines(output)
    if len(lines) != 1:
        raise Error(f"Expected exactly one GPU in nvidia-smi -L\n{output}")
    match = re.fullmatch(r"GPU [0-9]+: .+ \(UUID: (GPU-[A-Za-z0-9-]+)\)", lines[0])
    if not match:
        raise Error(f"Unrecognized nvidia-smi -L output\n{output}")
    return match.group(1)


def _initialized_libraries(output: str, family: str) -> list[str]:
    """Accept actual loader initialization, never merely 'trying file=...'."""
    paths = set()
    for line in output.splitlines():
        match = _INIT_LINE.fullmatch(line)
        if match and _LIBRARY_NAMES[family].fullmatch(PurePosixPath(match.group(1)).name):
            paths.add(match.group(1))
    if not paths:
        raise Error(
            f"No actual {family} library initialization in LD_DEBUG=libs output; "
            f"ldd/search candidates are not driver proof\n{output}"
        )
    return sorted(paths)


def _sample_success(output: str) -> str:
    lines = _program_lines(output)
    # The 2026 unified-memory sample exits zero even on a result mismatch.
    # Older vectorAdd prints Test PASSED before cudaFree, so successful process
    # exit (enforced by common.run) remains mandatory as well as this marker.
    failures = (
        "Unified Memory: Error - CPU and GPU answers do not match",
        "Result verification failed",
        "Test FAILED",
    )
    if any(marker in line for line in lines for marker in failures):
        raise Error(f"vectorAdd reported a failed result verification\n{output}")
    for marker in _SAMPLE_SOURCES:
        if marker in lines:
            return marker
    raise Error(f"vectorAdd did not report a verified NVIDIA success marker\n{output}")


def verify(target: dict, user: str, sample: str, state: dict) -> dict:
    """Execute GPU queries and vectorAdd, recording raw outputs and loaded libs.

    No dependency installation, synthetic sample, or root fallback is performed.
    The supplied sample's source revision/build flags must be recorded by its
    provider; success output alone cannot establish executable provenance.
    """
    if not user or user == "root":
        raise Error("verify requires an existing non-root container user")
    if not sample or not sample.startswith("/") or sample == "/":
        raise Error("--cuda-sample must be an absolute container executable path")
    for field in _IDENTITY_FIELDS:
        if state["target"].get(field) != target.get(field):
            raise Error(f"Saved injection belongs to a different target instance: {field}")

    uuid = state["gpu_uuid"]
    version = state["driver_version"]
    plan = state["plan"]
    environment = dict(plan["env"])
    environment["LC_ALL"] = "C"
    commands: list[dict] = []

    def container(stage: str, argv: list[str], *, env: dict | None = None,
                  timeout: int = 60) -> str:
        command_env = environment if env is None else env
        try:
            output = runtime.exec_container(
                target, argv, user=user, env=command_env, timeout=timeout,
                additional_gids=plan.get("additional_gids", [])
            )
        except Error as exc:
            raise Error(f"verify {stage}, user={user}, command={argv!r}: {exc}") from exc
        commands.append({
            "stage": stage, "scope": "container", "user": user,
            "argv": list(argv), "env": dict(command_env), "timeout": timeout,
            "exit_code": 0, "output": output,
        })
        return output

    def host_driver(stage: str) -> dict:
        argv = [*_QUERY, f"--id={uuid}"]
        try:
            output = run(argv)
        except Error as exc:
            raise Error(f"verify {stage}, command={argv!r}: {exc}") from exc
        commands.append({
            "stage": stage, "scope": "host", "argv": argv,
            "exit_code": 0, "output": output,
        })
        found_uuid, found_version = _parse_gpu_query(output)
        if (found_uuid, found_version) != (uuid, version):
            raise Error(
                f"verify {stage}: host GPU/driver changed; expected {uuid}, {version}; "
                f"re-plan and re-attach before verification\n{output}"
            )
        return {"gpu_uuid": found_uuid, "driver_version": found_version, "output": output}

    resolved: dict[str, str] = {}

    def canonical(path: str, evidence_output: str) -> str:
        if path not in resolved:
            try:
                output = container("resolve-driver-path", ["readlink", "-e", "--", path])
            except Error as exc:
                raise Error(f"{exc}\nLoader output:\n{evidence_output}") from exc
            value = output.strip()
            if not value.startswith("/") or "\n" in value:
                raise Error(
                    f"Invalid canonical driver path for {path}\n{output}\n"
                    f"Loader output:\n{evidence_output}"
                )
            resolved[path] = value
        return resolved[path]

    def library_proof(output: str, family: str) -> list[dict]:
        loaded = _initialized_libraries(output, family)
        mounts = [
            mount for mount in plan["mounts"]
            if any(_LIBRARY_NAMES[family].fullmatch(PurePosixPath(mount[key]).name)
                   for key in ("target", "source"))
        ]
        if not mounts:
            raise Error(f"No planned {family} driver mount for initialized libraries\n{output}")
        expected = {canonical(mount["target"], output): mount for mount in mounts}
        proof = []
        for path in loaded:
            actual = canonical(path, output)
            if actual not in expected:
                raise Error(
                    f"Loaded {family} library {path} resolves to {actual}, not an injected "
                    f"driver mount; expected {sorted(expected)!r}\n{output}"
                )
            mount = expected[actual]
            proof.append({
                "initialized_path": path, "resolved_path": actual,
                "mount_target": mount["target"], "mount_source": mount["source"],
                "evidence": "LD_DEBUG=libs calling init + container readlink -e",
            })
        return proof

    runtime.assert_identity(target)
    host_before = host_driver("host-driver-before")
    failure: Error | None = None
    result: dict = {}
    try:
        uid_output = container("ordinary-user-uid", ["id", "-u"])
        uid_text = uid_output.strip()
        if not uid_text.isdecimal() or int(uid_text) == 0:
            raise Error(f"verify must execute as a non-root container UID\n{uid_output}")
        user_identity = container("ordinary-user-identity", ["id"])
        list_output = container("nvidia-smi-list", ["nvidia-smi", "-L"])
        if _parse_gpu_list(list_output) != uuid:
            raise Error(f"Container nvidia-smi -L does not show only selected {uuid}\n{list_output}")

        debug_env = {**environment, "LD_DEBUG": "libs"}
        query_output = container("nvidia-smi-query", list(_QUERY), env=debug_env)
        if _parse_gpu_query(query_output) != (uuid, version):
            raise Error(
                f"Container GPU/driver differs from injected {uuid}, {version}\n{query_output}"
            )
        nvml = library_proof(query_output, "nvml")
        sample_env = {**debug_env, "CUDA_VISIBLE_DEVICES": uuid}
        sample_output = container("vectorAdd", [sample], env=sample_env, timeout=180)
        success_marker = _sample_success(sample_output)
        cuda = library_proof(sample_output, "cuda")
        result = {
            "verified": True, "target": dict(target), "user": user,
            "uid": int(uid_text), "user_identity": user_identity,
            "gpu_uuid": uuid, "driver_version": version,
            "sample": {
                "path": sample, "success_marker": success_marker,
                "output_contract_source": _SAMPLE_SOURCES[success_marker],
                "cuda_visible_devices": uuid,
            },
            "driver_proof": {"nvml": nvml, "cuda": cuda},
        }
    except Error as exc:
        failure = exc
    finally:
        # Perform both checks even if the actual GPU command failed. Keep its
        # original output if a simultaneous restart/driver update is detected.
        post_errors = []
        try:
            runtime.assert_identity(target)
        except Error as exc:
            post_errors.append(f"target identity after verification: {exc}")
        try:
            host_after = host_driver("host-driver-after")
        except Error as exc:
            post_errors.append(str(exc))
        if post_errors:
            details = "\n".join(post_errors)
            failure = Error(f"{failure}\n{details}" if failure else details)
    if failure is not None:
        raise failure
    result["host_driver_before"] = host_before
    result["host_driver_after"] = host_after
    result["commands"] = commands
    result["identity_unchanged"] = True
    return result
