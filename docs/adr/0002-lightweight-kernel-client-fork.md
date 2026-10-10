# ADR 0002：通过独立轻量 fork 保持远程 kernel 客户端兼容性

状态：已接受

## 背景

jcli 需要同步连接远程 Jupyter Server 的 kernel，通过 WebSocket 执行代码、收集输出和查询变量，并保持 Python >=3.10 支持。目标是让 PyPI 普通安装默认减重，而不是只改善开发者的本地依赖解析。

迁移前，jcli 直接依赖 `jupyter-kernel-client`，锁定版本为 0.15.0，并通过 `jupyter-mimetypes` 0.2.0 引入 `pyarrow` 23.0.1。Arrow 在该链路中用于 pandas 对象的 Arrow IPC 序列化和反序列化。该版本 Linux CPython 3.10 x86_64 manylinux Arrow wheel 约 45.36 MiB，而客户端自身 wheel 约 62 KB。

jcli 实际使用 `KernelClient`、`KernelWebSocketClient`、`output_hook` 和 `SNIPPETS_REGISTRY`。[执行逻辑](<../../jupyter_jcli/kernel.py#L351-L394>) 直接驱动底层 shell/iopub 通道；[变量查询](<../../jupyter_jcli/variables.py#L45-L118>) 使用 control 通道上的 DAP，或执行变量列表 snippet。它们不调用上游 `get_variable`、`set_variable`，也不使用高层 `execute(variables=...)` 的对象传输功能。因此默认安装为未使用的对象传输能力承担了较大的间接依赖成本。

现有实现还耦合 `kernel._manager.client`、`_recv_reply` 和连接关闭细节。源码评估表明，上游完整生产代码约四千行，保守核心闭包约三千行，其中 WebSocket 部分约一千四百行：维护范围可控，但直接替换 transport 仍会牵动成熟的协议和生命周期行为。

## 决策

1. 在主仓库的同级目录 `../jcli-kernel-client` 建立保留上游 Git 历史的独立 fork。发行名采用 `jcli-kernel-client`，Python import namespace 采用 `jcli_kernel_client`，首次发行版本采用 `0.1.1`。
2. 从默认依赖中移出 `jupyter-mimetypes`，将对象序列化能力作为 `objects` extra 保留，并在相关函数内部延迟导入。缺少可选依赖时，只有调用相应 API 才应报出明确的安装提示；普通 attach、执行和变量查询不应因此失败。
3. 保留现有 transport 和同步远程 WebSocket 行为，不重写协议、不深度裁剪源码。保持 Python >=3.10，以及现有连接、执行输出（包括图片、错误及 clear/update）、变量查询（DAP 与 fallback）、中断、超时和关闭语义。
4. 只 fork 第一层客户端，不 fork `jupyter-mimetypes`。保留上游 BSD-3-Clause 版权声明及 LICENSE，并在独立发行物中继续包含所需许可材料。
5. fork 不放入主仓库被忽略的嵌套目录，也不使用 submodule。两个仓库分别维护历史与发布。
6. 主项目依赖可安装的独立发行物，使默认依赖边界对 PyPI 普通安装者生效。uv 的 dependency-metadata、dependency override 和本地 source 配置不会作为发行物依赖元数据传递给这些安装者。

独立发行名是发布自己的依赖元数据所必需的；独立 import namespace 则避免与官方包共存时覆盖相同 Python 文件。两者解决不同问题，均纳入本次决策。

## 替代方案及取舍

| 方案 | 取舍 |
| --- | --- |
| 保持官方客户端，使用 uv dependency-metadata 或 dependency override | 可用于本地解析实验，但不能把元数据修正传递给 PyPI 普通安装者，无法实现发布目标。 |
| 私有 vendoring | 不必维护独立包及其发布，但主仓库需要维护较大的复制闭包、许可材料和上游更新；相较保留历史的独立 fork，追踪来源和同步改动更不方便。 |
| `jupyter-server-client` | 已有依赖可提供 HTTP kernel 查询，但没有所需的 WebSocket 客户端，不能替代执行链路。 |
| `jupyter-client` | 主要面向 ZMQ kernel 连接，不是远程 Jupyter Server WebSocket 的直接替代。 |
| `jupyasyncclient` | 有标准 server WebSocket/control 支持，完整闭包粗估不足 1 MiB；但必需 `jupywire`，所考察源码实际上要求 Python 3.11，且迁移到异步模型成本较高。尚未实测，不作为当前替代方案。 |
| 基于 `websocket-client` 自建 | 底层库约 94 KiB、没有强制第三方依赖，但需自行实现 Jupyter protocol、通道分发和连接生命周期。减少依赖的收益不足以抵消本轮重写成本。 |
| `jupyter-server` 的 `GatewayKernelClient` | 存在远程 WebSocket 能力，但引入整套 server 生态，不符合轻量客户端目标。`jupyter-kernel-gateway` 本身是 server，也不是客户端替代。 |

选择独立 fork，是以有限的包维护成本换取默认安装减重，同时保留当前已经依赖的同步接口与 transport。

## 后果

- 普通安装不再因客户端的对象传输功能而默认引入 mimetypes/Arrow 链路；主动选择可选对象传输能力的用户仍承担相应依赖。其他包或用户环境仍可能自行需要 Arrow。
- jcli 获得可自行发布和修正元数据的客户端，但维护者需要承担上游更新评估、补丁合并、兼容性回归及独立包发布。
- 私有 API 耦合继续存在。保留 transport 降低本轮迁移范围，但不意味着这些接口成为稳定的公共契约；后续同步上游时需要关注相关调用。

## 依据

- [上游客户端源码基线](https://github.com/datalayer/jupyter-kernel-client/tree/02acbe69699759c737383d0e5d710c7816ed5f3b)：fork 保留该提交及其之前的历史；客户端、WebSocket transport 与 BSD-3-Clause 许可均源于这一基线。
- [PyPI：jupyter-kernel-client 0.15.0](https://pypi.org/project/jupyter-kernel-client/0.15.0/)、[jupyter-mimetypes 0.2.0](https://pypi.org/project/jupyter-mimetypes/0.2.0/) 与 [pyarrow 23.0.1](https://pypi.org/project/pyarrow/23.0.1/)：迁移前依赖链及制品版本；上述大小来自迁移前锁文件记录，本地 mimetypes pandas 源码确认 Arrow IPC 用途。
- [uv：dependency metadata](https://docs.astral.sh/uv/concepts/resolution/#dependency-metadata)：项目配置可以覆盖特定包的解析元数据，但不是发布包自身的依赖元数据。
