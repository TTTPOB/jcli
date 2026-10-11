# ADR 0008：InSync 无写路径复用已确认状态

状态：提议中，待审阅。

## 背景

j-cli 的漂移检查会读取 py:percent 和 notebook，并判断它们是否表示相同共享状态。结果为 `InSync` 时，当前同步器仍重新读取 Python、构造目标状态，再重新读取两侧文件进行收敛验证，即使没有要求任何配对写回。

[ADR 0007](<0007-single-sync-dataflow-reuse.md>) 提议在一次同步中复用已准备的数据，但保留现有 verification 阶段。本提案单独决定：已经确认相等、且没有写回的 InSync 路径，是否还需要重复读取文件。

## 决策

**当 check 已确认 `InSync`，且本次操作没有要求或执行 Python／notebook 写回时，直接复用已确认的目标共享状态，不再重新加载配对文件进行第二轮验证。**

真正发生写回或需要写回的路径继续执行物理 reload 和 convergence 检查。baseline 是否需要 seed、何时持久化及失败如何报告，保持既有规则。

### 1. 适用条件

省略重复验证必须同时满足：

1. 使用现有漂移与共享状态规则完成检查，结果为 `InSync`。
2. check 阶段能提供本次已读取、规范化并确认相等的目标共享状态。
3. 本次没有要求或执行配对文件写回。

这里的“无写”指 Python 和 notebook 文件，不代表一定不写 baseline。不能仅根据 `py_changed=False`、`ipynb_changed=False` 判断：某条写回路径可能执行了序列化或写后检查，只是最终字节未变；authoritative 转换也不自动属于本提案的 InSync 快路径。

输入的首次读取、NotebookNode 转换、validation、metadata 投影和漂移判断全部保留。

### 2. 返回状态与 baseline

check 阶段传出已经确认的 state，由同步器用于结果返回和后续 baseline 决策，避免 InSync 分支再次加载 Python。该状态属于本次操作，不缓存到下一次调用。

- 已有可用 baseline 时，继续按现有 InSync 规则决定是否需要持久化。
- `BaselineMissing` 时，继续使用正确的 canonical seed 文本，并按既有选项决定是否写入。
- 严格模式的 baseline 查询或持久化失败仍必须报错，不能因“配对原本相等”而静默忽略。
- 需要配对写回的路径，仍须先完成写回、重新加载并确认收敛，再推进 baseline。

省去重复配对验证不是省去 baseline 操作，也不改变后续三方 merge 使用的历史状态。

### 3. 与数据流重整的关系

本提案依赖能够复用 check 结果中的目标状态，优先建立在 [ADR 0007](<0007-single-sync-dataflow-reuse.md>) 的单次 prepared 数据流上。若单独实施，也必须提供同样简洁的状态传递，不能靠全局缓存或重新解析结果文本模拟复用。

两项收益应分开衡量：[ADR 0007](<0007-single-sync-dataflow-reuse.md>) 的基准保留 verification；本提案只统计在该基础上移除 InSync 无写重复读取的额外收益。

## 性能依据

实验使用 jupyter-jcli 0.9.5、Python 3.10.19、nbformat 5.10.4；24／300 个完整稳定 ID、已规范化普通 code cells，其中 4 个 cells 各含 4 MiB 合法文本 MIME 输出，总输出约 16 MiB。样本还包含 cell metadata、execution count、共享／本地 metadata 和 Unicode 文本。

统一预热初始化，使用 warm 文件缓存；下表为进程内同步调用的 3 次非 profile wall-time 中位数，不计进程启动、输入构造、恢复和验证。实验注入已有 canonical Python baseline 并禁用持久化，因此不包含 baseline 读写时间。

| InSync 场景 | 数据流复用，保留 verification | 再省去无写 verification | 额外耗时下降 |
| --- | ---: | ---: | ---: |
| 24 cells，约 16 MiB 输出 | 251 ms | 123 ms | 约 51% |
| 300 cells，约 16 MiB 输出 | 481 ms | 220 ms | 约 54% |

