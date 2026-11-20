# DSH-SKILL

给 [DSH (DeepSeek Harness)](https://github.com/ranxianglei/billion-context) 用的技能集合。
每个子目录是一个独立技能，放进 `$DSH_HOME/skills/` 即可生效。

```
DSH-SKILL/
└── quartus-build-progress/      # Quartus 分阶段构建进度 / ETA / 取消 / 历史报告
    ├── SKILL.md                 # 技能说明（DSH 读取的入口）
    └── scripts/
        ├── qbuild.py
        └── make_report.py
```

## 安装

把技能目录整个拷进 DSH 的技能根目录（默认 `F:\DSH\DSH-Home\skills\`，按自己环境改）：

```powershell
Copy-Item -Recurse .\quartus-build-progress "$env:DSH_HOME\skills\" -Force
```

重启 DSH（或新开会话）后，`quartus-build-progress` 会出现在可用技能列表里。
不需要 pip 安装任何依赖，只用 Python 3 标准库。

## quartus-build-progress

Quartus 的 `quartus_sh --flow compile` 是一个不透明的长进程，编译期间没有任何进度输出。
这个技能把流程拆成 `quartus_map → quartus_fit → quartus_asm → quartus_sta` 四个子进程依次跑，
用「历史同阶段耗时」做时间插值，给出百分比与 ETA，并支持干净取消与构建历史报告。

**脚本不落在工程目录**：所有状态写进你用 `--dir` 指定的工程目录，工具本身常驻在技能目录。

```powershell
$Q = "$env:DSH_HOME\skills\quartus-build-progress\scripts"

python "$Q\qbuild.py" --dir F:\path\to\proj                 # 构建，实时进度 + ETA
python "$Q\qbuild.py" --dir F:\path\to\proj --cancel        # 另一个终端请求干净停止
python "$Q\qbuild.py" --dir F:\path\to\proj --status        # 打印状态 JSON
python "$Q\qbuild.py" --dir F:\path\to\proj --log fit --tail 40
python "$Q\qbuild.py" --dir F:\path\to\proj --report        # 历史阶段耗时表
python "$Q\make_report.py" --dir F:\path\to\proj            # 生成 report.html
```

工程名与 revision 自动探测（目录里第一个 `*.qpf`，revision 默认等于工程名）；
换 Quartus 版本时用 `$env:QUARTUS_BIN` 覆盖可执行文件目录。

### 产物（全部在工程目录内）

| 文件 | 含义 |
|---|---|
| `qbuild_status.json` | 当前状态 `state/pct/stage/label/elapsed/eta`，每秒原子更新 |
| `qbuild_events.jsonl` | 追加式事件流，一行一个快照 |
| `<revision>.history.json` | 最近 20 次构建的逐阶段耗时、预测值、警告/错误数、`ok` |
| `log_map.txt` / `log_fit.txt` / `log_asm.txt` / `log_sta.txt` | 各阶段原始输出 + 一行页脚 |
| `report.html` | 自包含报告：阶段构成、ETA 收敛、逐次记录、进度曲线 |

### 实测基线（EP4CE6F17C8 · Quartus Prime Lite 25.1 · 20 次构建）

- 热态 19–23 s：`map 9.8–12.3 / fit 5.2–6.3 / asm 1.5–1.9 / sta 2.5–2.7 s`；冷启动 32 s（首次建数据库慢约 60%）。
- ETA 误差从首轮的 −62% 收敛到 −1%（最近 5 次逐阶段中位数）。
- 取消：0.25 s 内响应，残留进程 0，被杀后立刻重编仍成功。

### 已知边界（实测，不是猜测）

1. **Quartus CLI 不输出构建百分比**——日志里唯一的 `%` 是 Fitter 的互连占用率。
2. **阶段输出在结束时集中吐出**——只有 Fitter 能在运行中给出子步骤标签，其余阶段停在 `running`。
3. **`taskkill /F /T` 在受限环境下会 `Access denied` 且进程照跑**——必须用 `Popen.kill()`（TerminateProcess）。
4. **后台任务（job）的退出码不可信**——同一取消，前台 130、后台包装器报 1；判状态读 `qbuild_status.json`。
5. 能证明的是阶段级进度与取消生效；不能证明段内精确百分比，也不做增量编译。

细节与更多踩坑见 [quartus-build-progress/SKILL.md](quartus-build-progress/SKILL.md)。

## 许可

MIT，见 [LICENSE](LICENSE)。
