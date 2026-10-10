# ADR 0006：OpenCode 按编辑段合并配对同步

状态：提议中，待审阅。

## 背景与范围

j-cli 的配对检查维护 py:percent 与 notebook 的一致性。编辑前检查会处理已有漂移，必要时同步；编辑后流程将修改同步到 notebook。连续修改同一 Python 文件时，逐次运行两项流程会重复启动进程并读写 notebook。

沿用 [ADR 0003](<0003-batched-pair-synchronization.md>) 的“编辑段”概念：对同一配对文件连续修改，直到需要消费配对 notebook 或到达可用的收尾边界。读取已经修改的 Python 文件本身不结束编辑段。

本提案只决定 OpenCode 适配方式，区分两种执行路径：

- **普通工具调用**：插件能等待每个工具的前后 hook，但没有公开的、可等待的整批工具结束 hook。
- **实验性 Code Mode**：原生 `execute` 编排连接的 MCP 工具，具有外层工具完成边界。它不包含任意脚本中的内置编辑，也不能代表其他平台的程序化工具调用。

版本依据为官方版本 **v1.18.35**。应先核实目标版本及接口并更新依据，再确认是否实施。决定实施后，将状态更新为“已接受”，再开始实现。

## 决策

**首次编辑前检查，段内编辑只登记待同步；消费者启动前等待同步。Code Mode 正常结束时等待尾部同步，普通调用以空闲通知触发尾部补刷，但不承诺回合返回前已同步。**

同步继续复用现有配对检查和后置流程，不增加服务、持久队列或定时防抖机制。

### 1. 最小归集状态

常驻插件按会话保存配对状态，以按工具工作目录解析的绝对路径作为配对键。每个配对记录本段是否已完成前置检查、是否待同步及后置流程所需参数；Code Mode 另记录外层 `execute` 的调用身份。

1. 首次编辑配对文件前，等待现有前置检查完成。
2. 本段后续编辑同一配对时，跳过重复的配对检查，保留原有权限检查、直接编辑 notebook 的拒绝规则及其他工具检查。
3. 写入后标记待同步，不逐次运行后置流程。
4. 到达同步边界时，每个待同步配对运行一次后置流程，成功后清除本段状态；后续编辑重新开始一段。

**前置流程本身可能同步，必须与后置流程一起合并。** 仅延迟后置流程，会让下一次编辑的前置检查提前同步，仍然重复处理 notebook。

普通调用没有可靠的整批开始身份，因此状态在消费同步或尾部补刷后重置，而不是假设同一 assistant 消息必然是一批。

### 2. 消费者前同步

| 后续操作 | 行为 |
| --- | --- |
| 再次编辑同一 Python 文件 | 继续本段 |
| 读取已修改的 Python 文件 | 直接读取，继续本段 |
| 读取对应 notebook 或配对输出 | 在工具 before 中等待相关配对同步 |
| 执行 shell 或 j-cli 执行工具 | 在工具 before 中等待当前会话待同步配对，再执行命令 |
| Code Mode 正常结束 | 在外层 `execute.after` 中等待剩余配对同步 |
| 普通调用进入空闲 | 通知触发尾部补刷；后续相关工具必须等待未完成同步 |

首期 shell 统一作为同步边界，不分析命令是否访问 notebook。j-cli 执行路径也必须接入消费屏障；工具内部绕过 OpenCode hook 的调用不能仅靠插件保障。

插件记录正在执行的同步 Promise，消费者必须等待该任务；不能仅检查待同步集合是否为空。没有待同步文件且没有进行中的任务时立即返回。同步失败保留待同步状态并阻止依赖工具启动，报告对应路径。

例如：

```text
编辑 → 编辑 → 读取 Python → 编辑 → 正常 execute 结束
首次检查一次，尾部同步一次。

编辑 → 执行 notebook → 编辑 → 正常 execute 结束
执行前同步一次；后续编辑重新检查，尾部再同步一次。
```

### 3. Code Mode 的根边界

