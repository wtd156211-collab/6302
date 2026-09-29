"""流式长度前缀分帧引擎。

帧格式（写死，见 README 第 2 节）：
- 前缀 4 字节，无符号大端序，取值是紧随其后的帧体字节数 N。
- 合法帧体长度 0 <= N <= 65536；N = 0 是合法帧（零长帧）。
- 帧区间为 [start, end)，start 是前缀首字节在整条流里的下标，
  end = start + 4 + N，内容长度为 N。

引擎只持有：未消费缓冲、已消费偏移、帧计数、终态标记。
"""

from __future__ import annotations

from typing import NamedTuple

PREFIX_LEN = 4
MAX_BODY_LEN = 65536  # 64 KiB
MAX_BUFFERED = 2 * 1024 * 1024  # 未消费字节上限 2 MiB（预算口径）

STATUS_OK = "ok"
STATUS_OVERSIZE = "oversize"
STATUS_INCOMPLETE_PREFIX = "incomplete-prefix"
STATUS_INCOMPLETE_BODY = "incomplete-body"


class Frame(NamedTuple):
    """一条完整帧在整条流里的位置。

    start: 帧起始偏移（含前缀），end: 结束偏移（左闭右开），length: 帧体长度。
    """

    start: int
    end: int
    length: int


class Framer:
    """把任意切分的字节块重组成帧。

    feed(chunk) 返回本次喂入后新产出的帧（按流顺序）；
    finish() 结束整条流并给出处置结论。
    """

    def __init__(self) -> None:
        self._buf = bytearray()  # 未消费缓冲（含已消费未压缩的前缀段）
        self._pos = 0  # 缓冲内已消费的字节数
        self._base = 0  # 缓冲首字节在整条流里的偏移
        self._fed = 0  # 累计喂入字节数（含停摆后丢弃的）
        self._frames = 0  # 已产出帧计数
        self._stalled = False  # 终态标记：读到超长前缀即停摆
        self._finished = False
        self._oversize_declared: int | None = None
        self._oversize_start: int | None = None

    @property
    def buffered(self) -> int:
        """当前未消费字节数。"""
        return len(self._buf) - self._pos

    def feed(self, chunk: bytes) -> list[Frame]:
        """喂入一块字节，返回新产出的帧。空块等价于没喂。"""
        if self._finished:
            raise RuntimeError("finish() 之后不能再 feed")
        self._fed += len(chunk)
        if self._stalled:
            # 已停摆：丢弃后续全部字节（仍计入 stream-bytes）。
            return []
        if not chunk:
            return []
        self._buf += chunk
        frames: list[Frame] = []
        buf = self._buf
        pos = self._pos
        base = self._base
        while True:
            avail = len(buf) - pos
            if avail < PREFIX_LEN:
                break  # 等前缀
            n = int.from_bytes(buf[pos : pos + PREFIX_LEN], "big")
            if n > MAX_BODY_LEN:
                # 停摆：读出前缀的那一刻立即停止，丢弃缓冲与后续字节。
                self._stalled = True
                self._oversize_declared = n
                self._oversize_start = base + pos
                self._buf = bytearray()
                self._pos = 0
                self._base = self._fed
                self._frames += len(frames)
                return frames
            if avail < PREFIX_LEN + n:
                break  # 等帧体
            start = base + pos
            frames.append(Frame(start, start + PREFIX_LEN + n, n))
            pos += PREFIX_LEN + n
        self._frames += len(frames)
        self._pos = pos
        # 压缩已消费前缀，保持缓冲有界（远小于 2 MiB 上限）。
        if pos >= 65536 or (pos > 0 and pos * 2 >= len(buf)):
            del buf[:pos]
            self._base = base + pos
            self._pos = 0
        return frames

    def finish(self) -> dict:
        """结束整条流，返回处置结论。字段不适用时为 None。"""
        if self._finished:
            raise RuntimeError("finish() 只能调用一次")
        self._finished = True
        tail_start: int | None = None
        declared: int | None = None
        if self._stalled:
            status = STATUS_OVERSIZE
            tail_start = self._oversize_start
            declared = self._oversize_declared
        else:
            avail = len(self._buf) - self._pos
            if avail == 0:
                status = STATUS_OK
            elif avail < PREFIX_LEN:
                status = STATUS_INCOMPLETE_PREFIX
                tail_start = self._base + self._pos
            else:
                status = STATUS_INCOMPLETE_BODY
                tail_start = self._base + self._pos
                declared = int.from_bytes(
                    self._buf[self._pos : self._pos + PREFIX_LEN], "big"
                )
        tail_bytes = self._fed - tail_start if tail_start is not None else 0
        return {
            "status": status,
            "frames": self._frames,
            "stream_bytes": self._fed,
            "tail_start": tail_start,
            "tail_bytes": tail_bytes,
            "declared_length": declared,
        }
