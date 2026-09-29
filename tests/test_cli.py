"""命令行端到端测试：对照 samples/expected 逐字节校验。"""

import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MAIN = os.path.join(ROOT, "main.py")
CHUNKS = os.path.join(ROOT, "samples", "chunks")
EXPECTED = os.path.join(ROOT, "samples", "expected")

CASES = [
    "01-prefix-split", "02-body-split", "03-frame-span-three",
    "04-zero-length-run", "05-oversize", "06-incomplete-prefix",
    "07-incomplete-body", "08-scale-32mib",
]


def run_cli(*args, env=None):
    return subprocess.run(
        [sys.executable, MAIN, *args],
        capture_output=True, text=False, env=env, cwd=ROOT)


class CliTest(unittest.TestCase):
    def test_all_cases_match_expected_bytes(self):
        for case in CASES:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                frames = os.path.join(tmp, "f.txt")
                report = os.path.join(tmp, "r.txt")
                proc = run_cli(os.path.join(CHUNKS, case),
                               "--frames", frames, "--report", report)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                with open(frames, "rb") as fh:
                    got_frames = fh.read()
                with open(os.path.join(EXPECTED, f"{case}.frames.txt"), "rb") as fh:
                    self.assertEqual(got_frames, fh.read())
                with open(report, "rb") as fh:
                    got_report = fh.read()
                with open(os.path.join(EXPECTED, f"{case}.report.txt"), "rb") as fh:
                    self.assertEqual(got_report, fh.read())

    def test_deterministic_across_runs_and_hashseeds(self):
        case_dir = os.path.join(CHUNKS, "04-zero-length-run")
        outputs = []
        for seed in ("0", "1", "12345"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            proc = run_cli(case_dir, "--frames", os.devnull, "--report", "-", env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            outputs.append(proc.stdout)
        self.assertTrue(all(o == outputs[0] for o in outputs))

    def test_frames_and_report_both_stdout_is_usage_error(self):
        proc = run_cli(os.path.join(CHUNKS, CASES[0]), "--report", "-")
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"error", proc.stderr)

    def test_manifest_mismatch_is_sample_error(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(CHUNKS, "01-prefix-split")
            dst = os.path.join(tmp, "bad")
            shutil.copytree(src, dst)
            with open(os.path.join(dst, "manifest.tsv"), "r",
                      encoding="utf-8") as fh:
                lines = fh.readlines()
            name, _size, _off = lines[0].rstrip("\n").split("\t")
            lines[0] = f"{name}\t999\t999\n"
            with open(os.path.join(dst, "manifest.tsv"), "w",
                      encoding="utf-8", newline="\n") as fh:
                fh.writelines(lines)
            proc = run_cli(dst)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("样本错误".encode(), proc.stderr)

    def test_missing_dir_is_error(self):
        proc = run_cli(os.path.join(CHUNKS, "no-such-case"))
        self.assertEqual(proc.returncode, 1)

    def test_html_is_single_file_and_matches_report(self):
        import json
        import re
        case = "05-oversize"
        with tempfile.TemporaryDirectory() as tmp:
            html_path = os.path.join(tmp, "frames.html")
            proc = run_cli(os.path.join(CHUNKS, case),
                           "--frames", os.devnull, "--html", html_path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(html_path, encoding="utf-8") as fh:
                html = fh.read()
            self.assertNotRegex(html, r"https?://")
            self.assertNotIn("<link", html)
            self.assertNotIn("src=", html)
            match = re.search(r"const DATA = (.*?);\nconst meta", html, re.S)
            self.assertIsNotNone(match)
            data = json.loads(match.group(1))
            with open(os.path.join(EXPECTED, f"{case}.report.txt"),
                      encoding="utf-8") as fh:
                expected = dict(line.split(" ", 1)
                                for line in fh.read().splitlines())
            self.assertEqual(data["report"]["status"], expected["status"])
            self.assertEqual(str(data["report"]["frames"]), expected["frames"])
            tail_start = None if expected["tail-start"] == "-" \
                else int(expected["tail-start"])
            self.assertEqual(data["report"]["tail-start"], tail_start)
            if tail_start is None:
                self.assertIsNone(data["tail"])
            else:
                self.assertEqual(data["tail"],
                                 [tail_start, data["report"]["stream-bytes"]])

    def test_large_case_streaming_memory(self):
        code = (
            "import sys, os, resource\n"
            f"sys.path.insert(0, {ROOT!r})\n"
            "from main import load_manifest\n"
            "from framing import Framer\n"
            f"d = {os.path.join(CHUNKS, '08-scale-32mib')!r}\n"
            "f = Framer(); peak = 0\n"
            "for name, _ in load_manifest(d):\n"
            "    with open(os.path.join(d, name), 'rb') as fh:\n"
            "        f.feed(fh.read())\n"
            "    peak = max(peak, f.buffered)\n"
            "rep = f.finish()\n"
            "assert rep['status'] == 'ok' and rep['frames'] == 23455, rep\n"
            "assert peak <= 2 * 1024 * 1024, peak\n"
            "rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n"
            "assert rss < 128 * 1024, rss\n"
            "print('peak-buffered', peak, 'maxrss-KB', rss)\n"
        )
        proc = subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
