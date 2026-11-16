"""Tests of our CDI selection, edit merge, and supported-hook boundary."""

import copy
import unittest

from nvidia_nspawn.cdi import parse
from nvidia_nspawn.common import Error


UUID = "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER = "GPU-11111111-2222-3333-4444-555555555555"


def fixture():
    return {
        "cdiVersion": "0.8.0", "kind": "nvidia.com/gpu",
        "containerEdits": {
            "deviceNodes": [{"path": "/dev/nvidiactl"}, {"path": "/dev/nvidia-uvm"}],
            "mounts": [{"hostPath": "/usr/lib64/libcuda.so.580.1",
                        "containerPath": "/usr/lib64/libcuda.so.580.1",
                        "options": ["ro", "nosuid", "nodev", "bind"]},
                       {"hostPath": "/usr/lib64/libEGL_nvidia.so.580.1",
                        "containerPath": "/usr/lib64/libEGL_nvidia.so.580.1",
                        "options": ["ro", "bind"]}],
            "env": ["NVIDIA_VISIBLE_DEVICES=void"],
        },
        "devices": [
            {"name": OTHER, "containerEdits": {"deviceNodes": [{"path": "/dev/nvidia1"}]}},
            {"name": UUID, "containerEdits": {"deviceNodes": [{"path": "/dev/nvidia0"}]}},
            {"name": "all", "containerEdits": {"deviceNodes": [
                {"path": "/dev/nvidia0"}, {"path": "/dev/nvidia1"}]}},
        ],
    }


def hook(kind, *args, ctk=False):
    binary = "nvidia-ctk" if ctk else "nvidia-cdi-hook"
    return {"hookName": "createContainer", "path": "/usr/bin/" + binary,
            "args": [binary] + (["hook"] if ctk else []) + [kind, *args],
            "env": ["NVIDIA_CTK_DEBUG=false"]}


