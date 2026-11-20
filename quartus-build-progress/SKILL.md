---
name: quartus-build-progress
description: 给任意 Quartus（Cyclone IV / EP4CE6 / EP4CE10 等）工程提供分阶段构建进度、ETA 预测、可取消构建与构建历史报告，不需要往工程目录搬脚本。当任务涉及 Quartus 编译很慢想知道进度、想预估还要多久、想中止一次编译、想比较几次编译的耗时构成，或需要一份构建耗时报告时使用。内含本机已验证的 Quartus 路径、Quartus CLI 不输出百分比的实测事实，以及 taskkill 在沙箱下失效时必须用 TerminateProcess 的修法。
---

# Quartus 分阶段构建进度

`qbuild` 把 `quartus_map → quartus_fit → quartus_asm → quartus_sta` 拆成四个独立子进程依次跑，
用「历史同阶段耗时」做时间插值给出百分比与 ETA，并把每次构建记进 `<revision>.history.json`。

**这个技能自带脚本，永远不要把它拷进工程目录。** 脚本在技能目录里，用 `--dir` 指向工程即可，
状态文件全部写在工程目录里，工具目录只有代码。

## 一、路径

| 东西 | 绝对路径 |
|---|---|
| 构建器 | `F:\DSH\DSH-Home\skills\quartus-build-progress\scripts\qbuild.py` |
| 报告生成 | `F:\DSH\DSH-Home\skills\quartus-build-progress\scripts\make_report.py` |
| Quartus 可执行文件 | `F:\altera_lite\25.1std\quartus\bin64`（`quartus_map.exe` / `fit` / `asm` / `sta`） |

- Quartus **不在 PATH 里**，脚本里写死 bin64 绝对路径；换机器用环境变量覆盖：
  `$env:QUARTUS_BIN = "D:\altera_lite\25.1std\quartus\bin64"`。
- 这套脚本只依赖 Python 3 标准库，不需要 pip 安装任何东西。

## 二、四个常用动作

```powershell
$Q = "F:\DSH\DSH-Home\skills\quartus-build-progress\scripts"

# 1) 构建（前台看进度；--dir 指向含 .qpf 的工程目录）
python "$Q\qbuild.py" --dir F:\path\to\proj

# 2) 另一个终端里请求干净停止
python "$Q\qbuild.py" --dir F:\path\to\proj --cancel

# 3) 看状态 / 看某阶段日志尾部
python "$Q\qbuild.py" --dir F:\path\to\proj --status
python "$Q\qbuild.py" --dir F:\path\to\proj --log fit --tail 40

# 4) 出报告（写到该工程目录的 report.html）
python "$Q\make_report.py" --dir F:\path\to\proj
```

- 工程名/版本名**自动探测**（目录里第一个 `*.qpf`，revision 默认等于工程名）；不常规时显式传
  `--project <name>` / `--revision <name>`。
- 长构建请用后台任务跑，再用 `--status` 轮询，而不是前台阻塞。
- **后台任务的退出码不要拿来判结果**：见 §四 第 7 条，取消/中断在后台包装器里会显示成 1。

## 三、退出码与三条结束路径

| 结束方式 | 退出码 | 状态文件 `state` | 阶段日志页脚 |
|---|---|---|---|
| 正常完成 | 0 | `done` | `state=done` |
| 失败（rc≠0） | 该阶段 rc | `failed` | `state=failed` |
| 外部 `--cancel` | 130 | `cancelled` | `state=cancelled` |
| Ctrl+C | 130 | `interrupted` | `state=interrupted` |

三种非正常结束**都会写阶段日志**，所以 `--log` 不会读到上一轮的旧日志。

上表的退出码是**前台**运行时的值。放进后台任务（job）里跑时，取消/中断会被包装器记成 `exit code: 1`，
前台同一条路径则是 130 —— 判断"成没成功、是不是被取消"，一律读 `qbuild_status.json` 的 `state`，
不要看 job 的退出码。

## 四、本机实测事实（别按直觉改）

1. **Quartus CLI 不输出构建百分比。** 四份阶段日志里唯一的 `%` 是
   `Info (170195): Router estimated average interconnect usage is 2%` 和
   `Info (170196): ... peak ... 7%`，都是布线资源占用，不是进度。所以进度只能靠时间插值。
