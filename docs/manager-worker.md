# Manager / worker v1

`rvbench-evald` 的服务入口已更名为 `rvbench-worker`，新增独立 `rvbench-manager`。
使用 Axum / Tokio。接口没有认证、token 配置或节点名单，任意符合 contract 的 worker 可注册。
配置拒绝未知字段。新服务与旧 Python evald 使用不同端口；旧生产进程暂留供迁移。

## Contract-first

人工权威为本仓 `contracts/api/openapi.json`（共享 schema），worker 仓的 `contract/openapi.yaml` 与
manager 仓 `rvbench-official-manager` 的 `contract/openapi.yaml` 以 YAML 语法引用它；后两份是 JSON 语法的合法 YAML，共享 schema 走指向本仓 `main` 的 raw URL `$ref`。
协议仓 `rvbench-core-rust` 内的 `openapi.json` 是随 crate 发布的副本，正本改动后用 `python tools/sync_contract.py` 同步。
先编写 contract 和正反例，再使用锁定的 openapi-to-rust 0.15.0 生成类型。
运行时校验原始 JSON 后反序列化生成类型。CLI、两端服务共用该类型和校验器。
变更 wire 接口必须先改 contract，然后生成、校验与实现。

```sh
# 在 rvbench-core-rust 仓执行
openapi-to-rust generate -c codegen.toml
openapi-to-rust generate -c codegen.toml --check
# 两个服务各自在所属仓回归
(cd ../rvbench-official-manager && cargo test --locked)
(cd ../rvbench-spacemit-worker && cargo test --locked)
cargo test -p rvbench-protocol
```

## 组件和执行

worker 可在 `manager: null` 时独立接收、排队、编译、评估、返回结果。
`Compiler::prepare` 创建不可变请求快照和源文件、实际编译/JIT 并 smoke 调用。
`Evaluator::evaluate` 分 correctness、performance。`func` 只做编译验证。
正确性结果仅证明提交的 shape / dtype / seed，不代表整个 suite 已通过。
当前烟测容差：Triton float32 的 atol/rtol 均为 0.005，float16 均为 0.05；
native C 的 atol=0.000001、rtol=0.00001。这些值不代替 suite 待裁决的正式容差。
性能结果保存真实样本、warmup 和重复次数；编译和首次调用不计入样本。
Triton 另测原生 PyTorch 参照，每个结果保留 worker/board/instance、工件大小和 SHA-256。

K1 和 K3 均可选择 `implementation: "native-c"` 或 `"triton"`，
支持 func、correctness、performance。省略该字段时保留旧默认值：K1=native-c、K3=triton。
两种实现共用硬件亲和、进程隔离、超时、工件和数值预检流程。

Triton 使用所选 worker 配置的 Python 及已安装 Torch / Triton / FlagGems，用户源文件导出
`candidate(*inputs)`。源文件在独立作业目录导入，不覆盖安装树。
支持 add/sub/mul/div/abs/exp/relu/sum/softmax/mm/mv/aminmax/topk/conv2d，
float32/float16；mm/mv 要求二维，conv2d 使用 NCHW。
其余输入构造和参数见 worker 仓的 `adapters/triton.py`。
Flash attention 不列入可用能力：完整辅助输出合同仍 pending，SDPA 与完整 Flash 接口不等价，
且 SDPA 适配探测的 float32/float16 输入触发 SpacemiT 编译失败。注册环境明确保存 unavailable_ops。

native C 在两种硬件上均使用配置的 `cc`（默认 gcc），支持 add/abs 和 float32。
候选签名为 `void candidate(const float *a, const float *b, float *out, int n)`。
编译参数为 `-O3 -fno-fast-math`，参照和计时见 worker 仓的 `adapters/native.c`。

启动时分别实际编译和 smoke 调用两种实现，在 `environment.implementations` 中保存
各实现的 ready、ops、dtypes 和 probe_compilation，缺失环境记录 ready=false 与 error。
至少一种实现可用即可启动；一种环境失败不会屏蔽另一种。K1 的 Triton 能力同样必须
由板上配置的 Torch/Triton/FlagGems 环境探测通过，代码支持不会代替环境安装。
manager 与直连准入按 implementation/op/dtype 匹配；排行榜按实现类型分别分组。
旧 worker 没有 implementations 能力表，只接收省略新字段的原默认实现任务。
请求示例见协议仓 `examples/k3-native-c.json` 和 `examples/k1-triton.json`。

一个 worker 广告一个执行槽；直连、Push、Pull 使用同一持久接收索引与执行循环。
直连队列上限 32，另保留一个已授予 manager 作业的排队位置，以处理容量快照与直连提交的竞争。
各来源仍共用唯一执行槽。manager 全局未完成任务上限 1000。源码上限 1 MiB UTF-8，
规范化请求上限 2 MiB；传输 envelope 上限 8 MiB，容纳重复绑定的请求和结果证据。张量最多 16777216 元素。
外部进程隔离进程组，超时杀死并回收整组。systemd `KillMode=control-group` 在服务重启时清理子进程。
重启后未完成作业标记中断失败并保留证据，不自动重新执行。
若重启发生在 claim 已提交、本机尚未接收的窗口，先注册新 instance，再重放旧 receipt；
将原授予保存为中断失败和 outbox，释放原任务归属供人工重新提交。旧 instance 不会领取新工作。

