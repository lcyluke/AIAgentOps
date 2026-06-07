# APEX 完整技术与功能落地开发方案 v1.0

> 多 Agent 编排器,面向 Kiro CLI / Claude Code / Hermes Agent 的统一调度、监控、协作与告警平台。
> 本方案是可直接开工的实现规格:模块边界、数据模型、协议、接口签名、目录结构、里程碑与验收标准齐备。设计依据见配套《APEX-技术设计方案-v0.2》(竞品对比与逆向分析)。

---

## 0. 文档约定

- **MUST / SHOULD / MAY**:RFC 2119 语义,标注实现优先级。
- 代码签名以 Python 3.12(核心)+ TypeScript(MCP/扩展面)双栈表达;Python 为主实现语言(配 pipx 分发),TS 仅用于 apex-ops MCP server 与未来 VS Code 面。
- 所有"运行时"指底层编码 agent:Kiro CLI、Claude Code、Hermes Agent。
- 所有"实例"指一个被命名、被调度的 agent 会话。

---

## 1. 目标与范围

### 1.1 一句话定位
APEX 是本地常驻的跨运行时多 agent 编排层:它给每个 agent 会话命名与生命周期管理(方案 C 外部注册表),做任务调度与定时巡检,提供跨 agent 共享黑板避免信息不对称与重复劳动,并用机制级校验保证"完成"可信。

### 1.2 必须做到(MUST,M1–M2)
1. 以命名实例为单位启动/监控/终止 Kiro agent(headless 起步,ACP 进阶)。
2. name ↔ session_id 创建时绑定,程序可查,跨运行模式统一(方案 C)。
3. 全生命周期事件流(注册/心跳/完成/认领/阻断/失败)机器可读、落库留痕。
4. 共享黑板:stop 事件提炼结论写入,userPromptSubmit 每轮注入 digest。
5. 任务认领表防重复 + 完成校验 block 反馈环。

### 1.3 应该做到(SHOULD,M3–M4)
6. apex-ops MCP server + apex-installer skill:Kiro 对话内安装与管理。
7. ClaudeCodeAdapter、HermesAdapter:多运行时联邦 + 企业微信告警。
8. 工具风险三级分级 + 停滞检测 + 编辑冲突保护(吸收自 Agent System 逆向)。

### 1.4 暂不做(Out of Scope,v1.0)
- 云端多机分布式调度(单机 launchd 常驻为限)。
- 自研模型推理(模型能力全部委托底层运行时)。
- Web 可视化大盘(v1.0 用 TUI + CLI;Web 面留 v2)。

---

## 2. 系统架构

### 2.1 分层

```
┌──────────────────────── 交互面 Surfaces ────────────────────────┐
│ apex CLI         apex-ops MCP        apex-installer skill        │
│ (Typer)          (TS, stdio)         (Kiro 对话内引导安装)         │
│ Hermes 通道(告警→企业微信 / cron 反向触发)                       │
└──────────┬──────────────────────────────────────────────────────┘
           │ Command Protocol (§5.2)  ── unix socket / 127.0.0.1
┌──────────▼──────────────── APEX Core (apexd 守护进程) ───────────┐
│ ┌─────────┐ ┌──────────┐ ┌───────────┐ ┌────────┐ ┌──────────┐ │
│ │Registry │ │Scheduler │ │Blackboard │ │Claims  │ │Auditor   │ │
│ │实例注册表 │ │队列/依赖图 │ │结论/digest │ │认领/锁  │ │完成校验   │ │
│ │状态机    │ │/cron巡检  │ │/脱敏      │ │        │ │block决策  │ │
│ └─────────┘ └──────────┘ └───────────┘ └────────┘ └──────────┘ │
│ ┌──────────────┐ ┌──────────────┐ ┌────────────────────────┐  │
│ │StagnationGuard│ │ToolRiskGate  │ │EventBus (SQLite WAL)    │  │
│ │防原地打转      │ │三级风险闸     │ │全事件 append-only 留痕   │  │
│ └──────────────┘ └──────────────┘ └────────────────────────┘  │
└──────────┬──────────────────────────────────────────────────────┘
           │ RuntimeAdapter 接口 (§4) ── 唯一 SDK/CLI 边界
┌──────────▼──────────────────────────────────────────────────────┐
│ KiroAdapter        ClaudeCodeAdapter        HermesAdapter        │
│ (ACP/headless,     (claude -p json /        (gateway REST /      │
│  tmux 隔离)         Agent SDK, --resume)      hermes mcp serve)    │
└──────────┬──────────────────────────────────────────────────────┘
           │ Event Protocol (§5.1) ── apex-hook.sh 上报
   Kiro hooks: agentSpawn / userPromptSubmit / preToolUse / postToolUse / stop
```

