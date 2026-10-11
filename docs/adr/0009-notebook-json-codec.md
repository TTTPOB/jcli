# ADR 0009：notebook 字节输入与高性能 JSON codec

状态：提议中，待审阅。

## 背景

notebook 包含大输出时，JSON 解码、编码和文本／字节转换会占用单次同步时间。当前读取先将整个文件解码为字符串，再交给 nbformat；写入则由 nbformat 生成字符串，配对写回再编码为 UTF-8 字节。

高性能 JSON 库只替换其中的 codec 阶段。NotebookNode 转换、版本转换、validation、输出换行处理、transient 字段处理及 writer 的复制工作仍需要执行。裸 JSON 微基准更快，不代表完整同步更快。

[ADR 0007](<0007-single-sync-dataflow-reuse.md>) 优先减少重复解析与 notebook 读取。本提案独立评估：在保留 nbformat 数据处理语义的前提下，是否采用 bytes 输入和高性能 JSON codec。

## 决策

**将 bytes 输入与高性能 JSON 保留为独立可选优化，当前建议延后，在数据流重整落地后复测并决定是否采用，不自动新增产品依赖。**

若满足采用条件，应联合评估 bytes 输入边界与 codec，保留 NotebookNode 转换、nbformat 版本转换和 validation。不能只替换现有 text 路径中的 `json.loads`／`json.dumps`，就假定能获得实验中的组合收益。

orjson 是本轮可行性实验使用的库，不在本提案中锁定为最终实现依赖。

### 1. 采用条件

决定采用前需要同时满足：

1. 在已完成数据流优化的实现上，以代表性真实负载和合成大输出复测，完整同步收益可重复且明显超过测量波动。
2. 明确选择解码、编码还是两者都替换，以及 bytes 输入如何进入现有 nbformat 转换与 validation 链。
3. 明确 notebook 的结构和格式契约，尤其是否接受缩进或数字字面量变化；若要求 byte 等价，必须提供满足该要求的实现和验证。
4. 确认目标 Python／平台的包支持、依赖维护成本和必要兼容行为，收益足以覆盖新增依赖与适配层成本。

只替换解码、保留 stdlib 编码是可评估选项，但本轮没有测量它的完整同步收益，不能直接据此选择或承诺性能。

### 2. 保留的处理链

读取仍须完成 JSON 解析、NotebookNode 构造、字符串／行数组重组、transient 处理、必要的版本转换和 validation。bytes 输入可避免提前在文件读取层生成整份 text；进入后续转换时仍必须保持相同数据含义和错误处理。

写入仍须保留 cell／输出的换行表示、transient 处理、必要的独立可变副本和 validation。不能通过直接序列化未经 nbformat 处理的 dict 来省略这些行为。

实验通过局部 adapter 替换 nbformat reader／writer 的 codec，验证了这条链上的可行性。生产实现应选择明确、可维护的扩展边界，避免全局替换 stdlib `json` 或依赖未核实的 nbformat 内部细节。

## 性能依据

实验基线为 jupyter-jcli 0.9.5、Python 3.10.19、nbformat 5.10.4，临时隔离环境中的 orjson 3.13.0。未更改产品依赖。样本为 24／300 个完整稳定 ID、已规范化普通 code cells，其中 4 个 cells 各含 4 MiB 合法文本 MIME 输出，总约 16 MiB；包含 execution count、cell metadata、共享／本地 metadata 和 Unicode 文本。

统一预热初始化、使用 warm 文件缓存。下表是进程内同步调用的 3 次非 profile wall-time 中位数，不含进程启动、输入构造、恢复和验证。实验注入已有 canonical Python baseline，禁用持久化，因此不包含 baseline 读写成本。普通修改仅编辑一个 Python cell，不以 authoritative 转换替代同步。

### 1. 仅替换现有 text 路径的 codec