基线实现中，外层 `execute` 经过通用工具 wrapper，插件能等待其 before 和 after。内部 MCP 调用也触发 before/after，子调用 ID 构造为 `${外层 callID}/${递增序号}`。

适配器只对已识别的原生 `execute` 使用这一实现约定：记录外层 callID，将其子编辑归集到对应根调用，内部消费者 before 提前同步，外层 after 同步剩余文件。这样，一个只编辑同一配对文件的正常脚本仅需一次前置检查和一次尾部同步。

子调用 ID 的编码不是公开的 `rootCallID` 字段；实施时必须重新核实。Code Mode 内部的 `onToolCallStart`、`onToolCallEnd` 是运行时回调，不是插件 hook。普通工具不能仅因 callID 包含斜杠就归入某个根调用。

### 程序化调用的限制

外层 `execute` 结束时同步只处理尾部剩余修改，不能代替中间读取前的同步。例如，子调用“编辑 → 编辑 → 读取 notebook → 编辑”应在读取工具的 before 中先完成同步，再继续程序。消费者前同步是适配层待新增的能力，必须与延迟同步一起实现。

这一保证仅覆盖经过 OpenCode before/after 的 MCP 工具调用；程序需等待依赖的编辑完成后再读取，不保证并发提交的依赖读写自动排序。MCP 工具内部或其他脚本直接使用文件／进程 API 的读写不产生对应子工具 hook，不保证修改被登记或读取前已同步。任意第三方包装器不能直接套用原生 Code Mode 的根边界约定。

### 4. 普通调用的收尾与一致性代价

普通工具之间通过消费者 before 保证工具链内一致性。使用 `session.status` 的 idle 通知触发尾部同步，并保存同步任务供下一次相关操作等待。

插件 `event` 回调的 Promise 不被宿主等待。因此 idle 补刷不能保证下一模型步骤、回合返回或 UI 显示空闲时同步已经完成。`message.part.updated` 中的 `step-finish` 同样是通知，不提供可等待的批末屏障，本提案不依赖它推断最后一个编辑。

编辑段内 notebook 可暂时落后于 Python；普通调用结束后，尾部补刷完成前也存在此窗口，Jupyter UI 等外部读取者可能看到旧内容。若产品要求普通调用在正常回合返回前必定完成同步，应先获得可等待的宿主收尾接口，再扩展该路径；不能用 idle 通知宣称已满足此保证。

### 5. 写入、失败与取消

工具 after 在 execute 成功返回后运行，并非 finally。工具写入后报错、外层脚本异常或取消，均可能缺少预期 after。

实施时应对编辑目标采用保守登记：首次 before 检查成功后即保存“可能待同步”的目标，成功 after 再确认结果。这样，异常路径可能多做一次无修改同步，但不会仅因缺少 after 遗漏已登记的写入。多文件 patch 必须覆盖其全部目标；无法提前识别的实际写入，需要另行验证文件通知是否足以补充，不能只靠最终成功结果推断。

idle 或错误通知用于触发有界补刷；通知本身不阻塞宿主。下一次消费仍等待补刷任务。同步失败明确报告“Python 已修改，notebook 未完成同步”；同步诊断不得覆盖原工具或脚本错误。取消后的补刷使用独立的有界执行条件，不复用已取消的工具信号。

进程强制退出不保证收尾，下一次首次检查负责发现漂移。首期不引入恢复日志或持久任务队列。

## 平台能力与设计依据

OpenCode 提供同步屏障的基础接口，但不自动合并 j-cli 同步：

- `tool.execute.before` 输入为 `{ tool, sessionID, callID }`，可修改输出对象中的 `args`；`tool.execute.after` 输入增加 `args`。两者由 dispatcher 按插件注册顺序等待。
- 普通工具真实顺序为 **before → execute → after → 返回结果**。将 after 写成 async 不会产生 debounce；逐次等待同步仍会逐次付出成本。
- hook 没有公开的 root、batch、step 或 turn 身份字段，也没有 defer-after 控制项。Code Mode 根关联来自具体实现。
- MCP after 实际接收原始 MCP 结果，不能统一假定存在普通工具结果的 `title/output/metadata` 形状。
- `session.status` 的状态包括 busy、retry、idle；`session.idle` 在基线已标记废弃。idle 也可来自错误和取消，不代表全部编辑成功。

