import re
import stat
import unittest

from nvidia_nspawn._worker import recovery_paths
from nvidia_nspawn.runtime import residual_candidates


def descriptor(path, **changes):
    return {"path": path, "resolved": path, "exists": True,
            "mode": stat.S_IFREG | 0o644, "uid": 0, "gid": 0,
            "inode": 101, "dev": 8, "rdev": 0, "size": 0,
            "mtime_ns": 1_000_000_000, "ctime_ns": 2_000_000_000,
            **changes}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.root = descriptor("/", mode=stat.S_IFDIR | 0o755, inode=2)

    def record(self, *paths):
        return {"root": self.root.copy(), "paths": list(paths)}

    def current(self, *paths, root=None, mounts=()):
        return {"root": self.root.copy() if root is None else root,
                "paths": list(paths),
                "mounts": [{"target": target} for target in mounts]}

    def test_temporary_hook_cleanup_does_not_claim_changed_parent_directory_times(self):
        parent = descriptor("/run", mode=stat.S_IFDIR | 0o755)
        removed = {"path": "/run/hook", "resolved": "/run/hook", "exists": False}
        before = {"paths": [parent, removed], "mounts": []}
        current: dict = {"paths": [{**parent, "mtime_ns": 3, "ctime_ns": 4, "size": 4096},
                             removed.copy()], "mounts": []}
        self.assertEqual(residual_candidates(before, current, ["/run", "/run/hook"]), [])
        current["paths"][0]["inode"] = 102
        self.assertEqual([p["path"] for p in residual_candidates(
            before, current, ["/run", "/run/hook"])], ["/run"])

    def test_recovers_placeholders_config_and_symlinks_in_record_order(self):
        placeholder = descriptor("/usr/bin/nvidia-smi")
        config = descriptor("/etc/ld.so.conf.d/nvidia.conf", inode=102, size=28)
        link = descriptor("/usr/lib64/libcuda.so.1", inode=103,
                          mode=stat.S_IFLNK | 0o777, size=19,
                          resolved="/usr/lib64/libcuda.so.580.196.07",
                          link="libcuda.so.580.196.07")
        unrelated = descriptor("/etc/existing-container-file", inode=104)
        observation = self.current(unrelated, link.copy(), config.copy(),
                                   placeholder.copy(), mounts=("/", "/usr"))

        self.assertEqual(recovery_paths(self.record(placeholder, config, link),
                                        observation),
                         [placeholder, config, link])

    def test_skips_resources_missing_from_recreated_dev_and_run(self):
        device = descriptor("/dev/nvidia0", mode=stat.S_IFCHR | 0o666,
                            inode=110, dev=9, rdev=49920)
        runtime = descriptor("/run/nvidia-ctk-hook", mode=stat.S_IFDIR | 0o755,
                             inode=111, dev=10)
        placeholder = descriptor("/usr/bin/nvidia-smi")
        observation = self.current(
            {"path": device["path"], "resolved": device["resolved"], "exists": False},
            placeholder.copy(),
            {"path": runtime["path"], "resolved": runtime["resolved"], "exists": False},
            mounts=("/dev", "/run"))

        self.assertEqual(recovery_paths(self.record(device, runtime, placeholder),
                                        observation), [placeholder])

    def test_rejects_same_size_file_modified_with_restored_mtime(self):
        config = descriptor("/etc/ld.so.conf.d/nvidia.conf", size=28)
        changed = {**config, "ctime_ns": config["ctime_ns"] + 1}

        with self.assertRaisesRegex(RuntimeError, re.escape(config["path"])):
            recovery_paths(self.record(config), self.current(changed))

    def test_rejects_replaced_or_changed_file_fingerprints(self):
        placeholder = descriptor("/usr/bin/nvidia-smi")
        changes = {"resolved": "/another-root/usr/bin/nvidia-smi",
                   "inode": 999, "dev": 99, "mode": stat.S_IFREG | 0o600,
                   "uid": 1000, "gid": 1000, "rdev": 1,
                   "size": 1, "mtime_ns": 3_000_000_000}
        for field, value in changes.items():
            with self.subTest(field=field):
                changed = {**placeholder, field: value}
                with self.assertRaisesRegex(RuntimeError, re.escape(placeholder["path"])):
                    recovery_paths(self.record(placeholder), self.current(changed))

    def test_rejects_changed_link_text_or_redirected_symlink_target(self):
        link = descriptor("/usr/lib64/libcuda.so.1", mode=stat.S_IFLNK | 0o777,
                          resolved="/usr/lib64/libcuda.so.580.196.07",
                          link="libcuda.so.580.196.07", size=25)
        for change in ({"link": "./libcuda.so.580.196.07"},
                       {"resolved": "/unowned/libcuda.so.580.196.07"}):
            with self.subTest(change=change):
                with self.assertRaisesRegex(RuntimeError, re.escape(link["path"])):
                    recovery_paths(self.record(link), self.current({**link, **change}))

    def test_does_not_claim_resources_in_replaced_root(self):
        placeholder = descriptor("/usr/bin/nvidia-smi")
        changed = {**placeholder, "inode": 999}
        for field in ("dev", "inode"):
            with self.subTest(field=field):
                root = {**self.root, field: self.root[field] + 1}
                observation = self.current(changed, root=root,
                                           mounts=(placeholder["resolved"],))
                self.assertEqual(recovery_paths(self.record(placeholder), observation), [])

    def test_root_identity_ignores_metadata_other_than_device_and_inode(self):
        placeholder = descriptor("/usr/bin/nvidia-smi")
        root = {**self.root, "mode": stat.S_IFDIR | 0o700,
                "uid": 1000, "gid": 1000, "size": 4096,
                "mtime_ns": 4_000_000_000, "ctime_ns": 5_000_000_000}

        self.assertEqual(recovery_paths(self.record(placeholder),
                                        self.current(placeholder.copy(), root=root)),
                         [placeholder])

    def test_rejects_mount_at_or_below_resolved_resource_path(self):
        directory = descriptor("/lib/nvidia", resolved="/usr/lib/nvidia",
                               mode=stat.S_IFDIR | 0o755)
        for target in ("/usr/lib/nvidia", "/usr/lib/nvidia/subdir/library.so"):
            with self.subTest(target=target):
                with self.assertRaisesRegex(RuntimeError, re.escape(directory["path"])):
                    recovery_paths(self.record(directory),
                                   self.current(directory.copy(), mounts=(target,)))

    def test_mount_prefix_sibling_does_not_block_recovery(self):
        directory = descriptor("/usr/lib/nvidia", mode=stat.S_IFDIR | 0o755)

        self.assertEqual(recovery_paths(
            self.record(directory), self.current(directory.copy(),
                                                 mounts=("/usr/lib/nvidia-other",))),
            [directory])

    def test_directory_content_metadata_changes_are_allowed(self):
        directory = descriptor("/etc/nvidia", mode=stat.S_IFDIR | 0o755,
                               size=4096)
        changed = {**directory, "size": 8192, "mtime_ns": 6_000_000_000,
                   "ctime_ns": 7_000_000_000}

        self.assertEqual(recovery_paths(self.record(directory), self.current(changed)),
                         [changed])

    def test_directory_identity_and_permissions_must_still_match(self):
        directory = descriptor("/etc/nvidia", mode=stat.S_IFDIR | 0o755)
        for change in ({"inode": 999}, {"mode": stat.S_IFDIR | 0o777},
                       {"uid": 1000}, {"gid": 1000}):
            with self.subTest(change=change):
                with self.assertRaisesRegex(RuntimeError, re.escape(directory["path"])):
                    recovery_paths(self.record(directory),
                                   self.current({**directory, **change}))


if __name__ == "__main__":
    unittest.main()
