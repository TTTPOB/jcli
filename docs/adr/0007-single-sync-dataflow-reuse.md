# ADR 0007：单次配对同步的数据流复用

状态：提议中，待审阅。

## 背景

j-cli 在单次配对同步中读取 py:percent 与 notebook，比较两份文件共有的 cell 源码和 metadata，处理差异与冲突，再写回并验证结果。

图片、表格、日志等运行结果只保存在 notebook 中，不参与两份文件是否同步的判断。默认同步策略会更新源码，同时保留仍存在的代码 cell 已保存的运行结果，不重新执行代码。因此，修改源码后留下的结果可能已过时，需要重新运行才能更新。

读取和写回 notebook 时，这些已有结果也会一起被读取和序列化。即使只修改一行注释，大图等输出仍会增加同步成本。

当前流程多次将同一内容转换为文本、重新解析为 cells，再构造 `PairState`。已有 baseline 的 InSync 路径会调用 `percent.loads` 11 次、`PairState.cell_text` 11 次，并两次完整读取 notebook。cell 数量较多时，重复构造 cell 引发的 validation、深拷贝和源码发射成为明显热点。

[ADR 0003](<0003-batched-pair-synchronization.md>) 减少连续编辑所需的同步次数。本提案减少一次同步内部的重复工作，适用于现有同步入口，不依赖宿主批次生命周期。

## 决策

**将单次同步组织为显式的 prepare → check → apply → verify 数据流，复用本次操作拥有的已解析文档、共享状态和 canonical cell 文本。保留真正写后的重新加载与收敛验证。**

实现采用一条共享同步流程。先整理现有阶段之间的数据传递，再在已证明等价的输入范围内减少解析和规范化；不维护一套永久平行的快速同步算法，也不引入通用 `loads` 缓存和反复深拷贝返回值的 clone 池。

### 1. 阶段职责

| 阶段 | 职责与复用内容 |
| --- | --- |
| prepare | 读取 Python、notebook 和 baseline；保留原始内容及必要的格式上下文，产生符合既有规范化规则的共享投影 |
| check | 使用已准备的 canonical cell 文本和共享 metadata 判断漂移、三方合并及冲突，返回目标状态和写回要求 |
| apply | 使用已读 notebook 和原始内容完成必要写回；保留原 cell 对齐、输出策略及本地 metadata 规则 |
| verify | 按现有入口重新加载配对文件，与目标共享状态比较；验证成功后才按既有规则推进 baseline |

prepared 状态只存活于一次同步，不跨调用、文件或进程缓存。它可以是少量内部数据字段或一个简单结构，不需要新增服务、调度器或持久状态协议。

### 2. 解析与规范化的边界

首期选择**通用的单条阶段结构，加受限的解析省略规则**：

- 对完整、唯一稳定 ID 且已规范化的普通 code cells，可复用已准备的 cells 和文本，省去等价 round trip。
- 对缺失或重复 ID、magics、尾空白、空 cell、markdown／raw 等输入，继续使用既有规范化步骤。未经验证不将首次 raw parsed 文档直接视为 canonical parsed 文档。
- baseline 文本与当前 Python 原文完全相同时，可在同一次操作中复用其准备结果；不同内容分别准备。
- merge 文本与某个已准备的 canonical cell 文本完全相同时，可复用对应 cells；双边互补修改产生新的 merge 文本时，仍须解析新结果。
- 写后的物理重新加载是独立的验证，不计入“同一输入只准备一次”的省略目标。

这些分支共享 merge、冲突诊断、apply 和 verify 逻辑。一般化规范化规则可后续扩展，但不是接受本提案后必须立即完成的全面重写。

### 3. 状态所有权与比较

本次操作拥有的 cells 和共享 metadata 在准备完成后按只读状态用于判断与比较。apply 可在已读 notebook 容器上替换 cells 和 metadata，但通过既有更新逻辑构造写回 cells，不能修改仍被输入状态引用的节点。需要复制的是明确的可变边界，而不是每次取得共享状态都深拷贝整份文档。

canonical cell 文本可显式保存在局部变量中，或按不可变的 emitter 输入快照复用。快照应包含 cell 类型、源码、稳定 ID、顺序及 ID 发射模式；复用结果是不可变字符串。不能仅以可变 `PairState` 的对象身份缓存，也不能用不带失效规则的 `cached_property`。

共享 metadata 仍按现有 projection 和 `json_equal` 规则比较。outputs、execution count 和 representation-local metadata 不应进入共享源码相等判断；raw source tuple 也不能替代既有 canonical 文本的等价规则。

## 性能依据

实验基线为 jupyter-jcli 0.9.5、Python 3.10.19、nbformat 5.10.4。合成样本包含 24 或 300 个稳定 ID code cells，其中 4 个 cells 各保存 4 MiB 合法文本 MIME 输出，总输出约 16 MiB；另含 execution count、cell metadata、共享与本地 metadata 及 Unicode 文本。

测量统一预热初始化，使用 warm 文件缓存；时间是进程内真实同步调用的 3 次非 profile wall-time 中位数，构造、恢复输入、验证和进程启动不计入。普通修改只改一个 Python cell 的源码，不以 authoritative 转换替代普通同步。实验注入原始 canonical Python 文本作为已有 baseline，禁用 baseline 持久化，表中不含 baseline 读取或写入成本。

| 场景 | 原版同步 | 数据流复用，保留验证 | 观察到的耗时下降 |
| --- | ---: | ---: | ---: |
| 24 cells，InSync | 417 ms | 251 ms | 约 40% |
| 24 cells，单 cell 普通同步 | 588 ms | 352 ms | 约 40% |
| 300 cells，InSync | 1,356 ms | 481 ms | 约 65% |
| 300 cells，单 cell 普通同步 | 1,588 ms | 670 ms | 约 58% |