| 场景 | 原版 | 仅 JSON codec | 数据流复用 | 数据流复用＋JSON text 路径 |
| --- | ---: | ---: | ---: | ---: |
| 24 cells，InSync | 417 ms | 399 ms | 251 ms | 254 ms |
| 24 cells，单 cell 普通同步 | 588 ms | 579 ms | 352 ms | 353 ms |
| 300 cells，InSync | 1,356 ms | 1,678 ms | 481 ms | 479 ms |
| 300 cells，单 cell 普通同步 | 1,588 ms | 1,776 ms | 670 ms | 701 ms |

**现有 text 路径换库没有稳定的完整同步收益。** 样本存在调度波动，不能据此断言必然变慢；但它也不支持仅为替换库而新增依赖。

### 2. 联合调整 bytes 输入边界

补充实验只测普通单 cell 同步，保留真正写后 reload；每变体 3 次，轮换运行顺序。

| 样本 | 数据流 text＋stdlib | 数据流 bytes＋stdlib | 数据流 bytes＋orjson |
| --- | ---: | ---: | ---: |
| 24 cells，约 16 MiB 输出 | 357 ms | 354 ms | 200 ms |
| 300 cells，约 16 MiB 输出 | 661 ms | 713 ms | 506 ms |

bytes＋orjson 相对同轮 text 控制的耗时下降约为 44%／24%。bytes＋stdlib 没有同样改善，现有 text 路径单独换库也没有同样改善，因此应将这一收益理解为输入边界与 codec 的组合效果。

24-cell 的“保留原版数据流，仅采用 bytes＋orjson”另测中位约 326 ms；该轮没有对应原版控制，不用它计算精确提速比例。数据流 bytes＋orjson 的同轮结果仍更快。

### 3. 单阶段收益与剩余成本

对同一个约 16 MiB 的 24-cell notebook，3 次中位数如下：

| 计时边界 | stdlib／现行 | orjson |
| --- | ---: | ---: |
| 内存 text 的裸解码 | 24 ms | 16 ms |
| 裸编码，返回 text | 80 ms | 59 ms |
| 完整 nbformat 文件读取，text 路径 | 96 ms | 102 ms |
| 完整 nbformat 文件读取，bytes 路径 | 未在本组测量 | 26 ms |
| 完整 nbformat 写成 text | 85 ms | 74 ms |

裸 codec 的差额只是对应阶段的证据，不能直接叠加为同步加速。在实验 adapter 中，orjson 编码返回的 bytes 仍需解码为 nbformat 要求的 text，写回再编码为 bytes；这部分成本尚未全部消除。

cProfile 只用于定位剩余工作。300-cell 数据流 bytes＋orjson 普通修改的 profile 总时间约 0.888 s，非 profile 中位约 0.506 s；其中 `percent.loads` 3 次累计约 0.422 s，validation 909 次累计约 0.328 s。两者嵌套，不能相加；优化 JSON 后仍有必要的源码解析、validation 和 canonical 发射成本。

## 格式与兼容取舍

### 1. 结构相等与字节相等

现行 nbformat writer 使用 1 空格缩进，实验 orjson adapter 使用其 2 空格缩进选项，两者都排序键并保留 Unicode 文本。修改后的 24／300-cell notebook 分别多约 1,472／14,996 字节，完整结构一致，文件字节不相等。

未写入的 InSync 路径仍保留原始字节。Python 文件的最终字节与原版相等。实验沿用 `nbformat.writes` 返回和既有写回的终止换行行为。

如果采用不同缩进，应把它作为明确的格式契约变更审阅；不能称作 byte 等价替换。浮点数字面量等其他格式差异也需要覆盖，不能仅验证 indentation。

### 2. 数据含义和回退

