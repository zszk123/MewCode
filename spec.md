# MewCode Loop Engineering + Harness Engineering 改造规格书

## 背景

MewCode 当前是一个单 Agent 迭代式 AI 编程助手。Agent 的核心循环是：构建提示词 → 流式获取 LLM 响应 → 执行工具调用 → 追加结果 → 重复。这套机制能完成单轮对话任务，但面对复杂工程任务时存在三个根本瓶颈：

1. **编排能力弱**：多 Agent 协作依赖手工编写的 coordinator 提示词，缺乏确定性控制流（条件分支、循环、fan-out/fan-in）。每次多步骤任务都需要 LLM 自行判断何时并行、何时串行、何时停止，消耗 token 且不可靠。
2. **自主性不足**：Agent 无法自我调度（定时任务、轮询等待）、无法感知 token 预算做动态调整、无法在中断后从断点恢复继续执行。
3. **Harness 被动**：上下文管理用固定阈值、权限规则需手动编辑 YAML、无可观测性回放能力、Agent 无法在运行时调整自身行为约束。

本次改造的核心思路：**让 Harness（约束框架）从静态配置变成可被 Agent 感知和操控的活系统，让 Loop（执行循环）从单 Agent 迭代变成可编排、可恢复、可自调度的多层级执行引擎。**

## 目标用户

- 使用 MewCode 进行日常开发的个人开发者
- 需要执行长时间、多步骤自动化任务（代码迁移、批量重构、审计扫描）的高级用户
- 希望通过 Python 脚本自定义 Agent 编排逻辑的 Power User

## 能力清单

### A. Workflow 编排引擎（Loop Engineering 核心）

1. **Python DSL 编排**：用户可在 `.mewcode/workflows/` 目录下编写 Python 异步函数，使用框架提供的 `agent()`/`pipeline()`/`parallel()`/`phase()` 原语编排多 Agent 协作。
2. **Pipeline 模式**：数据项流经多个处理阶段，阶段间无同步屏障——项 A 进入阶段 3 时项 B 可仍在阶段 1，最大化并行度。
3. **Parallel 模式**：并发执行多个独立任务，等待全部完成后返回结果集，失败的任务不阻塞其他任务。
4. **Structured Output**：每个 `agent()` 调用可声明 Pydantic 输出 Schema——引擎强制校验，校验失败自动重试。
5. **Token 预算追踪**：Workflow 执行期间实时追踪 token 消耗，支持 "最多消耗 X token" 的硬上限约束。
6. **Loop-Until 模式**：内建三种迭代终止条件——达到目标数量（count）、token 预算耗尽（budget）、连续 N 轮无新发现（dry）。
7. **Phase 进度分组**：可将 agent 调用分组到命名 phase 下，前端以分组树展示执行进度。

### B. Workflow 持久化与恢复

8. **Journal 日志**：每次 `agent()` 调用自动写入追加式日志，记录（prompt 哈希、参数哈希、结果、时间戳）。
9. **断点恢复**：Workflow 中断（Ctrl+C、崩溃、API 故障）后，重新执行同一 workflow 时自动从日志命中已完成的 agent 调用，仅执行未完成部分。
10. **幂等保证**：通过 (prompt, opts) 哈希匹配实现缓存命中，要求 workflow 函数对相同输入产生相同的 agent 调用序列。

### C. 定时调度系统

11. **Cron 表达式调度**：支持标准 5 字段 cron 表达式，按用户本地时区触发任务。
12. **一次性与周期性任务**：支持单次触发（到时执行后自动删除）和周期触发（按 cron 表达式反复执行）。
13. **持久化存储**：调度任务写入磁盘文件，重启后自动恢复。
14. **任务触发注入**：到达触发时间时，任务以系统消息形式注入 Agent 对话。

### D. 自步进调度

