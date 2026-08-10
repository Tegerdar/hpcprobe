import unittest

from hpcprobe.core.summarize import normalise_module, summarize


def _gpu_node(count, util, temp, power, used_mb, total_mb):
    return {"gpus": [
        {"index": i, "model": "H200", "util_pct": util, "mem_used_mb": used_mb,
         "mem_total_mb": total_mb, "temp_c": temp, "power_w": power}
        for i in range(count)
    ]}


class ModuleAliasTests(unittest.TestCase):
    def test_command_module_names_normalise_to_subcommand_names(self):
        self.assertEqual(normalise_module("gpus"), "gpu")
        self.assertEqual(normalise_module("memory"), "mem")
        self.assertEqual(normalise_module("network"), "net")
        self.assertEqual(normalise_module("storage"), "stg")

    def test_already_normalised_names_pass_through(self):
        self.assertEqual(normalise_module("net"), "net")

    def test_unknown_module_raises(self):
        with self.assertRaises(ValueError):
            summarize({}, "nonsense")


class GpuSummaryTests(unittest.TestCase):
    STATE = {
        "n1": _gpu_node(8, util=100.0, temp=70.0, power=500.0,
                        used_mb=1024.0, total_mb=2048.0),
        "n2": _gpu_node(2, util=0.0, temp=30.0, power=50.0,
                        used_mb=0.0, total_mb=2048.0),
        "n3": {"error": "timeout"},
    }

    def setUp(self):
        self.s = summarize(self.STATE, "gpus")

    def test_per_node_row_collapses_all_gpus_on_that_node(self):
        self.assertEqual(self.s["nodes"]["n1"]["gpus"], 8)
        self.assertEqual(self.s["nodes"]["n1"]["util_avg"], 100.0)
        self.assertEqual(self.s["nodes"]["n1"]["power_w"], 4000.0)

    def test_cluster_utilisation_is_weighted_per_gpu_not_per_node(self):
        # 8 GPUs at 100% and 2 at 0% is 80%, not the 50% a per-node mean gives.
        self.assertEqual(self.s["cluster"]["util_avg"], 80.0)

    def test_cluster_max_is_a_max_not_a_sum(self):
        self.assertEqual(self.s["cluster"]["temp_max"], 70.0)

    def test_unreachable_node_is_counted_not_silently_dropped(self):
        meta = self.s["meta"]
        self.assertEqual(meta["nodes_total"], 3)
        self.assertEqual(meta["nodes_ok"], 2)
        self.assertEqual(meta["nodes_error"], 1)
        self.assertEqual(meta["errors"], {"n3": "timeout"})
        self.assertNotIn("n3", self.s["nodes"])


class CpuSummaryTests(unittest.TestCase):
    STATE = {
        "n1": {"slurm_cputot": "100", "slurm_alloccpus": "50",
               "slurm_idlecpus": "50", "slurm_cpuload": "10.0", "socket(s)": "2"},
        "n2": {"slurm_cputot": "100", "slurm_alloccpus": "100",
               "slurm_idlecpus": "0", "slurm_cpuload": "20.0", "socket(s)": "2"},
    }

    def test_counts_sum_and_percentages_recompute(self):
        c = summarize(self.STATE, "cpu")["cluster"]
        self.assertEqual(c["cpus_total"], 200)
        self.assertEqual(c["cpus_alloc"], 150)
        self.assertEqual(c["alloc_pct"], 75.0)

    def test_cluster_load_is_a_mean_not_a_sum(self):
        self.assertEqual(summarize(self.STATE, "cpu")["cluster"]["load"], 15.0)

    def test_node_with_ssh_error_but_working_slurm_data_is_kept(self):
        state = {"n1": {"error": "ssh_failed", "slurm_cputot": "64",
                        "slurm_alloccpus": "64"}}
        s = summarize(state, "cpu")
        self.assertEqual(s["nodes"]["n1"]["cpus_total"], 64)
        self.assertEqual(s["meta"]["nodes_error"], 0)

    def test_node_failing_on_both_sides_is_an_error(self):
        state = {"n1": {"error": "ssh_failed", "slurm_error": "no scontrol"}}
        s = summarize(state, "cpu")
        self.assertEqual(s["meta"]["nodes_error"], 1)


