# contracts/api

控制面 wire 合同的正本。

| 文件 | 地位 |
|---|---|
| `openapi.json` | 共享 wire 模型的唯一正本。OpenAPI 3.1.0，`info.version` = `1.2.0`（硬件指标轴），75 个 `components/schemas` 模型，无 `paths` |

里面只有模型，没有端点。HTTP 路径属于各服务自己的 OpenAPI：manager 仓与 worker 仓的 `contract/openapi.yaml`，它们以 YAML 语法的 JSON 文档形式引用本文件。

## 谁消费它

| 消费方 | 方式 |
|---|---|
| `rvbench-official-manager` 仓 `contract/openapi.yaml` | raw URL `$ref` 指向本仓 `main` 的该文件 |
| `rvbench-spacemit-worker` 仓 `contract/openapi.yaml` | raw URL `$ref` 指向本仓 `main` 的该文件 |
| [`rvbench-core-rust`](https://github.com/rvbench/rvbench-core-rust) | `openapi-to-rust` 的输入；`SPEC` 常量在编译期嵌入该文档 |

引用地址：

```
https://raw.githubusercontent.com/rvbench/rvbench/main/contracts/api/openapi.json
```

协议仓内的 `openapi.json` 是随版本发布的副本，不是正本。`cli/tests/contracts.rs` 的 `shipped_spec_matches_contract` 逐字节比对两侧，漂移即测试失败；`contract_is_canonical_json_without_crlf` 另查 LF 与 `paths` 为空。

## 变更流程

```sh
# 1. 改正本
$EDITOR contracts/api/openapi.json

# 2. 同步协议仓副本（默认 ../rvbench-core-rust）
python tools/sync_contract.py

# 3. 在协议仓重新生成类型
cd ../rvbench-core-rust && openapi-to-rust generate -c codegen.toml

# 4. 回归
cargo test --workspace --locked
```

生成物禁止手改。任何 wire 变更先落到本目录，再生成、测试、实现。

## 移动过路径

`2026-10-19` 由 `contracts/openapi.json` 迁到本目录。跨仓 `$ref` 用的是 raw URL，路径改变即失效——改路径时必须同步更新 manager 仓与 worker 仓的 `contract/openapi.yaml` 全部 `$ref`。
