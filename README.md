<p align="center">
  <img src="assets/rvbench-logo.svg" alt="rvbench" width="720">
</p>

# rvbench

RISC-V 软硬协同评测基础设施。合同定义实验，worker 在真实板卡上执行，结果自带可复核的证据。

它要回答的不是「某段代码在板子上跑多快」，而是「这次测量的软件实现、硬件平台、输入、数值结论和样本，是否足以支撑一句可比较的话」。算子是这个基础设施第一个落地的典型场景，也是第一份公开测试集。

## 仓库

| 仓库 | 内容 |
|---|---|
| `rvbench`（本仓） | 运维工具、JSON Schema 与 wire 正本、hello-world 示例评测集 |
| [`rvbench-cli`](https://github.com/rvbench/rvbench-cli) | `rvbench` CLI（`rvbench` / `rb-cli` 两个命令）与离线校验库 |
| [`rvbench-official-manager`](https://github.com/rvbench/rvbench-official-manager) | 控制面服务：提交、队列、准入、授予、账本与观测汇总 |
| [`rvbench-core-rust`](https://github.com/rvbench/rvbench-core-rust) | `rvbench-protocol`：wire 合同副本、生成类型、校验、摘要、存储 |
| `rvbench-spacemit-worker` | 板端执行服务：编译、验证、计时、能力探测 · *暂未公开* |
| `rvbench-official-suites` | 完整官方评测集 · *后续开源* |

## 设计原则

### 一次观测由六部分组成

```text
观测 = 合同 + 代码身份 + 有效环境 + 验证证据 + 原始样本 + 显式状态
```

同一个算子名称不一定是同一个实验：shape、dtype、布局、输入生成规则、数值容差、工具链版本任一不同，延迟就不可直接比较。因此每个 case 固定自己的工作负载身份，结果显式记录代码与环境摘要，未知检查保持未知。

### 合同优先

`contracts/schemas/` 的 JSON Schema 是 suite 与结果格式的正本；控制面 wire 的正本是本仓 [`contracts/api/openapi.json`](contracts/README.md)，manager 与 worker 各自仓内的 OpenAPI 以 YAML 语法引用同一批 schema。协议仓 [`rvbench-core-rust`](https://github.com/rvbench/rvbench-core-rust) 内保存一份随 crate 发布的副本，[`rvbench-cli`](https://github.com/rvbench/rvbench-cli) 的 `tests/contracts.rs` 逐字节比对两侧，不同步即测试失败。

wire 类型由 `openapi-to-rust` 0.15.0 生成，不手写平行 DTO。接口变更顺序固定：改合同 → 重新生成 → conformance 测试 → 实现 → 实机验证。运行时先校验原始 JSON，再反序列化为生成类型；CLI 与两端服务共用同一套校验器与规范 JSON 摘要。

### 工作负载身份不可隐式改变

一个 case 固定 shape、dtype、layout、属性与输入生成器（generator + seed）。禁止隐式改 shape，禁止用 fp32 顶替 fp16，未知的输出检查记为 `not_passed` 而不是通过。

摘要绑定身份，但摘要本身不证明执行真的发生过。

### 执行、验证、计时是三件事

| 任务类型 | 做什么 | 不声称什么 |
|---|---|---|
| `func` | 实际编译或 JIT，并调用一次 smoke | 数值正确 |
| `correctness` | 与声明的原生参照逐输出比较 | 整个 suite 通过 |
| `performance` | 数值预检 → 预热 → 采样 | 预检外其他 case 正确 |

任务 `done` 是服务终态，不等于科学评测通过。合同校验通过、哈希一致、作业状态 `done`，任何一项单独都不能当作完整验证。

### 证据随结果走

结果保存原始样本、warmup 与重复次数、编译与数值预检结论、工件路径与 SHA-256、worker / board / instance 身份。编译与首次调用不计入样本；预检失败不进入计时。

## 系统结构

```text
client (rvbench CLI / webui)
        │  JobRequest
        ▼
   rvbench-manager          统一提交、队列、准入、原子授予、账本
        │  Push / Pull / Hybrid
        ▼
   rvbench-worker           独立可运行：接收、编译、评估、回传
        ├── Compiler::prepare    不可变请求快照、源文件、实际编译/JIT、smoke
        └── Evaluator::evaluate  correctness / performance
```

worker 在 `manager: null` 时完全独立工作；接上 manager 后由 manager 分配任务。两端只通过 `rvbench-protocol` 互通：生成类型、JSON Schema 校验、canonical JSON 与事务存储。服务机制各自持有——worker 内建进程组隔离与超时回收，manager 内建队列、账本与租约，没有共享的运行时 crate。

### 目录与职责

```text
rvbench/
├── tools/                  rvbench-tools 与集群部署 / 冒烟 / 验收脚本
├── contracts/              合同正本：api/ 存控制面 wire，schemas/ 存 spec / results / baseline
│   ├── api/                共享 OpenAPI schema，两端 OpenAPI 以 $ref 引用
│   └── schemas/            JSON Schema 正本
├── examples/               合成结果、baseline 与 hello-world 冒烟用例
├── deploy/observability/   VictoriaMetrics 抓取配置与约定
├── backends/spacemit/k3/   K3 板端资产：算子脚本、监控配置、平台档案
├── docs/                   格式 spec、ADR、运行手册
└── artifacts/              本地构建、部署包与验收记录（不入库）
```

CLI 在 [`rvbench-cli`](https://github.com/rvbench/rvbench-cli) 仓独立构建，`tools` 以 git 依赖固定其 rev 复用同一套离线校验；Python 只承担算子执行、原生参照、计时与 Triton JIT 编译，不参与控制流程。

## 可灵活定义的硬件后端与软件前端匹配机制

硬件后端由 worker 配置声明（板卡型号、CPU 亲和、AI thread、工具链），软件前端由请求声明（`implementation` 与候选源文件），两者在准入时匹配，不写死成一条固定链路。

`implementation` 是请求字段，与板卡型号无关：K1 和 K3 都能选 `native-c` 或 `triton`，都支持 `func` / `correctness` / `performance`。

| 实现 | 算子 | dtype | 说明 |
|---|---|---|---|
| `triton` | add、sub、mul、div、abs、exp、relu、sum、softmax、mm、mv、aminmax、topk、conv2d | float32、float16 | 用户源文件导出 `candidate(*inputs)`；使用 worker 配置的 Python 与已装 Torch/Triton/FlagGems |
| `native-c` | add、abs | float32 | `void candidate(const float *a, const float *b, float *out, int n)`，`cc` 默认 gcc，`-O3 -fno-fast-math` |

worker 启动时对两种实现分别实际编译并 smoke 调用，把 ready、ops、dtypes、probe 结果写入 `environment.implementations`；至少一种可用即启动，一种失败不屏蔽另一种。manager 与直连准入按 implementation / op / dtype / readiness 匹配，排行榜按实现分别分组。

烟测容差：Triton float32 atol/rtol = 0.005、float16 = 0.05；native C atol = 1e-6、rtol = 1e-5。这些值不代替 suite 待裁决的正式容差。

## 构建与检查

Rust stable（`rust-toolchain.toml` 指定 stable + rustfmt + clippy），依赖版本由 `Cargo.lock` 固定。

```sh
cargo build --workspace --locked
cargo test  --workspace --locked
cargo clippy --workspace --all-targets --locked -- -D warnings

cargo run -p rvbench-tools -- check
```

构建产出 `rvbench-tools`；`rvbench` 与别名 `rb-cli` 在 [`rvbench-cli`](https://github.com/rvbench/rvbench-cli) 仓构建，在该仓执行 `cargo install --path . --locked` 一次装齐两个命令。`rvbench-worker` 在 worker 仓、`rvbench-manager` 在 manager 仓各自构建。公开 spec 仍为 draft，`validate spec ... --require-ready` 返回非零是预期状态：合同合法与完成板上验证是两件事。

需要真实 Triton 环境的回归默认跳过：

```sh
RVBENCH_TEST_PYTHON=/path/to/venv/bin/python cargo test -p rvbench-worker real_triton -- --ignored
```

## 运行

### 配置

```jsonc
// worker
{
  "listen": "0.0.0.0:18801",
  "url": "http://10.18.60.100:18801",
  "worker_id": "human-k3",
  "board_id": "k3-100",
  "data_dir": "/home/c4y/.local/share/rvbench/worker",
  "kinds": ["func", "correctness", "performance"],
  "delivery": "pull",                // push | pull（每次注册只能选一种）
  "manager": "http://10.18.60.100:18800",
  "backend": { "backend": "k3", "cc": "gcc", "python": "python3", "environment": {}, "cpus": null, "ai_thread": false }
}
```

```jsonc
// manager
{ "listen": "0.0.0.0:18800", "data_dir": "/home/c4y/.local/share/rvbench/manager", "victoriametrics": null }
```

配置拒绝未知字段。真实 `config.json` 与环境文件默认不入库。

### 客户端

```sh
rvbench --base http://10.18.60.100:18800 control workers
rvbench --base http://10.18.60.100:18800 control submit examples/job.json --wait
rvbench --base http://10.18.60.167:18801 worker submit request-k1.json --wait
rvbench --base http://10.18.60.167:18801 worker capabilities
rvbench --base http://10.18.60.100:18800 control get JOB_UUID --wait
rvbench --base http://10.18.60.100:18800 control drain WORKER_ID
```

请求示例见 `examples/job.json`（manager 提交）；含候选源的 `k3-native-c.json`、`k1-triton.json` 在协议仓 `examples/` 下。直连 HTTP 的 `POST /jobs` 使用 `{"request": JobRequest}` 包装；查询为 `GET /jobs/{id}?wait=25`。相同 `client_request_id` 与相同摘要复用作业，摘要不同返回 409。

`worker` 子命令组只打 worker：作业文件收裸 `JobRequest` 与 `{"request": ...}` 包装两种；`--wait` 等到终态后，非 `done` 的退出码为 3。

### 页面

manager `http://10.18.60.100:18800/ui`，worker `http://板IP:18801/ui`，两端共用同一页面（`rvbench-protocol` 内嵌的 `CONSOLE_HTML`）。四个标签页为系统状态、任务提交、任务结果、排行榜；内联样式与脚本，无 CDN、无构建、无登录。

## 三节点集群

| 板 | 服务 | 执行角色 | 通道 |
|---|---|---|---|
| HUMAN-K3 10.18.60.100 | manager :18800 + worker :18801 | func、correctness | pull |
| K3 10.18.60.18 | worker :18801 | performance | push |
| K1 10.18.60.167 | worker :18801 | func、correctness、performance | pull |

```sh
# 两个服务各自在所属仓交叉编译
(cd ../rvbench-official-manager && cargo zigbuild --release --target riscv64gc-unknown-linux-gnu.2.38)
cargo zigbuild -p rvbench-worker --release --target riscv64gc-unknown-linux-gnu.2.38
python tools/deploy_cluster.py
python tools/cluster_smoke.py
python tools/cluster_recovery_smoke.py
python tools/verify_cluster_artifacts.py artifacts/cluster-smoke-2026-10-17/final
```

部署只用已有 SSH 密钥，不保存口令。K3 性能角色须先注册 `/proc/set_ai_thread` 再设 CPU8–15 亲和；其余角色用 CPU0–7。K1 的 Triton 能力同样取决于板上 Torch/Triton/FlagGems 是否探测通过，代码支持不代替环境安装。

## 可观测性

`GET /metrics` 输出 Prometheus text 0.0.4，抓取只读事务后的轻量缓存，不访问 SQLite。manager 另提供 `/observability/summary`、`/observability/targets`（HTTP SD）、`/observability/range`（仅固定 metric_id，≤24h / 1800 点）、`/results`、`/leaderboard` 分页查询。

```sh
bash tools/build_monitoring.sh
python tools/deploy_observability.py --vm-binary artifacts/monitoring-binaries/victoria-metrics --exporter-binary artifacts/monitoring-binaries/node_exporter
python tools/observability_smoke.py --output artifacts/observability-check/monitoring
```

VictoriaMetrics v1.154.0 与 node_exporter v1.10.2 均为 `CGO_ENABLED=0`、`GORISCV64=rva20u64` 的纯 Go 构建，仅监听 `127.0.0.1:8428`，初始保留 7d / 30s 抓取，数据不混入任务账本。详见 [docs/observability.md](docs/observability.md)。

## 评测集与结果合同

- `examples/hello-world/`：最小 add 用例与配套 synthetic 结果，用于跑通提交、编译、执行与结果落盘链路。公开仓只内置这一个示例。
- 完整算子评测集随 `rvbench-official-suites` 后续开源；本仓不存放具体用例。
- `examples/baseline/`：可选基准包（spec 2.1.0 的 `baseline.entries[]`）。

历史测速与当前通过状态分别管理：基准数据不自动裁决 case 是否 ready，也不写入 incumbent 记录。

## 文档

- [manager / worker 运行手册](docs/manager-worker.md)
- [可观测性与控制台](docs/observability.md)
- [spec 格式](docs/specs/2026-10-08-spec-json格式.md) · [结果格式](docs/specs/2026-10-08-eval-results-json格式.md) · [baseline 格式](docs/specs/2026-10-16-suite-baseline格式.md)
- [控制台](https://github.com/rvbench/rvbench-official-manager/blob/main/webui/README.md)（manager 仓）
- [ADR 0001 合仓](docs/adr/0001-monorepo.md) · [ADR 0002 Rust 控制面](docs/adr/0002-rust-control-plane.md)
- [架构图](docs/architecture.html)