2. **输出是阶段末集中吐出来的**：只有 Fitter 能在运行中给出子步骤（实测 `placing`、`routing`），
   `quartus_map / asm / sta` 的 stdout 在阶段结束才成批到达，进度标签会一直停在 `running`。这不是 bug。
3. **`taskkill /F /T /PID` 在本机沙箱下返回 `ERROR: Access denied` 且进程照跑。**
   早期版本因此以为"取消成功"，其实编译自己跑完了。现在用 `Popen.kill()`（TerminateProcess），
   取消失效会立刻暴露。
4. **Quartus 失败时会把记账行也加上 `Error:` 前缀**（`Error: Elapsed time:` / `Total CPU time` /
   `Peak virtual memory` / `Processing ended`）。脚本用白名单把这些排除，否则一次失败会多算 4–5 条错误。
5. `quartus_sh` 启动会往 stderr 打 `TBBmalloc: skip allocation functions replacement in ucrtbase.dll:
   unknown prologue for function _msize`，功能正常。**只判退出码的包装脚本会误判失败**，要判 stdout/关键行。
6. 冷启动（首次建数据库）比热态慢 ~60%（实测 32 s vs 19–21 s），首次 ETA 必然偏乐观。
7. **后台任务的退出码不可信。** 同样一次 `--cancel`：前台退出码 `130`，放进后台 job 后 job 报
   `exit code: 1`（包装器把"子进程被杀"记成终止）。判结果只看 `qbuild_status.json` 的 `state`
   与阶段日志页脚，**不要**用 job 退出码推断成功/取消。
8. **PowerShell 5.1 的 `Get-Content -Raw` 会按 ANSI 解码 UTF-8 产物**，导致 `report.html`、
   `.jsonl` 里的中文匹配出假阴性（文件本身是好的）。校验中文内容用
   `[IO.File]::ReadAllText($p, [Text.Encoding]::UTF8)`，或直接用 python 以 `encoding="utf-8"` 读。

## 四·附、实测基线（20 次构建的样本）

热态 `map 9.8–12.3 s / fit 5.2–6.3 s / asm 1.5–1.9 s / sta 2.5–2.7 s`，总计 19–23 s；
冷启动 32 s。每次构建固定 3 条 Fitter 警告（`292013` LogicLock 订阅提示、`15714` I/O 分配不完整、
`169085` 45 个引脚未定位）——没写引脚约束的工程属预期内，`errors=0`。
取消实测：发出 `--cancel` 后 0.25 s 内响应，阶段进程结束、残留 quartus 进程 0、`.cancel` 标志被消费、
状态保留取消时的 `pct`；随后立刻重编仍成功（`BUILD OK 00:20`）。

## 五、产物（全部落在工程目录）

| 文件 | 含义 |
|---|---|
| `qbuild_status.json` | 当前状态：`state/pct/stage/label/elapsed/eta`，每秒原子更新 |
| `qbuild_events.jsonl` | 追加式事件流，一行一个快照，用于画进度曲线 |
| `<revision>.history.json` | 最近 20 次构建：各阶段耗时、总耗时、预测值、警告/错误数、`ok` |
| `log_map.txt` 等 4 个 | 各阶段原始输出 + 一行页脚（rc/耗时/错误数/状态） |
| `report.html` | 自包含报告：阶段构成、ETA 收敛、逐次记录、进度曲线 |

`qbuild.py --report` 在终端打印历史表；`report.html` 是给人看的版本。

## 六、验收口径（这套东西能证明什么、不能证明什么）

- 能：阶段级别的进度与 ETA（最新误差 −1%，冷启动会到 +20% 量级）、取消真的生效、失败原因定位到阶段。
- 不能：段内精确百分比、增量编译、跨工程统计、MCP 层的工具调用。它只监控四个子进程的耗时。
- 取消后**编译数据库与工程锁不会被破坏**（实测被杀后立刻重编仍成功），但仍建议重编一次确认。

## 七、为什么是 Skill 而不是 MCP

这里每个动作都是一次命令行调用，没有常驻会话状态；做成 MCP 要额外维护一个 stdio 服务进程和
配置项，收益只有"工具名更好看"，代价是每次改动都要重启服务。若以后要做成 MCP，
脚本的 CLI 已经是干净接口（build / status / cancel / log / report 五个动词），包一层即可。
