"""Tests only for verification's own output interpretation algorithms."""

import unittest

from nvidia_nspawn.common import Error
from nvidia_nspawn.verify import (
    _initialized_libraries,
    _parse_gpu_list,
    _parse_gpu_query,
    _sample_success,
)


UUID = "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class GPUOutputTests(unittest.TestCase):
    def test_query_excludes_loader_records(self):
        output = (
            "     42: find library=libnvidia-ml.so.1 [0]; searching\n"
            "     42:   trying file=/usr/lib/libnvidia-ml.so.1\n"
            f"{UUID}, 580.65.06\n"
            "     42: calling fini: /usr/lib/libnvidia-ml.so.1 [0]\n"
        )
        self.assertEqual(_parse_gpu_query(output), (UUID, "580.65.06"))

    def test_query_rejects_multiple_gpus(self):
        with self.assertRaises(Error):
            _parse_gpu_query(f"{UUID}, 580.65.06\nGPU-other, 580.65.06\n")

    def test_query_rejects_extra_diagnostic(self):
        with self.assertRaises(Error):
            _parse_gpu_query(f"Warning: driver mismatch\n{UUID}, 580.65.06\n")

    def test_query_rejects_malformed_rows(self):
        for output in ("", f"{UUID}\n", f"{UUID}, unknown\n", '"unterminated'):
            with self.subTest(output=output), self.assertRaises(Error):
                _parse_gpu_query(output)

    def test_list_requires_only_selected_full_gpu(self):
        self.assertEqual(_parse_gpu_list(f"GPU 0: NVIDIA Example (UUID: {UUID})\n"), UUID)
        for output in (
            "No devices were found\n",
            f"GPU 0: NVIDIA Example (UUID: {UUID})\n  MIG 1g.5gb Device 0: (UUID: MIG-example)\n",
            f"GPU 0: NVIDIA Example (UUID: {UUID})\nGPU 1: Other (UUID: GPU-other)\n",
        ):
            with self.subTest(output=output), self.assertRaises(Error):
                _parse_gpu_list(output)


class LoaderEvidenceTests(unittest.TestCase):
    def test_only_initialized_matching_libraries_are_evidence(self):
        output = (
            "  123: trying file=/wrong/libcuda.so.1\n"
            "  123: calling init: /usr/lib/libnvidia-ml.so.580.65.06\n"
            "  123: calling init: /usr/lib/libcuda.so.1\n"
            "  123: calling init: /usr/lib/libcuda.so.1\n"
            "  123: calling fini: /wrong/libcuda.so.1 [0]\n"
        )
        self.assertEqual(_initialized_libraries(output, "cuda"), ["/usr/lib/libcuda.so.1"])
        self.assertEqual(
            _initialized_libraries(output, "nvml"), ["/usr/lib/libnvidia-ml.so.580.65.06"]
        )

    def test_search_candidates_and_lookalike_names_are_not_evidence(self):
        for output in (
            "  123: trying file=/usr/lib/libcuda.so.1\n",
            "  123: calling init: /usr/lib/libcudart.so.12\n",
            "  123: calling init: /usr/lib/libcuda.so.1.backup\n",
            "libcuda.so.1 => /usr/lib/libcuda.so.1 (0xdeadbeef)\n",
        ):
            with self.subTest(output=output), self.assertRaises(Error):
                _initialized_libraries(output, "cuda")


class SampleResultTests(unittest.TestCase):
    def test_versioned_nvidia_success_markers(self):
        self.assertEqual(_sample_success("Test PASSED\nDone\n"), "Test PASSED")
        marker = "Unified Memory: CPU and GPU answers match"
        self.assertEqual(_sample_success(f"  123: calling init: /lib/libcuda.so.1\n{marker}\n"), marker)

    def test_zero_exit_output_still_needs_result_verification(self):
        for output in (
            "CUDA kernel launch with 196 blocks of 256 threads\nDone\n",
            "Not really Test PASSED\n",
            "Result = PASS\n",  # Not a marker in the verified vectorAdd sources.
            "Unified Memory: Error - CPU and GPU answers do not match\n",
            "Test PASSED\nResult verification failed at element 5!\n",
        ):
            with self.subTest(output=output), self.assertRaises(Error):
                _sample_success(output)


if __name__ == "__main__":
    unittest.main()
