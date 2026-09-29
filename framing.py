"""长度前缀流式分帧引擎。

帧格式（写死，见 README 第 2 节）：
- 前缀 4 字节，无符号大端序，值为紧随其后的帧体字节数 N。
- 合法帧体长度 0 <= N <= 65536；N = 0 是合法零长帧。
- 帧区间 [start, end)：start 为前缀首字节在整条流里的下标，end = start + 4 + N。

用法：
    framer = Framer()
    for chunk in chunks:
        frames = framer.feed(chunk)   # 本次喂入新产出的帧，按流顺序
    report = framer.finish()          # 结束整条流，给出处置结论
"""

from typing import NamedTuple, Optional

PREFIX_SIZE = 4
MAX_BODY_LENGTH = 65536          # 64 KiB，声明长度大于它即超长帧
MAX_BUFFERED = 2 * 1024 * 1024   # 2 MiB，任意时刻未消费字节数上限


class Frame(NamedTuple):
    start: int    # 帧起始偏移（含前缀），流首字节为 0
    end: int      # 结束偏移，左闭右开，end = start + 4 + length
    length: int   # 帧体字节数 N


class Framer:
    """流式分帧器：字节一块块喂入，吐出完整帧。"""

    def __init__(self) -> None:
        self._buf = bytearray()   # 未消费缓冲（_pos 之前为已消费前缀）
        self._pos = 0             # 缓冲内已消费字节数，feed 末尾压实
        self._base = 0            # buf[_pos] 对应的流偏移
        self._stream_bytes = 0    # 累计喂入字节数（含停摆后丢弃的）
        self._frames = 0          # 已产出帧数
        self._stalled = False     # 终态标记：读到超长前缀即停摆
        self._stall_declared: Optional[int] = None
        self._stall_start: Optional[int] = None

    @property
    def buffered(self) -> int:
        """当前未消费字节数（验收口径：不得超过 MAX_BUFFERED）。"""
        return len(self._buf) - self._pos

    def feed(self, chunk: bytes) -> list:
        """喂入一块字节，返回本次新产出的帧（按流顺序）。

        空块等价于没喂。停摆后仍计数流长，但字节全部丢弃、不再解析。
        """
        if not chunk:
            return []
        self._stream_bytes += len(chunk)
        if self._stalled:
            return []
        self._buf += chunk
        buf = self._buf
        pos = self._pos
        base = self._base
        out = []
        while len(buf) - pos >= PREFIX_SIZE:
            declared = int.from_bytes(buf[pos:pos + PREFIX_SIZE], "big")
            if declared > MAX_BODY_LENGTH:
                # 停摆：读出前缀的那一刻立即停止，丢弃缓冲与后续字节
                self._stalled = True
                self._stall_declared = declared
                self._stall_start = base
                del buf[:]
                pos = 0
                base += 0  # base 之后不再使用
                break
            need = PREFIX_SIZE + declared
            if len(buf) - pos < need:
                break
            out.append(Frame(base, base + need, declared))
            pos += need
            base += need
            self._frames += 1
        self._pos = pos
        self._base = base
        # 压实：丢弃已消费前缀，未消费部分保持在缓冲头部
        if pos:
            del buf[:pos]
            self._pos = 0
        return out

    def finish(self) -> dict:
        """结束整条流，返回处置结论（字段与 README 第 4 节一致，不适用为 None）。"""
        if self._stalled:
            status = "oversize"
            tail_start = self._stall_start
            declared = self._stall_declared
        elif not self._buf:
            status = "ok"
            tail_start = None
            declared = None
        elif len(self._buf) < PREFIX_SIZE:
            status = "incomplete-prefix"
            tail_start = self._base
            declared = None
        else:
            status = "incomplete-body"
            tail_start = self._base
            declared = int.from_bytes(self._buf[:PREFIX_SIZE], "big")
        tail_bytes = self._stream_bytes - tail_start if tail_start is not None else 0
        return {
            "status": status,
            "frames": self._frames,
            "stream-bytes": self._stream_bytes,
            "tail-start": tail_start,
            "tail-bytes": tail_bytes,
            "declared-length": declared,
        }
