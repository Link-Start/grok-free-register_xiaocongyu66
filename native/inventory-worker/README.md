# inventory-worker (Rust)

账号库存扫描与 CPA / sub2api 合并包构建。与 Python `grok_register.account_inventory` 对齐，是项目 **硬性** 原生组件之一。

## 构建

```bash
# 推荐：项目根目录
bash scripts/build-native.sh

# 或单独
cd native/inventory-worker
cargo build --release
cp target/release/inventory-worker ./inventory-worker
```

## 用法

```bash
./inventory-worker version
./inventory-worker check --keys-dir keys
./inventory-worker scan --keys-dir keys --json
./inventory-worker rebuild --keys-dir keys
```

环境变量：`KEY_EXPORT_DIR`（默认 `keys`）。

## 与 Python 的关系

| 引擎 | 职责 |
|------|------|
| Rust (默认) | 合并包重建、大批量扫描 |
| Python | 编排、面板 API、ledger 融合、回退 |

`INVENTORY_ENGINE=rust|python`，`INVENTORY_WORKER_BIN` 可覆盖二进制路径。
