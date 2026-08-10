import time
import unittest

from hpcprobe.commands import net


class ParseEthstatsTests(unittest.TestCase):
    def test_parses_key_value_pairs(self):
        self.assertEqual(
            net._parse_ethstats("rx_out_of_buffer=0;rx_crc_errors_phy=5;"),
            {"rx_out_of_buffer": "0", "rx_crc_errors_phy": "5"},
        )

    def test_empty_string_yields_empty_dict(self):
        self.assertEqual(net._parse_ethstats(""), {})

    def test_skips_fragments_without_equals(self):
        self.assertEqual(
            net._parse_ethstats("rx_out_of_buffer=0;;garbage;rx_crc_errors_phy=5"),
            {"rx_out_of_buffer": "0", "rx_crc_errors_phy": "5"},
        )


class FmtRateTests(unittest.TestCase):
    def test_extracts_leading_number(self):
        self.assertEqual(net._fmt_rate("100 Gb/sec (4X EDR)"), "100G")

    def test_dash_passes_through(self):
        self.assertEqual(net._fmt_rate("-"), "-")

    def test_empty_string_passes_through(self):
        self.assertEqual(net._fmt_rate(""), "-")


class ParseRemoteOutputTests(unittest.TestCase):
    def test_ibport_netdev_ethstats_join_by_netdev_name(self):
        stdout = "\n".join([
            "IBPORT|mlx5_0|1|ACTIVE|LinkUp|InfiniBand|100 Gb/sec (4X EDR)|ib0",
            "NETDEV|ib0|up|1|100000|4200",
            "ETHSTATS|ib0|rx_out_of_buffer=5;rx_crc_errors_phy=10;",
        ])
        result = net._parse_remote_output(stdout)
        self.assertEqual(len(result["ports"]), 1)
        self.assertEqual(result["ports"][0]["rate"], "100 Gb/sec (4X EDR)")
        self.assertEqual(result["netdevs"]["ib0"]["stats"]["rx_crc_errors_phy"], "10")

    def test_malformed_lines_are_ignored_not_fatal(self):
        result = net._parse_remote_output("this is not a delimited record")
        self.assertEqual(result, {"ports": [], "netdevs": {}})


class ComputeDeltasTests(unittest.TestCase):
    def _snapshot(self, stats, age_seconds=60):
        return {
            "timestamp": time.time() - age_seconds,
            "data": {"node01": {"netdevs": {"ib0": {"stats": stats}}}},
        }

    def test_increasing_counter_yields_positive_delta(self):
        current = {"node01": {"netdevs": {"ib0": {"stats": {"rx_crc_errors_phy": "15"}}}}}
        previous = self._snapshot({"rx_crc_errors_phy": "10"})
        annotated, elapsed = net._compute_deltas(current, previous)
        self.assertEqual(annotated["node01"]["netdevs"]["ib0"]["delta"]["rx_crc_errors_phy"], 5)
        self.assertGreater(elapsed, 0)

    def test_counter_reset_yields_none_not_negative(self):
        # CLAUDE.md: "Counter resets are reported as None/'reset', never a negative delta."
        current = {"node01": {"netdevs": {"ib0": {"stats": {"rx_out_of_buffer": "5"}}}}}
        previous = self._snapshot({"rx_out_of_buffer": "20"})
        annotated, _ = net._compute_deltas(current, previous)
        self.assertIsNone(annotated["node01"]["netdevs"]["ib0"]["delta"]["rx_out_of_buffer"])

    def test_error_nodes_are_left_untouched(self):
        current = {"node01": {"error": "timeout"}}
        previous = self._snapshot({"rx_out_of_buffer": "20"})
        annotated, _ = net._compute_deltas(current, previous)
        self.assertEqual(annotated["node01"], {"error": "timeout"})


if __name__ == "__main__":
    unittest.main()
