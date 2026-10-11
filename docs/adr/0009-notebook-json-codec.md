# ADR 0009：notebook 字节输入与高性能 JSON codec

状态：已接受（评估完成，暂不采用高性能 JSON codec）。

## 背景

[ADR 0007](<0007-single-sync-dataflow-reuse.md>) 的数据流复用和 [ADR 0008](<0008-in-sync-verification-reuse.md>) 的无写省验证已接受。在此基础上，包含大输出的 notebook 仍有 JSON 编解码、文本／字节转换和 nbformat 处理成本。本决策独立评估 bytes 输入与高性能 codec，不将前两项优化的收益计入换库收益。

高性能 JSON 库只替换 codec 阶段。NotebookNode 构造、版本转换、validation、行数组重组、transient 处理及 writer 的复制工作仍需执行；裸 JSON 微基准不能替代完整同步测量。

## 决策

**本轮完成条件式评估，延后生产采用，维持现有 stdlib 处理链，不新增 JSON 依赖或生产 adapter。** 候选只解码路径在普通合成负载上有收益，但缺少代表性真实 notebook 的采用证据；快速编码的额外收益不稳定，且改变文件格式。特殊数值的兼容回退保留值和类型，却有明显时间成本。

这是评估决策完成，不代表产品已实现候选 codec 或 bytes 输入去重。orjson 3.13.0 仅用于隔离评估，不是产品依赖选择。

### 1. 未来采用条件

重新考虑采用前，应同时满足：

1. 在已接受的数据流与无写省验证实现上，以代表性脱敏真实 notebook 和合成大输出验证完整同步收益，且收益可重复、明显超过波动。
2. 区分 bytes 输入去重、快速解码与快速编码的增量，明确进入 nbformat 转换和 validation 链的边界；优先评估只解码、保留 stdlib encoder 的有限改动。
3. 明确 notebook 的结构与字节格式契约，覆盖缩进、终止换行、Unicode 和数字表示；格式变化须单独接受。
4. 确认目标 Python／平台、项目支持的 nbformat >=5 范围、升级及回退维护成本。一个版本的局部接口审计不能充当全版本保证。

代表性真实样本是未来采用条件，本轮不要求补交用户资料，也不因缺少样本扩展 serializer 或提前增加依赖。

### 2. 必须保留的处理链

读取保留 JSON 解析、NotebookNode 构造、字符串／行数组重组、transient 处理、必要版本转换和 validation。写入保留换行表示、transient 处理、独立可变副本及 validation，不直接序列化未经 nbformat 处理的 dict。

修改路径必须真实从文件 reload 并确认收敛；无写省验证仅遵守 ADR 0008 的既有门槛。不引入跨调用缓存，不通过 authoritative 转换或清理输出获得收益。

nbformat 5.10.4 没有公开 codec 注入参数。评估候选在一次性进程中局部连接项目与 nbformat 入口，不修改 stdlib json 或安装包文件；它不是可直接启用的生产补丁。非默认 kwargs、旧版本输入等边界整体退回既有处理链，避免扩成通用 JSON 兼容框架。

## 性能依据

固定代码为 `e34219e`，Python 3.10.19、nbformat 5.10.4、隔离 orjson 3.13.0。合成配对文件包含 24／300 个唯一稳定 ID、已规范化普通 code cells；四个 cells 各保存 4 MiB 文本 MIME 输出，总计 16 MiB，另含 Unicode、execution count 和 cell／output／共享／本地 metadata。

每变体先预热，取三次 warm 文件缓存、轮换顺序的 wall-time 样本；只计进程内 `synchronize_pair` 调用，默认 `PRESERVE`。构造、恢复、独立正确性检查和进程启动不计入。注入已有 canonical baseline、禁用持久化，不含 baseline Git 读写，也不代替 baseline 失败语义验证。profile 另轮运行，wall 关闭事件计数。

原始三次值、独立补测分组、特殊数值测量与 28 项探针摘要保存在 [持久化评估证据](<../performance/notebook-json-evaluation.json>)，各组不混池。

### 1. 四组完整同步对照

| 组 | 输入与 codec | 区分的收益 |
| --- | --- | --- |
| A | 原生 text＋stdlib 读写；保留 bytes 快照后另从文件读取 text | 当前产品控制，未经 patch |
| B | 复用 prepare 已读 bytes＋stdlib 读写 | 输入边界及去重读取，A→B 不是库收益 |
| C | B 输入边界＋orjson 解码、stdlib 编码，必要时回退 | B→C 为解码候选增量 |
| D | B 输入边界＋orjson 读写，必要时回退 | C→D 为编码候选增量 |

所有组的修改路径保留真实写后 reload。首轮中位数如下，单位 ms：

| 场景 | A | B | C | D |
| --- | ---: | ---: | ---: | ---: |
| 24 cells，InSync | 166.5 | 139.9 | 99.0 | 96.9 |
| 24 cells，单 cell 修改 | 457.9 | 448.7 | 377.4 | 303.0 |
| 300 cells，InSync | 354.2 | 364.2 | 322.5 | 347.8 |
| 300 cells，单 cell 修改 | 822.7 | 841.7 | 685.7 | 704.0 |

部分首轮极差较大，例如 24-cell 修改 A／D 为 107.8／88.4 ms，300-cell InSync C 为 115.3 ms。因此仅补关键修改比较，各三次，以下中位数与首轮分列：