### 2.2 进程与隔离模型
- `apexd`:常驻守护(macOS launchd / Linux systemd),持有全部 Core 组件与单文件 SQLite(WAL)。SHOULD 与 Hermes gateway 同机共存,互不抢占端口。
- 每个实例:由对应 Adapter 启动在独立 tmux 会话,会话名 = 实例名(`{agent}-{taskid}`),人可 `tmux attach` 介入(吸收 CAO)。
- IPC:Surfaces ↔ apexd 走 unix domain socket(默认 `~/.apex/apexd.sock`),回退 127.0.0.1:8717 REST;hook shim → apexd 走同一 socket。
- 存储:`~/.apex/apex.db`(SQLite,WAL),崩溃恢复依赖 checkpoint 表。

### 2.3 关键不变量(Invariants,实现必须维持)
- **INV-1 单边界**:除 Adapter 实现文件外,任何模块 MUST NOT 直接拼底层 CLI 命令或 import 运行时 SDK。
- **INV-2 命名唯一**:Registry 中 `name` 与 `session_id` 一一映射,创建即绑定,生命周期内不变。
- **INV-3 黑板单写**:Blackboard 唯一写入者是 apexd(经 Auditor 校验);agent 只能经 stop 事件"申请写入"。
- **INV-4 事件不可变**:EventBus 仅 append,不更新不删除;状态由事件投影得出。
- **INV-5 脱敏前置**:任何落库或注入上下文的文本 MUST 先过脱敏器(§8.3)。

---

## 3. 数据模型(SQLite Schema)

> 单库 `apex.db`,WAL 模式。建表语句即实现契约。

```sql
-- 实例注册表(方案 C 命名的载体)
CREATE TABLE IF NOT EXISTS instances (
  name          TEXT PRIMARY KEY,          -- {agent}-{taskid},人类可读主键
  session_id    TEXT UNIQUE NOT NULL,      -- 运行时返回的会话 UUID
  agent         TEXT NOT NULL,             -- 角色名(= .kiro/agents/<agent>.json 文件名)
  runtime       TEXT NOT NULL,             -- kiro | claude | hermes
  cwd           TEXT NOT NULL,
  tmux_session  TEXT,                      -- tmux 会话名(= name)
  state         TEXT NOT NULL,             -- idle|running|waiting_approval|done|failed|disposed
  parent_name   TEXT,                      -- 委派链:父实例(单层或依赖图)
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

-- 事件总线(append-only,全生命周期留痕,审计与状态投影双用途)
CREATE TABLE IF NOT EXISTS events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            TEXT NOT NULL,
  type          TEXT NOT NULL,             -- 见 §5.1 ApexEvent.type
  session_id    TEXT,
  instance_name TEXT,
  payload_json  TEXT NOT NULL,             -- 已脱敏的结构化负载
  INDEX_NOTE    TEXT                       -- 占位;实际索引见下
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_type    ON events(type, ts);

-- 共享黑板:已校验的结论(广播注入的来源)
CREATE TABLE IF NOT EXISTS findings (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  author        TEXT NOT NULL,             -- 产出该结论的实例名
  task_id       TEXT,
  category      TEXT NOT NULL,             -- fact|decision|verification|risk
  summary       TEXT NOT NULL,             -- 脱敏后的结论摘要(≤500 字符)
  evidence      TEXT,                      -- 证据指针(文件路径/命令,脱敏)
  verified      INTEGER NOT NULL DEFAULT 0,-- Auditor 校验通过才置 1
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_findings_task ON findings(task_id, created_at);

-- 任务认领表(乐观锁,防重复)
CREATE TABLE IF NOT EXISTS claims (
  task_id       TEXT PRIMARY KEY,
  claimed_by    TEXT NOT NULL,             -- 实例名
  status        TEXT NOT NULL,             -- claimed|done|released
  criteria_json TEXT,                      -- 完成判据(Auditor 据此校验)
  claimed_at    TEXT NOT NULL,
  done_at       TEXT
);

-- agent 循环检查点(崩溃续跑,吸收自 Agent System 的 agent_loop_checkpoints)
CREATE TABLE IF NOT EXISTS checkpoints (
  session_id    TEXT NOT NULL,
  step          INTEGER NOT NULL,
  max_steps     INTEGER NOT NULL,
  messages_json TEXT NOT NULL,             -- 脱敏后的消息快照
  usage_json    TEXT,
  created_at    TEXT NOT NULL,
  PRIMARY KEY (session_id, step)
);

-- 成本与用量归集(Claude json 输出的 total_cost_usd 等)
CREATE TABLE IF NOT EXISTS usage (
  session_id    TEXT NOT NULL,
  ts            TEXT NOT NULL,
  model         TEXT,
  input_tokens  INTEGER,
  output_tokens INTEGER,
  cost_usd      REAL,
  PRIMARY KEY (session_id, ts)
);
```

