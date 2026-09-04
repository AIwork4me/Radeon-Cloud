# radeon-cloud 技能端到端体验测试设计（2026-09-04）

## 目标与归因原则

以端到端用户旅程为主线，验证用户在 WorkBuddy 中安装 radeon-cloud 技能后，
从识别启用 → 冷机诊断 → 数据搬运 → 远程执行与后台任务 → 收尾释放的完整链路体验。

**归因原则**：远端机器自身健康问题（loadavg 异常、rocminfo 挂死、磁盘水位）
单列为「环境因素」，不计入技能流畅度评分；但技能对这些问题的诊断引导质量
（报错是否清晰、是否给出下一步）属于技能体验考察范围。

**评分维度**（1-5 分）：可发现性、上手摩擦、命令工效学、错误信息质量、
文档-实现一致性、护栏打扰度。

## 测试范围决策

- 深度：全链路（含真实 push/pull 写远端、真实后台任务）
- 启用环节：审查（SKILL.md 元数据 + 安装完整性）+ 本会话实测结合
- 异常制造：仅无害异常（非法参数、越界路径、不存在资源、dry-run），
  不修改本地 ssh 配置与远端共享环境

## 用例清单

### 阶段 1 — 识别与启用
| # | 用例 | 方法 |
|---|---|---|
| T1.1 | 安装完整性 | 检查 `~/.workbuddy-ai/skills/radeon-cloud/` 四件套 |
| T1.2 | 触发词覆盖 | 审查 SKILL.md description 与 Triggers |
| T1.3 | 冷机引导 | 实测 `rc guide`，新用户视角走读 |
| T1.4 | 双版本同步 | diff 安装版与开发版 SKILL.md / rc.py |

### 阶段 2 — 冷机诊断
| # | 用例 | 期望 |
|---|---|---|
| T2.1 | `rc doctor` | 分层检查全过、直达 GPU 检查 |
| T2.2 | `rc status` / `--torch` | GPU 读数合理、venv 清单透明 |
| T2.3 | `rc env` | env.sh PATH 交叉验证、缺失 venv 显式标注 |

### 阶段 3 — 数据搬运
| # | 用例 | 期望 |
|---|---|---|
| T3.1 | push + `--exclude` | 排除生效（远端 find 验证） |
| T3.2 | pull 回传 | 内容 diff 一致 |
| T3.3 | pull 覆盖拒绝 | 拒绝且提示 `--overwrite` |
| T3.4/3.5 | push `--dry-run` | exit 0 且远端不落盘（远端 test -e 验证） |

### 阶段 4 — 远程执行与后台任务
| # | 用例 | 期望 |
|---|---|---|
| T4.1 | exec torch 导入 | 输出版本号，auto-venv 生效 |
| T4.4 | exec 远端 `exit 3` | 返回 3（远端码透传，非 1/2） |
| T4.2 | run → jobs → logs | job id/pid/log 路径齐全，状态追踪准确 |
| T4.3 | stop + 二次 stop | SIGTERM / 幂等，均 exit 0 |

### 阶段 5 — 异常与边界
| # | 用例 | 期望 |
|---|---|---|
| T5.1 | `--host evil-box` | 白名单拒绝，exit 2，给出 doctor 出口 |
| T5.2 | exec/push 越界 `/tmp` | 拒绝并提示 `--allow-ephemeral` |
| T5.3 | stop/logs 不存在 job | 明确报错并列出已知 job |
| T5.4 | 非交互无 `--yes` | 远程执行类拒绝；只读类行为核对文档 |
| T5.5 | `config --show` | 配置透明可读 |
| T5.6 | `exec --timeout 5 -- sleep 30` | exit 124，总耗时有界（高负载下回归验证） |

## 结果

实测数据与结论见工作区根目录《radeon-cloud端到端体验测试报告.md》。
