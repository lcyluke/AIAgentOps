# APEX 技术设计方案 v0.1

多 Agent 编排器:面向 Kiro CLI / Claude Code / Hermes Agent 的统一调度、监控与协作平台。
本方案合并了对 gnassro/phi、requix/kiro-team、awslabs/cli-agent-orchestrator(CAO)三个开源项目的对比分析结论。

---

## 1. 背景与定位

APEX 是运行在本地(macOS 优先)的多 Agent 编排器。它不替代任何编码 agent,而是站在它们之上,解决四个问题:实例的命名与生命周期管理、任务调度与定时巡检、跨 agent 的上下文共享(黑板)、以及完成状态的机制级校验。底层 agent 运行时是可插拔的:Kiro CLI(headless / ACP)、Claude Code(print 模式 / Agent SDK)、Hermes Agent(gateway / MCP serve)。

APEX 与 Hermes 的关系是双向的:Hermes 的 cron 与消息网关充当 APEX 的告警与人机通道(已落地企业微信链路);APEX 充当 Hermes 之外、面向编码型 agent 的专职调度层。

---

## 2. 对比分析:三个项目各自贡献什么

### 2.1 gnassro/phi(VS Code 内嵌 Pi agent 的客户端)

phi 是单 agent 的前端客户端,不做编排。但其工程模式质量高,APEX 吸收以下四项:

**(a) 单一 SDK 边界(Adapter 铁律)。** phi 的 `agent-manager.ts` 有一条显式规则:它是全项目唯一允许 import Pi SDK 的文件,所有 SDK 生命周期(initialize → prompt/steer/abort → dispose)收敛在此。APEX 据此设立 RuntimeAdapter 层:`KiroAdapter`、`ClaudeCodeAdapter`、`HermesAdapter` 各自封装对应 CLI/协议,核心编排逻辑(Scheduler、Blackboard、Registry)只面向 Adapter 接口编程,永不直接拼 CLI 命令。换 Kiro 版本、换 ACP 协议细节,只改一个文件。

**(b) 类型化消息协议,单通道路由。** phi 的 Webview 与扩展宿主之间所有消息是一个穷举的 discriminated union(`{ type: "prompt" | "abort" | "switch_session" | ... }`),经唯一通道路由,协议单独成文档(ipc-protocol.md)。APEX 的事件与指令协议照此定义(见 §4),hook 上报、Adapter 回调、CLI 指令共用同一套 schema,杜绝散装 JSON。

**(c) 完整的会话动词集。** phi 对会话的操作动词是:`prompt`(空闲时发送)、`steer`(流式中注入打断当前轮)、`abort`(取消)、`switchSession` / `newSession`(切换/新建并重绑监听)。其中 steer 的语义(agent 忙时不开新轮、把输入注入当前运行)与 Hermes 的 busy-input 三模式(interrupt / queue / steer)同构。APEX 的实例操作接口采用同一动词集,语义跨运行时统一。

**(d) 凭据分域。** phi 的 EnvManager 区分"全局环境变量"与"扩展本地 SecretStorage"(`ctx.secrets.get/store/delete`),provider 凭据可只在扩展作用域生效。APEX 据此实现 per-agent 凭据注入:每个 agent 实例启动时仅注入其角色所需的密钥(经 macOS Keychain 或加密文件),黑板与事件流中绝不落明文凭据。

phi 另有两项流程资产值得照搬:AGENTS.md 的"改代码必须同步改文档"纪律,与一条命令完成版本判定、changelog 生成、打 tag、CI 发布的 release 脚本。

### 2.2 requix/kiro-team(Kiro 内部的 team 编排)

team-lead agent 读计划、派发给专职 subagent(builder / validator / documenter)。其核心局限正是 APEX 要解决的:team-lead 是唯一协调者,subagent 对任务列表是盲的,没有共享黑板。它贡献了三条 Kiro 实测工程约束,直接写入 APEX 的 `doctor` 检查项:

