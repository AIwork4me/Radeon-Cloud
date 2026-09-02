# UX 测试设计 — Radeon Cloud Connector 端到端 one-pass & 丝滑

日期：2026-09-02
状态：已设计 → 已实现（`scripts/journey_check.py`）→ 已核验

## 目标

bug 修复（别名前置闸门 `require_ssh_alias` + 去除写死 IP）之后，用一套**自动化**测试保证：

1. **冷启动 one-pass**：全新用户（还没连过云）跑 skill，会被**唯一、清晰、含连接指南链接**的提示引导去连接，绝不出现级联的原始 ssh 报错。
2. **连上之后 one-pass**：一旦 `radeon-cloud` 别名配好，从 `rc guide` 到首个 GPU 结果全程一次成功、输出可扫读（GPU 摘要单行、无原始 rocm-smi 横幅、无超长行）。
3. **丝滑 = 结构正确性**（客观、CI 稳定，不引入耗时/性能阈值）。

## 落点（已与用户确认）

- **扩展现有 `journey_check.py`**，不新建 harness。复用 `run_rc()` / `Results.check()` / `res.stage()`。
- **R6（新增 review 维度，离线、秒级）**：随 `--phase review` 跑，无需真机，纯静态 CI 也能守住 bug 修复不回退。
- **Stage 9（新增 journey 阶段，实时）**：用**临时 HOME** 模拟全新用户，跑完即弃，绝不碰真实 `~/.ssh/config`。
- **J1.9 / J2.10（既有阶段增强）**：给首接触与看懂机器加"可扫读"的结构断言。

## R6 — 离线冷启动 & 丝滑闸门（review 阶段）

| ID | 断言 | 离线？ |
|---|---|---|
| R6.1 | `status --host <bogus>` 退出码 = 连接码(2) | 是（失败在 SSH 之前） |
| R6.2 | 同上输出含连接指南链接 | 是 |
| R6.3 | 同上 **仅一个 [FAIL]**，不是级联 | 是 |
| R6.4 | 同上**不泄漏**原始 ssh 报错（Permission denied / Could not resolve / Connection refused / timed out / REMOTE HOST IDENTIFICATION） | 是 |
| R6.5 | `doctor --host <bogus>` 同样指向指南、无级联 | 是 |
| R6.6 | `guide --host <bogus>`（文档首命令）同样退到指南链接 | 是 |
| R6.7 | `connection_setup_hint()` 返回**逐字精确**的提示语（单测级回归） | 是 |
| R6.8 | `require_ssh_alias()` 已定义且被 `require_remote()` 第一行调用 | 是 |
| R6.9 | shipped 文件（rc.py / environment.md / SKILL.md）**不含写死 IP** `36.150.116.220` / `31622` | 是 |

## Stage 9 — 模拟全新用户的冷启动剧本（journey 阶段，临时 HOME）

临时 HOME = `tempfile.mkdtemp(prefix="rc-ux-")`，子进程以 `env={**os.environ, HOME=tmp, USERPROFILE=tmp}` 运行（Windows 上 `Path.home()` 取 `USERPROFILE`，故两者皆设）。跑完 `shutil.rmtree` 清理。

- **场景 A（冷，无别名，全离线）**：临时 HOME 无 `~/.ssh/config` → `status`/`guide` 退出 2、含指南链接、仅一个 [FAIL]、无原始 ssh 报错。证明 bug 修复的"第一步先连云"契约。
- **场景 B（已连，隔离副本，实时）**：把真实 `~/.ssh` 整目录拷进临时 HOME（只读副本，私钥亦在内、跑完即删），并在副本 config 追加 `StrictHostKeyChecking accept-new` + `UserKnownHostsFile` 指向临时 known_hosts（避免 known_hosts 交互卡住）。然后 `guide`/`doctor`/`status`/`exec` 全部退出 0、输出可扫读、首个 GPU 命令无 flag 即 `torch.cuda.is_available()==True`。证明"连上之后全程 one-pass & 丝滑"。

## 既有阶段增强

- **J1.9**：`rc guide` 输出含 `step 1` … `step 8` 全部步骤标签（逐步叙述、可扫读）。
- **J2.10**：`rc status` 输出含 `GPU[0]`、**无** `====` 原始 rocm-smi 横幅、且无超过 200 字符的行（蒸馏、可扫读）。

## 安全与稳定性

- 绝不修改真实 `~/.ssh/config`；临时 HOME 子进程隔离，跑完即弃。
- 场景 B 只跑只读命令（guide/doctor/status/exec），不在远端留 job/scratch。
- 全部断言是结构正确性，不含耗时阈值 → CI 不抖。
- 新增代码在 `journey_check.py` 中保持纯 ASCII（中文提示语用 `\u` 转义），不破坏 R3.1。

## 运行

```bash
"$PY" scripts/journey_check.py --phase review     # 含 R6，离线秒级
"$PY" scripts/journey_check.py --phase journey    # 含 Stage 9（场景 B 需真机）
"$PY" scripts/journey_check.py --stage 9          # 只看冷启动剧本
```

两阶段任一失败均非零退出，CI 就绪。本会话中实现后：`--phase review` 含 R6 全绿；`--phase journey` 含 Stage 9 + J1.9/J2.10，随 nightly CI 持续守护。