状态投影规则:实例的 `state` 由最新相关事件决定(`instance.registered`→idle,`turn.heartbeat`→running,`turn.completed`→idle 或 done,`instance.failed`→failed),Registry 维护投影缓存,events 为真相源(INV-4)。

---

## 4. RuntimeAdapter 接口(INV-1 唯一边界)

所有运行时差异收敛在此层。Core 只依赖抽象接口,换运行时 = 加一个实现文件。

### 4.1 接口定义(Python Protocol)

```python
# apex/adapters/base.py
from typing import Protocol, Literal, Optional
from dataclasses import dataclass

InstanceState = Literal["idle","running","waiting_approval","done","failed","disposed"]

@dataclass
class SessionHandle:
    name: str            # 实例名 {agent}-{taskid}
    session_id: str      # 运行时会话 UUID
    runtime: str         # kiro|claude|hermes
    tmux_session: str    # tmux 会话名

@dataclass
class InstanceStatus:
    state: InstanceState
    last_event_ts: str
    detail: Optional[str] = None

@dataclass
class SpawnSpec:
    agent: str                  # 角色名
    instance: str               # 实例名(方案 C 已生成)
    brief: str                  # 初始任务描述
    cwd: str
    env: dict[str, str]         # 仅注入该角色所需凭据(§8.2)
    allowed_tools: list[str]    # 角色工具白名单(§7.3)
    max_steps: int

class RuntimeAdapter(Protocol):
    name: str  # "kiro" | "claude" | "hermes"

    def spawn(self, spec: SpawnSpec) -> SessionHandle:
        """启动实例(tmux 隔离),返回绑定 session_id 的句柄。MUST 在返回前确保 session_id 已确定。"""
        ...
    def prompt(self, h: SessionHandle, text: str) -> None:
        """空闲时发送新一轮提示(phi 动词集)。"""
        ...
    def steer(self, h: SessionHandle, text: str) -> None:
        """运行中注入,不开新轮(对应 Hermes busy-input steer)。"""
        ...
    def abort(self, h: SessionHandle) -> None:
        """取消当前轮。"""
        ...
    def status(self, h: SessionHandle) -> InstanceStatus:
        ...
    def dispose(self, h: SessionHandle) -> None:
        """终止实例并清理 tmux 会话(MUST 幂等)。"""
        ...
```

### 4.2 KiroAdapter(M1 headless,M3 升 ACP)

**headless 路径(M1):**
- spawn:`tmux new-session -d -s <name>`,会话内 `cd <cwd> && kiro-cli chat --no-interactive --agent <agent> --trust-tools=<allowed> "<brief>"`。session_id 由 agentSpawn hook 上报回填(spawn 阻塞等待该事件或超时)。
- prompt/steer:headless 无多轮,prompt = 新起一次 headless 调用并 `--resume <session_id>`(若运行时支持);steer 在 headless 下降级为 queue(记录待下一轮)。
- status:读 EventBus 投影,不轮询进程。
- 实测约束(写入 doctor,来自 kiro-team 逆向):agent JSON MUST 平铺 `.kiro/agents/`;trustedAgents 名字 = 文件名;subagent 工具静默不可用需探测。

**ACP 路径(M3):**
- spawn:`kiro-cli acp` 起 JSON-RPC 子进程,`initialize` → `session/new`(拿 session_id,真正的创建即绑定)→ 持有句柄。
- prompt/steer/abort 直接映射 ACP 方法,流式事件转为 ApexEvent。
- 这是与 phi 动词集语义最贴合的路径,M3 设为 Kiro 主通道。

### 4.3 ClaudeCodeAdapter(M4)
- spawn:`claude -p "<brief>" --output-format json --allowedTools <...> --max-turns <n>`,从 json 取 `session_id`/`total_cost_usd`/`subtype`。
- prompt:`claude -p "<text>" --resume <session_id> --output-format json`。
- 认证按企业 SSO(`claude auth login --sso`),凭据 `~/.claude`;服务化场景与企业管理员确认 token 刷新策略。
- usage:把 json 的 cost/tokens 写入 usage 表。

