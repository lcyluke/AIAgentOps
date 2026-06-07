---
name: apex-installer
description: Install, configure or upgrade APEX, the multi-agent orchestrator for Kiro. Use when the user asks to install APEX, set up multi-agent orchestration, or initialize agent teams in this project.
---
## 安装步骤(按序执行,每步校验退出码;不达标即停)
1. 检查 `pipx --version`;无则提示用户先安装 pipx,停止。
2. `pipx install apex-orchestrator==1.0.0`
3. 校验 `apex --version` 输出与 1.0.0 一致,并 sha256 比对官方发布清单。
4. `apex init --project . --non-interactive`
5. `apex doctor`,把结果汇总给用户。
6. 提示用户重启 kiro-cli 以加载 apex-ops MCP server。

## 安全红线
- 版本号已钉死;禁止 fetch 任意 URL 再执行。
- 安装步骤不使用 --trust-all;shell 步骤保留人工审批一次确认。