## 验收条件

使用真实配对文件和目标 OpenCode 执行链验证，kernel 使用替身，无需远端服务器。

1. **次数与内容**：连续编辑同一配对，每段仅一次前置检查和一次后置同步；路径别名去重，不同配对独立处理；源码、cell ID 和需保留的输出正确。
2. **消费边界**：读取 Python 不增加同步；读取 notebook、配对输出及执行 shell/j-cli 前同步已完成。同步失败时消费者未启动，待同步状态仍保留。
3. **普通调用**：idle 触发补刷；在补刷未完成时提交新的相关操作，该操作等待任务。测试不得将 idle 出现作为同步完成断言。
4. **Code Mode**：确认只覆盖 MCP 编排；连续子编辑在正常外层返回前同步一次，内部消费导致正确分段，根身份与目标版本实现一致。
5. **异常**：编辑工具写入后抛错、脚本失败及取消均尝试有界补刷；原错误保留，失败路径可诊断；测试覆盖没有 after 的路径和多文件 patch。
6. **既有行为与性能**：无配对文件快速跳过，直接编辑 notebook 仍被拒绝；记录代表性 notebook 的进程数及整段耗时，以重复工作减少为目标，不承诺固定提速倍数。

## 实施与部署

确认实施范围时，应决定是否接受普通调用“消费者前强制同步、idle 后异步补刷”的可见性窗口，以及是否纳入实验性 Code Mode 支持。

接受后，修改 j-cli 的 OpenCode 源适配、安装生成流程及聚焦测试；通过项目既有 setup 流程更新集成并重载 OpenCode，不手工修改生成文件或已安装副本。部署前重新核实实际工具名称、编辑参数、配对流程、消费入口及 hook 时序；未满足验收时保留逐次同步策略。

## 官方依据

以下源码链接固定到调查基线，实施前需更新为目标版本：

- [插件官方文档](https://opencode.ai/docs/plugins/)；[插件 hook 类型](https://github.com/anomalyco/opencode/blob/v1.18.35/packages/plugin/src/index.ts#L222-L281)。
- [普通工具执行顺序](https://github.com/anomalyco/opencode/blob/v1.18.35/packages/opencode/src/session/tools.ts#L99-L131)；[MCP 调用及原始结果](https://github.com/anomalyco/opencode/blob/v1.18.35/packages/opencode/src/session/tools.ts#L398-L424)。
- [可等待的 hook dispatcher](https://github.com/anomalyco/opencode/blob/v1.18.35/packages/opencode/src/plugin/index.ts#L284-L297)；[非等待的 event 转发](https://github.com/anomalyco/opencode/blob/v1.18.35/packages/opencode/src/plugin/index.ts#L255-L262)。
- [Code Mode 子工具 hook](https://github.com/anomalyco/opencode/blob/v1.18.35/packages/opencode/src/tool/code-mode.ts#L134-L185)；[子调用 ID 与外层执行](https://github.com/anomalyco/opencode/blob/v1.18.35/packages/opencode/src/tool/code-mode.ts#L219-L305)；[Code Mode 的 MCP catalog 范围](https://github.com/anomalyco/opencode/blob/v1.18.35/packages/opencode/src/tool/registry.ts#L280-L308)。
- [会话状态及废弃的 idle 事件](https://github.com/anomalyco/opencode/blob/v1.18.35/packages/schema/src/session-status-event.ts#L9-L49)；[runner 空闲及取消路径](https://github.com/anomalyco/opencode/blob/v1.18.35/packages/opencode/src/session/run-state.ts#L59-L85)。
- [step part 生成](https://github.com/anomalyco/opencode/blob/v1.18.35/packages/opencode/src/session/processor.ts#L424-L470)；[message.part.updated 载荷](https://github.com/anomalyco/opencode/blob/v1.18.35/packages/schema/src/v1/session.ts#L612-L619)。