在相同受限样本中，Python 解析由初读加验证的 2 次降为 1 次，完整 notebook 解码由 2 次降为 1 次。300-cell 的 nbformat validator 调用由 605 次降为 303 次。source／ID／输出／metadata 保持正确，Python 和 notebook 均没有发生写入，结果字节与原版相同；随后用原版漂移检查确认 InSync。

这些是数据流复用后的增量收益，不能再与原版到数据流版本的百分比相加。少量重复存在调度波动，不承诺固定提速比例；cProfile 只用于定位和计数，不用其累计耗时替代 wall-time。

## 一致性取舍

重复读取可以发现首次检查与第二次读取之间的外部修改。省略这一步后，本次 InSync 结果表示首次检查时已经确认的状态，不再额外探测随后极短窗口内的文件变化。原来的第二次读取也不能保证函数返回后文件持续不变。

对同机自用的文件同步，这一取舍可以接受。下一次同步仍重新读取和判断漂移；需要消费外部修改的操作应使用正常同步入口。本提案不引入文件 fingerprint、额外 hash 校验、锁或跨调用有效期来重建该窗口的探测。

写后验证有不同作用：它检查本次写回是否真正收敛到选定状态，能发现序列化、metadata 或 cell 更新错误。因此以下路径不省略验证：

- merge 后需要更新任意一侧的配对文件。
- authoritative 转换或其他要求写回的操作。
- 需要修正 ID、header 或其他 canonical 内容的操作，即使最终报告的字节变化为零。

## 验收条件

使用真实配对文件和同步入口验证，不运行 kernel。

1. **InSync 无写**：复用已确认 state；Python 与 notebook 字节不变，不执行额外配对 reload，结果字段与既有契约一致。
2. **首读检查**：首次读取的格式转换、validation、源码与 metadata 判断仍执行；不能仅按文件存在、修改时间或上次缓存认定 InSync。
3. **baseline seed**：`BaselineMissing` 的 seed 内容正确，持久化开关保持；严格查询／写入失败继续报错。不能以实验禁用持久化代替产品验证。
4. **真正写后验证**：单侧或双侧更新仍 physical reload；写回未收敛时报错且不推进 baseline。authoritative 和 canonical 修正不误入无写快路径。
5. **状态隔离**：下一次调用重新检查，前次返回的 mutable state 或配对文件变化不污染新调用；不要为这一测试新增全局缓存机制。
6. **兼容性**：复用现有 source／ID、输出策略、metadata、三方 merge 与冲突测试；仅为省读条件和状态传递补充必要例子。
7. **性能**：以保留 verification 的数据流版本为对照，分别记录初读和验证读取次数、非 profile wall-time，确认收益来自无写省读而不是关闭 validation 或跳过 baseline。

## 实施条件

实施前确认 [ADR 0007](<0007-single-sync-dataflow-reuse.md>) 的 prepared 状态如何传递到 InSync 返回路径，并验证 baseline seed 与严格失败处理。可以将本提案作为数据流重整后的独立小改动，以便分别审阅和回归；接受数据流重整不自动代表接受这里的一致性取舍。

## 依据

- [InSync 及 baseline 结果类型](<../../jupyter_jcli/diff/drift.py#L43-L79>)、[漂移检查返回路径](<../../jupyter_jcli/diff/drift.py#L175-L232>)。
- [当前 InSync 状态重建](<../../jupyter_jcli/pairing.py#L97-L110>)、[写后验证与 baseline 持久化](<../../jupyter_jcli/pairing.py#L121-L148>)。
- [baseline 读取与写入](<../../jupyter_jcli/pair_baseline.py#L241-L330>)。
- [单次同步回归测试](<../../tests/test_pair_sync.py>)、[guard 漂移测试](<../../tests/test_hooks_pair_drift.py>)。
- [单次数据流复用提案](<0007-single-sync-dataflow-reuse.md>)及本文“性能依据”记录的独立无写省读对照实验。