## 调度和一致性

manager 只接收统一请求、保存队列、匹配 backend/implementation/kind/op/dtype/readiness，按剩余槽位分配。
Push、Pull 共用 SQLite 原子授予入口。不同 worker 竞争时，最早提交且兼容的任务优先。
注册心跳与执行续租独立；心跳间隔约 2–3 秒、在线 TTL 30 秒、执行 lease 30 秒。
分配与网络交付分离：先提交分配事务，再派发；丢失响应重发相同 assignment。
领取请求持久化 request_id，相同请求返回同一回执；改变请求内容返回 409。
已授予 assignment 在超时后显示 uncertain，保持原执行归属，绝不自动跨板重跑。
旧 lease/instance/job/attempt 不匹配时拒绝确认与回报；迟到的原始结果可对账。

SQLite WAL / synchronous FULL 原子保存控制状态。v1 采用单快照事务，适合当前小型板卡集群；
事务不含网络/子进程或工件 IO。未来规模增大时可迁移为按行表，而不改协议。
worker 终态与 outbox 在同一事务保存，至少一次重传；manager 保存事件摘要去重。
确认后删除对应 outbox；本地 job/attempt 和 manager 的有效授予回执/事件墓碑在 v1 不自动清理。
没有领取到任务的空回执保留至少 300 秒，再清理，以免空轮询长期累积。
manager 重启继续读取原 epoch 和授予；无需把既有在途执行重新派发。
节点 drain 持久化，停止新分配并保留在途任务。
取消仅支持未分配的 manager 队列和 worker 直连未运行任务；运行中取消返回 409。
未交付而状态不明的分配保留用于人工对账，不因心跳离线释放所有权。

## 三板部署

| 板 | 服务 | 执行角色 | 通道 |
|---|---|---|---|
| HUMAN-K3 10.18.60.100 | manager :18800 + worker :18801 | func、correctness | pull |
| K3 10.18.60.18 | worker :18801 | performance | push |
| K1 10.18.60.167 | worker :18801 | func、correctness、performance | pull |

```sh
# manager 与 worker 分属两个仓，各自交叉编译
(cd ../rvbench-official-manager && cargo zigbuild --release --target riscv64gc-unknown-linux-gnu.2.38)
(cd ../rvbench-spacemit-worker && cargo zigbuild --release --target riscv64gc-unknown-linux-gnu.2.38)
python tools/deploy_cluster.py
python tools/cluster_smoke.py
python tools/cluster_recovery_smoke.py
python tools/verify_cluster_artifacts.py artifacts/cluster-smoke-2026-10-17/final
```

在匹配的板端环境中运行真实 Triton 回归（默认本地测试会跳过）：

```sh
RVBENCH_TEST_PYTHON=/path/to/venv/bin/python cargo test -p rvbench-worker real_triton -- --ignored
```

K1 配置同样需将 `backend.python` 指向已安装对应工具链的解释器，并设置所需的
`backend.environment`（如供应商选择、动态库路径）；部署脚本不安装这些硬件依赖。

部署脚本只用已有 SSH 密钥，不保存登录口令。`.100` 和 K1 使用 c4y 的 systemd user 服务并启用 linger；
`.18` 使用 system service 的 initer 用户。首次启动先完成两种实现的独立编译探测后才开放 HTTP 并广告 ready。
性能 K3 必须先注册 `/proc/set_ai_thread` 再设置 CPU8–15 亲和性。
K3 func/correctness 使用 CPU0–7，K1 使用 CPU0–7。
新服务端口 18800/18801；旧 `.100` 的 8788/8791 不受这次部署替换。
恢复验收脚本会短暂停止新 manager 并重启 K1 worker；在没有其他客户端提交任务时运行。
`cluster_smoke.py --operators` 可额外执行 K3 支持算子的 float32 矩阵，逐项保存原始记录。

## 使用

manager 页面 `http://10.18.60.100:18800/ui`，每块板 worker 页面 `http://板IP:18801/ui`。
两端共用 `rvbench-protocol` 内嵌的 `CONSOLE_HTML`，内联样式和脚本，无 CDN。
四个标签页为系统状态、任务提交、任务结果、排行榜。观测、指标与部署见 [observability.md](observability.md)。

```sh
rvbench --base http://10.18.60.100:18800 control workers
rvbench --base http://10.18.60.100:18800 control submit examples/job.json --wait
rvbench --base http://10.18.60.167:18801 control submit request-k1.json --direct --wait
rvbench --base http://10.18.60.100:18800 control get JOB_UUID --wait
rvbench --base http://10.18.60.100:18800 control drain WORKER_ID
```

直连 HTTP 的 POST `/jobs` 使用 `{ "request": JobRequest }` 包装，manager 直接提交 JobRequest。
查询均为 GET `/jobs/{id}?wait=25`；相同 client_request_id + 同摘要复用作业，异摘要返回409。
所有端点见 OpenAPI。工件路径相对 worker 的 `data_dir/jobs/JOB_UUID`，可用 SSH 取回并核对摘要；
v1 API 返回 manifest，不提供任意文件路径下载。

两端互不依赖，各自只依赖 `rvbench-protocol`；`rvbench-worker` 与 `rvbench-manager` 都没有共享的运行时 crate。
公开仓内置的 hello-world 示例保持 draft/readiness 状态，服务上线并不自动使 suite ready。