class NetSummaryTests(unittest.TestCase):
    STATE = {
        "n1": {
            "ports": [
                {"device": "mlx5_0", "state": "ACTIVE", "netdev": "ib0",
                 "link_layer": "InfiniBand"},
                {"device": "mlx5_1", "state": "DOWN", "netdev": "-",
                 "link_layer": "InfiniBand"},
            ],
            "netdevs": {"ib0": {"stats": {
                "rx_out_of_buffer": "7", "rx_crc_errors_phy": "1",
                "rx_symbol_err_phy": "2", "rx_discards_phy": "0",
                "tx_discards_phy": "0", "link_down_events_phy": "3",
                "rx_pause_ctrl_phy": "1", "tx_pause_ctrl_phy": "0"}}},
        },
    }

    def test_port_states_are_counted(self):
        n = summarize(self.STATE, "network")["nodes"]["n1"]
        self.assertEqual((n["ports"], n["ports_up"], n["ports_down"]), (2, 1, 1))

    def test_errors_roll_the_individual_counters_into_one_number(self):
        # 1 crc + 2 symbol + 0 + 0 + 3 link-down
        self.assertEqual(summarize(self.STATE, "network")["nodes"]["n1"]["errors"], 6)

    def test_a_netdev_backing_two_ports_is_counted_once(self):
        state = {"n1": {
            "ports": [
                {"device": "mlx5_0", "state": "ACTIVE", "netdev": "ib0",
                 "link_layer": "InfiniBand"},
                {"device": "mlx5_0", "state": "ACTIVE", "netdev": "ib0",
                 "link_layer": "InfiniBand"},
            ],
            "netdevs": {"ib0": {"stats": {"rx_out_of_buffer": "10"}}},
        }}
        self.assertEqual(summarize(state, "net")["nodes"]["n1"]["out_of_buffer"], 10)


class StgSummaryTests(unittest.TestCase):
    def _node(self, local_blocks="1048576", used="524288"):
        return {
            "mounts": [
                {"source": "beegfs_nodev", "fstype": "beegfs",
                 "blocks_1k": "10485760", "used_1k": "5242880",
                 "avail_1k": "5242880", "pcent": "50%", "mountpoint": "/beegfs"},
                {"source": "/dev/sda2", "fstype": "xfs",
                 "blocks_1k": local_blocks, "used_1k": used,
                 "avail_1k": "524288", "pcent": "50%", "mountpoint": "/var"},
            ],
            "beegfs": {"meta": [], "storage": [
                {"target_id": "1", "pool": "Default", "free_pct": 20}]},
            "lustre": [],
        }

    def test_shared_filesystem_counted_once_local_disks_summed(self):
        state = {"n1": self._node(), "n2": self._node()}
        c = summarize(state, "storage")["cluster"]
        # 10 GiB shared, counted once, plus 1 GiB local on each of two nodes.
        self.assertEqual(c["mounts"], 3)
        self.assertEqual(c["size_gb"], 12.0)
        self.assertEqual(c["shared_mounts_deduped"], 1)

    def test_beegfs_targets_are_global_not_per_client(self):
        state = {"n1": self._node(), "n2": self._node()}
        s = summarize(state, "stg")
        self.assertEqual(s["nodes"]["n1"]["beegfs_targets"], 1)
        self.assertEqual(s["cluster"]["beegfs_targets"], 1)

    def test_worst_percentage_is_the_max_across_mounts(self):
        state = {"n1": self._node()}
        state["n1"]["mounts"][1]["pcent"] = "97%"
        self.assertEqual(summarize(state, "stg")["nodes"]["n1"]["worst_pct"], 97.0)

    def test_missing_lustre_reports_the_no_data_sentinel(self):
        s = summarize({"n1": self._node()}, "stg")
        self.assertEqual(s["nodes"]["n1"]["lustre_use_pct_max"], -1.0)


class EmptyStateTests(unittest.TestCase):
    def test_every_module_survives_an_empty_cluster(self):
        for module in ("gpu", "cpu", "mem", "net", "stg"):
            s = summarize({}, module)
            self.assertEqual(s["nodes"], {})
            self.assertEqual(s["meta"]["nodes_total"], 0)

    def test_no_division_by_zero_when_totals_are_zero(self):
        s = summarize({"n1": {"slurm_cputot": "0", "slurm_alloccpus": "0"}}, "cpu")
        self.assertEqual(s["nodes"]["n1"]["alloc_pct"], 0.0)


if __name__ == "__main__":
    unittest.main()