### 4.4 HermesAdapter(M4,告警 + 反向触发)
- 不用于跑编码 agent,而是 APEX 的人机通道:`apex_alert(text, level)` → 调 Hermes gateway 把消息推企业微信(复用已建 WeCom 链路)。
- 反向:Hermes cron → 调 `apex query status` 实现双向心跳。
- 实现:HTTP 调本地 Hermes gateway,或经 `hermes mcp serve` 暴露的端点。

---

## 5. 协议定义(类型化 discriminated union,继承 phi)

### 5.1 Event Protocol(上行:hook shim / Adapter → Core)

```typescript
type ApexEvent =
  | { type: "instance.registered"; agent: string; instance: string;
      session_id: string; runtime: "kiro"|"claude"|"hermes"; cwd: string; ts: string }
  | { type: "turn.heartbeat";   session_id: string; tool: string; ok: boolean; ts: string }
  | { type: "turn.completed";   session_id: string; response_digest: string;
      verdict: "pass"|"blocked"; reason?: string; ts: string }
  | { type: "task.claimed";     session_id: string; task_id: string; ts: string }
  | { type: "task.done";        session_id: string; task_id: string; evidence: string; ts: string }
  | { type: "guard.blocked";    session_id: string; tool: string; rule: string; ts: string }
  | { type: "instance.failed";  session_id: string; error: string; ts: string };
```

Kiro 侧由统一 shim `apex-hook.sh <verb> <agent>` 把 hook STDIN 的 JSON 包装为上述事件 POST 到 apexd。hook 事件本身无 agent 名,由 shim 第二参数补齐(每个 agent 的 hooks 配置写死自己的名字)。

### 5.2 Command Protocol(下行:Surfaces → Core)

```typescript
type ApexCommand =
  | { type: "spawn";  agent: string; task: TaskSpec }
  | { type: "assign"; instance: string; task: TaskSpec }
  | { type: "steer";  instance: string; text: string }
  | { type: "abort";  instance: string }
  | { type: "dispose"; instance: string }
  | { type: "alert";  text: string; level: "info"|"warn"|"error" }
  | { type: "query";  what: "status"|"blackboard"|"claims"|"events"|"usage"; filter?: object };

interface TaskSpec {
  task_id: string;
  description: string;
  criteria?: CompletionCriterion[];   // Auditor 据此校验完成(§6.5)
  context_refs?: string[];            // 文件路径或黑板条目 id
  max_steps?: number;
  depends_on?: string[];              // 依赖图:前置 task_id(Scheduler 排序)
}

interface CompletionCriterion {
  kind: "shell_exit_zero" | "file_exists" | "grep_absent" | "grep_present";
  cmd?: string;        // shell_exit_zero: 可执行断言(如 "pytest -q")
  path?: string;       // file_exists
  pattern?: string;    // grep_*
}
```

CLI、apex-ops MCP、Hermes 三面共用同一指令集,仅传输层不同。所有指令经 socket 单通道路由(对应 phi 的"唯一 IPC 通道")。

### 5.3 协议版本化
事件与指令负载 MUST 带 `v` 字段(当前 `v:1`);apexd 拒绝未知主版本,记 `guard.blocked`。

---

## 6. 核心模块功能规格

### 6.1 Registry(实例注册表 + 状态机)
职责:方案 C 命名的载体,维护 name↔session_id 映射与状态投影。
- `register(spec, session_id) -> name`:写 instances 表,发 `instance.registered`。命名规则 `{agent}-{taskid}`;冲突时追加 `-2`、`-3`。
- `resolve(name) -> SessionHandle` / `reverse(session_id) -> name`:双向查。
- `project_state(session_id)`:从 events 投影最新状态(INV-4)。
- `list(filter)`:供 `apex status` 与 crew 视图。
- 实例名同时用作 tmux 会话名,保证 TUI 与注册表一致(人工 `/spawn --name` 时沿用同名)。

### 6.2 Scheduler(队列 + 依赖图 + cron 巡检)
职责:把 TaskSpec 排成可执行序,管理并发与定时。
- 依赖图:`depends_on` 构成 DAG,拓扑排序;无依赖的任务并行(并发上限 `max_parallel`,默认 4)。
- 派发:取就绪任务 → 选/起实例 → Adapter.spawn 或 assign。
- cron 巡检:`cron.d/*.yaml` 定义周期任务(如"每小时检查服务健康"),到点生成一次性 headless 任务;异常摘要经 HermesAdapter 推企业微信。
- 轻量委派档(吸收 Agent System):提供 `delegate_to_worker` 风格的工具档给"单层、无需黑板"的简单场景,绕过完整 DAG,降低开销。嵌套步数预算 `子 = clamp(floor(父×0.75), 1, 24)`。