1. agent JSON 必须平铺在 `.kiro/agents/` 根下,子目录不参与 subagent 解析;
2. `trustedAgents` 中的名字必须与 JSON 文件名(去 .json)严格一致;
3. 部分工具在 agent 以 subagent 身份运行时会静默不可用——配置里声明了也没用,需在 doctor 中主动探测。

### 2.3 awslabs/cli-agent-orchestrator(CAO,外部编排器)

与 APEX 同赛道,supervisor–worker 模式,每个 agent 跑在隔离 tmux 会话中,经 MCP 暴露 handoff / assign / send_message 三原语,支持跨 provider(supervisor 用 Kiro、worker 用 Claude Code)、cron Flows、headless 异步执行、人随时 attach tmux 介入。APEX 直接采纳其两个设计:tmux 隔离每个 agent 进程(可观察、可人工介入),以及 ops-mcp 模式(主 agent 在自己的聊天循环里调用编排器工具)。

### 2.4 差异化汇总

| 能力 | phi | kiro-team | CAO | APEX |
|---|---|---|---|---|
| 定位 | 单 agent 客户端 | Kiro 内 team | 外部编排器 | 外部编排器 |
| 共享上下文 | — | 无(subagent 盲) | 点对点消息 | 黑板 + digest 每轮注入(广播) |
| 完成校验 | — | validator agent | 无强制 | stop hook block 反馈环(机制级) |
| 遥测 | 前端实时流 | 主会话内可见 | tmux 人工看 | hook 全生命周期事件流(机器可读) |
| 防重复 | — | lead 单点调度 | supervisor 调度 | claims 认领表 + preToolUse 阻断 |
| 凭据管理 | SecretStorage 分域 | — | — | per-agent Keychain 注入(继承 phi) |
| 告警通道 | — | — | — | Hermes gateway → 企业微信 |
| 审计合规 | — | — | — | 事件留痕 + digest 脱敏 |

---

## 3. 总体架构

```
┌────────────────────────────── 交互面(Surfaces) ──────────────────────────────┐
│  apex CLI          apex-ops MCP server        Kiro skill         Hermes 通道   │
│ (init/up/spawn/   (apex_status/spawn/        (apex-installer,   (告警→企业微信, │
│  status/doctor)    assign/blackboard/claims)  对话内安装)         cron 触发巡检)  │
└───────────────┬───────────────────────────────────────────────────────────────┘
                │  Command Protocol(§4.2,类型化指令)
┌───────────────▼────────────────── APEX Core ───────────────────────────────────┐
│  Registry        Scheduler          Blackboard        Claims        Auditor    │
│  (实例注册表:     (任务队列、依赖图、   (findings 提炼、    (任务认领、     (完成判据、    │
│   name↔session_id, cron 巡检)        digest 生成、脱敏)  乐观锁)        block 决策)   │
│   状态机)                                                                       │
│                          Event Bus(SQLite WAL,全事件留痕)                      │
└───────────────┬────────────────────────────────────────────────────────────────┘
                │  Adapter 接口(§3.2,唯一 SDK/CLI 边界 —— 继承 phi 铁律)
┌───────────────▼──────────────────────────────────────────────────────────────┐
│  KiroAdapter            ClaudeCodeAdapter         HermesAdapter               │
│  (ACP JSON-RPC 为主,    (claude -p --output-      (gateway REST /             │
│   headless 为辅,         format json / Agent SDK)  hermes mcp serve)           │
│   tmux 隔离)                                                                   │
└───────────────┬──────────────────────────────────────────────────────────────┘
                │  Event Protocol(§4.1,hook shim 上报)
        Kiro hooks(agentSpawn / userPromptSubmit / preToolUse / postToolUse / stop)
```

### 3.1 进程模型

`apexd` 为常驻守护进程(launchd 安装,与 Hermes gateway 同机共存),持有 Registry / Scheduler / Blackboard / Event Bus;每个 agent 实例由对应 Adapter 启动在独立 tmux 会话中(继承 CAO),会话名即实例名(方案 C 命名:`{agent}-{taskid}`),人工可随时 `tmux attach` 介入。存储用单文件 SQLite(WAL 模式),零运维。

