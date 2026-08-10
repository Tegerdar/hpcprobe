import subprocess
import unittest
from unittest import mock

from hpcprobe.commands import jobs
from hpcprobe.formatters import csv_out, prometheus_out


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=["squeue"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class CollectTests(unittest.TestCase):
    def test_counts_jobs_per_state(self):
        out = "RUNNING\nRUNNING\nPENDING\nCOMPLETING\nRUNNING\n"
        with mock.patch("subprocess.run", return_value=_completed(out)):
            data = jobs.collect()
        self.assertEqual(data["states"], {"RUNNING": 3, "PENDING": 1, "COMPLETING": 1})
        self.assertEqual(data["running"], 3)
        self.assertEqual(data["pending"], 1)
        self.assertEqual(data["other"], 1)
        self.assertEqual(data["total"], 5)

    def test_empty_queue_is_zero_not_an_error(self):
        with mock.patch("subprocess.run", return_value=_completed("")):
            data = jobs.collect()
        self.assertNotIn("error", data)
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["running"], 0)

    def test_blank_lines_are_ignored(self):
        with mock.patch("subprocess.run", return_value=_completed("RUNNING\n\n\n")):
            self.assertEqual(jobs.collect()["total"], 1)

    def test_expand_arrays_passes_dash_r(self):
        with mock.patch("subprocess.run", return_value=_completed("")) as run:
            jobs.collect(expand_arrays=True)
        self.assertIn("-r", run.call_args[0][0])

    def test_default_does_not_expand_arrays(self):
        with mock.patch("subprocess.run", return_value=_completed("")) as run:
            jobs.collect()
        self.assertNotIn("-r", run.call_args[0][0])

    def test_missing_squeue_is_reported_as_data_not_an_exception(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(jobs.collect()["error"], "squeue_not_found")

    def test_timeout_is_reported(self):
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired("squeue", 30)):
            self.assertEqual(jobs.collect()["error"], "squeue_timeout")

    def test_nonzero_exit_surfaces_stderr(self):
        with mock.patch("subprocess.run",
                        return_value=_completed(returncode=1, stderr="no cluster\n")):
            self.assertEqual(jobs.collect()["error"], "no cluster")


class JobsFormatterTests(unittest.TestCase):
    DATA = {"states": {"RUNNING": 2, "PENDING": 5}, "running": 2,
            "pending": 5, "other": 0, "total": 7}

    def test_prometheus_emits_no_empty_label_braces(self):
        out = prometheus_out.render(self.DATA, module="jobs")
        self.assertIn("hpcprobe_jobs_running 2.0", out)
        self.assertNotIn("{}", out)

    def test_prometheus_labels_each_state(self):
        out = prometheus_out.render(self.DATA, module="jobs")
        self.assertIn('hpcprobe_jobs_by_state{state="PENDING"} 5.0', out)

    def test_csv_lists_states_then_rollups(self):
        rows = csv_out.render(self.DATA, module="jobs").splitlines()
        self.assertEqual(rows[0].strip(), "State,Jobs")
        self.assertIn("PENDING_TOTAL,5", [r.strip() for r in rows])


if __name__ == "__main__":
    unittest.main()
