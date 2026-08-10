import unittest

from hpcprobe.cli import build_parser


class NodesFlagParsingTests(unittest.TestCase):
    """Locks down the three-way state of -n/--nodes at the argparse level -
    this is exactly the behavior resolve_nodes() depends on, so a change to
    nargs here would silently break it without a test at this layer."""

    def setUp(self):
        self.parser = build_parser()

    def test_flag_absent_is_none(self):
        args = self.parser.parse_args(["gpu"])
        self.assertIsNone(args.nodes)

    def test_bare_flag_is_an_empty_list_not_none(self):
        args = self.parser.parse_args(["gpu", "-n"])
        self.assertEqual(args.nodes, [])

    def test_flag_with_one_name(self):
        args = self.parser.parse_args(["gpu", "-n", "node01"])
        self.assertEqual(args.nodes, ["node01"])

    def test_flag_with_multiple_names(self):
        args = self.parser.parse_args(["gpu", "-n", "node01", "node02"])
        self.assertEqual(args.nodes, ["node01", "node02"])

    def test_bare_flag_followed_by_other_flags_stays_empty(self):
        # nargs='*' must stop consuming as soon as it hits another option
        # string, not swallow -t/-p as if they were node names.
        args = self.parser.parse_args(["gpu", "-n", "-t", "-p"])
        self.assertEqual(args.nodes, [])
        self.assertTrue(args.total)
        self.assertTrue(args.prometheus)

    def test_available_on_every_subcommand_that_targets_nodes(self):
        for cmd in ("gpu", "cpu", "mem", "net", "stg"):
            with self.subTest(cmd=cmd):
                args = self.parser.parse_args([cmd, "-n"])
                self.assertEqual(args.nodes, [])

    def test_jobs_has_no_nodes_flag_at_all(self):
        # jobs is cluster-wide by design (see commands/jobs.py) - it was
        # never given -n, and this pins that on purpose. argparse prints its
        # own usage/error text to stderr before exiting; swallow it here so
        # a passing test doesn't look like a failure in CI output.
        import contextlib
        import io
        with self.assertRaises(SystemExit), \
             contextlib.redirect_stderr(io.StringIO()):
            self.parser.parse_args(["jobs", "-n"])


class BundledShortFlagFootgunTests(unittest.TestCase):
    """-n's variable arity means it CANNOT be bundled with other short flags
    the way -t and -p can be bundled with each other. This is documented in
    the README rather than "fixed", because there is no fix: argparse reads
    a bundle starting with a nargs='*' option as that option's value, full
    stop. These tests exist so nobody "fixes" this by accident later and
    silently changes what -n<anything> means."""

    def setUp(self):
        self.parser = build_parser()

    def test_ntp_is_read_as_a_single_node_named_tp_not_three_flags(self):
        args = self.parser.parse_args(["gpu", "-ntp"])
        self.assertEqual(args.nodes, ["tp"])
        self.assertFalse(args.total)
        self.assertFalse(args.prometheus)

    def test_correct_form_is_space_separated(self):
        args = self.parser.parse_args(["gpu", "-n", "-t", "-p"])
        self.assertEqual(args.nodes, [])
        self.assertTrue(args.total)
        self.assertTrue(args.prometheus)

    def test_two_zero_arg_flags_can_still_bundle_with_each_other(self):
        # Sanity check that the footgun is specific to -n, not bundling
        # in general - -t and -p (both store_true) bundle fine together.
        args = self.parser.parse_args(["gpu", "-tp"])
        self.assertTrue(args.total)
        self.assertTrue(args.prometheus)


if __name__ == "__main__":
    unittest.main()