### 3.2 RuntimeAdapter 接口(继承 phi 的边界铁律与动词集)

```python
class RuntimeAdapter(Protocol):
    def spawn(self, agent: str, instance: str, brief: str, env: dict) -> SessionHandle
    def prompt(self, h: SessionHandle, text: str) -> None      # 空闲时发送
    def steer(self, h: SessionHandle, text: str) -> None       # 运行中注入,不开新轮
    def abort(self, h: SessionHandle) -> None
    def status(self, h: SessionHandle) -> InstanceStatus       # idle|running|waiting_approval|done|failed
    def dispose(self, h: SessionHandle) -> None
```

KiroAdapter 首选 ACP(`kiro-cli acp`,initialize → create session → prompt → stream 的 JSON-RPC 生命周期),session 由 APEX 创建并持有句柄,steer/abort 语义可直接映射;一次性巡检任务退化用 headless(`kiro-cli chat --no-interactive --agent <name> --trust-tools=...`)。ClaudeCodeAdapter 用 print 模式 + `--output-format json`(取 session_id / total_cost_usd / subtype 做状态与成本核算),多轮续接走 `--resume`。

### 3.3 凭据管理(继承 phi EnvManager 分域)

凭据三层:机器层(macOS Keychain / 加密 .env,apexd 启动时解锁)→ 角色层(每个 agent JSON 声明所需凭据名单,Adapter spawn 时按名单注入子进程 env,不多给)→ 禁区(Blackboard digest 与 Event Bus 落库前经脱敏器,正则 + 实体识别双道,密钥/令牌/PII 替换为指纹)。审计日志记录"谁在何时被注入了哪些凭据名"(只记名不记值)。

---

## 4. 协议定义(继承 phi 的类型化 discriminated union)

### 4.1 Event Protocol(上行:hook shim / Adapter → Core)

```typescript
type ApexEvent =
  | { type: "instance.registered"; agent: string; instance: string;
      session_id: string; runtime: "kiro"|"claude"|"hermes"; cwd: string; ts: string }
  | { type: "turn.heartbeat";   session_id: string; tool: string; ok: boolean; ts: string }
  | { type: "turn.completed";   session_id: string; response_digest: string;
      verdict: "pass"|"blocked"; reason?: string; ts: string }      // 来自 stop hook
  | { type: "task.claimed";     session_id: string; task_id: string; ts: string }
  | { type: "task.done";        session_id: string; task_id: string; evidence: string; ts: string }
  | { type: "guard.blocked";    session_id: string; tool: string; rule: string; ts: string } // preToolUse exit 2
  | { type: "instance.failed";  session_id: string; error: string; ts: string };
```

Kiro 侧由统一的 `apex-hook.sh <verb> <agent>` shim 把 hook STDIN 的 JSON 包装成上述事件 POST 到 `apexd`(本地 unix socket / 127.0.0.1 REST)。hook 事件本身不含 agent 名,由 shim 第二参数补齐(每个 agent 的 hooks 配置里写死自己的名字)。

### 4.2 Command Protocol(下行:Surfaces → Core)

```typescript
type ApexCommand =
  | { type: "spawn";  agent: string; task: TaskSpec }
  | { type: "assign"; instance: string; task: TaskSpec }
  | { type: "steer";  instance: string; text: string }
  | { type: "abort";  instance: string }
  | { type: "query";  what: "status"|"blackboard"|"claims"|"events"; filter?: object };
```

CLI、apex-ops MCP、Hermes 通道三个面共用这一套指令,只是传输层不同。

---

## 5. 协作机制(承接既有设计,定稿)

**命名(方案 C):** Registry 持有 `{agent}-{taskid}` ↔ session_id 映射,创建即绑定;人工 `/spawn --name` 时沿用同名,保证 crew monitor 与 Registry 一致。