### 6.3 Blackboard(共享上下文,解决信息不对称)
职责:三层共享中的动态层(INV-3 单写)。
- 写入:`turn.completed` 事件 → Auditor 提炼 → 校验通过写 findings(verified=1)。
- 读出/注入:userPromptSubmit hook 调 `digest(exclude_author, max_tokens=800)`,产出"其他实例已确认结论 + 进行中认领"摘要,经脱敏后由 hook STDOUT 注入目标实例上下文。
- digest 策略:按 task_id 相关性 + 时间倒序取条目,截断到 token 预算内(对应 Kiro 上下文 75% 上限约束,留足余量)。
- 静态层(steering/resources)与大体量层(knowledge base)由 `apex init` 铺设,不经 Blackboard 运行时管理。

### 6.4 Claims(认领表,防重复)
- `claim(task_id, by, criteria)`:乐观锁,已被认领则拒绝并返回当前 holder。
- `digest` 注入时附带进行中认领,prompt 约定"CLAIMS 中已认领任务不要重复做"。
- 强约束:preToolUse 闸门可按角色拦截越权工具(如非 tester 实例跑 pytest → exit 2 回话"测试归 test-runner,结果见黑板")。
- `release(task_id)` / `complete(task_id, evidence)`:完成由 Auditor 校验后置 done。

### 6.5 Auditor(完成校验,block 反馈环)
职责:保证"完成"可信,这是去重的前提(结论可信他人才敢不重查)。
- 触发:stop 事件携带 `task_id` 时,按 claims.criteria_json 逐条校验 CompletionCriterion。
- 校验执行:`shell_exit_zero` 跑断言命令查 exit;`file_exists`/`grep_*` 查文件。在实例 cwd 下执行,只读不改。
- 不达标:返回 `{"decision":"block","reason":"<未通过的判据>"}` 给 stop hook,reason 作为新用户消息让 agent 续跑。
- 达标:claims 置 done,写 findings(verified=1),发 `task.done`。
- 防滥用:同一 task 的 block 次数上限(默认 3),超限标 failed 并告警,避免无限循环烧预算。

### 6.6 StagnationGuard(防原地打转,吸收自 Agent System 逆向)
- `record(tool, args, success)`:记录工具调用结果。
- `is_repeated_failure(tool, args) -> bool`:同一 (tool,args) 失败 ≥ 阈值(默认 2)即判定停滞,preToolUse 阶段拦截。
- 连续失败步数惩罚:`effective_max_steps = max_steps - penalty`,加速触底而非空转。
- 与 Auditor 互补:Auditor 管"做没做对",StagnationGuard 管"有没有卡死"。

### 6.7 ToolRiskGate(三级风险闸,吸收自 Agent System 逆向)
风险分级(MUST 内置,集中下发,agent 配置只引用):
| 等级 | 工具 | 默认策略 |
|---|---|---|
| READ_ONLY | read_file / read_files / list_files | 自动放行 |
| STATE_CHANGING | apply_edit / create_file / search_web | `auto_approve_state_changing` 开关控制 |
| DESTRUCTIVE | delete_file / run_shell | 始终需人工批准 |
- 实现为 preToolUse hook 内的规则表 + 角色白名单(§7.3)双重判定;违规 exit 2 并发 `guard.blocked`。
- 危险命令模式(`rm -rf` / `curl|sh` / `git push --force`)硬拦截,等同 Hermes Tirith。

### 6.8 EventBus(事件总线 + 审计留痕)
- `publish(event)`:脱敏 → 写 events 表(append-only)→ 通知订阅者(Registry 投影、cron 触发器)。
- `query(filter)`:供 `apex query events` 与审计("谁、何时、对哪个仓库、做了什么、花了多少")。
- 全事件不可变(INV-4),满足合规追溯;usage 表归集成本。

---

## 7. 项目初始化与 agent 角色(承接既有设计)