class ParseTests(unittest.TestCase):
    def test_selects_uuid_and_preserves_all_common_resources(self):
        result = parse(fixture(), UUID)
        self.assertEqual([d["target"] for d in result["devices"]],
                         ["/dev/nvidiactl", "/dev/nvidia-uvm", "/dev/nvidia0"])
        self.assertEqual(len(result["mounts"]), 2)  # No filename-based graphics pruning.
        self.assertEqual(result["env"], {"NVIDIA_VISIBLE_DEVICES": "void"})
        self.assertEqual(result["cdi_device"], UUID)
        self.assertEqual(result["additional_gids"], [])

    def test_deduplicates_consistent_edits_without_mutating_input(self):
        spec = fixture()
        edits = spec["devices"][1]["containerEdits"]
        edits["deviceNodes"].append({"path": "/dev/nvidiactl", "permissions": "mwr"})
        edits["mounts"] = [copy.deepcopy(spec["containerEdits"]["mounts"][0])]
        edits["mounts"][0]["options"].reverse()
        edits["env"] = ["NVIDIA_VISIBLE_DEVICES=void"]
        original = copy.deepcopy(spec)
        result = parse(spec, UUID)
        self.assertEqual(len(result["devices"]), 3)
        self.assertEqual(len(result["mounts"]), 2)
        self.assertEqual(spec, original)

    def test_device_metadata_is_preserved(self):
        spec = fixture()
        node = spec["devices"][1]["containerEdits"]["deviceNodes"][0]
        node.update(major=195, minor=0, fileMode=0o660, uid=0, gid=44)
        result = parse(spec, UUID)["devices"][-1]
        for key in ("major", "minor", "fileMode", "uid", "gid"):
            self.assertEqual(result[key], node[key])

    def test_additional_gids_merge_without_mutating_or_selecting_other_devices(self):
        spec = fixture()
        spec["containerEdits"]["additionalGids"] = [485, 485]
        spec["devices"][1]["containerEdits"]["additionalGids"] = [488, 485, 44]
        spec["devices"][0]["containerEdits"]["additionalGids"] = [999]
        original = copy.deepcopy(spec)
        self.assertEqual(parse(spec, UUID)["additional_gids"], [485, 488, 44])
        self.assertEqual(spec, original)

    def test_invalid_additional_gids_rejected(self):
        for gids in ("485", [-1], [True], ["485"], [None]):
            spec = fixture()
            spec["containerEdits"]["additionalGids"] = gids
            with self.subTest(gids=gids), self.assertRaisesRegex(Error, "additionalGids"):
                parse(spec, UUID)

    def test_missing_ambiguous_and_non_uuid_selection_rejected(self):
        for identifier in ("0", "all", "MIG-123", "GPU-missing"):
            with self.subTest(identifier=identifier), self.assertRaises(Error):
                parse(fixture(), identifier)
        spec = fixture()
        spec["devices"].append(copy.deepcopy(spec["devices"][1]))
        with self.assertRaisesRegex(Error, "found 2"):
            parse(spec, UUID)

    def test_conflicting_edits_rejected(self):
        for field, edit in (
            ("deviceNodes", [{"path": "/dev/nvidiactl", "hostPath": "/dev/other"}]),
            ("mounts", [{"hostPath": "/another", "containerPath": "/usr/lib64/libcuda.so.580.1",
                         "options": ["ro", "bind"]}]),
            ("env", ["NVIDIA_VISIBLE_DEVICES=all"]),
        ):
            spec = fixture()
            spec["devices"][1]["containerEdits"][field] = edit
            with self.subTest(field=field), self.assertRaisesRegex(Error, "Conflicting"):
                parse(spec, UUID)

    def test_unsupported_edits_rejected(self):
        for field, value in (("intelRdt", {"closID": "x"}), ("futureEdit", [])):
            spec = fixture()
            spec["containerEdits"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(Error, "Unsupported"):
                parse(spec, UUID)

    def test_only_readonly_bind_mounts(self):
        spec = fixture()
        spec["containerEdits"]["mounts"][0]["options"] = ["bind", "nosuid"]
        self.assertEqual(parse(spec, UUID)["mounts"][0]["options"], ["bind", "nosuid", "ro"])
        spec["containerEdits"]["mounts"][0]["options"] = ["rbind", "rprivate", "nosuid"]
        self.assertEqual(parse(spec, UUID)["mounts"][0]["options"],
                         ["rbind", "rprivate", "nosuid", "ro"])
        for options in (["rw", "bind"], ["ro"], ["bind", "shared"]):
            spec["containerEdits"]["mounts"][0]["options"] = options
            with self.subTest(options=options), self.assertRaises(Error):
                parse(spec, UUID)

    def test_mount_device_and_link_collisions_rejected(self):
        spec = fixture()
        spec["containerEdits"]["mounts"].append(
            {"hostPath": "/somewhere", "containerPath": "/dev/nvidia0", "options": ["bind"]})
        with self.assertRaisesRegex(Error, "both mount and device"):
            parse(spec, UUID)
        spec = fixture()
        spec["containerEdits"]["hooks"] = [hook("create-symlinks", "--link", "other::/dev/nvidia0")]
        with self.assertRaisesRegex(Error, "conflicts with mounted"):
            parse(spec, UUID)

    def test_hook_actions_and_preflight_details(self):
        spec = fixture()
        hooks = [
            hook("create-symlinks", "--link", "libcuda.so.580.1::/usr/lib64/libcuda.so.1"),
            hook("update-ldcache", "--folder=/usr/lib64", "--folder", "/usr/lib/x86_64-linux-gnu",
                 "--ldconfig-path", "@/sbin/ldconfig", ctk=True),
            hook("enable-cuda-compat", "--host-driver-version=580.1"),
            hook("disable-device-node-modification"),
            hook("update-application-profile"),
        ]
        spec["containerEdits"]["hooks"] = hooks
        result = parse(spec, UUID)
        self.assertEqual(result["hooks"], hooks)
        self.assertEqual(result["links"], [{"target": "libcuda.so.580.1",
                                            "link": "/usr/lib64/libcuda.so.1"}])
        self.assertEqual(result["ldconfig_folders"], ["/usr/lib64", "/usr/lib/x86_64-linux-gnu"])
        self.assertEqual(result["hook_actions"][1]["ldconfig_path"], "@/sbin/ldconfig")
        self.assertEqual([action["kind"] for action in result["hook_actions"]],
                         [item["args"][2 if "hook" in item["args"] else 1] for item in hooks])
        self.assertEqual(result["hook_actions"][0]["options"],
                         {"link": ["libcuda.so.580.1::/usr/lib64/libcuda.so.1"]})
        self.assertEqual(result["hook_actions"][1]["options"],
                         {"folder": ["/usr/lib64", "/usr/lib/x86_64-linux-gnu"],
                          "ldconfig-path": "@/sbin/ldconfig"})
        self.assertEqual(result["hook_actions"][-1]["options"], {})

    def test_toolkit_1_20_2_edits_reach_consumers(self):
        # Reduced from the dedicated VM's generated CDI, not a hook implementation test.
        gpu_uuid = "GPU-661f5e69-d0b1-35a2-0b48-d3a4f8e2c2af"
        symlinks = hook("create-symlinks", "--link",
                        "../renderD129::/dev/dri/by-path/pci-0000:03:00.0-render")
        memory = hook("apply-cuda-memory-limits", "--driver-root", "", "--gpu-id", gpu_uuid)
        memory["hookName"] = "createRuntime"
        common_hooks = [
            hook("enable-cuda-compat", "--host-driver-version=580.196.07"),
            hook("update-ldcache", "--folder", "/usr/lib64", "--folder", "/usr/lib64/vdpau"),
            hook("disable-device-node-modification"),
            hook("update-application-profile"),
        ]
        spec = {
            "cdiVersion": "0.7.0", "kind": "nvidia.com/gpu",
            "containerEdits": {
                "env": ["NVIDIA_CTK_LIBCUDA_DIR=/usr/lib64", "NVIDIA_VISIBLE_DEVICES=void"],
                "additionalGids": [485],
                "hooks": common_hooks,
                "mounts": [
                    {"hostPath": "/run/nvidia-persistenced/socket",
                     "containerPath": "/run/nvidia-persistenced/socket",
                     "options": ["nosuid", "nodev", "rbind", "rprivate", "noexec"]},
                    {"hostPath": "/usr/lib64/libcuda.so.580.196.07",
                     "containerPath": "/usr/lib64/libcuda.so.580.196.07",
                     "options": ["ro", "nosuid", "nodev", "rbind", "rprivate"]},
                ],
            },
            "devices": [{"name": gpu_uuid, "containerEdits": {
                "deviceNodes": [
                    {"path": "/dev/nvidia0", "major": 195, "fileMode": 432,
                     "permissions": "rwm", "gid": 485},
                    {"path": "/dev/dri/renderD129", "major": 226, "minor": 129,
                     "fileMode": 432, "permissions": "rwm", "gid": 488},
                ],
                "additionalGids": [485, 488], "hooks": [symlinks, memory],
            }}],
        }
        original = copy.deepcopy(spec)
        result = parse(spec, gpu_uuid)
        self.assertEqual(result["additional_gids"], [485, 488])
        self.assertEqual(result["devices"][-1]["gid"], 488)
        self.assertEqual(result["devices"][-1]["fileMode"], 0o660)
        self.assertEqual(result["mounts"][0]["options"],
                         ["nosuid", "nodev", "rbind", "rprivate", "noexec", "ro"])
        self.assertEqual(result["mounts"][1]["options"],
                         spec["containerEdits"]["mounts"][1]["options"])
        self.assertEqual(result["env"],
                         {"NVIDIA_CTK_LIBCUDA_DIR": "/usr/lib64", "NVIDIA_VISIBLE_DEVICES": "void"})
        self.assertEqual(result["hooks"], common_hooks + [symlinks, memory])
        self.assertEqual(result["hook_actions"][-1],
                         {"kind": "apply-cuda-memory-limits",
                          "options": {"driver-root": "", "gpu-id": [gpu_uuid]}})
        self.assertEqual(result["links"], [
            {"target": "../renderD129", "link": "/dev/dri/by-path/pci-0000:03:00.0-render"}])
        self.assertEqual(result["ldconfig_folders"], ["/usr/lib64", "/usr/lib64/vdpau"])
        self.assertEqual(spec, original)

    def test_memory_hook_preserves_repeated_gpu_ids_and_empty_driver_root_forms(self):
        for root_args in (("--driver-root", ""), ("--driver-root=",)):
            spec = fixture()
            item = hook("apply-cuda-memory-limits", *root_args,
                        "--gpu-id", UUID, "--gpu-id=" + OTHER)
            item["hookName"] = "createRuntime"
            spec["devices"][1]["containerEdits"]["hooks"] = [item]
            result = parse(spec, UUID)
            self.assertEqual(result["hooks"], [item])
            self.assertEqual(result["hook_actions"], [
                {"kind": "apply-cuda-memory-limits",
                 "options": {"driver-root": "", "gpu-id": [UUID, OTHER]}}])

    def test_memory_quota_environment_explicitly_rejected(self):
        for name in ("NVIDIA_GPU_MEMORY_REQUEST", "NVIDIA_GPU_MEMORY_LIMIT"):
            for value in ("", "1024"):
                for common in (True, False):
                    spec = fixture()
                    edits = spec["containerEdits"] if common else spec["devices"][1]["containerEdits"]
                    edits["env"] = [name + "=" + value]
                    with self.subTest(name=name, value=value, common=common):
                        with self.assertRaisesRegex(Error, name):
                            parse(spec, UUID)

    def test_hook_lifecycles_are_kind_specific(self):
        for kind in ("create-symlinks", "update-ldcache", "enable-cuda-compat",
                     "disable-device-node-modification", "update-application-profile",
                     "apply-cuda-memory-limits"):
            for lifecycle in ("createContainer", "createRuntime", "prestart"):
                if (kind == "apply-cuda-memory-limits" and lifecycle == "createRuntime"
                        or kind != "apply-cuda-memory-limits" and lifecycle == "createContainer"):
                    continue
                args = ("--link", "target::/some/link") if kind == "create-symlinks" else ()
                if kind == "apply-cuda-memory-limits":
                    args = ("--gpu-id", UUID)
                item = hook(kind, *args)
                item["hookName"] = lifecycle
                spec = fixture()
                spec["containerEdits"]["hooks"] = [item]
                with self.subTest(kind=kind, lifecycle=lifecycle):
                    with self.assertRaisesRegex(Error, "lifecycle"):
                        parse(spec, UUID)

    def test_memory_hook_invalid_options_rejected(self):
        for args in ((), ("--gpu-id", ""), ("--gpu-id", "   "),
                     ("--gpu-id", UUID, "--driver-root"),
                     ("--gpu-id", UUID, "--driver-root", "", "--driver-root", ""),
                     ("--gpu-id", UUID, "--container-spec", "/host/spec"),
                     ("--gpu-id", UUID, "--driver-root", "\x00")):
            item = hook("apply-cuda-memory-limits", *args)
            item["hookName"] = "createRuntime"
            spec = fixture()
            spec["containerEdits"]["hooks"] = [item]
            with self.subTest(args=args), self.assertRaises(Error):
                parse(spec, UUID)

    def test_unsupported_hook_semantics_rejected(self):
        cases = [hook("unknown"), hook("create-symlinks", "--link", "broken"),
                 hook("update-ldcache", "--container-spec", "/host/spec"),
                 hook("disable-device-node-modification", "--unexpected"),
                 hook("update-ldcache", "--folder", "relative"),
                 hook("update-ldcache", "--ldconfig-path", "/sbin/ldconfig", "--ldconfig-path", "/other")]
        bad = hook("update-ldcache")
        bad["path"] = "/usr/bin/unrelated"
        cases.append(bad)
        bad = hook("update-ldcache")
        bad["hookName"] = "createRuntime"
        cases.append(bad)
        bad = hook("update-ldcache")
        bad["env"] = ["LD_PRELOAD=/evil.so"]
        cases.append(bad)
        for item in cases:
            spec = fixture()
            spec["containerEdits"]["hooks"] = [item]
            with self.subTest(hook=item), self.assertRaises(Error):
                parse(spec, UUID)

    def test_duplicate_links_and_hooks(self):
        spec = fixture()
        item = hook("create-symlinks", "--link", "libcuda.so.580.1::/usr/lib64/libcuda.so.1")
        spec["containerEdits"]["hooks"] = [item]
        spec["devices"][1]["containerEdits"]["hooks"] = [copy.deepcopy(item)]
        self.assertEqual(len(parse(spec, UUID)["hooks"]), 1)
        spec["devices"][1]["containerEdits"]["hooks"] = [
            hook("create-symlinks", "--link", "other.so::/usr/lib64/libcuda.so.1")]
        with self.assertRaisesRegex(Error, "Conflicting CDI symlink"):
            parse(spec, UUID)

    def test_invalid_paths_and_metadata_rejected(self):
        for path in ("relative", "/", "/usr/../etc/file"):
            spec = fixture()
            spec["containerEdits"]["mounts"][0]["containerPath"] = path
            with self.subTest(path=path), self.assertRaises(Error):
                parse(spec, UUID)
        spec = fixture()
        spec["devices"][1]["containerEdits"]["deviceNodes"][0]["major"] = -1
        with self.assertRaises(Error):
            parse(spec, UUID)


if __name__ == "__main__":
    unittest.main()