**上下文共享三层:** 静态层 = steering + resources(custom agent 必须显式 `file://.kiro/steering/**/*.md`);动态层 = Blackboard,写入端为 stop hook 上报经 Auditor 校验后的结论,读取端为 userPromptSubmit hook 每轮注入 digest(排除本人条目,≤800 token,经脱敏);大体量参考 = 共享 knowledge base(检索前不占上下文)。

**防重复:** claims 认领表(乐观锁,inject 时随 digest 注入)+ preToolUse 阻断(职责外工具 exit 2 拦回,如非 test-runner 跑 pytest)。

**完成校验:** Auditor 按 TaskSpec 中的判据(测试通过、文件存在、lint 干净等可执行断言)在 stop hook report 分支校验;不达标输出 `{"decision":"block","reason":...}` 让 agent 续跑,达标才标 done 并写黑板——黑板条目因此可信,他人方可免重查。

**巡检:** Scheduler 的 cron 巡检产出异常摘要 → 经 HermesAdapter 推送企业微信(复用已建 WeCom 链路);Hermes 侧 cron 亦可反向触发 `apex query status` 实现双向心跳。

---

## 6. 安装与分发

### 6.1 CLI 路径

```bash
pipx install apex-orchestrator      # 或 brew tap 安装
apex init [--project DIR]           # 铺设 .kiro/agents(平铺)、steering、blackboard、hooks、mcp.json
apex doctor                         # 检查:kiro/claude 版本、agents 平铺、trustedAgents 名字匹配、
                                    # subagent 工具可用性探测、hook 可执行、launchd PATH 新鲜度
apex up                             # 安装并启动 apexd(launchd)
apex spawn <agent> "<task>"         # 起实例(方案 C 命名)
apex status [--watch]               # 实例状态总览
```

### 6.2 Kiro 对话内安装(skill 引导 + ops-mcp 接管)

`apex-installer` skill(YAML frontmatter 的 description 写明触发语义:"安装/初始化 APEX 多 agent 编排")随团队仓库 `.kiro/skills/` 分发,clone 即有;个人机一条命令拷入 `~/.kiro/skills/`。用户在 Kiro 对话框说"装一下 APEX",skill 命中后按步骤执行:检查 pipx → `pipx install apex-orchestrator==<钉死版本>` → sha256 比对发布清单 → `apex init --non-interactive` → `apex doctor` 汇总 → 提示重启加载 apex-ops MCP。装毕后,对话式管理全部走 apex-ops MCP 工具(`apex_status / apex_spawn / apex_assign / apex_blackboard / apex_claims`),即 CAO 的 ops-mcp 模式。

安全红线:skill 钉死版本与校验和;绝不让 agent fetch 任意 URL 再执行;安装步骤不授予 trust-all,shell 保留人工审批一次确认;首选 git 内置分发而非公网拉取。

---

## 7. 安全与合规层

策略闸(Tirith 同类):preToolUse shim 内置规则表(危险命令模式、职责外工具、敏感路径),exit 2 阻断并回话;规则表由 APEX 集中下发,agent 配置只引用。审计:Event Bus 全事件留痕(SQLite,append-only 视图),满足"谁、何时、对哪个仓库、做了什么、花了多少"的追溯;Claude Code 侧用 json 输出的 total_cost_usd 做成本归集。数据边界:blackboard 与 claims 不入 git(.gitignore),或入 git 前强制过脱敏器;凭据按 §3.3 三层管理。隔离:Kiro 写路径用 toolsSettings.allowedPaths 收敛,`.kiro/agents/**` 与 `.kiro/blackboard/**` 对所有 agent 只读,黑板唯一写入者是 apexd。

---

## 8. 里程碑

M1(可用):KiroAdapter(headless)+ Registry + Event Bus + apex-hook.sh + CLI(init/spawn/status/doctor)。
M2(协作):Blackboard + Claims + Auditor(block 反馈环)+ userPromptSubmit 注入。
M3(对话面):apex-ops MCP + apex-installer skill + ACP 切换为 Kiro 主通道。
M4(联邦):ClaudeCodeAdapter + HermesAdapter(企业微信告警、双向 cron)+ 成本归集报表。
M5(工程化):release 自动化(借 phi 的 release.mjs 流程)、AGENTS.md 文档纪律、tmux 看板 TUI。