15. **动态 Wakeup**：Agent 可在 Loop 中声明 "N 秒后唤醒我"，用于等待外部条件（CI 完成、部署就绪）。
16. **缓存感知间隔**：Wakeup 间隔选择考虑 prompt 缓存 TTL（5 分钟），避免不必要的缓存失效。

### E. 动态上下文管理（Harness 增强）

17. **动态 Compact 阈值**：不再使用固定字符数阈值，改为根据模型上下文窗口利用率（如 85%）动态触发压缩。
18. **语义分段压缩**：压缩时识别对话的语义边界（工具调用-结果对、用户轮次），在边界处切割而非硬截断。
19. **Completeness Critic**：每轮 Agent 响应后（无工具调用时），可选运行一个轻量 critic 检查——"是否遗漏了模态？是否有未验证的声明？是否有未读的来源？"，发现遗漏则注入追问。

### F. 权限与安全深化

20. **审计日志**：所有工具执行决策（允许/拒绝/询问）写入结构化审计日志，包含时间戳、工具名、参数摘要、决策来源（哪一层规则命中）。
21. **运行时规则管理**：Agent 可通过工具在运行时添加/删除权限规则（受"允许自我修改"的元权限控制）。
22. **工具级速率限制**：防止 Agent 在短时间内疯狂调用同一工具（如连续 50 次 Bash），可配置每工具每分钟最大调用次数。

### G. 可观测性平台

23. **全链路 Trace**：每次 Agent 调用（包括 sub-agent 和 workflow 内 agent）记录完整输入/输出/延迟/token 消耗到 trace 树。
24. **Trace 回放**：选择历史 trace 节点，用相同输入重新执行并对比输出差异。
25. **性能指标收集**：聚合统计 token 效率（输出 token / 总 token）、工具调用延迟分位数、缓存命中率、compact 频率。

### H. Agent 自配置

26. **运行时 Hook 管理**：Agent 可通过工具添加/移除/修改生命周期 hook（受元权限控制）。
27. **运行时设置更新**：Agent 可通过工具修改自身配置项（如切换模型、调整权限模式）。
28. **Memory 自组织**：Agent 可主动调用 Memory 管理工具整理、合并、删除记忆条目。

### I. Workflow Agent Tool

29. **Workflow 调用工具**：主 Agent 的 tool list 中增加 Workflow 工具，可指定 workflow 名称和参数，由引擎执行并返回结果。
30. **后台 Workflow**：支持后台模式——Agent 发起 workflow 后继续其他工作，workflow 完成后以通知形式回传结果。

## 非功能要求

- **向后兼容**：所有现有 CLI 接口 (`mewcode`, `mewcode -p PROMPT`) 保持不变，现有配置文件格式兼容。
- **渐进式采用**：不写 workflow 的用户感知不到 workflow 引擎的存在——主循环行为不变。
- **Python 3.11+**：使用 `asyncio.TaskGroup`、`ExceptionGroup` 等 3.11 特性。
- **无外部编排依赖**：不引入 Airflow/Prefect/Temporal 等重量级框架，自定义轻量引擎。
- **Journal 文件大小**：单 workflow 运行的 journal 文件不超过 10MB（超出后自动截断旧条目）。
- **调度精度**：Cron 触发精度为分钟级（±90 秒抖动），不保证秒级精度。
- **Trace 存储**：Trace 数据存储在 `.mewcode/traces/` 下，自动清理 30 天前的记录。

## 设计骨架

