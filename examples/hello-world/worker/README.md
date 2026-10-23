# hello-world worker

`examples/hello-world` 的最小 worker：一个文件、一份配置，直接跑通 `add` 的提交、编译、验证与落盘。

它实现 [`contracts/api/openapi.json`](../../../contracts/api/openapi.json) 的 worker 一侧，只支持 **push** 投递：接收 `JobRequest`，回 `JobRecord`，不轮询 manager。

## 依赖

| 用途 | 需要 |
|---|---|
| 服务本体 | Python 3.10+，只用标准库，无需 pip |
| `native-c` 实现 | `gcc` 或 `clang`（配置项 `backend.cc`，找不到时自动试 `gcc` / `clang`） |
| `triton` 实现 | 一个装了 `torch` 的 Python（配置项 `backend.python`） |

## 跑起来

```sh
cd examples/hello-world/worker
python worker.py --config config.json          # 或 make run
```

启动时分别探测两种实现，各写一行状态：

```text
[rvbench-worker] native-c ready (ok: gcc (GCC) 13.2.0)
[rvbench-worker] WARNING: triton   unavailable: ModuleNotFoundError: No module named 'torch'
```

**缺环境不是致命错误。** 只要有一种实现可用，worker 照常启动；缺的那一种会在 `/capabilities` 里标 `ready: false` 并附错误原因。两种都不可用才退出。

## 提交

```sh
curl -s -X POST http://127.0.0.1:18801/jobs \
  -H 'Content-Type: application/json' -d @job-native-c.json
```

或者用 CLI 直连（`rb-cli` 与 `rvbench` 同一入口）：

```sh
rvbench --base http://127.0.0.1:18801 worker submit job-native-c.json --wait
```

返回 `202` 与 `JobRecord`。轮询等待结果：

```sh
curl -s "http://127.0.0.1:18801/jobs/<job_id>?wait=25"
```

`job-native-c.json` 与 `job-triton.json` 与 `rb-cli init` 生成的作业同形：候选程序就是 `request.source` 本身。

### 环境缺失时的行为

| 情况 | 启动 | 提交对应实现 |
|---|---|---|
| 没有 `gcc` / `clang` | `WARNING: native-c unavailable` | `409 {"error": "implementation 'native-c' is unavailable: no C compiler found ..."}` |
| 没有 `torch` | `WARNING: triton unavailable` | `409 {"error": "implementation 'triton' is unavailable: ModuleNotFoundError: No module named 'torch'"}` |

拒绝发生在准入阶段：这类请求在本机永远不可能成功，不占用队列，也不留下失败作业。

## 路由

| 路由 | 作用 |
|---|---|
| `GET /healthz` | 存活探测 |
| `GET /capabilities` | `WorkerRegistration`，含 `environment.implementations` 的逐实现能力与探测结论 |
| `GET /metrics` | Prometheus 文本：作业计数、实现就绪度、队列深度、身份 |
| `POST /jobs` | `WorkerSubmission{request}` → `202 JobRecord` |
| `GET /jobs/{id}?wait=N` | 查结果，`N` 上限 25 秒 |
| `GET /jobs` | 列出本进程见过的作业 |
| `DELETE /jobs/{id}` | 取消；运行中的作业会杀掉整个进程组 |

不提供 `/ui` 与 `/observability/summary`：这两处由 manager 与协议 crate 的控制台承担。

## 执行链路

```text
POST /jobs → 准入 → 队列（上限 32，容量 1）→ 编译 → 评估 → JobRecord 落盘
```

- **准入**：按 `backend` / `kind` / `implementation` / `op` / `dtype` / `ready` 匹配。不做隐式回退，不认识的直接 `409`。
- **编译**：`native-c` 写 `candidate.c` 后与固定 harness 一起编成可执行文件（`cc -O3 -fno-fast-math ... -lm`）；`triton` 写 `candidate.py` 并在子进程里 import + smoke 调用一次。`func` 只走到这一步。
- **评估**：先按 `seed` 生成输入，跑候选，再与 worker 侧的参照逐元素比较；`performance` 另做预热与采样，编译和首次调用不计入样本。
- **进程隔离**：每个子进程独占进程组，超时或取消时整组杀掉。
- 作业记录写在 `data/jobs/<job_id>/`，`artifacts` 带相对路径、大小与 SHA-256。

## 与生产 worker 的差别

| 项 | 这里 | `rvbench-spacemit-worker` |
|---|---|---|
| 输入生成 | 自带 `xorshift32_v1` | Torch RNG，与 suite 容差配套 |
| 算子 | `add` | 14 个 Triton 算子 + `add`/`abs` 原生 C |
| 投递 | 仅 push | push / pull |
| 控制台 | 无 | `/ui` 四标签页 |
| 重启恢复 | 未完成作业标记中断失败 | 同样标记，另有 outbox 重放 |

因为生成器不同，本 worker 的数值与生产 worker **不可直接对比**。`environment.generator` 里写明了这一点。

## 接到 manager

把 `config.json` 的 `manager` 从 `null` 改成 manager 地址即可：

```json
{ "manager": "http://10.18.60.100:18800" }
```

worker 每 2 秒向 `/cluster/workers/register` 上报一次注册信息（manager 的在线 TTL 是 30 秒）。
