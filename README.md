# 智能采集 — 高德地图打车采集

基于 **VLM 视觉定位** + **YAML 配置驱动** + **FSM 状态机** 的移动端自动采集。

## 架构

```
截图 → VLM (Qwen3-VL-Plus) 返回 bbox → Python 缩放坐标 → ADB 执行
```

- **零硬编码坐标** — VLM 用 1000×1000 归一化坐标系
- **YAML 配置流程** — 新流程写 ~30 行 YAML，无需 Python 代码
- **Double Check** — 勾选框操作先判断状态再点击，截图重验证

## 快速开始

```bash
# 安装依赖
python -m venv .venv && source .venv/bin/activate && pip install openai Pillow pyyaml
brew install android-platform-tools  # macOS

# 连接手机，USB 调试已开启
adb devices

# 运行（以 gaode v2 为例）
.venv/bin/python -m collector.demo \
    --platform gaode --flow v2 \
    --address "西北旺万象汇" --pickup "北京西站" \
    --adb-path $(which adb) \
    --vlm-api-key "sk-..." \
    --vlm-base-url "https://..."
```

每次运行前自动清空 `output/`，结果保存在 `output/`（截图）和 `output/_annotations/`（标注）。

## 流程

| 流程 | 入口 | 步骤 | 说明 |
|------|------|------|------|
| `v1` | 首页搜索框 | 5 | 搜索目的地 → 选候选 → 打车tab |
| `v2` | 底部打车tab | 7 | 直接进打车页 → 输起终点 → 上滑 |
| `v3` | 打车tab + 冒泡 | 16 | v2 前缀 + 三角形展开 → 盒子全选 → 确认 → 经济型全选 |

v2 / v3 需要 `--pickup`，v1 从当前位置出发。

## 计价采集

```bash
.venv/bin/python -m collector.demo ... --flow v1 --collect
```

进入打车页后自动执行：全选经济型 → 逐个供应商点击问号 → 工作日/休息日计价规则截图。

## 项目结构

```
assets/                # 素材（参考图）
collector/
  cli/                 # 参数解析和依赖组装（入口 demo.py）
  workflows/           # 正常确定性流程（YAML 流程引擎）
  platform/gaode/      # 高德页面、Prompt、Flow、Profile
    flows/             # YAML 流程配置
    profiles/          # 平台配置
  infrastructure/
    device/            # ADB 设备控制
    vision/            # VLM 视觉定位
  domain/              # 数据模型、接口、错误（不依赖 SDK）
  quality/             # 状态验证、Ground Truth、Diff
tests/
output/                # 运行时输出（截图 + 标注）
```

> 结构按 `codex.md` 目标布局重构；旧导入路径（如 `collector.adb_utils`、
> `collector.ride_pricing`）保留兼容层，新代码请使用 canonical 路径。
> 入口：`python -m collector.demo`（兼容）或 `python -m collector.cli.demo`。
> 平台由 `collector/platform/registry.py` 注册，见下方「多平台接入」。

## 多平台接入

平台注册表（`collector/platform/registry.py`）驱动 `--platform` 选择。接入新平台：

1. 新建 `collector/platform/<name>/`：`flows/`（YAML 流程）、`profiles/<name>.json`（Profile）。
2. 在 `collector/platform/<name>/platform.py` 实现 `build_platform()`：
   - `flows_dir` / `profile_path` / `default_flow`
   - 平台特有 CLI 参数（`add_cli_args`，如 gaode 的 `--address`/`--pickup`）
   - 模板变量（`build_flow_vars`）
   - 平台特有步骤（`step_handlers`，如 gaode 的 `pricing_collect`）
3. 在 `collector/platform/registry.py` 的 `_register_builtins()` 加一行注册。

通用代码（`cli`、`workflows/flow_engine`、`infrastructure`）无需改动。
示例：`tests/test_pricing_collect.py` 的 `test_fake_platform_zero_intrusion`。

## 测试

```bash
# Mock 测试（无需设备/API）
.venv/bin/python tests/test_double_check.py

# 真实 VLM 测试（需 API key）
.venv/bin/python tests/test_double_check.py --real-vlm \
    --vlm-api-key "sk-..." --vlm-base-url "https://..."
```


## todo 
- 特殊场站
- 出现广告弹窗（交给LLM进行判断）
- 目前点击勾选框，还是有点问题，容易识别错误（如何改进）

## 详细文档

[架构文档](~/Documents/My-Vault/20-Projects/智能采集/架构文档.md)