```
mewcode/
├── workflow/                    # [新增] Workflow 编排引擎
│   ├── engine.py                #   核心执行引擎：加载模块、执行函数、管理 journal
│   ├── context.py               #   WorkflowContext：agent()/pipeline()/parallel()/phase()/log()/budget
│   ├── journal.py               #   追加式日志 + 缓存命中查询
│   ├── models.py                #   数据结构定义
│   ├── patterns.py              #   loop_until_count/budget/dry 内建模式
│   ├── resume.py                #   断点恢复逻辑
│   └── tool.py                  #   Workflow Agent Tool
│
├── scheduler/                   # [新增] 定时调度
│   ├── cron.py                  #   Cron 表达式解析 + 触发计算
│   ├── store.py                 #   任务持久化存储 (.mewcode/scheduled_tasks.json)
│   ├── runtime.py               #   后台调度循环
│   ├── wakeup.py                #   自步进 ScheduleWakeup
│   └── tools.py                 #   CronCreate/CronDelete/CronList Agent Tools
│
├── context/                     # [增强]
│   ├── manager.py               #   改造：动态阈值 + 语义分段
│   └── critic.py                #   新增：Completeness Critic
│
├── permissions/                 # [增强]
│   ├── audit.py                 #   新增：审计日志
│   ├── rate_limit.py            #   新增：工具级速率限制
│   └── checker.py               #   改造：集成审计 + 速率检查
│
├── agents/
│   ├── trace.py                 # [增强] 全链路 I/O 记录 + 回放支持
│   └── metrics.py               # [新增] 性能指标收集与聚合
│
├── harness/                     # [新增] Agent 自配置子系统
│   ├── hook_manager.py          #   运行时 Hook 增删改
│   ├── config_manager.py        #   运行时配置更新
│   ├── permission_manager.py    #   运行时权限规则管理
│   ├── memory_manager.py        #   Memory CRUD 工具
│   └── tools.py                 #   暴露给 Agent 的自配置工具集
│
├── agent.py                     # [改造] 集成 workflow + scheduler + critic
├── app.py                       # [改造] TUI 展示 workflow 进度 + phase 分组树
└── __main__.py                  # [改造] 启动时初始化 scheduler runtime
```

### 关键数据流

```
用户输入 → Agent Loop
  ├─ [每轮开始] 检查 Scheduler 是否有到期任务 → 注入系统消息
  ├─ [发送前] 构建提示词（含活跃 Workflow 上下文）
  ├─ [LLM 响应] 流式输出到 TUI
  ├─ [工具调用]
  │    ├─ Workflow 工具 → Workflow Engine 接管
  │    │    ├─ 加载 .mewcode/workflows/{name}.py
  │    │    ├─ 执行 DSL 函数（agent/pipeline/parallel）
  │    │    │    └─ 每次 agent() → Journal 写入 → 实际 LLM 调用
  │    │    └─ 返回结果给主 Agent
  │    ├─ Cron 工具 → Scheduler Store 增删查
  │    ├─ Harness 工具 → Hook/Config/Permission/Memory 运行时修改
  │    └─ 普通工具 → 权限检查（审计日志 + 速率限制）→ 执行
  ├─ [无工具调用时] Completeness Critic 检查 → 可能注入追问
  ├─ [Compact 检查] 动态阈值 → 语义分段压缩
  └─ [每轮结束] Trace 记录完整轮次数据
```

## Out of Scope

- **可视化 Workflow 编辑器**：不提供 GUI 拖拽编排界面，workflow 以 Python 代码形式编写。
- **分布式执行**：Workflow 中的所有 agent 调用在同一进程内执行，不支持跨机器分发。
- **Workflow 版本管理**：不提供 workflow 的版本对比、回滚、灰度发布能力。
- **多租户调度**：Cron 调度器不区分用户/项目，所有任务在同一命名空间。
- **实时协作**：不支持多个用户同时与同一 Agent 会话交互。
- **Workflow 市场/分享**：不提供 workflow 模板库或社区分享机制。
- **SLA 保障**：调度精度不保证秒级，不做高可用。
- **安全沙箱逃逸防护**：Workflow Python 代码在 Agent 进程内执行，不提供独立沙箱——信任模型与现有 Skill/Agent 定义文件一致（用户自写自用）。
- **Hook 热更新**：修改 hook 配置后需重新触发对应事件才生效，不支持运行中的 Agent 即时切换。