---

## 附:phi 仓库速览(对比依据)

phi v0.7.2,MIT,TypeScript/ESM,VS Code 扩展(发布于 Open VSX)。两个隔离运行时(扩展宿主 Node.js / Webview Chromium)仅经 postMessage 通信;src 仅 9 个顶层文件,职责单一:extension.ts(入口)、agent-manager.ts(Pi SDK 唯一边界)、panel-manager.ts(Webview)、ipc-bridge.ts(路由)、editor-context.ts(编辑器上下文)、env-manager.ts(凭据分域)、commands.ts、utils.ts、legacy-google/(OAuth provider)。文档四件套:architecture.md、ipc-protocol.md、ROADMAP.md、TASKS.md,外加约 30KB 的 AGENTS.md 行为守则。

---

# 附录 A:Agent System 0.12.99 深度逆向分析(竞品)

> 来源:闭源 vsix(`agent-system.agent-system-0.12.99`)。经 js-beautify 反压缩(7.4MB → 24.2 万行),打包器保留了原始模块路径注释(`../../infra/dist/agents/*` 等),据此还原控制流。作者 Jorge Leal,真实源码仓 `github.com/BSTCMX/agent-system`。这是与 APEX 最同类的产物(多 agent + 固定角色 + supervisor/worker + 本地记忆),但形态是 IDE 内嵌、单运行时。以下为逐函数对照,供 APEX 取舍。

## A.1 关键发现:Swarm 不是"调度器",而是"把委派做成一个工具"

最重要的结论:它没有独立的 supervisor 调度循环。**`delegate_to_worker` 是 agent 工具集里的一个普通工具**,supervisor(只能是 coordinator)在自己的 agent loop 里像调用 read_file 一样调用它来派单。一次 supervisor step 触发一个嵌套 worker 子循环,worker 跑完把结构化结果当作 tool_result 返回给 supervisor。这是"用工具调用实现委派"的极简模式,和 CAO 的 MCP 三原语(handoff/assign/send_message)是同一思路的不同实现。

工具入参 schema(zod,逐字段):

```
delegate_to_worker(workerId: string, taskDescription: string, context?: string[])
```

## A.2 逐函数控制流

**`resolveAgentLoopRunProfile(input)`** — 决定本次 loop 的角色画像。唯一的分叉:`swarmMode === "supervisor-worker" && agentId 含 "coordinator"` → 返回 supervisor 画像(工具集 = coordinator 只读工具 + delegate_to_worker,`supervisorSwarmProtocol: true`);否则返回普通角色画像。**含义:swarm 模式只对 coordinator 生效,其他角色即使在 swarm 模式下也只是普通 agent。**

**`runDelegatedWorkerLoop(raw, parentOptions)`** — 委派工具的实际执行体,控制流:
1. 校验 workerId / taskDescription 非空,否则返回 `INVALID_INPUT`。
2. `resolveAgentIdToCanonical` 规范化;`agent:auto` → 强制改写为 `agent:coder`。
3. **防递归:若 workerId 含 coordinator,直接拒绝 `INVALID_WORKER`("不能委派给 coordinator")。** 这是它防止无限委派的唯一机制——靠"协调者不可被委派"这条规则把委派图限制成单层。
4. 算嵌套步数预算:`nestedMax = clamp(floor(parentMax * 0.75), 1, 24)`。子循环步数是父级的 75%,硬上限 24(`NESTED_DELEGATION_MAX_STEPS_CAP`)。
5. 给 worker 拼 system prompt(worker 自己的角色画像)+ user 消息("执行下面的委派任务,完成后用简洁摘要回复,附重要文件路径")。可选 context 作为"来自 supervisor 的路径/笔记"附在任务后。
6. 跑子循环(同一个 `runAgentLoop`,换 allowedToolNames 和 maxSteps),收集 `done`/`assistant_content.final` 作为 output。
7. 返回结构化 JSON:`{success, workerId, output, error?, code?, nestedRunId, durationMs, nestedMaxSteps}`。

