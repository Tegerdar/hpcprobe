import unittest

from hpcprobe.commands import stg


class BlocksToGbTests(unittest.TestCase):
    def test_converts_1k_blocks_to_gib(self):
        self.assertEqual(stg._blocks_to_gb("1048576"), 1.0)  # 1024 * 1024 1k-blocks = 1 GiB

    def test_zero_blocks(self):
        self.assertEqual(stg._blocks_to_gb("0"), 0.0)

    def test_non_numeric_returns_zero_not_crash(self):
        self.assertEqual(stg._blocks_to_gb("not-a-number"), 0.0)


class FmtSizeTests(unittest.TestCase):
    def test_stays_in_gb_below_threshold(self):
        self.assertEqual(stg._fmt_size(500.0), "500.0G")

    def test_switches_to_tb_at_1024(self):
        self.assertEqual(stg._fmt_size(1024.0), "1.0T")

    def test_just_below_threshold_stays_gb(self):
        self.assertEqual(stg._fmt_size(1023.9), "1023.9G")


class PcentIntTests(unittest.TestCase):
    def test_parses_percentage(self):
        self.assertEqual(stg._pcent_int("50%"), 50)

    def test_empty_string_returns_sentinel_not_crash(self):
        self.assertEqual(stg._pcent_int(""), -1)

    def test_none_returns_sentinel_not_crash(self):
        self.assertEqual(stg._pcent_int(None), -1)


class ParseBeegfsRowTests(unittest.TestCase):
    # Classic layout: TargetID Pool Total Free Free% ITotal IFree IFree%
    CLASSIC = "1        default  10.5TiB  2.3TiB   22% 100.0M  80.0M  80%"
    # Newer layout inserts a Cap. column between TargetID and Pool.
    WITH_CAP_COLUMN = "1   1.5PiB  default  10.5TiB  2.3TiB   22% 100.0M  80.0M  80%"

    def test_classic_layout(self):
        row = stg._parse_beegfs_row(self.CLASSIC)
        self.assertEqual(row["target_id"], "1")
        self.assertEqual(row["pool"], "default")
        self.assertEqual(row["free_pct"], 22)
        self.assertEqual(row["ifree_pct"], 80)

    def test_cap_column_layout_matches_classic_fields(self):
        # The optional Cap. column some BeeGFS versions add must not shift
        # any of the fields that come after it.
        classic = stg._parse_beegfs_row(self.CLASSIC)
        with_cap = stg._parse_beegfs_row(self.WITH_CAP_COLUMN)
        self.assertEqual(classic["pool"], with_cap["pool"])
        self.assertEqual(classic["free_pct"], with_cap["free_pct"])
        self.assertEqual(classic["ifree_pct"], with_cap["ifree_pct"])

    def test_unrecognized_row_is_marked_unparsed_not_dropped(self):
        row = stg._parse_beegfs_row("garbage line that does not match")
        self.assertEqual(row, {"unparsed": "garbage line that does not match"})

    def test_error_row_passed_through(self):
        row = stg._parse_beegfs_row("[ERROR] beegfs-df failed")
        self.assertEqual(row, {"error": "[ERROR] beegfs-df failed"})


class ParseLustreRowTests(unittest.TestCase):
    def test_target_row(self):
        row = stg._parse_lustre_row("fsname-OST0001_UUID  10.5T  2.3T  8.0T  22% /mnt/lustre")
        self.assertEqual(row["target"], "fsname-OST0001_UUID")
        self.assertEqual(row["use_pct"], 22)
        self.assertNotIn("is_summary", row)

    def test_summary_row_flagged_distinctly_from_target_rows(self):
        row = stg._parse_lustre_row("filesystem_summary:   100.0T  23.0T  77.0T  23% /mnt/lustre")
        self.assertTrue(row["is_summary"])
        self.assertEqual(row["use_pct"], 23)

    def test_unrecognized_row_is_marked_unparsed(self):
        row = stg._parse_lustre_row("UUID   bytes  Used   Available Use% Mounted")
        self.assertIn("unparsed", row)


class ParseRemoteOutputTests(unittest.TestCase):
    # MOUNT payloads are raw `df -PT` lines (whitespace-separated columns),
    # not pipe-separated - only the "MOUNT" record tag itself is pipe-prefixed.

    def test_skips_pseudo_filesystems(self):
        result = stg._parse_remote_output("MOUNT|tmpfs tmpfs 1000 0 1000 0% /dev/shm")
        self.assertEqual(result["mounts"], [])

    def test_keeps_real_mount(self):
        result = stg._parse_remote_output("MOUNT|/dev/sda1 ext4 1048576 500000 548576 48% /")
        self.assertEqual(len(result["mounts"]), 1)
        self.assertEqual(result["mounts"][0]["fstype"], "ext4")

    def test_lustre_rows_repeated_across_mountpoints_dedup_by_target(self):
        # lfs df -h repeats every target once per local mountpoint the same
        # filesystem is mounted at; the same OST reported twice must collapse.
        stdout = "\n".join([
            "LUSTRE_ROW|fsname-OST0000_UUID  10.0T  1.0T  9.0T  10% /mnt/a",
            "LUSTRE_ROW|fsname-OST0000_UUID  10.0T  1.0T  9.0T  10% /mnt/b",
        ])
        result = stg._parse_remote_output(stdout)
        self.assertEqual(len(result["lustre"]), 1)

    def test_beegfs_rows_routed_to_meta_or_storage_by_preceding_section(self):
        stdout = "\n".join([
            "BEEGFS_SECTION|meta",
            "BEEGFS_ROW|" + ParseBeegfsRowTests.CLASSIC,
            "BEEGFS_SECTION|storage",
            "BEEGFS_ROW|" + ParseBeegfsRowTests.CLASSIC,
        ])
        result = stg._parse_remote_output(stdout)
        self.assertEqual(len(result["beegfs"]["meta"]), 1)
        self.assertEqual(len(result["beegfs"]["storage"]), 1)


if __name__ == "__main__":
    unittest.main()
