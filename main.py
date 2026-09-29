#!/usr/bin/env python3
"""命令行入口：读分块样本目录，输出帧清单与处置结论，或生成页面。

用法：
    python3 main.py <用例目录> [--frames <路径|->] [--report <路径|->] [--html <路径>]

退出码：0 正常跑完（含超长帧、不完整尾部等已定义处置）；1 用法、读写或样本错误。
"""

import argparse
import json
import os
import sys

from framing import Framer

CHUNK_LIMIT = 1048576  # 1 MiB，单块字节数上限


class SampleError(Exception):
    """分块样本与 manifest 对不上，或目录结构不合规。"""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"error: {message}", file=sys.stderr)
        sys.exit(1)


def load_manifest(case_dir):
    """读 manifest.tsv 并与 chunk-NNN.bin 交叉校验，返回 [(文件名, 字节数), ...]。"""
    try:
        names = os.listdir(case_dir)
    except OSError as exc:
        raise SampleError(f"读用例目录失败: {exc}") from exc
    chunks = sorted(n for n in names
                    if n.startswith("chunk-") and n.endswith(".bin")
                    and os.path.isfile(os.path.join(case_dir, n)))
    if not chunks:
        raise SampleError("目录里没有 chunk-NNN.bin")
    for index, name in enumerate(chunks):
        if name != f"chunk-{index:03d}.bin":
            raise SampleError(f"块文件名不从 000 连续递增: {name}")
    try:
        sizes = [os.path.getsize(os.path.join(case_dir, n)) for n in chunks]
    except OSError as exc:
        raise SampleError(f"读块文件失败: {exc}") from exc
    for name, size in zip(chunks, sizes):
        if size > CHUNK_LIMIT:
            raise SampleError(f"块超过 1 MiB 上限: {name} ({size} 字节)")

    manifest_path = os.path.join(case_dir, "manifest.tsv")
    try:
        with open(manifest_path, "r", encoding="utf-8", newline="") as fh:
            rows = [line.rstrip("\n").split("\t") for line in fh if line.strip()]
    except OSError as exc:
        raise SampleError(f"读 manifest.tsv 失败: {exc}") from exc
    if len(rows) != len(chunks):
        raise SampleError(
            f"manifest 行数 ({len(rows)}) 与块文件数 ({len(chunks)}) 不一致")
    cumulative = 0
    for name, size, row in zip(chunks, sizes, rows):
        if len(row) != 3:
            raise SampleError(f"manifest 行不是 3 列: {row!r}")
        mname, msize, moff = row
        cumulative += size
        if mname != name or msize != str(size) or moff != str(cumulative):
            raise SampleError(
                f"manifest 与实际不符: {row!r}，实际 {name} {size} {cumulative}")
    return list(zip(chunks, sizes))


def open_output(spec, default_stdout):
    """按路径或 '-' 打开文本输出流；返回 (流, 是否需要关闭)。"""
    if spec == "-":
        return sys.stdout, False
    try:
        return open(spec, "w", encoding="utf-8", newline="\n"), True
    except OSError as exc:
        print(f"error: 打不开输出文件 {spec}: {exc}", file=sys.stderr)
        sys.exit(1)


def write_report_line(fh, key, value):
    fh.write(f"{key} {'-' if value is None else value}\n")


def run(case_dir, frames_spec, report_spec, html_path):
    entries = load_manifest(case_dir)
    framer = Framer()
    frames_out, frames_close = open_output(frames_spec, True) if frames_spec else (None, False)
    report_out, report_close = open_output(report_spec, True) if report_spec else (None, False)
    collected = [] if html_path else None
    seq = 0
    try:
        for name, _size in entries:
            path = os.path.join(case_dir, name)
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError as exc:
                raise SampleError(f"读块文件失败: {path}: {exc}") from exc
            for frame in framer.feed(data):
                seq += 1
                if frames_out is not None:
                    frames_out.write(f"{seq} {frame.start} {frame.end} {frame.length}\n")
                if collected is not None:
                    collected.append(frame)
        report = framer.finish()
    finally:
        if frames_close:
            frames_out.close()
    if report_out is not None:
        try:
            write_report_line(report_out, "status", report["status"])
            write_report_line(report_out, "frames", report["frames"])
            write_report_line(report_out, "stream-bytes", report["stream-bytes"])
            write_report_line(report_out, "tail-start", report["tail-start"])
            write_report_line(report_out, "tail-bytes", report["tail-bytes"])
            write_report_line(report_out, "declared-length", report["declared-length"])
        finally:
            if report_close:
                report_out.close()
    if html_path:
        chunks = []
        offset = 0
        for name, size in entries:
            chunks.append({"name": name, "start": offset, "size": size})
            offset += size
        data = {
            "case": os.path.basename(os.path.normpath(case_dir)),
            "chunks": chunks,
            "frames": [[f.start, f.end, f.length] for f in collected],
            "tail": ([report["tail-start"], report["stream-bytes"]]
                     if report["tail-start"] is not None else None),
            "report": report,
        }
        try:
            with open(html_path, "w", encoding="utf-8", newline="\n") as fh:
                write_html(fh, data)
        except OSError as exc:
            print(f"error: 写页面失败 {html_path}: {exc}", file=sys.stderr)
            return 1
    return 0


