# ADR 0005：Codex 按编辑段合并配对同步

状态：提议中，待审阅。

## 背景

j-cli 在编辑前检查 py:percent 与 notebook 的配对漂移，在编辑后同步 notebook。前置检查本身也可能执行同步。连续修改同一 Python 文件时，逐调用运行完整前后流程会重复启动进程、读取和写入 notebook。

本提案沿用 [ADR 0003](<0003-batched-pair-synchronization.md>) 的编辑段概念，为 Codex 单独定义接入边界。Codex 已提供可等待的 `PreToolUse`、`PostToolUse` 和 `Stop` hook；公开 hook 输入提供当前调用和回合身份，但没有嵌套调用的根调用或工具批次身份。因此，本方案按消费操作与回合结束分段，不以 assistant 消息、模型响应或 Code Mode 程序结束作为同步边界。

## 决策

**使用 Codex 现有同步 hooks：首次编辑前检查，连续编辑只登记待同步配对，在消费配对内容前及正常回合结束时等待同步。**

接入复用现有 [配对检查与同步流程](<../../jupyter_jcli/commands/hooks/pair_drift.py>)。不修改 Codex 宿主，不新增持久服务、定时防抖或通用事务框架。

### 1. 编辑段与前置检查

每个配对独立维护一个编辑段：从首次编辑前检查开始，到相关消费操作或正常回合收尾同步完成为止。

1. `PreToolUse` 匹配 `^apply_patch$`，从 `tool_input.command` 解析整份补丁的所有受影响路径，按 `cwd` 解析并归一化。
2. 对本段尚未检查的配对，等待现有前置流程完成，处理原有漂移；直接编辑 notebook 的拒绝规则及无配对文件的快速跳过继续保留。
3. 本段再次编辑同一配对时，跳过重复的配对检查和同步，保留其他工具检查。不能仅延迟后置同步，否则下一次前置检查仍可能同步上一轮修改。
4. 工具执行前保存候选修改路径；成功的 `PostToolUse` 登记待同步配对。收尾时也检查尚未确认成功的候选路径，以覆盖失败或取消后可能留下的实际修改。候选登记不意味着工具已经写入。
5. 同步成功后清除该配对的本段状态；后续编辑重新执行首次检查。

一次 `apply_patch` 可以包含多个文件及同一文件的多个修改块。前置检查必须覆盖整个补丁涉及的配对，后置登记按配对去重，不能只处理首个路径。移动操作同时处理原路径和 `*** Move to:` 目标，核对移动前后的配对关系；若适配层不能确定移动后的配对，必须在执行补丁前给出明确诊断，不把移动当普通内容编辑静默跳过。

### 2. 同步边界

| 操作 | 行为 |
| --- | --- |
| 再次编辑本段 Python 文件 | 继续本段，不重复前置同步 |
| 明确只读取已修改的 Python 文件 | 直接读取，继续本段 |
| 读取 notebook 或配对输出 | 同步相关待处理配对后再放行 |
| 启动 Bash／`exec_command` | 同步当前作用域全部待处理配对后再放行 |
| 本地读取工具无法可靠确定消费路径 | 同步当前作用域全部待处理配对后再放行 |
| 根线程正常结束回合 | 在同步 `Stop` hook 中处理剩余配对 |
| 受支持的子代理正常结束回合 | 在同步 `SubagentStop` hook 中处理其剩余配对 |

首期对 shell 使用统一边界，不分析复杂命令是否访问 notebook。读取工具按实际启用的本地函数名或 MCP 名称配置匹配；`Bash` 同时覆盖普通 shell 和 unified exec。没有待处理配对时立即返回。

消费前 hook 必须等待同步结束，成功后才放行工具。同步失败时通过 `PreToolUse` 支持的拒绝结果阻止消费者，报告未同步路径，保留待处理状态。仅让 hook 报错或超时不能保证阻止工具。

例如：

