import unittest

from nvidia_nspawn._worker import validate_user_namespace


class UserNamespaceTests(unittest.TestCase):
    own_namespace = "user:[4026531837]"
    target_namespace = "user:[4026533000]"
    safe_map = "0 524288 65536\n"

    def test_rejects_shared_user_namespace(self):
        with self.assertRaises(ValueError):
            validate_user_namespace(
                self.own_namespace, self.own_namespace, self.safe_map, self.safe_map)

    def test_rejects_host_root_in_either_map(self):
        for unsafe_map in ("0 0 65536\n", "0 524288 65536\n65536 0 1\n"):
            for kind in ("uid", "gid"):
                with self.subTest(mapping=unsafe_map, kind=kind):
                    uid_map = unsafe_map if kind == "uid" else self.safe_map
                    gid_map = unsafe_map if kind == "gid" else self.safe_map
                    with self.assertRaises(ValueError):
                        validate_user_namespace(
                            self.own_namespace, self.target_namespace, uid_map, gid_map)

    def test_rejects_unmapped_container_root_in_either_map(self):
        for unmapped in ("", "1 524289 65535\n", "0 524288 0\n"):
            for kind in ("uid", "gid"):
                with self.subTest(mapping=unmapped, kind=kind):
                    uid_map = unmapped if kind == "uid" else self.safe_map
                    gid_map = unmapped if kind == "gid" else self.safe_map
                    with self.assertRaises(ValueError):
                        validate_user_namespace(
                            self.own_namespace, self.target_namespace, uid_map, gid_map)

    def test_accepts_multiple_nonroot_host_ranges(self):
        self.assertIsNone(validate_user_namespace(
            self.own_namespace, self.target_namespace,
            "0 524288 1000\n1000 1000 1\n1001 525289 64535\n",
            "0 1048576 1000\n1000 1000 1\n1001 1049577 64535\n"))

    def test_accepts_standard_private_user_namespace(self):
        self.assertIsNone(validate_user_namespace(
            self.own_namespace, self.target_namespace, self.safe_map, self.safe_map))
