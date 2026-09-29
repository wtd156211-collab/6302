"""分帧引擎单元测试（标准库 unittest）。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framing import Frame, Framer, MAX_BUFFERED


def frame_bytes(length, body_byte=0x61):
    return length.to_bytes(4, "big") + bytes([body_byte]) * length


class FramerTest(unittest.TestCase):
    def test_single_frame_one_shot(self):
        f = Framer()
        frames = f.feed(frame_bytes(5))
        self.assertEqual(frames, [Frame(0, 9, 5)])
        self.assertEqual(f.finish()["status"], "ok")

    def test_prefix_split_across_chunks(self):
        f = Framer()
        raw = frame_bytes(3)
        self.assertEqual(f.feed(raw[:2]), [])   # 前缀 2+2 切开
        self.assertEqual(f.feed(raw[2:4]), [])
        frames = f.feed(raw[4:])
        self.assertEqual(frames, [Frame(0, 7, 3)])
        self.assertEqual(f.finish()["status"], "ok")

    def test_prefix_split_one_plus_three(self):
        f = Framer()
        raw = frame_bytes(2)
        self.assertEqual(f.feed(raw[:1]), [])
        self.assertEqual(f.feed(raw[1:]), [Frame(0, 6, 2)])

    def test_body_split_across_three_chunks(self):
        f = Framer()
        raw = frame_bytes(10)
        self.assertEqual(f.feed(raw[:5]), [])
        self.assertEqual(f.feed(raw[5:9]), [])
        frames = f.feed(raw[9:])
        self.assertEqual(frames, [Frame(0, 14, 10)])

    def test_sticky_packets_one_chunk_many_frames(self):
        f = Framer()
        raw = frame_bytes(1) + frame_bytes(2) + frame_bytes(0)
        frames = f.feed(raw)
        self.assertEqual(frames, [Frame(0, 5, 1), Frame(5, 11, 2), Frame(11, 15, 0)])

    def test_zero_length_frames_run(self):
        f = Framer()
        raw = frame_bytes(0) * 4
        frames = f.feed(raw)
        self.assertEqual(len(frames), 4)
        for i, fr in enumerate(frames):
            self.assertEqual(fr, Frame(i * 4, i * 4 + 4, 0))

    def test_empty_chunk_is_noop(self):
        f = Framer()
        self.assertEqual(f.feed(b""), [])
        self.assertEqual(f.feed(frame_bytes(1)), [Frame(0, 5, 1)])
        self.assertEqual(f.feed(b""), [])
        report = f.finish()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["stream-bytes"], 5)

    def test_oversize_stalls_immediately_and_discards(self):
        f = Framer()
        first = frame_bytes(4)
        self.assertEqual(f.feed(first), [Frame(0, 8, 4)])
        # 声明 65537 的前缀一到就停摆，不等帧体
        self.assertEqual(f.feed((65537).to_bytes(4, "big")), [])
        self.assertEqual(f.feed(b"x" * 100), [])  # 后续字节丢弃但仍计流长
        report = f.finish()
        self.assertEqual(report["status"], "oversize")
        self.assertEqual(report["frames"], 1)
        self.assertEqual(report["stream-bytes"], 8 + 4 + 100)
        self.assertEqual(report["tail-start"], 8)
        self.assertEqual(report["tail-bytes"], 4 + 100)
        self.assertEqual(report["declared-length"], 65537)

    def test_max_body_length_is_legal(self):
        f = Framer()
        frames = f.feed(frame_bytes(65536))
        self.assertEqual(frames, [Frame(0, 65540, 65536)])
        self.assertEqual(f.finish()["status"], "ok")

    def test_incomplete_prefix(self):
        f = Framer()
        f.feed(frame_bytes(2) + b"\x00\x00")
        report = f.finish()
        self.assertEqual(report["status"], "incomplete-prefix")
        self.assertEqual(report["tail-start"], 6)
        self.assertEqual(report["tail-bytes"], 2)
        self.assertIsNone(report["declared-length"])

    def test_incomplete_body(self):
        f = Framer()
        f.feed((64).to_bytes(4, "big") + b"y" * 25)
        report = f.finish()
        self.assertEqual(report["status"], "incomplete-body")
        self.assertEqual(report["tail-start"], 0)
        self.assertEqual(report["tail-bytes"], 29)
        self.assertEqual(report["declared-length"], 64)

    def test_offsets_are_stream_wide_not_per_chunk(self):
        f = Framer()
        f.feed(frame_bytes(3))
        frames = f.feed(frame_bytes(4))
        self.assertEqual(frames, [Frame(7, 15, 4)])

    def test_chunking_does_not_change_result(self):
        raw = frame_bytes(5) + frame_bytes(0) + frame_bytes(300) + frame_bytes(1)
        once = Framer()
        expected = once.feed(raw)
        for cut in (1, 2, 3, 4, 5, 7, 64):
            f = Framer()
            got = []
            for i in range(0, len(raw), cut):
                got.extend(f.feed(raw[i:i + cut]))
            self.assertEqual(got, expected)
            self.assertEqual(f.finish()["status"], "ok")

    def test_buffer_stays_under_limit(self):
        f = Framer()
        peak = 0
        for _ in range(64):
            f.feed(frame_bytes(65536))
            peak = max(peak, f.buffered)
        self.assertLessEqual(peak, MAX_BUFFERED)
        self.assertEqual(f.finish()["frames"], 64)


if __name__ == "__main__":
    unittest.main()
