import csv
import io
import json
import unittest

from hpcprobe.formatters import csv_out, json_out, prometheus_out

GPU_STATE = {
    "node01": {
        "gpus": [{
            "index": 0, "model": "H100", "util_pct": 10.0,
            "mem_used_mb": 100.0, "mem_total_mb": 200.0,
            "temp_c": 40.0, "power_w": 300.0,
        }],
    },
}


class JsonOutTests(unittest.TestCase):
    def test_round_trips_the_input_dict(self):
        data = {"node01": {"foo": 1, "bar": [1, 2, 3]}}
        self.assertEqual(json.loads(json_out.render(data)), data)


class CsvAliasEquivalenceTests(unittest.TestCase):
    """gpu.py emits module="gpus"; mem.py emits module="memory" but the CSV
    formatter accepts "gpu"/"mem" too. Both spellings must render identically
    so the alias handling can later be collapsed without changing output."""

    def test_gpus_and_gpu_produce_identical_csv(self):
        self.assertEqual(csv_out.render(GPU_STATE, module="gpus"), csv_out.render(GPU_STATE, module="gpu"))

    def test_memory_and_mem_produce_identical_csv(self):
        state = {"node01": {"os_memtotal_mb": 1000.0}}
        self.assertEqual(csv_out.render(state, module="memory"), csv_out.render(state, module="mem"))


class PrometheusAliasEquivalenceTests(unittest.TestCase):
    def test_gpus_and_gpu_produce_identical_output(self):
        self.assertEqual(
            prometheus_out.render(GPU_STATE, module="gpus"),
            prometheus_out.render(GPU_STATE, module="gpu"),
        )

    def test_memory_and_mem_produce_identical_output(self):
        state = {"node01": {"os_memtotal_mb": 1000.0}}
        self.assertEqual(
            prometheus_out.render(state, module="memory"),
            prometheus_out.render(state, module="mem"),
        )

    def test_cpu_and_cpus_alias_accepted(self):
        state = {"node01": {"cpu(s)": "64"}}
        self.assertEqual(
            prometheus_out.render(state, module="cpu"),
            prometheus_out.render(state, module="cpus"),
        )


class CsvModuleShapeTests(unittest.TestCase):
    """csv_out branches carrying real per-row logic (int-conversion fallbacks,
    beegfs/lustre row filtering) - exercised end to end, not just via alias
    equivalence, since that's where a KeyError or wrong-column bug would hide."""

    def test_network_csv_renders_port_and_stats_row(self):
        state = {
            "node01": {
                "ports": [{
                    "device": "mlx5_0", "port": "1", "state": "ACTIVE", "phys_state": "LinkUp",
                    "link_layer": "InfiniBand", "rate": "100 Gb/sec (4X EDR)", "netdev": "ib0",
                }],
                "netdevs": {"ib0": {"stats": {"rx_out_of_buffer": "5", "rx_crc_errors_phy": "10"}}},
            },
            "node02": {"error": "timeout"},
        }
        rows = csv_out.render(state, module="network").splitlines()
        self.assertEqual(len(rows), 3)  # header + node01 port row + node02 error row
        self.assertIn("mlx5_0", rows[1])
        self.assertIn("ib0", rows[1])
        self.assertTrue(rows[2].startswith("node02,"))
        self.assertTrue(rows[2].endswith(",timeout"))

    def test_storage_csv_renders_mount_beegfs_and_lustre_rows(self):
        state = {
            "node01": {
                "mounts": [{"mountpoint": "/", "fstype": "ext4", "blocks_1k": "1048576", "used_1k": "500000", "avail_1k": "548576", "pcent": "48%"}],
                "beegfs": {"meta": [{"target_id": "1", "pool": "default", "free_pct": 80}], "storage": []},
                "lustre": [{"target": "fs-OST0000", "use_pct": 42}],
            },
        }
        rows = csv_out.render(state, module="storage").splitlines()
        self.assertEqual(len(rows), 4)  # header + mount row + beegfs row + lustre row
        self.assertIn("/,ext4,1.0,", rows[1])  # blocks_1k -> GB conversion, exact value
        self.assertIn(",1,meta,default,80,", rows[2])
        self.assertIn(",fs-OST0000,42,", rows[3])

    def test_storage_csv_mount_with_unparseable_blocks_falls_back_to_blank(self):
        state = {"node01": {"mounts": [{"mountpoint": "/weird", "fstype": "9p", "blocks_1k": "n/a", "used_1k": "n/a", "avail_1k": "n/a", "pcent": "-"}]}}
        row = csv_out.render(state, module="storage").splitlines()[1]
        self.assertIn("/weird,9p,,,,-,", row)


class ErrorNodeHandlingTests(unittest.TestCase):
    """A failed node's entry is {"error": "..."}; every formatter must skip it
    cleanly rather than crashing or emitting a bogus metric row for it."""

    def test_csv_gpu_error_row_has_no_gpu_fields(self):
        state = {"node01": {"error": "timeout"}}
        rows = list(csv.reader(io.StringIO(csv_out.render(state, module="gpus"))))
        self.assertEqual(len(rows), 2)  # header + one error row
        header, row = rows
        # By column name, not position: Status/PCI_Bus_ID were appended after
        # Error, so "last field" is no longer a stand-in for "the error".
        self.assertEqual(row[header.index("Error")], "timeout")
        for col in ("GPU_Index", "Model", "Util_Pct", "Mem_Used_MB",
                    "Mem_Total_MB", "Temp_C", "Power_W"):
            self.assertEqual(row[header.index(col)], "")

    def test_prometheus_skips_error_nodes_entirely(self):
        state = {"node01": {"error": "timeout"}}
        self.assertEqual(prometheus_out.render(state, module="gpus"), "")

    def test_prometheus_mixed_ok_and_error_nodes(self):
        state = dict(GPU_STATE, node02={"error": "timeout"})
        out = prometheus_out.render(state, module="gpus")
        self.assertIn('node="node01"', out)
        self.assertNotIn("node02", out)




class GpuUnreadableFieldTests(unittest.TestCase):
    """A faulted GPU carries None metrics; no formatter may emit that as a
    number, and none may render it as a healthy zero."""

    BAD = {"node01": {"gpus": [{
        "index": 4, "model": "H200", "pci_bus_id": "00000000:BA:00.0",
        "status": "unreadable", "util_pct": None, "mem_used_mb": None,
        "mem_total_mb": None, "temp_c": None, "power_w": None,
    }]}}

    def test_prometheus_omits_unreadable_series_and_marks_gpu_down(self):
        out = prometheus_out.render(self.BAD, module="gpus")
        self.assertNotIn("None", out)
        self.assertNotIn("hpcprobe_gpu_utilization_percent{", out)
        self.assertIn("hpcprobe_gpu_up{", out)
        self.assertTrue(out.rstrip().endswith(" 0.0"))

    def test_csv_leaves_unreadable_cells_empty_not_zero(self):
        rows = list(csv.reader(io.StringIO(csv_out.render(self.BAD, module="gpus"))))
        header, row = rows
        self.assertEqual(row[header.index("Status")], "unreadable")
        self.assertEqual(row[header.index("PCI_Bus_ID")], "00000000:BA:00.0")
        for col in ("Util_Pct", "Mem_Used_MB", "Temp_C", "Power_W"):
            self.assertEqual(row[header.index(col)], "")


if __name__ == "__main__":
    unittest.main()