**`runAgentLoop(host, messages, options)`** — 通用主循环(supervisor 和 worker 共用),骨架:
- `while (steps < maxSteps)`:连续失败会触发步数惩罚 `effectiveMaxSteps = maxSteps - penalty`(`CONSECUTIVE_FAILURE_THRESHOLD` + `STEP_PENALTY_PER_FAILURE`)。
- 每个工具调用前:`requestToolApproval`(按风险等级,见 A.4)→ 若 rejected 则把拒绝原因作为 tool_result 喂回模型继续。
- **停滞检测**:`stagnationDetector.isRepeatedFailure(name, args)` 在执行前拦截"重复失败的同一调用"(默认同一 (tool,args) 失败 ≥ 阈值即 block);每次执行后 `record(name,args,success)`;`getStagnation().stagnant` 为真时注入提示。
- 每步后 `saveCheckpointSafe(...)` 落检查点(对应 SQLite 表 `agent_loop_checkpoints`),支持崩溃续跑。
- 编辑类工具走 `editConflictTracker` 防并发改同一文件。

## A.3 完整系统提示词构建器 `buildAgentLoopSystemPrompt(options)`

这是它行为的"宪法",按 role / supervisorSwarm / isReadOnly 三个维度拼装。提取出的关键段落(原文):

**Supervisor 段(仅 coordinator + swarm):**
```
=== SWARM SUPERVISOR MODE ===
You analyze the user request and route work. Use read_file or read_files at most
once or twice for orientation when needed.
Then call delegate_to_worker with workerId (coder, reviewer, tester, or explainer),
a precise taskDescription, and optional context (file paths or short notes you
already gathered).
Use coder for implementation and edits; reviewer for audits; tester for tests;
explainer for conceptual answers.
After the tool returns, summarize results clearly for the user.
...
=== SUPERVISOR WORKFLOW ===
Implementation, multi-file edits, tests, and deep review belong in
delegate_to_worker with the right workerId.
Answer the user's question directly when a short reply suffices without workspace changes.
```

**只读角色段(reviewer/explainer/coordinator 非 swarm):**
```
Your role is READ-ONLY analysis and feedback. You must NOT modify files.
If file modifications are needed, delegate to a coder agent.
...
=== CRITICAL DIRECTIVE ON PERMISSIONS ===
ROLE: You are a READ-ONLY {ROLE}.
REQUIRED ACTION FOR WRITES: You are NOT authorized to create, edit, or delete files.
```

**通用约束段(所有角色):**
```
=== VOICE & BREVITY ===
You are a Senior Engineer. Your communication is 80% action, 20% explanation.
Your success is measured by code quality, not chat politeness.
=== SYSTEM CONSTRAINTS & BUDGET ===
- STEP BUDGET: strict limit of {maxSteps} tool execution steps per request.
- OUTPUT LIMIT: Tool outputs are truncated at {maxToolOutputChars} characters.
=== STEERING & SAFETY ===
If the user requests a technically dangerous action, pause and warn briefly. Offer a
safer alternative before asking for final approval.
If a tool call returns a "file does not exist" error, treat it as real until verified.
Rely on tool results rather than conversation history alone.
=== WISDOM & SCOPE ===
Presumption of intent: Prefer the minimum change necessary. For reversible actions act;
for irreversible/broad actions propose a concrete scope or ask confirmation.
For vague instructions, propose a concrete option and ask for a variant instead of
open questions like "What should I clean?".
```

默认常量:`DEFAULT_MAX_STEPS = 20`,`DEFAULT_MAX_TOOL_OUTPUT_CHARS = 8000`。

## A.4 角色工具白名单与风险分级(可直接借鉴)

`ROLE_TOOL_WHITELIST`(机制级职责隔离,不靠提示词自觉):

