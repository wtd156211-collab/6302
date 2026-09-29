"""命令行入口：读分块样本目录，输出帧清单与处置结论，或生成页面。

用法：
    python3 main.py <用例目录> [--frames <路径|->] [--report <路径|->] [--html <路径>]

--frames 默认 -（stdout）；--report 默认不输出；- 表示 stdout，
两者同时为 - 属于用法错误。退出码 0 表示正常跑完（含超长帧、
不完整尾部这类已定义的处置），1 表示用法、读写或样本错误。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

from framing import Frame, Framer

MAX_CHUNK_BYTES = 1048576  # 单块上限 1 MiB
_CHUNK_NAME = re.compile(r"chunk-(\d{3})\.bin\Z")

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>帧流视图 - __CASE__</title>
<style>
  body { font: 14px/1.5 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         margin: 0; padding: 16px 24px 32px; color: #222; background: #fafafa; }
  h1 { font-size: 18px; margin: 8px 0 4px; }
  #summary { color: #555; margin-bottom: 12px; }
  #summary b { color: #111; }
  .legend { margin: 8px 0 12px; font-size: 13px; color: #444; }
  .legend span { display: inline-block; margin-right: 16px; }
  .sw { display: inline-block; width: 12px; height: 12px; border-radius: 2px;
        vertical-align: -1px; margin-right: 4px; }
  #wrap { position: relative; border: 1px solid #ccc; background: #fff; }
  canvas { display: block; }
  #tip { position: absolute; display: none; pointer-events: none; background: #111;
         color: #fff; font-size: 12px; padding: 4px 8px; border-radius: 4px;
         white-space: nowrap; z-index: 2; }
  #scale { display: flex; justify-content: space-between; font-size: 12px;
           color: #777; margin-top: 4px; }
</style>
</head>
<body>
<h1>帧流视图：<span id="case"></span></h1>
<div id="summary"></div>
<div class="legend">
  <span><span class="sw" style="background:#4c9aff"></span>帧（色块，相邻帧交替深浅）</span>
  <span><span class="sw" style="background:#e5484d"></span>未归入任何帧的区间（tail）</span>
  <span><span class="sw" style="background:#333"></span>块边界（竖线）</span>
</div>
<div id="wrap"><canvas id="bar"></canvas><div id="tip"></div></div>
<div id="scale"><span id="lo"></span><span id="hi"></span></div>
<script>
"use strict";
const DATA = __DATA__;
document.getElementById("case").textContent = DATA.caseName;
const r = DATA.report;
const tailDesc = r.tail_start === null ? "无" :
  "[" + r.tail_start + ", " + r.stream_bytes + ") 共 " + r.tail_bytes + " 字节";
document.getElementById("summary").innerHTML =
  "状态 <b>" + r.status + "</b> ｜ 帧数 <b>" + r.frames + "</b> ｜ 流长 <b>" +
  r.stream_bytes + "</b> 字节 ｜ 块数 <b>" + DATA.chunks.length + "</b> ｜ 尾部区间 <b>" +
  tailDesc + "</b>" +
  (r.declared_length === null ? "" : " ｜ 声明长度 <b>" + r.declared_length + "</b>");
document.getElementById("lo").textContent = "偏移 0";
document.getElementById("hi").textContent = "偏移 " + r.stream_bytes;

const wrap = document.getElementById("wrap");
const canvas = document.getElementById("bar");
const tip = document.getElementById("tip");
const W = Math.max(640, Math.min(1600, window.innerWidth - 48));
const ROW_H = 26, ROW_GAP = 6, PAD = 4;
const total = Math.max(1, r.stream_bytes);
const rows = Math.max(1, Math.min(64, Math.ceil(total / 65536)));
const rowBytes = total / rows;
const H = PAD * 2 + rows * ROW_H + (rows - 1) * ROW_GAP;
const dpr = window.devicePixelRatio || 1;
canvas.width = W * dpr; canvas.height = H * dpr;
canvas.style.width = W + "px"; canvas.style.height = H + "px";
const ctx = canvas.getContext("2d");
ctx.scale(dpr, dpr);

function xOf(off, row) { return (off - row * rowBytes) / rowBytes * W; }

// 背景行
for (let i = 0; i < rows; i++) {
  ctx.fillStyle = i % 2 ? "#f3f4f6" : "#eceef1";
  ctx.fillRect(0, PAD + i * (ROW_H + ROW_GAP), W, ROW_H);
}
// 帧色块
const COLORS = ["#4c9aff", "#2f7de1"];
DATA.frames.forEach((f, idx) => {
  ctx.fillStyle = COLORS[idx % 2];
  const first = Math.floor(f[0] / rowBytes), last = Math.floor((f[1] - 1) / rowBytes);
  for (let row = first; row <= last && row < rows; row++) {
    const x0 = Math.max(0, xOf(f[0], row));
    const x1 = Math.min(W, xOf(f[1], row));
    ctx.fillRect(x0, PAD + row * (ROW_H + ROW_GAP), Math.max(x1 - x0, 0.5), ROW_H);
  }
});
// 尾部标红：恰好是 tail-* 描述的那段
if (r.tail_start !== null && r.tail_bytes > 0) {
  ctx.fillStyle = "#e5484d";
  const end = r.tail_start + r.tail_bytes;
  const first = Math.floor(r.tail_start / rowBytes), last = Math.floor((end - 1) / rowBytes);
  for (let row = first; row <= last && row < rows; row++) {
    const x0 = Math.max(0, xOf(r.tail_start, row));
    const x1 = Math.min(W, xOf(end, row));
    ctx.fillRect(x0, PAD + row * (ROW_H + ROW_GAP), Math.max(x1 - x0, 0.5), ROW_H);
  }
}
// 块边界竖线
ctx.strokeStyle = "rgba(20,20,20,0.55)";
ctx.lineWidth = 1;
DATA.chunks.forEach(c => {
  const row = Math.min(rows - 1, Math.floor(c / rowBytes));
  const x = xOf(c, row);
  ctx.beginPath();
  ctx.moveTo(x, PAD + row * (ROW_H + ROW_GAP) - 2);
  ctx.lineTo(x, PAD + row * (ROW_H + ROW_GAP) + ROW_H + 2);
  ctx.stroke();
});

// 悬停提示：按偏移二分查帧
const starts = DATA.frames.map(f => f[0]);
function frameAt(off) {
  let lo = 0, hi = starts.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (starts[mid] <= off) { ans = mid; lo = mid + 1; } else hi = mid - 1;
  }
  if (ans >= 0 && off < DATA.frames[ans][1]) return ans;
  return -1;
}
canvas.addEventListener("mousemove", ev => {
  const rect = canvas.getBoundingClientRect();
  const x = ev.clientX - rect.left, y = ev.clientY - rect.top - PAD;
  const row = Math.floor(y / (ROW_H + ROW_GAP));
  if (row < 0 || row >= rows || y % (ROW_H + ROW_GAP) > ROW_H) { tip.style.display = "none"; return; }
  const off = Math.floor(row * rowBytes + x / W * rowBytes);
  let text;
  const idx = frameAt(off);
  if (idx >= 0) {
    const f = DATA.frames[idx];
    text = "帧 #" + (idx + 1) + "  [" + f[0] + ", " + f[1] + ")  长度 " + f[2];
  } else if (r.tail_start !== null && off >= r.tail_start && off < r.tail_start + r.tail_bytes) {
    text = "未归入帧的区间  偏移 " + off;
  } else {
    text = "偏移 " + off;
  }
  tip.textContent = text;
  tip.style.display = "block";
  tip.style.left = Math.min(x + 12, W - 160) + "px";
  tip.style.top = (ev.clientY - rect.top + 14) + "px";
});
canvas.addEventListener("mouseleave", () => { tip.style.display = "none"; });
</script>
</body>
</html>
"""


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # 用法错误退出码统一为 1
        self.exit(1, f"用法错误: {message}\n{self.format_usage()}")