HTML_HEAD = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>分帧排查</title>
<style>
  body { font: 13px/1.5 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         margin: 16px; color: #1c1f26; background: #fff; }
  h1 { font-size: 16px; margin: 0 0 8px; }
  #meta { margin-bottom: 8px; white-space: pre-wrap; }
  #meta b { display: inline-block; min-width: 7em; color: #555; font-weight: 600; }
  #legend span { display: inline-block; margin-right: 14px; }
  .sw { display: inline-block; width: 12px; height: 12px; border-radius: 2px;
        vertical-align: -2px; margin-right: 4px; }
  #wrap { overflow-x: auto; border: 1px solid #d8dbe0; border-radius: 4px; }
  canvas { display: block; }
  #tip { position: fixed; pointer-events: none; background: #1c1f26; color: #fff;
         padding: 4px 8px; border-radius: 4px; font: 12px/1.4 monospace;
         white-space: pre; z-index: 9; }
  [hidden] { display: none; }
</style>
</head>
<body>
<h1>字节流分帧排查</h1>
<div id="meta"></div>
<div id="legend">
  <span><span class="sw" style="background:#4c9be8"></span>帧（深色小段为 4 字节前缀）</span>
  <span><span class="sw" style="background:#e02424"></span>未归入任何帧的尾部区间</span>
  <span><span class="sw" style="background:#fff;border:1px solid #8a8f98"></span>块边界（每行一个块）</span>
</div>
<div id="wrap"><canvas id="cv"></canvas></div>
<div id="tip" hidden></div>
<script>
const DATA = """

HTML_TAIL = """;
const meta = document.getElementById("meta");
const rep = DATA.report;
const dash = v => v === null ? "-" : v;
meta.innerHTML =
  `<b>用例</b>${DATA.case}\\n` +
  `<b>status</b>${rep["status"]}\\n` +
  `<b>frames</b>${rep["frames"]}\\n` +
  `<b>stream-bytes</b>${rep["stream-bytes"]}\\n` +
  `<b>tail</b>${dash(rep["tail-start"])} .. ${rep["stream-bytes"]} (${rep["tail-bytes"]} 字节)\\n` +
  `<b>declared-length</b>${dash(rep["declared-length"])}`;

const cv = document.getElementById("cv");
const tip = document.getElementById("tip");
const LABEL_W = 250, PAD_R = 24, TOP = 10, ROW_H = 22, GAP = 10;
const maxChunk = Math.max(1, ...DATA.chunks.map(c => c.size));
const W = 1280, BAR_MAX = W - LABEL_W - PAD_R;
const H = TOP * 2 + DATA.chunks.length * (ROW_H + GAP);
const dpr = window.devicePixelRatio || 1;
cv.width = W * dpr; cv.height = H * dpr;
cv.style.width = W + "px"; cv.style.height = H + "px";
const ctx = cv.getContext("2d");
ctx.scale(dpr, dpr);

const rowY = j => TOP + j * (ROW_H + GAP);
const barW = c => Math.max(2, BAR_MAX * c.size / maxChunk);
const frameColor = i => `hsl(${(i * 47) % 360}, 62%, 55%)`;

ctx.font = "11px monospace";
DATA.chunks.forEach((c, j) => {
  const y = rowY(j), w = barW(c);
  ctx.fillStyle = "#f2f4f8";
  ctx.fillRect(LABEL_W, y, w, ROW_H);
  ctx.strokeStyle = "#8a8f98";
  ctx.strokeRect(LABEL_W + 0.5, y + 0.5, w - 1, ROW_H - 1);
  ctx.fillStyle = "#444";
  ctx.textAlign = "right";
  ctx.fillText(`${c.name} [${c.start}, ${c.start + c.size})`, LABEL_W - 8, y + ROW_H / 2 + 4);
});