| 行为 | 兼容要求与实验结果 |
| --- | --- |
| Unicode | 中文、µ、emoji 经真实 nbformat writes／reads 后结构相同；格式字节可以不同 |
| NaN／Infinity | stdlib 可读写，orjson 默认编码会改变为 null，解码也不同；必须明确保留或改变既有契约，不能静默替换 |
| 超 64 位整数 | orjson 编码限制与解码转 float 可能改变数值类型／精度；不能仅比较数值近似相等 |
| ASCII bytes | 现行 `BytesEncoder` 接受例如 base64 bytes；adapter 需要保留对应 ASCII 解码含义 |
| kwargs／custom encoder | 非默认参数、encoder 和错误行为需要明确支持或有界回退，不能直接丢弃参数 |

实验对非有限数和超范围整数使用 stdlib 回退；解码遇可能由大整数转换来的大幅 float 也保守回退。NaN、`2**80` 及 ASCII bytes 的真实 nbformat 链探针通过，Unicode 结构一致。回退增加检查和维护成本，遇到其他数据类型或大量嵌套数值时还需重新衡量收益。

实验未穷尽旧 notebook 版本、invalid JSON、深度限制、全部浮点表示和特殊 metadata 类型。应优先保留现有项目实际需要的契约，不为假设性的所有 JSON 用法构建通用兼容框架。

## 验收条件

使用真实 nbformat 处理链和配对同步入口，不运行 kernel。

1. **内容保留**：逐结构比较 source、ID、顺序、outputs、output metadata、execution count、cell metadata、共享与本地 metadata；用未替换 codec 的漂移检查确认最终 InSync。
2. **格式契约**：Python 字节保持；notebook byte 等价或允许差异必须与采用决定一致。明确缩进、终止换行、Unicode 和数字表示，不只检查 JSON 能否解析。
3. **nbformat 行为**：保留 NotebookNode 转换、版本转换、validation、行数组处理与 transient 处理；格式错误继续得到符合既有入口的诊断。
4. **兼容边界**：覆盖项目支持的 NaN／Infinity、超 64 位整数、ASCII bytes、kwargs／custom encoder；fallback 不得静默损坏值或改变类型。
5. **同步语义**：普通三方 merge、metadata 冲突、输出策略和真正写后 convergence 验证保持，不用 authoritative 或清理输出获得加速。
6. **性能**：在数据流优化后的版本，分别测 text、bytes、codec 及必要组合的进程内非 profile wall-time，记录输出体积与调用次数。固定输入、统一预热，收益须明显超过波动。
7. **依赖维护**：确认目标版本和平台的可安装性、升级范围及回退策略；仅在完整链收益足以覆盖维护成本时增加产品依赖。

## 实施条件

当前推荐先完成 [ADR 0007](<0007-single-sync-dataflow-reuse.md>) 的数据流重整，再按采用条件复测。本提案不承诺实施，产品依赖暂时保持不变。若决定采用，应同时记录 codec 选择、bytes API 边界和格式契约；只解码或保留 stdlib 编码等未测组合需要独立验证。

## 依据

- [notebook 格式读取与写入入口](<../../jupyter_jcli/formats/ipynb.py#L20-L42>)、[同步写回与序列化](<../../jupyter_jcli/pairing.py#L216-L237>)。
- [单次数据流复用提案](<0007-single-sync-dataflow-reuse.md>)、[InSync 无写验证复用提案](<0008-in-sync-verification-reuse.md>)。
- [nbformat 5.10.4 读取、转换与 validation](https://github.com/jupyter/nbformat/blob/v5.10.4/nbformat/__init__.py)、[JSON reader](https://github.com/jupyter/nbformat/blob/v5.10.4/nbformat/reader.py)、[v4 JSON writer](https://github.com/jupyter/nbformat/blob/v5.10.4/nbformat/v4/nbjson.py)：实验同时核对了该版本本地源码。
- [orjson 项目文档](https://github.com/ijl/orjson#readme)：实施前应按选定版本复核 API、类型和数值行为，不将实验版本视为长期兼容保证。
- 本文“性能依据”记录的隔离 codec 与真实同步链对照实验，产品环境未增加依赖。