```text
编辑 → 编辑 → 读取 Python → 编辑 → Stop
首次检查一次；回合末同步一次。

编辑 → Bash 执行 notebook → 编辑 → Stop
执行前同步一次；后续编辑重新检查，回合末再同步一次。
```

合并目标是消除没有消费需求时的重复工作。中间存在消费者时，需要提前同步，不能同时承诺整个回合只同步一次。

### 3. 跨 hook 状态

命令 hook 是独立进程，不能依赖常驻插件内存。首期使用一个简单的本地状态文件保存检查过的配对、候选修改、待同步路径及现有 guard 所需参数，按 `session_id`、可选 `agent_id`、`turn_id` 和工作目录划分作用域。路径使用解析后的配对键去重。

状态放在集成指定的可写缓存目录；若采用插件打包，可使用 `PLUGIN_DATA`。它只用于跨 hook 协调，不是重启后自动执行的持久队列。下次首次检查仍负责发现未完成同步留下的漂移。

同一作用域的状态更新和同步使用短时文件锁，避免并发 hook 丢失 dirty 登记或对同一配对重复启动同步。这是独立进程共享状态所需的协调，不引入后台服务。不同代理同时编辑同一配对不属于首期支持的合并场景，应分配不同文件或顺序执行。

每个事件使用一个处理入口，在入口内部完成必要顺序。Codex 会并发启动同一事件的多个匹配 command handlers，不能通过配置中的 handler 顺序表达“先登记，再同步”。

### 4. Code Mode 与长驻 shell

Code Mode 内的编辑沿用相同规则：读取或执行 notebook 前同步，没有消费操作则在正常回合结束时同步。程序返回本身不保证 notebook 已更新，因为当前公开 hook 接口未提供可用于同步的程序结束通知。

例如，程序内“编辑 → 编辑 → 读取 notebook → 编辑”必须在中间读取前同步；不能仅等待程序或回合结束。受支持的嵌套工具调用会触发工具前后 hook，适配层需要新增消费前同步拦截，并与延迟编辑同步一起实现。程序必须先等待编辑完成再读取，不能依赖 hook 给并发提交的读写建立依赖顺序。

直接使用运行时文件 API 或进程 API 读写文件会绕过这些 hook：直接写入不保证登记，直接读取也不会自动等待同步。此类入口不在本方案的一致性保证内。