class SampleError(Exception):
    """分块样本本身不合法（命名、大小、manifest 对不上）。"""


def load_chunks(case_dir: str) -> list[tuple[str, int]]:
    """按文件名升序返回 [(路径, 累计结束偏移)]，并与 manifest.tsv 交叉校验。"""
    try:
        names = os.listdir(case_dir)
    except OSError as exc:
        raise SampleError(f"读用例目录失败: {exc}") from exc
    chunks = sorted(n for n in names if _CHUNK_NAME.fullmatch(n))
    if not chunks:
        raise SampleError(f"{case_dir}: 没有 chunk-NNN.bin 文件")
    for i, name in enumerate(chunks):
        if name != f"chunk-{i:03d}.bin":
            raise SampleError(f"块文件名不连续: 期望 chunk-{i:03d}.bin, 实际 {name}")
    entries: list[tuple[str, int]] = []
    cumulative = 0
    for name in chunks:
        path = os.path.join(case_dir, name)
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            raise SampleError(f"读块文件失败: {exc}") from exc
        if size > MAX_CHUNK_BYTES:
            raise SampleError(f"{name}: {size} 字节, 超过单块上限 {MAX_CHUNK_BYTES}")
        cumulative += size
        entries.append((path, cumulative))
    manifest_path = os.path.join(case_dir, "manifest.tsv")
    try:
        with open(manifest_path, "r", encoding="utf-8", newline="") as fh:
            lines = [line.rstrip("\n") for line in fh if line.strip()]
    except OSError as exc:
        raise SampleError(f"读 manifest 失败: {exc}") from exc
    if len(lines) != len(entries):
        raise SampleError(
            f"manifest 行数 ({len(lines)}) 与块数 ({len(entries)}) 不一致"
        )
    for line, (path, cum) in zip(lines, entries):
        parts = line.split("\t")
        name = os.path.basename(path)
        size = os.path.getsize(path)
        if len(parts) != 3:
            raise SampleError(f"manifest 行格式错误: {line!r}")
        m_name, m_size, m_cum = parts
        if m_name != name or m_size != str(size) or m_cum != str(cum):
            raise SampleError(
                f"manifest 与实际不符: {line!r} != {name}\\t{size}\\t{cum}"
            )
    return entries