### 7.1 init 铺设的目录
```
<project>/.kiro/
├── agents/                  # 平铺!子目录不被 Kiro 解析(kiro-team 实测)
│   ├── architect.json
│   ├── backend-dev.json
│   ├── test-runner.json
│   └── security-auditor.json
├── steering/                # 静态共享层(所有角色 resources 显式引用)
│   ├── product.md  tech.md  conventions.md
└── skills/
    └── apex-installer/SKILL.md   # 对话内安装引导(§9.2)
~/.apex/                     # apexd 私有,不入项目 git
├── apex.db   apexd.sock   cron.d/   hooks/apex-hook.sh
```
注意:`.kiro/blackboard/` 不再放项目内(避免 agent 互改、避免敏感结论入 git);黑板改由 apexd 持有于 `~/.apex/apex.db`,仅经 hook 注入(强化 INV-3 + §8.4)。

### 7.2 角色 JSON 模板(backend-dev.json)
```json
{
  "description": "Backend implementation specialist",
  "prompt": "你是后端实现专家。只负责实现,不做安全审计(那是 security-auditor 的职责)。开始任何检查前,先看上下文里的 [BLACKBOARD] 段,已有结论不要重复验证。",
  "tools": ["read", "write", "shell"],
  "toolsSettings": { "fs_write": { "allowedPaths": ["src/**", "tests/**"] } },
  "resources": ["file://.kiro/steering/**/*.md", "file://README.md"],
  "hooks": {
    "agentSpawn":       [{ "command": "~/.apex/hooks/apex-hook.sh register backend-dev" }],
    "userPromptSubmit": [{ "command": "~/.apex/hooks/apex-hook.sh inject backend-dev", "cache_ttl_seconds": 0 }],
    "preToolUse":       [{ "matcher": "*", "command": "~/.apex/hooks/apex-hook.sh guard backend-dev" }],
    "postToolUse":      [{ "matcher": "*", "command": "~/.apex/hooks/apex-hook.sh heartbeat backend-dev" }],
    "stop":             [{ "command": "~/.apex/hooks/apex-hook.sh report backend-dev", "timeout_ms": 15000 }]
  }
}
```

