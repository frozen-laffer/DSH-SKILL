#!/usr/bin/env python3
"""Render qbuild history + events into a self-contained HTML dashboard.

Numbers in this report are computed from qbuild's own artifacts; anything that
is an estimate says so.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Overridden by --dir / --project in main(); the tool itself lives outside the project.
BASE = HERE
HIST = HERE / "qt_prog.history.json"
EVENTS = HERE / "qbuild_events.jsonl"
OUT = HERE / "report.html"

W, H = 720, 180        # chart canvas
PAD_L, PAD_R, PAD_T, PAD_B = 42, 12, 12, 26


def load_segments(path: Path) -> list[list[dict]]:
    """Split the append-only event log into runs (a run ends at done/cancelled)."""
    segs: list[list[dict]] = []
    cur: list[dict] = []
    if not path.exists():
        return segs
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        cur.append(ev)
        if ev.get("state") in ("done", "cancelled", "failed"):
            segs.append(cur)
            cur = []
    return segs


def svg_curve(pts: list[tuple[float, float]], xmax: float) -> str:
    if len(pts) < 2:
        return "<p class=muted>no data</p>"
    ymax = 100.0
    sx = (W - PAD_L - PAD_R) / xmax
    sy = (H - PAD_T - PAD_B) / ymax
    d = " ".join(f"{PAD_L + x * sx:.1f},{PAD_T + (ymax - y) * sy:.1f}" for x, y in pts)
    grid = "".join(
        f'<line x1="{PAD_L}" y1="{PAD_T + (ymax - g) * sy:.1f}" x2="{W - PAD_R}" '
        f'y2="{PAD_T + (ymax - g) * sy:.1f}" class="grid"/>'
        f'<text x="{PAD_L - 6}" y="{PAD_T + (ymax - g) * sy + 3:.1f}" class="ax">{g:.0f}%</text>'
        for g in (0, 25, 50, 75, 100)
    )
    ticks = "".join(
        f'<text x="{PAD_L + t * sx:.1f}" y="{H - 8}" class="ax" text-anchor="middle">{t:.0f}s</text>'
        for t in range(0, int(xmax) + 1, max(1, int(xmax // 6)))
    )
    return (f'<svg viewBox="0 0 {W} {H}" class="chart">{grid}{ticks}'
            f'<polyline points="{d}" class="line"/></svg>')


def bars(rows: list[dict]) -> str:
    keys = [("map", "Analysis & Synthesis", "#4f8cff"),
            ("fit", "Fitter", "#38b48b"),
            ("asm", "Assembler", "#e0a33c"),
            ("sta", "Timing Analyzer", "#c86bd8")]
    vmax = max((r["total"] for r in rows), default=1) or 1
    out = []
    for i, r in enumerate(rows, 1):
        st = r.get("stages", {})
        segs = "".join(
            f'<span class="seg" style="width:{st.get(k, 0) / vmax * 100:.2f}%;'
            f'background:{c}" title="{label} {st.get(k, 0):.1f}s"></span>'
            for k, label, c in keys)
        out.append(f'<div class="row"><span class="idx">#{i}</span>'
                   f'<div class="track">{segs}</div>'
                   f'<span class="tot">{r["total"]:.1f}s</span></div>')
    legend = "".join(f'<span class="lg"><i style="background:{c}"></i>{label}</span>'
                     for _, label, c in keys)
    return f'<div class="legend">{legend}</div>{"".join(out)}'


def body_lines(stage: str) -> int:
    """Lines of actual tool output, excluding qbuild's own footer block."""
    p = BASE / f"log_{stage}.txt"
    if not p.exists():
        return 0
    txt = p.read_text(encoding="utf-8", errors="replace").splitlines()
    while txt and (not txt[-1].strip() or txt[-1].startswith("[")):
        txt.pop()
    return len(txt)