| 补测场景 | B | C | D |
| --- | ---: | ---: | ---: |
| 24 cells，单 cell 修改 | 未补测 | 327.8 | 281.8 |
| 300 cells，单 cell 修改 | 722.8 | 654.9 | 710.3 |

- **A→B 无跨场景稳定收益**：24-cell InSync 较快，300-cell InSync／修改略慢，24-cell 修改差异落在波动内；本轮不以稳定提速为由采用输入去重。
- **B→C 有普通合成负载收益**：24-cell InSync／修改首轮约降 29%／16%，300-cell 修改首轮约降 19%、补测约降 9%；300-cell InSync 噪声较大，且所有结果缺少真实负载采用证据。
- **C→D 编码收益不稳定**：24-cell 修改首轮／补测约降 20%／14%，300-cell 修改两轮均未稳定优于 C，中位数分别约慢 3%／8%，补测 D 极差 115.7 ms。
- **InSync 的 C／D 没有编码调用**，执行相同读路径，其差异视为测量波动，不能解释为编码收益。

三次样本不是统计显著性结论；这些受限大文本合成输入不构成真实用户 notebook 的固定提速承诺。

### 2. 数值回退的完整同步成本

同一 24-cell／16 MiB 样本，仅在 output 的合法 `application/json` 中加入特殊数值，普通单 cell 修改各取三次。中位数单位 ms：

| 数值 payload | B | C | D | C 相对 B |
| --- | ---: | ---: | ---: | --- |
| NaN、±Infinity | 403.8 | 494.6 | 501.0 | +90.8 ms，约 +22% |
| ±2**80、±(2**80+1) | 410.8 | 453.0 | 436.6 | +42.2 ms，约 +10% |

非有限数触发快速 parse 失败后回退；大整数由候选解码后的数值 guard 识别可能发生整数→float 转换，再用 stdlib 解码。C／D 两次读取均回退，D 编码也回退；值和类型保持正确，但重复尝试有实际成本。D 在大整数轮略快于 C 不是快速编码收益，因为二者最终均用 stdlib encoder。

guard 还扫描普通解码结果。本轮以大文本为主，不能推断大规模数值 JSON 数组同样划算；不为消除该成本新增 token parser 或通用兼容层。

## 内容、格式与兼容边界

每次预热、wall 和 profile 调用后均在计时外比较完整 notebook 树：source、ID、顺序、outputs、output metadata、execution count、cell metadata 及共享／本地 metadata。数值比较区分 int／float、NaN／Infinity、-0.0，并精确比较浮点值。Python bytes 保持，未替换 codec 的漂移检查确认最终 InSync。

无写场景两侧原始 bytes 不变。A／B／C 修改后的 notebook 与 stdlib writer 字节一致；D 特殊数值回退也一致。D 普通写回结构一致但采用 2 空格缩进，24／300-cell 文件分别多 1,835／18,947 bytes，不是 byte 等价替换。同步的 `writes(...).encode` 路径仍无最终 LF，独立 `nbformat.write` 文件入口仍补单个 LF，未统一两个入口的既有行为。

28 项真实 nbformat 链小探针全部通过，覆盖 Unicode、ASCII bytes、非有限数、正负超 64 位整数及邻值、有限浮点和 -0.0、MIME JSON／文本数组、attachments、stream、markdown／raw、transient、有界 kwargs 回退、错误诊断、validation capture 及一个空 v3→v4 样例。它们不等于全部旧版本、kwargs、特殊类型或目标平台覆盖，也不代替同步行为回归。

独立 profile 确认四组同场景的源码解析、canonical 发射、NotebookNode 转换、validation 和 verify 次数相同；没有靠关闭 validation 或省略修改后的 reload 获益。快速 writer 还依赖 v4 的 split_lines／strip_transient／BytesEncoder，仍有 bytes→text→bytes 转换；当前证据不足以承担额外 writer 维护及格式变化。

## 采用前验收要求

若未来重新提出生产采用，须保留以上处理链，并验证完整内容、精确数值类型与值、明确的文件格式契约、非默认 kwargs／旧版本回退、三方合并与输出策略、写后收敛及 baseline 规则。使用代表性真实 notebook 与固定合成样本，分别评估输入去重、只解码和编码增量；目标版本／平台可用性与维护成本满足条件后，才考虑新增依赖。

## 依据

- [持久化原始 wall 数据与探针摘要](<../performance/notebook-json-evaluation.json>)：固定 `e34219e` 的四对照评估，产品依赖未改变。
- [notebook 格式入口](<../../jupyter_jcli/formats/ipynb.py>)、[同步与写回序列化](<../../jupyter_jcli/pairing.py>)。
- [单次数据流复用决策](<0007-single-sync-dataflow-reuse.md>)、[InSync 无写验证复用决策](<0008-in-sync-verification-reuse.md>)。
- [nbformat 5.10.4 读取、转换与 validation](https://github.com/jupyter/nbformat/blob/v5.10.4/nbformat/__init__.py)、[JSON reader](https://github.com/jupyter/nbformat/blob/v5.10.4/nbformat/reader.py)、[v4 JSON writer](https://github.com/jupyter/nbformat/blob/v5.10.4/nbformat/v4/nbjson.py)：评估核对了该版本本地源码，不保证整个 nbformat >=5 范围。
- [orjson 项目文档](https://github.com/ijl/orjson#readme)：未来采用前按选定版本复核 API、类型、数值及平台支持。