`exec_command` 首次启动前可以通过 `PreToolUse(Bash)` 同步。命令持续运行时，原命令的 `PostToolUse` 可能在后续 `write_stdin` 轮询完成时才交付；`write_stdin` 不重新运行 `PreToolUse`。因此，本方案不能保证向已经启动的交互式 shell 再发送命令前自动同步。需要在该 shell 中消费 notebook 时，应使用新工具调用，或由 j-cli 执行入口／显式包装器先完成同步。保留现有 [执行前配对校验](<../../jupyter_jcli/outputs/notebook.py#L118-L129>)，不将校验自动等同于 flush。

## 错误、取消与一致性

编辑段内 Python 已写入，notebook 可以暂时保留旧内容。外部 Jupyter UI 和其他绕过工具 hooks 的读取者可能看到旧 notebook；支持的工具消费者启动前，以及正常 `Stop`／`SubagentStop` hook 返回前，应完成同步。

普通 `PostToolUse` 不是所有失败和取消路径都执行的 finally。候选路径检查用于发现已发生的写入；同步失败时保留状态，并明确报告“Python 已修改，notebook 未完成同步”。后续相关操作先处理未完成状态，不能因新 `turn_id` 而遗忘它。

`Interrupt` 和 `SessionEnd` 的 command hook 执行预算很短，不能承担较慢 notebook 同步的可靠收尾。取消时不承诺完成 flush；下一次相关操作重新检查并处理实际漂移。进程强制退出同样依靠下次首次检查恢复，不新增重启任务调度。

所有登记和 flush hooks 均同步执行。`async: true` 会让每次匹配调用独立后台运行，不提供去重或批末等待，还可能让消费者先于登记运行。旧 `notify` 的 `agent-turn-complete` 通过启动通知进程交付，不等待其完成，也不用于一致性屏障。App Server 的 `item/completed`、`turn/diff/updated` 和 `turn/completed` 可用于观察，但监听通知不会使后续工具等待客户端同步。

## 接入与部署条件

首期使用用户或项目级 command hooks 即可；插件只作为可选分发形式，不赋予额外批次生命周期。项目层和具体 hook 定义必须按 Codex 要求完成信任审阅。hook 命令应能在实际执行环境找到 j-cli 及所需解释器，超时应覆盖代表性 notebook 的同步耗时。

范围限定为支持本地工具 hooks 的 Codex 工作流。云编排、托管工具和管理策略的支持范围按部署时官方说明核实，不直接套用本地配置。插件包含 lifecycle hooks 时，其分发资格也需核实；调查基线文档不允许此类插件进入公开插件目录。

应先核实平台最新版本、实际部署版本与 hook schema，更新版本依据，再确认是否实施。决定实施后，将状态更新为“已接受”，再开始适配、聚焦测试及既有 setup 分发流程的更新，不直接修改已安装副本。

## 验收条件

1. 同一配对连续多次 `apply_patch`，包括 Code Mode 嵌套调用，每段仅运行一次完整前置流程与一次后置同步；轻量状态登记可逐次发生。
2. 一个补丁的多文件、多修改块和移动路径均被识别；无配对文件快速跳过，直接编辑 notebook 继续被拒绝。
3. 读取 Python 不增加同步；读取 notebook、配对输出或启动 shell 时，消费者实际启动前同步已完成。同步失败阻止消费者并保留待处理状态。
4. 正常回合及受支持子代理收尾完成剩余同步。Code Mode 程序返回不是验收中的独立 flush 保证。
5. 部分写入、工具失败、取消和同步失败后，下一次相关操作发现并处理漂移；不宣称中断或强制退出已完成同步。覆盖长驻 shell 后续输入绕过 Pre hook 的限制。
6. 同一作用域并发登记不丢失路径；不同配对独立处理，路径别名去重；notebook 源码、cell ID 和需保留的输出正确。
7. 对代表性配对文件比较逐次策略与编辑段策略的 guard 进程数、同步次数和耗时，以重复工作减少为目标，不承诺固定提速倍数。

## 官方依据与调查基线

调查基线为已发布的 [Codex rust-v0.162.1](https://github.com/openai/codex/releases/tag/rust-v0.162.1)，仅用于记录本决策依据，不指定未来实施版本。官方文档可能继续更新，`main` schema 也可能包含尚未发布字段。

- [Hooks 官方文档](https://learn.chatgpt.com/docs/hooks)：同步等待、工具覆盖、Code Mode、异步限制、信任及中断行为。
- [Pre/Post 输入 schema](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/hooks/src/schema.rs#L276-L342)：`turn_id`、`tool_use_id`、`tool_input`、`tool_response` 及可选代理身份。
- [工具执行顺序](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/tools/registry.rs#L609-L746)：等待 Pre hook、执行工具、等待适用的 Post hook。
- [apply_patch 输入输出适配](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/tools/handlers/apply_patch.rs#L395-L431)、[补丁语法](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/assets/tools/apply_patch.lark#L1-L17)：整份 `tool_input.command`、多路径与移动。
- [根线程与子代理收尾](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/hook_runtime.rs#L392-L460)：`Stop` 与 `SubagentStop`。
- [异步 handler 调度](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/hooks/src/engine/dispatcher.rs#L115-L150)、[旧 notify 实现](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/hooks/src/legacy_notify.rs#L13-L69)：逐调用调度与通知进程启动语义。
- [App Server 文档](https://developers.openai.com/codex/app-server)、[插件打包文档](https://developers.openai.com/plugins/build/plugins#bundled-mcp-servers-and-lifecycle-hooks)：观察事件、hook 分发及执行环境要求。