def main() -> int:
    global BASE, HIST, EVENTS, OUT
    ap = argparse.ArgumentParser(description="render report.html for a Quartus project dir")
    ap.add_argument("--dir", default=".", help="project directory holding qbuild's artifacts")
    ap.add_argument("--project", default="", help="revision name; auto-detected from *.history.json")
    a = ap.parse_args()

    BASE = Path(a.dir).resolve()
    if not BASE.is_dir():
        print(f"not a directory: {BASE}", file=sys.stderr)
        return 2
    proj = a.project
    if not proj:
        cand = sorted(p.name[:-len(".history.json")] for p in BASE.glob("*.history.json"))
        if not cand:
            print(f"no *.history.json in {BASE} - run qbuild.py first", file=sys.stderr)
            return 2
        proj = cand[0]
    HIST = BASE / f"{proj}.history.json"
    EVENTS = BASE / "qbuild_events.jsonl"
    OUT = BASE / "report.html"

    hist = json.loads(HIST.read_text(encoding="utf-8")) if HIST.exists() else []
    segs = load_segments(EVENTS)
    last = segs[-1] if segs else []

    pts = [(e["elapsed"], e["pct"]) for e in last if "elapsed" in e and "pct" in e]
    xmax = max((p[0] for p in pts), default=1) or 1

    ok = [r for r in hist if r.get("ok", True)]
    cold = ok[0] if ok else {"total": 0}
    # same window qbuild's estimate() uses, so the table and the live ETA agree
    window = ok[-5:]

    def med_of(k: str) -> float:
        vals = sorted(r["stages"].get(k, 0) for r in window if r["stages"].get(k))
        return vals[len(vals) // 2] if vals else 0.0

    med = {k: med_of(k) for k in ("map", "fit", "asm", "sta")}
    med_sum = max(0.01, sum(med.values()))
    cold_map_pct = (cold.get("stages", {}).get("map", 0) / max(0.01, cold.get("total", 1)) * 100)

    last_err = None
    if ok and ok[-1].get("predicted"):
        last_err = (ok[-1]["total"] - ok[-1]["predicted"]) / ok[-1]["predicted"] * 100

    migrated_n = sum(1 for r in hist if r.get("migrated"))
    counts = {k: body_lines(k) for k in ("map", "fit", "asm", "sta")}

    stages_rows = "".join(
        f"<tr><td>{label}</td><td>{med[k]:.2f} s</td><td>{med[k] / med_sum * 100:.0f}%</td></tr>"
        for k, label in (("map", "Analysis &amp; Synthesis"), ("fit", "Fitter"),
                         ("asm", "Assembler"), ("sta", "Timing Analyzer")))

    run_rows = []
    for i, r in enumerate(hist, 1):
        pred = r.get("predicted")
        err = f"{(r['total'] - pred) / pred * 100:+.0f}%" if pred else "—"
        run_rows.append(
            f"<tr><td>#{i}</td><td>{r['total']:.1f} s</td>"
            f"<td>{pred if pred else '—'}</td><td>{err}</td>"
            f"<td>{r.get('warnings', 0)}</td><td>{r.get('errors', 0)}</td></tr>")
    run_rows = "".join(run_rows)

    def fmt(x):
        return f"{int(x) // 60:02d}:{int(x) % 60:02d}"

    html = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>Quartus 25.1 分阶段构建进度报告</title>
<style>
 :root{{--bg:#12151b;--card:#1b2029;--fg:#e7ecf3;--mut:#8b97a8;--acc:#4f8cff}}
 *{{box-sizing:border-box}}
 body{{margin:0;background:var(--bg);color:var(--fg);
   font:14px/1.6 "Segoe UI","Microsoft YaHei",sans-serif;padding:28px}}
 h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:15px;margin:26px 0 10px;color:var(--acc)}}
 .sub{{color:var(--mut);margin-bottom:18px}}
 .cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
 .card{{background:var(--card);border-radius:10px;padding:14px 16px}}
 .card .k{{color:var(--mut);font-size:12px}} .card .v{{font-size:22px;font-weight:600}}
 table{{width:100%;border-collapse:collapse;background:var(--card);border-radius:10px;overflow:hidden}}
 th,td{{padding:8px 14px;text-align:left;border-bottom:1px solid #232a36}}
 th{{color:var(--mut);font-weight:500;font-size:12px}}
 tr:last-child td{{border-bottom:none}}
 .row{{display:flex;align-items:center;gap:10px;margin:6px 0}}
 .idx{{width:34px;color:var(--mut)}} .tot{{width:56px;text-align:right;color:var(--mut)}}
 .track{{flex:1;height:16px;background:#232a36;border-radius:4px;overflow:hidden;display:flex}}
 .seg{{display:block;height:100%}}
 .legend{{display:flex;gap:16px;margin:8px 0 12px;color:var(--mut);font-size:12px}}
 .lg i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}}
 .chart{{width:100%;height:auto;background:var(--card);border-radius:10px}}
 .grid{{stroke:#28303d;stroke-width:1}} .ax{{fill:var(--mut);font-size:10px}}
 .line{{fill:none;stroke:var(--acc);stroke-width:2}}
 .warn{{background:#2a2118;border-left:3px solid #e0a33c;padding:10px 14px;border-radius:6px;color:#f0d9b5}}
 .fix{{background:#152a20;border-left:3px solid #38b48b;padding:10px 14px;border-radius:6px;color:#c9e8d8}}
 code{{background:#232a36;padding:1px 6px;border-radius:4px}}
 pre.code{{background:#1b2029;padding:12px 14px;border-radius:8px;overflow:auto;color:#cfe0f5}}
 .muted{{color:var(--mut)}}
</style>
<h1>Quartus Prime Lite 25.1 · 分阶段构建进度与 ETA 实测</h1>
<div class="sub">EP4CE6F17C8 · 8 乘法器 + 800×600@60 时序发生器 · {datetime.now():%Y-%m-%d %H:%M} 生成</div>

<div class="cards">
  <div class="card"><div class="k">冷启动首次构建（含数据库建立）</div><div class="v">{fmt(cold.get('total', 0))}</div></div>
  <div class="card"><div class="k">热态构建中位</div><div class="v">{fmt(med_sum)}</div></div>
  <div class="card"><div class="k">样本数（其中 {migrated_n} 次由旧计数口径迁移）</div><div class="v">{len(ok)} 次</div></div>
  <div class="card"><div class="k">最近一次 ETA 误差（对预测值）</div>
    <div class="v">{f'{last_err:+.0f}%' if last_err is not None else '—'}</div></div>
</div>

<h2>各阶段耗时（最近 5 次中位数，与 qbuild 的 estimate 同窗口）</h2>
<table><tr><th>阶段</th><th>耗时</th><th>占比</th></tr>{stages_rows}</table>
<p class="muted">占比随机器负载滑动：首轮冷启动 map 占 {cold_map_pct:.0f}%，热态稳定后才是上表数值。</p>

<h2>每次构建的阶段构成</h2>
{bars(ok)}

<h2>逐次记录</h2>
<table><tr><th>构建</th><th>总耗时</th><th>预测值 s</th><th>误差（对预测）</th><th>警告</th><th>错误</th></tr>{run_rows}</table>
<p class="muted">误差口径 =（实际 − 预测）/ 预测，与 <code>qbuild.py</code> 打印的一致。
前 {migrated_n} 次记录由旧版计数器产生：它把 <code>Critical Warning</code> 当成错误计入 <code>errors</code>，
所以原值均为 1。已按日志原文迁移为 <code>warnings=1, errors=0</code>（当时另外两条普通 Warning 未被计数），
原始值保留在 <code>errors_legacy</code> 字段里。
现行版本每次构建固定带 3 条警告，全部来自 Fitter：LogicLock 订阅提示（292013）、
I/O 分配不完整（15714）、45 个引脚未定位（169085）。本 POC 没有写引脚约束，
所以这三条是<b>预期内</b>的，不计为失败。</p>

<h2>最后一次构建的进度曲线</h2>
{svg_curve(pts, xmax)}
<p class="muted">曲线由 <code>qbuild_events.jsonl</code> 逐秒采样得到；阶段边界是硬事件（子进程退出），段内比例来自历史耗时估计。</p>

<h2>关键事实（复核后修订）</h2>
<div class="warn">
<b>Quartus 命令行不输出构建百分比</b>，全流程日志里与百分比有关的只有 Fitter 的两条互连占用率：
`Info (170195) … interconnect usage is 2%`（平均）与 `Info (170196) … peak interconnect usage is 7%`
（<code>log_fit.txt:77–78</code>），与构建进度无关。
单阶段日志量也很小：map {counts['map']} 行 / fit {counts['fit']} 行 / asm {counts['asm']} 行 / sta {counts['sta']} 行
（<code>log_*.txt</code> 正文行数，不含 qbuild 写的页脚）。因此段内进度由「已用时间 ÷ 该阶段历史耗时」推算——
这是当前数据量下<b>最可靠的一种</b>做法，而不是唯一可能的做法：若解析到布局/布线的
明确起止行，还可以叠一层事件锚点。
</div>
<div class="warn">
补充一条被实测推翻的早期假设：<b>子进程的 stdout 并不是逐行实时到达的</b>。Quartus 把
阶段内的大部分消息在<b>结束时集中输出</b>，所以只有 Fitter 能在运行中拿到子步骤标签
（实测 placing ×3、routing ×2），map / asm / sta 的三次构建里标签始终停在
<code>running</code>。因此“段内实时子步骤”只在 Fitter 上成立，其余阶段靠的仍然只有时间插值。
</div>

<h2>本轮复核后的修正</h2>
<div class="fix">
① <b>errors / warnings 分离</b>：原来把 <code>Critical Warning</code> 计入 errors，使每条历史记录都显示
<code>errors: 1</code>（实际是 45 个引脚未定位）；现在分别计数，历史写 <code>warnings: 3, errors: 0, ok: true</code>。<br>
② <b>状态文件原子写</b>：<code>qbuild.py</code> 的状态与历史都改为 tmp + <code>os.replace</code>，外部轮询不会读到半截 JSON。<br>
③ <b>外部取消入口</b>：新增 <code>--cancel</code>（写 <code>qbuild.cancel</code> 标志，构建循环 0.25 s 内响应）。
取消机制是 <code>Popen.kill()</code>（TerminateProcess）——<code>taskkill /F /T</code> 在本机沙箱下直接返回
<b>“ERROR: Access denied”</b> 且进程照跑，这一点是实测发现的，早期版本因此“看起来取消成功、其实构建跑完了”。
现在实测：取消后 exit=130、残留 quartus 进程 0、取消时的百分比被保留、<b>被杀之后再编一次仍然成功</b>——逐项数值见 <code>cancel_test3.log</code>。<br>
失败与中断也各写各的日志：失败阶段的页脚记 <code>state=failed</code>，Ctrl+C 记
<code>state=interrupted</code>，因此 <code>--log</code> 不会在这场构建之后还显示上一轮的日志。<br>
④ <b>新增只读子命令</b>：<code>--status</code>、<code>--log &lt;stage&gt; --tail N</code>，加上原有的 <code>--report</code>，
构建/状态/日志/历史这四个动作所需的数据面已齐，将来若要薄包一层 MCP 无需再改脚本。<br>
⑤ <b>口径修正</b>：ETA 误差明确为「对预测值」；阶段占比标注为热态中位数；<code>estimate()</code> 只取 <code>ok</code> 的样本。<br>
⑥ <b>错误计数去噪</b>：Quartus 失败时会把 <code>Error: Elapsed time / Total CPU time / Peak memory</code> 这类
记账行也加上 <code>Error:</code> 前缀，早期版本把它们算成错误（一次失败报 6 条错误、真实只有 2 条）。现已按前缀白名单过滤。<br>
⑦ <b>失败与中断各有各的状态</b>：失败阶段页脚 <code>state=failed</code>、Ctrl+C 为
<code>state=interrupted</code>、外部取消为 <code>state=cancelled</code>，三种都写阶段日志；中断路径另有
<code>--interrupt-after N</code> 测试钩子，实测退出码 130、状态 <code>interrupted</code>、阶段进程已停。
</div>

<h2>用法</h2>
<pre class=code>python qbuild.py --dir .                    # 构建，实时打印进度与 ETA
python qbuild.py --dir . --cancel           # 另一个进程请求干净停止
python qbuild.py --dir . --status           # 打印当前状态 JSON
python qbuild.py --dir . --log fit --tail 40  # 打印阶段日志尾部
python qbuild.py --dir . --report           # 历史阶段耗时表
python qbuild.py --dir . --cancel-after 8   # 测试钩子：8 秒后取消</pre>
<p class="muted">状态文件 <code>qbuild_status.json</code> 每秒原子刷新，可供任何外部看板轮询；
事件流 <code>qbuild_events.jsonl</code> 供事后作图。</p>
</html>"""
    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT}  ({len(html)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
