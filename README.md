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

# 运行（以 v2 为例）
.venv/bin/python -m collector.demo \
    --address "西北旺万象汇" --pickup "北京西站" \
    --adb-path $(which adb) \
    --vlm-api-key "sk-..." \
    --vlm-base-url "https://..." \
    --flow v2
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
  demo.py              # 入口
  flow_engine.py       # YAML 流程引擎
  vlm_grounder.py      # VLM 视觉定位
  ride_pricing.py      # 计价采集 FSM
  adb_utils.py         # ADB 封装
  flows/               # YAML 流程配置
  profiles/            # 平台配置
tests/
output/                # 运行时输出（截图 + 标注）
```

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
