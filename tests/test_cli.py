"""命令行端到端测试：逐用例对期望输出、确定性、错误口径、页面生成。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(REPO, "main.py")
CHUNKS = os.path.join(REPO, "samples", "chunks")
EXPECTED = os.path.join(REPO, "samples", "expected")

CASES = [
    "01-prefix-split",
    "02-body-split",
    "03-frame-span-three",
    "04-zero-length-run",
    "05-oversize",
    "06-incomplete-prefix",
    "07-incomplete-body",
    "08-scale-32mib",
]


def run_cli(*args, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, MAIN, *args],
        capture_output=True,
        env=env,
    )


class TestCli(unittest.TestCase):
    def test_all_cases_match_expected_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            for case in CASES:
                with self.subTest(case=case):
                    frames_out = os.path.join(tmp, "frames.txt")
                    report_out = os.path.join(tmp, "report.txt")
                    proc = run_cli(
                        os.path.join(CHUNKS, case),
                        "--frames", frames_out,
                        "--report", report_out,
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr.decode())
                    with open(frames_out, "rb") as fh:
                        frames = fh.read()
                    with open(os.path.join(EXPECTED, f"{case}.frames.txt"), "rb") as fh:
                        self.assertEqual(frames, fh.read())
                    with open(report_out, "rb") as fh:
                        report = fh.read()
                    with open(os.path.join(EXPECTED, f"{case}.report.txt"), "rb") as fh:
                        self.assertEqual(report, fh.read())

    def test_frames_default_to_stdout(self):
        proc = run_cli(os.path.join(CHUNKS, "01-prefix-split"))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        with open(os.path.join(EXPECTED, "01-prefix-split.frames.txt"), "rb") as fh:
            self.assertEqual(proc.stdout, fh.read())

    def test_deterministic_across_runs_and_hashseeds(self):
        case_dir = os.path.join(CHUNKS, "04-zero-length-run")
        first = run_cli(case_dir)
        second = run_cli(case_dir)
        third = run_cli(case_dir, env_extra={"PYTHONHASHSEED": "1"})
        fourth = run_cli(case_dir, env_extra={"PYTHONHASHSEED": "424242"})
        for proc in (second, third, fourth):
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, first.stdout)

    def test_both_stdout_is_usage_error(self):
        proc = run_cli(os.path.join(CHUNKS, "01-prefix-split"), "--report", "-")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, b"")

    def test_missing_dir_is_error(self):
        proc = run_cli(os.path.join(CHUNKS, "no-such-case"))
        self.assertEqual(proc.returncode, 1)

    def test_manifest_mismatch_is_sample_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(CHUNKS, "01-prefix-split")
            dst = os.path.join(tmp, "case")
            shutil.copytree(src, dst)
            manifest = os.path.join(dst, "manifest.tsv")
            with open(manifest, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
            lines[0] = lines[0].replace("\t2\t", "\t3\t")
            with open(manifest, "w", encoding="utf-8", newline="\n") as fh:
                fh.writelines(lines)
            proc = run_cli(dst)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("manifest", proc.stderr.decode())

    def test_html_is_self_contained_and_matches_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            html_path = os.path.join(tmp, "frames.html")
            frames_out = os.path.join(tmp, "frames.txt")
            proc = run_cli(
                os.path.join(CHUNKS, "05-oversize"),
                "--frames", frames_out,
                "--html", html_path,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            with open(html_path, "r", encoding="utf-8") as fh:
                html = fh.read()
            self.assertNotIn("http://", html)
            self.assertNotIn("https://", html)
            self.assertNotIn("<link", html)
            self.assertNotIn("src=", html)
            match = re.search(r"const DATA = (\{.*?\});\n", html)
            self.assertIsNotNone(match)
            data = json.loads(match.group(1))
            self.assertEqual(data["report"]["status"], "oversize")
            self.assertEqual(data["report"]["tail_start"], 24)
            self.assertEqual(data["report"]["tail_bytes"], 13)
            self.assertEqual(data["report"]["declared_length"], 65537)
            self.assertEqual(len(data["chunks"]), 4)
            with open(frames_out, "r", encoding="utf-8") as fh:
                rows = [line.split() for line in fh.read().splitlines()]
            self.assertEqual(len(data["frames"]), len(rows))
            for row, frame in zip(rows, data["frames"]):
                self.assertEqual([int(row[1]), int(row[2]), int(row[3])], frame)


if __name__ == "__main__":
    unittest.main()