function paintInterval(s, e, color, darkenPrefixFrom) {
  let j = 0;
  while (j < DATA.chunks.length && DATA.chunks[j].start + DATA.chunks[j].size <= s) j++;
  for (; j < DATA.chunks.length && DATA.chunks[j].start < e; j++) {
    const c = DATA.chunks[j];
    if (c.size === 0) continue;
    const cs = Math.max(s, c.start), ce = Math.min(e, c.start + c.size);
    const w = barW(c);
    const x0 = LABEL_W + w * (cs - c.start) / c.size;
    const x1 = LABEL_W + w * (ce - c.start) / c.size;
    ctx.fillStyle = color;
    ctx.fillRect(x0, rowY(j), Math.max(1, x1 - x0), ROW_H);
    if (darkenPrefixFrom !== null) {
      const ps = Math.max(darkenPrefixFrom, cs), pe = Math.min(darkenPrefixFrom + 4, ce);
      if (pe > ps) {
        ctx.fillStyle = "rgba(0,0,0,0.45)";
        ctx.fillRect(LABEL_W + w * (ps - c.start) / c.size, rowY(j),
                     Math.max(1, w * (pe - ps) / c.size), ROW_H);
      }
    }
  }
}

DATA.frames.forEach((f, i) => paintInterval(f[0], f[1], frameColor(i), f[0]));
if (DATA.tail) paintInterval(DATA.tail[0], DATA.tail[1], "#e02424", null);

function frameAt(off) {
  let lo = 0, hi = DATA.frames.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1, f = DATA.frames[mid];
    if (off < f[0]) hi = mid - 1;
    else if (off >= f[1]) lo = mid + 1;
    else { ans = mid; break; }
  }
  return ans;
}

cv.addEventListener("mousemove", ev => {
  const rect = cv.getBoundingClientRect();
  const mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
  const j = Math.floor((my - TOP) / (ROW_H + GAP));
  let text = null;
  if (j >= 0 && j < DATA.chunks.length && my - rowY(j) <= ROW_H) {
    const c = DATA.chunks[j], w = barW(c);
    if (c.size > 0 && mx >= LABEL_W && mx <= LABEL_W + w) {
      const off = Math.min(c.start + c.size - 1,
                           c.start + Math.floor((mx - LABEL_W) / w * c.size));
      const k = frameAt(off);
      if (k >= 0) {
        const f = DATA.frames[k];
        text = `帧 #${k + 1}  [${f[0]}, ${f[1]})  内容长度 ${f[2]}` +
               (off < f[0] + 4 ? "\\n（前缀区）" : "");
      } else if (DATA.tail && off >= DATA.tail[0] && off < DATA.tail[1]) {
        text = `未归入任何帧的尾部区间\\n[${DATA.tail[0]}, ${DATA.tail[1]})`;
      } else {
        text = `偏移 ${off}`;
      }
    }
  }
  if (text) {
    tip.textContent = text;
    tip.hidden = false;
    tip.style.left = (ev.clientX + 12) + "px";
    tip.style.top = (ev.clientY + 12) + "px";
  } else {
    tip.hidden = true;
  }
});
cv.addEventListener("mouseleave", () => { tip.hidden = true; });
</script>
</body>
</html>
"""


def write_html(fh, data):
    fh.write(HTML_HEAD)
    fh.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    fh.write(HTML_TAIL)


def main(argv=None):
    parser = Parser(description="长度前缀流式分帧：帧清单 / 处置结论 / 排查页面")
    parser.add_argument("case_dir", help="分块样本目录（含 chunk-NNN.bin 与 manifest.tsv）")
    parser.add_argument("--frames", default="-", metavar="路径|-",
                        help="帧清单输出位置，默认 -（stdout）")
    parser.add_argument("--report", default=None, metavar="路径|-",
                        help="处置结论输出位置，默认不输出")
    parser.add_argument("--html", default=None, metavar="路径",
                        help="生成单文件排查页面")
    args = parser.parse_args(argv)
    if args.frames == "-" and args.report == "-":
        parser.error("--frames 与 --report 不能同时为 -")
    try:
        return run(args.case_dir, args.frames, args.report, args.html)
    except SampleError as exc:
        print(f"error: 样本错误: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: 读写失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
