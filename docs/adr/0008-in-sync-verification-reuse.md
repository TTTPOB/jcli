# ADR 0008：InSync 无写路径复用已确认状态

状态：已接受；无写增量性能与真实 hook 入口验收完成。

## 背景

j-cli 的漂移检查会读取 py:percent 和 notebook，并判断它们是否表示相同共享状态。[ADR 0007](<0007-single-sync-dataflow-reuse.md>) 的实现已经复用目标状态，但仍在没有要求配对写回的 `InSync` 路径重新读取两侧文件进行收敛验证。

本决策在该数据流上单独省略已经确认相等、且没有写回要求的 InSync 路径重复读取。

## 决策

**当 check 已确认 `InSync`，且本次操作没有要求或执行 Python／notebook 写回时，直接复用已确认的目标共享状态，不再重新加载配对文件进行第二轮验证。**

真正发生写回或需要写回的路径继续执行物理 reload 和 convergence 检查。baseline 是否需要 seed、何时持久化及失败如何报告，保持既有规则。

### 1. 适用条件

省略重复验证必须同时满足：

1. 普通同步（`authoritative is None`）使用现有漂移与共享状态规则完成检查，结果为 `InSync`。
2. check 阶段能提供本次已读取、规范化并确认相等的目标共享状态。
3. `py_needs_update` 与 `ipynb_needs_update` 均为 `False`，没有要求或执行配对文件写回。

这里的“无写”指 Python 和 notebook 文件，不代表一定不写 baseline。不能仅根据 `py_changed=False`、`ipynb_changed=False` 判断：某条写回路径可能执行了序列化或写后检查，只是最终字节未变；authoritative 转换也不自动属于本决策的 InSync 快路径。

输入的首次读取、NotebookNode 转换、validation、metadata 投影和漂移判断全部保留。

### 2. 返回状态与 baseline

check 阶段传出已经确认的 state，由同步器用于结果返回和后续 baseline 决策，避免 InSync 分支再次加载 Python。该状态属于本次操作，不缓存到下一次调用。

- 已有可用 baseline 时，继续按现有 InSync 规则决定是否需要持久化。
- `BaselineMissing` 时，继续使用正确的 canonical seed 文本，并按既有选项决定是否写入。
- 严格模式的 baseline 查询或持久化失败仍必须报错，不能因“配对原本相等”而静默忽略。
- 需要配对写回的路径，仍须先完成写回、重新加载并确认收敛，再推进 baseline。

省去重复配对验证不是省去 baseline 操作，也不改变后续三方 merge 使用的历史状态。

### 3. 与数据流重整的关系

实现使用 [ADR 0007](<0007-single-sync-dataflow-reuse.md>) 的单次 prepared 数据流，直接复用 check 传出的目标状态，不引入全局缓存或重新解析结果文本。

两项收益应分开衡量：[ADR 0007](<0007-single-sync-dataflow-reuse.md>) 的基准保留 verification；本决策只统计在该基础上移除 InSync 无写重复读取的额外收益。

## 性能依据

实际验收对照保留 verification 的数据流实现 `25db49e` 与无写省读实现 `e34219e`。使用 ADR 0007 冻结的同一组 24／300 个唯一稳定 ID 普通 code cells 合成配对文件，四个 cells 各保存 4 MiB 文本 MIME 输出，总计 16 MiB，并保留 Unicode、execution count 及 cell／output／共享／本地 metadata。环境为 Python 3.10.19、nbformat 5.10.4。

只计进程内真实 `synchronize_pair` 调用，默认 `PRESERVE`；每个场景一次不计时预热后取三次 warm 文件缓存、非 profile wall-time 样本。构造、恢复、独立检查和进程启动排除；注入已有 canonical baseline，禁用持久化，性能结果不含 baseline Git 读写。两个版本按阶段顺序测量，未交错。

| InSync 场景 | 保留 verification 中位数 | 省去无写 verification 中位数 | 额外耗时下降 |
| --- | ---: | ---: | ---: |
| 24 cells，16 MiB 输出 | 299 ms | 162 ms | 45.9% |
| 300 cells，16 MiB 输出 | 528 ms | 302 ms | 42.7% |

省读实现的样本范围分别为 154–165 ms、294–314 ms，波动约 6–7%。这些仅是受限合成输入的无写增量收益，不与 ADR 0007 的百分比相加，也不承诺真实用户负载的固定提速。普通单 cell 写回仅作保留 verification 的控制，其计数与对照相同，耗时波动不归入本决策收益。