### 7.3 角色工具白名单(吸收 Agent System 的 ROLE_TOOL_WHITELIST)
| 角色 | 允许工具 |
|---|---|
| architect | read, list, grep(只读规划) |
| backend-dev / coder | read, list, write, shell(受 allowedPaths 限制) |
| test-runner / tester | read, list, write(仅 tests/**), shell(仅测试命令) |
| security-auditor / reviewer | read, list, grep(纯只读) |
角色边界三道防线:prompt 声明(软)+ toolsSettings.allowedPaths(中)+ preToolUse 白名单判定(硬)。

### 7.4 apex-hook.sh shim 契约
单文件多分支,从 STDIN 读 hook JSON,按第一参数 verb 分发:
- `register <agent>`:包装 agentSpawn → `instance.registered`(回填 session_id,解除 spawn 阻塞)。
- `inject <agent>`:调 apexd 取 blackboard digest + claims,STDOUT 输出注入文本(exit 0)。
- `guard <agent>`:ToolRiskGate + StagnationGuard 判定,违规 exit 2 + STDERR 原因。
- `heartbeat <agent>`:postToolUse → `turn.heartbeat`。
- `report <agent>`:stop → 触发 Auditor 校验,按结果 STDOUT 输出 block decision 或放行。
所有分支经同一 socket 与 apexd 通信;agent 名由第二参数补齐(§5.1)。

---

## 8. 安全与合规层(DPO 视角强化)

### 8.1 威胁模型(简版)
- 编码 agent 拥有文件/shell 权限,等价于受限本地执行体;最大风险是越权写、危险命令、敏感数据外泄、凭据泄露、委派无限递归。
- "对话内安装"= 让 agent 跑安装脚本,等价 agent 化的 `curl|sh`,单列治理(§9.3)。

### 8.2 凭据管理(三层,继承 phi EnvManager 分域)
1. 机器层:凭据存 macOS Keychain(或加密 .env),apexd 启动解锁一次。
2. 角色层:每个 agent JSON 声明所需凭据名单;Adapter.spawn 仅注入名单内变量到子进程 env,不多给。
3. 禁区:Blackboard digest 与 EventBus 落库前过脱敏器;审计日志只记"注入了哪些凭据名",不记值。

### 8.3 脱敏器(INV-5,落库/注入前置)
- 双道:正则(API key / token / 私钥 / 邮箱 / 手机号 / 路径中的用户名)+ 轻量实体识别。
- 命中替换为不可逆指纹 `«REDACTED:type:hint_hash»`(借鉴 Agent System 的 hintHash 审计模式:可审计、不可还原)。
- 单元测试 MUST 覆盖常见凭据格式;脱敏失败时 fail-closed(宁可不注入)。

### 8.4 数据边界
- `~/.apex/apex.db` 含黑板与事件,默认不入任何 git;`apex init` 自动写 `.gitignore` 排除 `.apex/`。
- 黑板唯一写入者 apexd(INV-3);agent 不可读写 `.kiro/agents/**`(各角色 allowedPaths 排除之),防互改角色定义(kiro-team 风险点)。

### 8.5 委派安全
- 单层限制:轻量委派档禁止委派给 coordinator 类角色(吸收 Agent System 的防递归规则)。
- DAG 档:Scheduler 检测环并拒绝;委派深度上限可配(默认 3)。

### 8.6 审计与可追溯
- EventBus append-only 提供完整链路;`apex export-audit --since <date>` 导出脱敏审计报告(供合规留存)。
- 成本归集:usage 表 + `apex report cost` 出按实例/项目/模型的花费表。

---

## 9. 安装与分发

### 9.1 CLI 安装(标准路径)
```bash
pipx install apex-orchestrator          # 隔离环境全局命令;或 brew tap
apex init [--project DIR]               # 铺设 agents(平铺)/steering/skills/hooks,写 .gitignore
apex doctor                             # 见 §9.4 检查清单
apex up                                 # 安装并启动 apexd(launchd/systemd)
apex spawn <agent> "<task>"             # 起命名实例
apex status [--watch]                   # 实例状态总览(TUI)
```

### 9.2 Kiro 对话内安装(skill 引导 + ops-mcp 接管)
`apex-installer` skill(YAML frontmatter 的 description 写明触发语义),随团队仓库 `.kiro/skills/` 分发,clone 即有;个人机一条命令拷入 `~/.kiro/skills/`。用户在 Kiro 对话框说"装一下 APEX / 初始化多 agent 编排",skill 命中后按序执行:
```
1. 检查 pipx --version,无则提示先装,停止
2. pipx install apex-orchestrator==<PINNED_VERSION>
3. 校验 apex --version 与 PINNED_VERSION 一致 + sha256 比对发布清单
4. apex init --project . --non-interactive
5. apex doctor,汇总结果
6. 提示重启 kiro-cli 以加载 apex-ops MCP server
```
装毕后,对话式管理全走 apex-ops MCP 工具(§9.5)。Skill 负责"从无到有",MCP 负责"从有到用"。

### 9.3 对话内安装的安全红线(MUST)
- skill 钉死版本号 + 校验 sha256;绝不让 agent fetch 任意 URL 再执行。
- 安装步骤不授予 trust-all;shell 步骤保留人工审批一次确认。
- 团队分发首选 git 内置 `.kiro/skills/`,供应链可控,优于公网拉取。

### 9.4 doctor 检查清单(吸收 kiro-team 实测)
- kiro-cli / claude / hermes 版本与可执行性(`which`)。
- `.kiro/agents/*.json` 平铺(无子目录);trustedAgents 名字 == 文件名。
- subagent 工具可用性探测(部分工具作为 subagent 静默不可用)。
- hook shim 可执行 + apexd socket 可达。
- launchd/systemd PATH 新鲜度(macOS 静态 plist 坑:装新工具后需重跑 install)。
- `which claude` 可解析(npm 全局命令,服务子进程需能找到)。

### 9.5 apex-ops MCP server(对话式管理,吸收 CAO ops-mcp)
TS 实现,stdio transport,`apex init` 写入项目 mcp.json。暴露工具:
```
apex_status()                 -> 各实例状态 + 最近 stop 事件
apex_spawn(agent, task)       -> 方案 C 命名起实例
apex_assign(instance, task)   -> 派单
apex_blackboard(query)        -> 查共享黑板
apex_claims()                 -> 查认领表
apex_alert(text, level)       -> 经 Hermes 推企业微信
```
每个工具内部转成 ApexCommand 经 socket 发 apexd;即"主 agent 从自己的聊天循环里调编排器"。

---

## 10. 目录结构(可开工骨架)

```
apex-orchestrator/
├── pyproject.toml                  # pipx 入口 apex=apex.cli:app
├── README.md  AGENTS.md            # AGENTS.md 含"改码必同步改文档"纪律(借 phi)
├── apex/
│   ├── cli.py                      # Typer:init/up/spawn/status/doctor/query/...
│   ├── daemon.py                   # apexd 主进程:socket server + 组件装配
│   ├── protocol.py                 # ApexEvent / ApexCommand / TaskSpec(pydantic,v 版本化)
│   ├── core/
│   │   ├── registry.py  scheduler.py  blackboard.py
│   │   ├── claims.py    auditor.py    stagnation.py
│   │   ├── tool_risk.py event_bus.py
│   ├── adapters/
│   │   ├── base.py  kiro.py  claude.py  hermes.py
│   ├── security/
│   │   ├── redactor.py  creds.py
│   ├── storage/
│   │   ├── db.py  schema.sql  migrations/
│   ├── init/                       # 模板:agents/*.json, steering/*.md, SKILL.md, apex-hook.sh
│   └── service/                    # launchd plist / systemd unit 生成器
├── ops-mcp/                        # TS:apex-ops MCP server
│   ├── package.json  src/server.ts
├── tests/
│   ├── unit/   integration/   e2e/
└── scripts/
    └── release.mjs                 # 一键版本判定+changelog+tag+CI 发布(借 phi)
```

---

## 11. 里程碑与验收标准

| 里程碑 | 交付 | 验收(可演示) |
|---|---|---|
| **M1 可用** | KiroAdapter(headless)+ Registry + EventBus + apex-hook.sh + CLI(init/spawn/status/doctor)+ DB | `apex spawn coder "修 auth bug"` 起命名实例;`apex status` 见状态;事件落库;doctor 全绿 |
| **M2 协作** | Blackboard + Claims + Auditor(block 环)+ StagnationGuard + ToolRiskGate + userPromptSubmit 注入 | 两实例并行,A 的结论经 digest 进 B 上下文;B 不重复 A 已验证的检查;判据不达标被 block 续跑;越权工具被拦 |
| **M3 对话面** | apex-ops MCP + apex-installer skill + ACP 升为 Kiro 主通道 | Kiro 对话框"装 APEX"完成安装;对话内 `apex_spawn`/`apex_status` 生效 |
| **M4 联邦** | ClaudeCodeAdapter + HermesAdapter(企业微信告警 + 双向 cron)+ 成本归集 | 同一编排同时驱动 Kiro 与 Claude 实例;巡检异常推企业微信;`apex report cost` 出表 |
| **M5 工程化** | release 自动化 + AGENTS.md 文档纪律 + 脱敏器测试覆盖 + TUI 看板 | 一键发版;脱敏单测通过;TUI 实时看板 |

每个里程碑 MUST 附:单元测试(核心模块)、集成测试(Adapter↔Core)、一个端到端冒烟脚本。

---

## 12. 测试策略

- **单元**:redactor(各类凭据格式)、auditor(各 CompletionCriterion)、stagnation(阈值/惩罚)、tool_risk(三级判定)、registry 状态投影。
- **集成**:KiroAdapter 用桩 kiro-cli(回放 hook 事件)验证 spawn→register→heartbeat→stop 全链;socket 协议往返。
- **E2E**:真实 kiro-cli 在样例仓跑"收消息→调 agent→黑板共享→去重→完成校验"全流程冒烟。
- **混沌**:杀 apexd 验证 checkpoint 续跑;并发改同一文件验证 EditConflict;block 超限验证降级告警。

---

## 13. 风险与缓解

| 风险 | 缓解 |
|---|---|
| Kiro headless 无真多轮,steer 语义弱 | M3 尽快上 ACP;M1 期 steer 降级 queue,文档明示 |
| SSO token 过期致无人值守中断 | 与 Claude Code 企业管理员确认 service 凭据/刷新策略(已识别动作项) |
| Agent SDK 计费变更(2026-06-15 起独立额度) | usage 表归集 + 预算上限 + 超限告警 |
| sql.js 多进程不安全(Agent System 踩过) | 单 apexd 持库,Adapter 不直连 DB;同库单写 |
| 对话内安装被滥用为任意执行 | §9.3 红线:钉版本/校验和/不 fetch 任意 URL/保留人工审批 |
| 黑板注入超上下文预算 | digest token 预算 + 75% 上限留余;大材料走 knowledge base |

---

## 14. 与既有资产的衔接

- 复用上一轮已落地的 Hermes + 企业微信 WeCom 链路作为 APEX 告警通道(HermesAdapter)。
- 角色身份层对齐你 AgentCorp OS 的 SOUL.md / SKILL.md / Hermes Profile:APEX 的 agent JSON 即角色身份载体,可由现有 profile 生成。
- 竞品坐标与逆向结论见配套《APEX-技术设计方案-v0.2》附录 A(Agent System 0.12.99 逐函数对照),四项被吸收能力(工具式委派轻量档、嵌套预算衰减、停滞检测、风险分级)已并入本方案 §6.2/§6.6/§6.7。

— 文档结束 —