def _open_out(spec: str):
    """按输出目标返回 (文本流, 是否需要在结束时关闭)。"""
    if spec == "-":
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        return sys.stdout, False
    return open(spec, "w", encoding="utf-8", newline="\n"), True


def render_html(case_name: str, chunk_ends: list[int], frames: list[Frame],
                report: dict) -> str:
    data = {
        "caseName": case_name,
        "chunks": chunk_ends,
        "frames": [[f.start, f.end, f.length] for f in frames],
        "report": report,
    }
    payload = json.dumps(data, separators=(",", ":"))
    return _HTML_TEMPLATE.replace("__DATA__", payload).replace("__CASE__", case_name)


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(
        prog="main.py",
        description="长度前缀分帧：读分块样本, 输出帧清单/处置结论/页面",
    )
    parser.add_argument("case_dir", help="分块样本目录")
    parser.add_argument("--frames", default="-", help="帧清单输出路径, - 为 stdout (默认)")
    parser.add_argument("--report", default=None, help="处置结论输出路径, - 为 stdout")
    parser.add_argument("--html", default=None, help="生成单文件页面到该路径")
    args = parser.parse_args(argv)

    if args.frames == "-" and args.report == "-":
        print("用法错误: --frames 与 --report 不能同时为 stdout", file=sys.stderr)
        return 1

    try:
        entries = load_chunks(args.case_dir)
    except SampleError as exc:
        print(f"样本错误: {exc}", file=sys.stderr)
        return 1

    frames_out = report_out = None
    frames_close = report_close = False
    try:
        frames_out, frames_close = _open_out(args.frames)
        if args.report is not None:
            report_out, report_close = _open_out(args.report)

        framer = Framer()
        collected: list[Frame] | None = [] if args.html else None
        chunk_ends: list[int] = []
        seq = 0
        for path, cum in entries:
            try:
                with open(path, "rb") as fh:
                    chunk = fh.read()
            except OSError as exc:
                print(f"读块文件失败: {exc}", file=sys.stderr)
                return 1
            chunk_ends.append(cum)
            for frame in framer.feed(chunk):
                seq += 1
                frames_out.write(
                    f"{seq} {frame.start} {frame.end} {frame.length}\n"
                )
                if collected is not None:
                    collected.append(frame)
        report = framer.finish()

        if report_out is not None:
            dash = "-"
            tail_start = report["tail_start"]
            declared = report["declared_length"]
            report_out.write(f"status {report['status']}\n")
            report_out.write(f"frames {report['frames']}\n")
            report_out.write(f"stream-bytes {report['stream_bytes']}\n")
            report_out.write(
                f"tail-start {tail_start if tail_start is not None else dash}\n"
            )
            report_out.write(f"tail-bytes {report['tail_bytes']}\n")
            report_out.write(
                f"declared-length {declared if declared is not None else dash}\n"
            )

        if args.html:
            case_name = os.path.basename(os.path.normpath(args.case_dir))
            html = render_html(case_name, chunk_ends, collected or [], report)
            try:
                with open(args.html, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(html)
            except OSError as exc:
                print(f"写页面失败: {exc}", file=sys.stderr)
                return 1
    except OSError as exc:
        print(f"读写错误: {exc}", file=sys.stderr)
        return 1
    finally:
        if frames_close:
            frames_out.close()
        if report_close:
            report_out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