24-cell 原版 InSync 的 3 次结果约为 280–430 ms，存在明显波动；这些数值是代表性可行性证据，不是固定提速承诺。实验快路径限于完整稳定 ID、已规范化普通 code cells。

| 每次调用中的工作 | 原版 InSync | 复用 InSync | 原版单 cell 修改 | 复用单 cell 修改 |
| --- | ---: | ---: | ---: | ---: |
| `percent.loads` | 11 | 2 | 10 | 3 |
| `percent.canonicalize` round trip | 4 | 0 | 4 | 0 |
| 真实 `PairState.cell_text` 发射 | 11 | 1 | 11 | 2 |
| `PairState.from_parsed` 深拷贝投影 | 6 | 0 | 5 | 0 |
| 完整 notebook 解码 | 2 | 2 | 3 | 2 |

复用 InSync 的两次 Python 解析分别是初读和验证重读；修改路径还需准备不同的 baseline。新 merge 文本产生时仍增加必要解析。表中的零次 canonicalize 只表示受限样本省掉了 round trip，不表示取消规范化契约。

cProfile 用于确认调用路径和剩余热点，不用作 wall-time 基准。例如 300-cell 原版 InSync 的 profile 耗时约 3.476 s，非 profile 中位数约 1.356 s；数据流版本分别约 0.654 s 和 0.481 s。累计耗时包含子调用，不能将 parser、validation、deepcopy 和 emitter 的累计占比相加。

## 边界与取舍

- **保留语义**：继续使用三方 cell merge、metadata merge、冲突诊断、ID 对齐和输出策略。不得用一侧 authoritative 覆盖另一侧来获得性能收益。
- **输出保留**：默认 `PRESERVE` 的大输出、cell metadata、execution count 及本地 notebook metadata 必须保留；减少输出或改成清理输出不是本提案的优化方式。
- **验证范围**：本提案保留 InSync 的现有验证，便于独立评估数据流变化。[ADR 0008](<0008-in-sync-verification-reuse.md>) 单独决定无写路径是否省去重复读取，其收益不包含在本提案表中。
- **依赖范围**：不新增 JSON 库。[ADR 0009](<0009-notebook-json-codec.md>) 单独评估输入字节边界和 codec。
- **实现成本**：跨阶段传递 prepared 状态属于中等规模内部重构。关键工作是明确所有权、保留规范化上下文和统一返回路径；不以代码行数最少为由保留两套长期同步算法。

## 验收条件

使用真实配对文件和同步函数验证，不运行 kernel。

1. **最终内容**：比较优化前后的 source、ID、顺序、outputs、output metadata、execution count、cell metadata，以及共享和本地 notebook metadata。受限等价路径的 Python 与 notebook 字节应与现有实现一致。
2. **合并与冲突**：覆盖单侧修改、双边互补修改、同 cell 冲突、metadata 三方合并及 kernel 原子冲突；比较状态、写回标志和诊断。新 merge 文本不得遗漏解析。
3. **规范化**：覆盖缺失／重复 ID、code／markdown／raw、空 cell、尾空白、magics 和 header metadata。受限分支以外继续走正确的准备步骤，不能仅因未命中优化而改变结果。
4. **所有权**：验证 input parsed 文档、目标状态和写回 cells 不发生意外 alias；source、ID、类型或顺序变化后不能复用旧 canonical 文本，下一次同步不残留上次状态。
5. **写后收敛**：保留真实 reload，无法收敛时仍报错且不推进 baseline；baseline seed 和持久化失败语义保持。
6. **输出策略**：复用现有 `PRESERVE`、`CLEAR_EDITED`、`CLEAR_ALL`、插删／重排、attachments 和 widgets／kernel 变更测试，只为实际改变点补充聚焦例子。
7. **性能**：按相同样本、预热和计时边界对比非 profile wall-time，并检查实际 parser、emitter、深拷贝和 notebook 解码次数；不把 cProfile overhead 或进程启动混入收益。

可行性实验已验证稳定 ID 样本的完整内容及字节一致、原版漂移检查最终 InSync、metadata 合并与冲突、source 冲突、双 cell 互补 merge，以及 magics／尾空白的正确回退。缺失／重复 ID 等一般化路径尚需实施时的聚焦验证。

## 实施条件

接受范围应明确为单条 prepare／check／apply／verify 数据流，以及首期允许省略解析的输入条件。先完成既有行为回归，再逐步启用受限复用；不同时改动宿主批次策略、输出保留策略或 JSON 依赖。

## 依据

- [单次同步与写回验证](<../../jupyter_jcli/pairing.py#L32-L148>)、[notebook 写回与 cell 更新](<../../jupyter_jcli/pairing.py#L216-L307>)。
- [漂移检查、规范化与三方合并](<../../jupyter_jcli/diff/drift.py#L125-L232>)。
- [共享状态投影、canonical 文本与 equality](<../../jupyter_jcli/pair_state.py#L28-L68>)、[percent 解析与规范化](<../../jupyter_jcli/formats/percent.py>)。
- [同步回归测试](<../../tests/test_pair_sync.py>)、[共享状态测试](<../../tests/test_pair_state.py>)、[写回测试](<../../tests/test_pair_apply.py>)、[漂移测试](<../../tests/test_drift.py>)。
- 本文“性能依据”记录的合成 notebook 进程内对照实验；实施前按相同输入与计时边界重新测量。
