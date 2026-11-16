import unittest

from nvidia_nspawn.common import Error
from nvidia_nspawn.host import merge_allow
from nvidia_nspawn.runtime import mapped_id, shared_with_host, validate_destinations
from nvidia_nspawn._worker import decode_mount_path, parent_mount


class PolicyTests(unittest.TestCase):
    def test_preserves_unrelated_rules_and_unions_requested_permissions(self):
        existing = [["char-pts", "rw"], ["/dev/nvidia0", "r"]]
        devices = [{"source": "/dev/nvidia0", "permissions": "rw"},
                   {"source": "/dev/nvidiactl", "permissions": "rw"}]
        self.assertEqual(merge_allow(existing, devices), [
            ["char-pts", "rw"], ["/dev/nvidia0", "rw"], ["/dev/nvidiactl", "rw"]])
        self.assertEqual(existing[1][1], "r")

    def test_mapped_ids_use_the_containing_range(self):
        self.assertEqual(mapped_id("0 524288 65536\n"), 524288)
        self.assertEqual(mapped_id("0 524288 65536\n", 107), 524395)
        self.assertEqual(mapped_id("0 100000 1000\n1000 1000 1\n", 1000), 1000)
        with self.assertRaises(Error):
            mapped_id("1000 1000 1\n")


class MountTests(unittest.TestCase):
    def test_longest_mount_prefix_respects_path_boundaries(self):
        mounts = [{"target": "/"}, {"target": "/dev"}, {"target": "/dev/shm"}]
        self.assertEqual(parent_mount("/dev/shm/file", mounts)["target"], "/dev/shm")
        self.assertEqual(parent_mount("/device", mounts)["target"], "/")

    def test_mountinfo_escape_decoding_does_not_redecode_literal_backslash(self):
        self.assertEqual(decode_mount_path(r"/a\040b\134040"), "/a b\\040")

    def test_preflight_rejects_overwriting_container_resource(self):
        observation = {"paths": [{"path": "/usr/bin/nvidia-smi", "exists": True}],
                       "mounts": []}
        with self.assertRaisesRegex(Error, "existing container resource"):
            validate_destinations([{"target": "/usr/bin/nvidia-smi"}], {}, observation)

    def test_preflight_rejects_mount_propagation_to_peers(self):
        observation = {"paths": [{"path": "/dev/nvidia0", "exists": False,
                                  "resolved": "/dev/nvidia0"}],
                       "mounts": [{"target": "/", "optional": ["shared:1"]}],
                       "host_shared_groups": ["shared:1"]}
        with self.assertRaisesRegex(Error, "Shared mount propagation"):
            validate_destinations([{"target": "/dev/nvidia0"}], {}, observation)

    def test_container_only_shared_mounts_do_not_propagate_to_host(self):
        self.assertFalse(shared_with_host({"optional": ["shared:268", "master:1"]},
                                          ["shared:1"]))
        self.assertTrue(shared_with_host({"optional": ["shared:1"]}, ["shared:1"]))
