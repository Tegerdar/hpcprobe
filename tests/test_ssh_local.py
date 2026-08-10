import subprocess
import unittest
from unittest import mock

from hpcprobe.core import ssh


class IsLocalNodeTests(unittest.TestCase):
    def setUp(self):
        ssh._local_names.cache_clear()
        patcher_hn = mock.patch("socket.gethostname", return_value="node01")
        patcher_fqdn = mock.patch("socket.getfqdn", return_value="node01.cluster.example")
        self.addCleanup(patcher_hn.stop)
        self.addCleanup(patcher_fqdn.stop)
        patcher_hn.start()
        patcher_fqdn.start()
        self.addCleanup(ssh._local_names.cache_clear)

    def test_short_hostname_matches(self):
        self.assertTrue(ssh.is_local_node("node01"))

    def test_fqdn_matches(self):
        self.assertTrue(ssh.is_local_node("node01.cluster.example"))

    def test_case_insensitive(self):
        self.assertTrue(ssh.is_local_node("NODE01"))

    def test_surrounding_whitespace_from_slurm_output_is_tolerated(self):
        self.assertTrue(ssh.is_local_node("  node01  "))

    def test_different_node_is_not_local(self):
        self.assertFalse(ssh.is_local_node("node02"))

    def test_hpcprobe_force_ssh_overrides_a_real_match(self):
        with mock.patch.dict("os.environ", {"HPCPROBE_FORCE_SSH": "1"}):
            self.assertFalse(ssh.is_local_node("node01"))

    def test_gethostname_and_getfqdn_disagreeing_both_still_match(self):
        # DHCP/hosts-file mismatches are common enough that both spellings
        # need to resolve as local, not just whichever call happens to run.
        self.assertTrue(ssh.is_local_node("node01"))
        self.assertTrue(ssh.is_local_node("node01.cluster.example"))


class SshPollDispatchTests(unittest.TestCase):
    """ssh_poll must never touch the network for a local node, and must
    never skip the network for a remote one - this is the actual behavior
    change, so it's the part worth locking down with mocks rather than
    trusting local_run/ssh_run's own tests to imply it."""

    def test_local_node_never_shells_out_to_ssh(self):
        with mock.patch.object(ssh, "is_local_node", return_value=True), \
             mock.patch.object(ssh, "local_run") as mock_local, \
             mock.patch.object(ssh, "ssh_run") as mock_ssh:
            mock_local.return_value = subprocess.CompletedProcess(
                args="x", returncode=0, stdout="ok", stderr="")
            result, err = ssh.ssh_poll("node01", "echo hi")
        mock_local.assert_called_once()
        mock_ssh.assert_not_called()
        self.assertIsNone(err)
        self.assertEqual(result.stdout, "ok")

    def test_remote_node_never_uses_the_local_shortcut(self):
        with mock.patch.object(ssh, "is_local_node", return_value=False), \
             mock.patch.object(ssh, "ssh_run") as mock_ssh, \
             mock.patch.object(ssh, "local_run") as mock_local:
            mock_ssh.return_value = subprocess.CompletedProcess(
                args="x", returncode=0, stdout="ok", stderr="")
            ssh.ssh_poll("node02", "echo hi")
        mock_ssh.assert_called_once()
        mock_local.assert_not_called()

    def test_local_nonzero_exit_reports_the_given_fail_label(self):
        with mock.patch.object(ssh, "is_local_node", return_value=True), \
             mock.patch.object(ssh, "local_run") as mock_local:
            mock_local.return_value = subprocess.CompletedProcess(
                args="x", returncode=1, stdout="", stderr="boom")
            result, err = ssh.ssh_poll("node01", "false", fail_label="custom_fail")
        self.assertIsNone(result)
        self.assertEqual(err, {"error": "custom_fail"})

    def test_local_timeout_reports_timeout_not_a_traceback(self):
        with mock.patch.object(ssh, "is_local_node", return_value=True), \
             mock.patch.object(ssh, "local_run",
                               side_effect=subprocess.TimeoutExpired("x", 3)):
            result, err = ssh.ssh_poll("node01", "sleep 999")
        self.assertIsNone(result)
        self.assertEqual(err, {"error": "timeout"})


class LocalRunIntegrationTests(unittest.TestCase):
    """No mocks - actually runs a shell on this machine, since this is the
    one place hpcprobe now executes without SSH ever being in the loop."""

    def test_runs_a_real_command_and_captures_stdout(self):
        result = ssh.local_run("echo hello")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "hello")

    def test_multiline_script_runs_like_a_remote_shell_would(self):
        # Same shape as net.py's REMOTE_SCRIPT: several statements, one string.
        script = "x=1\ny=2\necho $((x + y))"
        result = ssh.local_run(script)
        self.assertEqual(result.stdout.strip(), "3")

    def test_nonzero_exit_is_reported_not_raised(self):
        result = ssh.local_run("exit 7")
        self.assertEqual(result.returncode, 7)

    def test_via_ssh_poll_end_to_end_for_the_real_local_host(self):
        # No mocks at all: is_local_node() must recognize *this* machine by
        # its actual hostname for ssh_poll to route locally in the first
        # place - this is what would have failed silently if the hostname
        # matching logic were subtly wrong.
        import socket
        result, err = ssh.ssh_poll(socket.gethostname(), "echo real")
        self.assertIsNone(err)
        self.assertEqual(result.stdout.strip(), "real")


if __name__ == "__main__":
    unittest.main()
