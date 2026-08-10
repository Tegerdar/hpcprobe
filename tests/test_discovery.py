import unittest
from unittest import mock

from hpcprobe.core.discovery import resolve_nodes


def _args(nodes):
    return mock.Mock(nodes=nodes)


class ResolveNodesTests(unittest.TestCase):
    def test_flag_absent_triggers_discovery(self):
        with mock.patch("hpcprobe.core.discovery.discover_nodes",
                        return_value=["n1", "n2"]) as disc:
            result = resolve_nodes(_args(None), gres_filter="gpu")
        disc.assert_called_once_with("gpu")
        self.assertEqual(result, ["n1", "n2"])

    def test_explicit_list_is_used_verbatim_and_skips_discovery(self):
        with mock.patch("hpcprobe.core.discovery.discover_nodes") as disc:
            result = resolve_nodes(_args(["nodeA", "nodeB"]))
        disc.assert_not_called()
        self.assertEqual(result, ["nodeA", "nodeB"])

    def test_bare_flag_targets_only_the_local_host(self):
        with mock.patch("socket.gethostname", return_value="node01"), \
             mock.patch("hpcprobe.core.discovery.discover_nodes") as disc:
            result = resolve_nodes(_args([]))
        disc.assert_not_called()
        self.assertEqual(result, ["node01"])

    def test_bare_flag_uses_short_hostname_not_fqdn(self):
        with mock.patch("socket.gethostname", return_value="node01.cluster.example"):
            result = resolve_nodes(_args([]))
        self.assertEqual(result, ["node01"])

    def test_bare_flag_ignores_gres_filter(self):
        # Matches the pre-existing explicit-list behavior: if you named the
        # node (even implicitly, by "just this one"), the GPU filter that
        # exists purely to narrow discovery results doesn't apply.
        with mock.patch("socket.gethostname", return_value="node01"), \
             mock.patch("hpcprobe.core.discovery.discover_nodes") as disc:
            result = resolve_nodes(_args([]), gres_filter="gpu")
        disc.assert_not_called()
        self.assertEqual(result, ["node01"])

    def test_missing_nodes_attribute_falls_back_to_discovery(self):
        # jobs.py's argparser has no -n at all; getattr's default covers it.
        with mock.patch("hpcprobe.core.discovery.discover_nodes",
                        return_value=["n1"]) as disc:
            result = resolve_nodes(mock.Mock(spec=[]))
        disc.assert_called_once()
        self.assertEqual(result, ["n1"])


if __name__ == "__main__":
    unittest.main()