独立 cProfile 中，24／300 cells 的控制流次数相同：

| 每次调用中的工作 | 对照 InSync | 省读 InSync | 对照修改 | 省读实现修改 |
| --- | ---: | ---: | ---: | ---: |
| Python 物理文件读取入口调用 | 2 | 1 | 2 | 2 |
| `percent.loads` | 2 | 1 | 3 | 3 |
| 完整 notebook 解码 | 2 | 1 | 2 | 2 |
| notebook 原始 bytes 快照读取 | 1 | 1 | 1 | 1 |
| notebook 总物理文件读取入口调用 | 3 | 2 | 3 | 3 |
| 实际 canonical cell 文本发射 | 1 | 1 | 2 | 2 |
| `_verify_pair` | 1 | 0 | 1 | 1 |

首次 prepare 的读取、NotebookNode 转换和 validation 保留；无写路径仍分别读取原始 bytes 快照和解码输入，因此一次解码不等于一次总物理读取。这里统计文件读取入口调用，不是 OS 页缓存 miss 或硬盘 I/O。24／300-cell InSync 的 validator 调用分别由 52／604 降至 26／302，写回路径仍为 79／907。cProfile 单独运行，只用于计数和路径，不替代 wall-time。

## 一致性取舍

重复读取可以发现首次检查与第二次读取之间的外部修改。省略这一步后，本次 InSync 结果表示首次检查时已经确认的状态，不再额外探测随后极短窗口内的文件变化。原来的第二次读取也不能保证函数返回后文件持续不变。

对同机自用的文件同步，这一取舍可以接受。下一次同步仍重新读取和判断漂移；需要消费外部修改的操作应使用正常同步入口。本决策不引入文件 fingerprint、额外 hash 校验、锁或跨调用有效期来重建该窗口的探测。

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
5. **状态隔离**：下一次调用重新检查，前次返回的 mutable state 或配对文件变化不污染新调用。
6. **兼容性**：复用现有 source／ID、输出策略、metadata、三方 merge 与冲突测试；仅为省读条件和状态传递补充必要例子。
7. **性能**：以保留 verification 的数据流版本为对照，分别记录初读和验证读取次数、非 profile wall-time，确认收益来自无写省读而不是关闭 validation 或跳过 baseline。

## 验收结果

版本适用的聚焦回归为 29 passed、6 deselected；无写复用、baseline seed 与持久化开关、authoritative 保留验证、ID 修复后验证及下一次调用重新读取等边界通过。各版本历史回归结果不视为本版本全套重跑。

随后针对首读 validation 断言的增强单独运行一例，1 passed：确认 validation 接收完整保存的 notebook 对象，并且 `ipynb.load` 仅执行一次；该结果不与前述阶段计数合并。

所有预热、wall 与 profile 调用均通过完整内容和精确 bytes 检查：source、ID、顺序、outputs、output metadata、execution count、cell metadata 及共享／本地 notebook metadata 保持正确。无写两侧 bytes 不变；修改后 notebook bytes 与原版同步输出相同，16 MiB 输出完整保留。原版 `4abb59c` 独立进程复核最终修改配对，漂移均为 InSync。

一条真实 hook 入口链使用真实 subprocess、stdin JSON 与 Git baseline 验收：

1. `pair-drift-guard-pre` 成功，配对 bytes 不变，缺失 baseline seed 为 canonical Python 文本。
2. 修改一个 Python cell 后，`pair-drift-guard-post` 成功同步并推进 baseline，完整 notebook 内容及 bytes 与原版输出相同。
3. 再次无修改运行 post，成功且无输出，两侧 bytes 与 baseline ref 均不变。

该成功入口链不替代严格查询／持久化失败、冲突或 authoritative 的聚焦回归。验收不执行 kernel；写回路径的真实 reload 继续保留。

## 依据

- [InSync、baseline 结果类型及漂移检查返回路径](<../../jupyter_jcli/diff/drift.py>)。
- [无写省读门槛、写后验证与 baseline 持久化](<../../jupyter_jcli/pairing.py>)。
- [baseline 读取与写入](<../../jupyter_jcli/pair_baseline.py>)。
- [单次同步回归测试](<../../tests/test_pair_sync.py>)、[guard 漂移测试](<../../tests/test_hooks_pair_drift.py>)。
- [单次数据流复用决策](<0007-single-sync-dataflow-reuse.md>)及本文“性能依据”记录的实际实现无写增量验收（`25db49e` → `e34219e`）。
