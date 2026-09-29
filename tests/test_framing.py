"""分帧引擎单元测试（标准库 unittest）。"""

from __future__ import annotations

import random
import struct
import unittest

from framing import (
    MAX_BODY_LEN,
    MAX_BUFFERED,
    STATUS_INCOMPLETE_BODY,
    STATUS_INCOMPLETE_PREFIX,
    STATUS_OK,
    STATUS_OVERSIZE,
    Frame,
    Framer,
)


def encode(body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + body


def run_whole(stream: bytes):
    framer = Framer()
    frames = framer.feed(stream)
    return frames, framer.finish()


def run_chunked(stream: bytes, cuts: list[int]):
    framer = Framer()
    frames: list[Frame] = []
    pos = 0
    for cut in cuts:
        frames.extend(framer.feed(stream[pos:cut]))
        pos = cut
    frames.extend(framer.feed(stream[pos:]))
    return frames, framer.finish()


class TestFraming(unittest.TestCase):
    def test_prefix_split_2_plus_2_and_1_plus_3(self):
        stream = encode(b"hello") + encode(b"world!")
        frames, report = run_chunked(stream, [2, 4])
        self.assertEqual(frames, [Frame(0, 9, 5), Frame(9, 19, 6)])
        self.assertEqual(report["status"], STATUS_OK)
        frames, report = run_chunked(stream, [9, 10, 13])
        self.assertEqual(frames, [Frame(0, 9, 5), Frame(9, 19, 6)])
        self.assertEqual(report["status"], STATUS_OK)

    def test_body_split_across_chunks(self):
        stream = encode(b"a" * 100) + encode(b"b" * 50)
        frames, _ = run_chunked(stream, [40, 104, 120])
        self.assertEqual(frames, [Frame(0, 104, 100), Frame(104, 158, 50)])

    def test_frame_span_three_chunks(self):
        stream = encode(b"x") + encode(b"y" * 300) + encode(b"z")
        frames, report = run_chunked(stream, [5, 200, 310])
        self.assertEqual(
            frames,
            [Frame(0, 5, 1), Frame(5, 309, 300), Frame(309, 314, 1)],
        )
        self.assertEqual(report["status"], STATUS_OK)
        self.assertEqual(report["stream_bytes"], len(stream))

    def test_zero_length_run_with_split_prefix(self):
        stream = encode(b"") * 4 + encode(b"q")
        frames, report = run_chunked(stream, [9, 10, 13])
        self.assertEqual(
            frames,
            [
                Frame(0, 4, 0),
                Frame(4, 8, 0),
                Frame(8, 12, 0),
                Frame(12, 16, 0),
                Frame(16, 21, 1),
            ],
        )
        self.assertEqual(report["status"], STATUS_OK)
        self.assertEqual(report["frames"], 5)

    def test_empty_chunk_is_noop(self):
        framer = Framer()
        self.assertEqual(framer.feed(b""), [])
        self.assertEqual(framer.feed(b""), [])
        frames = framer.feed(encode(b"ab"))
        self.assertEqual(frames, [Frame(0, 6, 2)])
        report = framer.finish()
        self.assertEqual(report["status"], STATUS_OK)
        self.assertEqual(report["stream_bytes"], 6)

    def test_oversize_stalls_immediately(self):
        good = encode(b"ok")
        stream = good + struct.pack(">I", MAX_BODY_LEN + 1) + b"junk-after"
        framer = Framer()
        frames = framer.feed(stream[: len(good) + 4])
        self.assertEqual(frames, [Frame(0, len(good), 2)])
        self.assertEqual(framer.feed(b"more"), [])
        self.assertEqual(framer.feed(stream[len(good) + 4 :]), [])
        report = framer.finish()
        self.assertEqual(report["status"], STATUS_OVERSIZE)
        self.assertEqual(report["frames"], 1)
        self.assertEqual(report["tail_start"], len(good))
        self.assertEqual(report["tail_bytes"], len(stream) + 4 - len(good))
        self.assertEqual(report["declared_length"], MAX_BODY_LEN + 1)

    def test_oversize_does_not_wait_for_body(self):
        framer = Framer()
        framer.feed(struct.pack(">I", 10_000_000))
        self.assertEqual(framer.buffered, 0)
        report = framer.finish()
        self.assertEqual(report["status"], STATUS_OVERSIZE)

    def test_incomplete_prefix(self):
        stream = encode(b"ab") + b"\x00\x00"
        frames, report = run_whole(stream)
        self.assertEqual(frames, [Frame(0, 6, 2)])
        self.assertEqual(report["status"], STATUS_INCOMPLETE_PREFIX)
        self.assertEqual(report["tail_start"], 6)
        self.assertEqual(report["tail_bytes"], 2)
        self.assertIsNone(report["declared_length"])

    def test_incomplete_body(self):
        stream = encode(b"ab") + struct.pack(">I", 64) + b"x" * 25
        frames, report = run_whole(stream)
        self.assertEqual(frames, [Frame(0, 6, 2)])
        self.assertEqual(report["status"], STATUS_INCOMPLETE_BODY)
        self.assertEqual(report["tail_start"], 6)
        self.assertEqual(report["tail_bytes"], 29)
        self.assertEqual(report["declared_length"], 64)

    def test_max_body_length_is_legal(self):
        stream = encode(b"m" * MAX_BODY_LEN)
        frames, report = run_whole(stream)
        self.assertEqual(frames, [Frame(0, 4 + MAX_BODY_LEN, MAX_BODY_LEN)])
        self.assertEqual(report["status"], STATUS_OK)

    def test_chunked_equals_whole_random_splits(self):
        rng = random.Random(20260930)
        bodies = [
            bytes(rng.randrange(256) for _ in range(rng.randrange(0, 200)))
            for _ in range(60)
        ]
        bodies += [b"", b"", b"m" * MAX_BODY_LEN]
        stream = b"".join(encode(b) for b in bodies)
        whole_frames, whole_report = run_whole(stream)
        for _ in range(20):
            cuts = sorted(rng.randrange(0, len(stream)) for _ in range(80))
            frames, report = run_chunked(stream, cuts)
            self.assertEqual(frames, whole_frames)
            self.assertEqual(report, whole_report)
        for prev, nxt in zip(whole_frames, whole_frames[1:]):
            self.assertEqual(prev.end, nxt.start)
        self.assertEqual(whole_frames[-1].end, len(stream))

    def test_buffer_stays_bounded(self):
        rng = random.Random(7)
        framer = Framer()
        worst = 0
        total = 0
        pending = b""
        while total < 8 * 1024 * 1024:
            if len(pending) < 1024 * 1024:
                body = bytes(
                    rng.randrange(256)
                    for _ in range(rng.choice([0, 1, 1000, MAX_BODY_LEN]))
                )
                pending += encode(body)
            piece, pending = pending[: 1024 * 1024], pending[1024 * 1024 :]
            total += len(piece)
            framer.feed(piece)
            worst = max(worst, framer.buffered)
            self.assertLessEqual(framer.buffered, MAX_BUFFERED)
        framer.feed(pending)
        report = framer.finish()
        self.assertEqual(report["status"], STATUS_OK)
        self.assertLess(worst, MAX_BUFFERED)

    def test_feed_after_finish_raises(self):
        framer = Framer()
        framer.finish()
        with self.assertRaises(RuntimeError):
            framer.feed(b"x")
        with self.assertRaises(RuntimeError):
            framer.finish()


if __name__ == "__main__":
    unittest.main()