| 角色 | 允许工具 |
|---|---|
| coder | read_file, list_files, read_files, apply_edit, apply_edits, create_file, search_web, get_web_content |
| reviewer | read_file, list_files, read_files, search_web, get_web_content(纯只读) |
| tester | read_file, list_files, read_files, create_file, search_web, get_web_content |
| coordinator | read_file, list_files, read_files, search_web, get_web_content(只读;swarm 时 +delegate_to_worker) |
| explainer | 同 coordinator(只读) |
| default | read_file, list_files, read_files |

注意:**没有任何角色的白名单包含 run_shell 或 delete_file**——这两个只在"全工具"兜底集里,且被风险分级拦截。`normalizeAgentRole` 支持模糊匹配(含 "developer"/"writer"→coder,"audit"→reviewer 等),容错友好。

`ToolRiskLevel` 三级:
- `READ_ONLY`:read_file / read_files / list_files
- `STATE_CHANGING`:apply_edit / apply_edits / create_file / search_web / get_web_content
- `DESTRUCTIVE`:delete_file / run_shell

配合配置项 `autoApproveStateChanging`:开启后 STATE_CHANGING 自动批,DESTRUCTIVE 始终需人工批。这是比 Kiro 的 trust-all / trust-tools 更细的一档策略。

## A.5 旧编排路径 `AgentOrchestrator`(已被 swarm 取代但仍在包内)

包内还留着一套更"重"的编排器:`AgentOrchestrator` 持有 `agents: Map`、`taskQueue`、`selectionStrategy`("best-match")、`enableCollaboration`、EventBus 发 `agent:registered` 等事件。它有 `registerAgent`/`unregisterAgent`/`delegateTo` 等方法,更接近经典的中心调度器。但实际运行路径走的是 A.1–A.2 的工具式委派——说明它经历过一次"从中心调度器 → 工具式委派"的架构演进,把重编排器降级成了一个工具。这条演进对 APEX 是反向参考:工具式委派轻但只能单层,中心调度器重但能做依赖图。

## A.6 对 APEX 的取舍结论

**值得吸收:**
1. **工具式委派作为轻量档**:APEX 的 Scheduler 之外,可提供一个 `delegate_to_worker` 风格的工具档,给"不需要黑板、单层派单"的简单场景用,降低开销。
2. **嵌套步数预算衰减**(子 = 父 × 0.75,硬上限):直接并入 APEX 的 TaskSpec 预算模型,防止委派链耗尽预算。
3. **StagnationDetector**:执行前拦截"重复失败的同一 (tool,args)" + 连续失败步数惩罚——APEX 的 Auditor 之外再加一道"防原地打转"闸,比单纯 block decision 更早介入。
4. **三级工具风险 + autoApprove 分档**:并入 APEX 的 preToolUse 闸门策略,替代 Kiro 的二元 trust。
5. **角色硬白名单**:它的 ROLE_TOOL_WHITELIST 证明"机制级隔离比提示词约束可靠"——印证 APEX 用 toolsSettings.allowedPaths 的方向。
6. **EditConflictTracker**:多 agent 改同一文件的并发保护,APEX 多实例并行时必需,之前未细化。

**APEX 的差异化仍然成立(它都没有):**
- 跨 agent 共享黑板 + digest 广播注入(它的 supervisor→worker 是单向、worker 之间互盲,与 kiro-team 同病)。
- 完成校验的 block 反馈环(它只有 stagnation 拦截,没有"判据不达标不许停")。
- 多运行时联邦(它锁定单一内嵌 loop;APEX 跨 Kiro/Claude/Hermes)。
- 机器可读的全生命周期事件流 + 审计留痕(它只有内部 logger)。
- 单层委派 vs APEX 的依赖图调度(它靠"coordinator 不可被委派"硬限单层)。

**一句话定位:** Agent System 是"把多 agent 压进一个 IDE 内 loop 的精致单机版";APEX 是"跨运行时、带共享记忆与强校验的编排层"。它的工具式委派、预算衰减、停滞检测、风险分级四项可直接吸收为 APEX 的轻量档与安全闸,但其架构上限(单层、互盲、单运行时)正是 APEX 要超越的地方。
