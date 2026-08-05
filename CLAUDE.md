# CLAUDE.md

本文件是项目的 AI Coding 规则和开发计划。每次对话开始时先阅读，结束时必须更新“开发计划”和“开发记录”。

用户的当前要求优先于本文档；如有冲突，需要在交付说明中指出。

## 1. 项目定位

这是一个确定性优先的 Android 打车 App 截图采集系统：

```text
自动化流程 + 稀疏视觉定位 + 闭环截图采集 + 状态验证 + 异常恢复
```

本项目负责：

- 打开 App、输入起终点、进入计价页和遍历运力商。
- 以尽量少的截图完整覆盖价格、补贴和计价规则。
- 把截图交给已有线上识别链路。
- 保存运行证据，支持识别结果 Diff 和异常分析。

本项目不负责重建截图识别和落库服务，也不执行叫车、支付、登录、验证码等高风险操作。

## 2. 核心原则

### 2.1 正常流程不用 Agent

正常流程由 FlowEngine/FSM 确定性执行。以下动作不调用 LLM：

- 打开 App、等待、输入、返回、截图、滑动。
- 已知重试、超时、图片相似度、位移和重叠计算。

只有动态元素定位或复杂视觉判断才调用 VLM。Recovery Agent 只在固定重试和已知恢复策略失败后介入。

### 2.2 每个关键动作都要验证

```text
执行动作 → 等待稳定 → StateVerifier 验证 → 成功继续 / 失败恢复
```

LLM、VLM 和 Agent 不能自行宣布成功。无法证明状态正确时必须停止并保留现场。

### 2.3 详情页采用闭环采集

详情页终止条件是出现蓝色“预约用车”，不是“滑到底”。

每轮执行：

```text
滑动一次
→ 等待稳定
→ 获取一张候选图
→ 检测“预约用车”
→ 测量实际位移和内容重叠
→ 有新内容才保存为关键帧
→ 调整下一次手势
```

约束：

- 手势距离不等于页面实际位移，必须通过 OCR 锚点或图像重叠测量。
- 相邻正式截图保留约 20%～30% 重叠。
- 同一张候选图同时用于终点检测、位移计算和关键帧选择。
- Probe Frame 可以临时保存；Keyframe 才进入识别链路。
- 默认用本地 OCR 检测“预约用车”，VLM 只做兜底。
- （CAP-01/CAP-05 当前实现：按用户要求，进入详细计价页后每次滑动都调用 LLM 判断是否出现“预约用车”，出现“预约用车”**或**页面不再变化即停止滚动并回顶后继续；本地 OCR 留待 CAP-02+ 优化。）
- 无法证明内容连续时停止，不得静默跳过内容。

## 3. 目标结构

已按本结构一次性重构完成（REF-01，2026-08-03）：现有代码全部迁移到位，旧导入路径保留兼容层。以下目录当前已有代码：`cli/`、`workflows/`、`platform/gaode/`、`infrastructure/{device,vision}/`、`domain/`、`quality/`；其余目录（`application/`、`features/detail_capture/`、`infrastructure/persistence/`、`integrations/recognition/`、`recovery/`）为后续子任务预留，落地时再创建：

```text
collector/
├── cli/                    # 参数解析和依赖组装
├── application/            # 用例编排、RunContext
├── domain/                 # 数据模型、接口、错误；不依赖SDK
├── workflows/              # 正常确定性流程
├── features/
│   └── detail_capture/     # 终点、位移、自适应滑动、关键帧
├── platform/
│   └── gaode/              # 高德页面、Prompt、Flow、Profile
├── infrastructure/
│   ├── device/             # ADB；未来可替换为Go服务
│   ├── vision/             # Qwen、PaddleOCR、本地模型
│   └── persistence/        # Manifest、Incident
├── integrations/
│   └── recognition/        # 现有识别服务客户端
├── quality/                # Ground Truth、标准化、Diff
└── recovery/               # 已知策略、SafetyGuard、Recovery Agent
```

依赖规则：

- `domain` 不得导入 ADB、模型 SDK、HTTP SDK 或平台代码。
- 基础设施实现接口，由 `application/cli` 组装。
- 高德特有逻辑不得散落在通用模块。
- 新需求不得继续堆进 `flow_engine.py` 或 `ride_pricing.py` 的条件分支。
- 旧导入路径已由兼容层保留可用；新代码一律使用 canonical 路径，禁止再经旧路径新增依赖。

### 3.1 接入新平台

平台注册表（`collector/platform/registry.py`）驱动 `--platform` 选择；新增平台不改通用代码：

1. 新建 `collector/platform/<name>/`：`flows/`（YAML 流程）、`profiles/<name>.json`（Profile）。
2. 在 `collector/platform/<name>/platform.py` 实现 `build_platform()`，返回 `domain.platform.Platform`：
   - `flows_dir` / `profile_path` / `default_flow`
   - 平台特有 CLI 参数（`add_cli_args`）
   - 模板变量（`build_flow_vars`）
   - 平台特有步骤 handler（`step_handlers`，如 gaode 的 `pricing_collect`）
3. 在 `collector/platform/registry.py` 的 `_register_builtins()` 加一行注册。

零侵入示例见 `tests/test_pricing_collect.py::test_fake_platform_zero_intrusion`。

需要长期稳定的接口：

- `DeviceController`
- `GroundingService`
- `EndMarkerDetector`
- `DisplacementEstimator`
- `StateVerifier`
- `RecognitionGateway`
- `IncidentRepository`
- `RecoveryAgent`

当前继续使用 Python。只有监控证明 ADB/设备管理成为瓶颈时，才考虑用 Go 实现 `DeviceController`，不整体重写业务代码。

## 4. 单对话单子任务规则

这是后续 AI Coding 的强制开发节奏。

### 4.1 开始对话

1. 阅读本文件和相关代码、配置、测试。
2. 用户每次只指定一个子任务；如果需求过大，先拆分任务，不同时实现多个任务。
3. 在“开发计划”中找到对应任务，将状态从 `TODO` 改为 `IN_PROGRESS`。
4. 任意时刻最多只能有一个 `IN_PROGRESS`。
5. 明确本次验收条件后再修改代码。

一个合格的子任务应当：

- 只有一个清晰结果。
- 可以独立测试和验收。
- 通常只涉及一个能力或少量直接相关文件。
- 不包含“顺便重构”的无关改动。

### 4.2 开发过程

- 先检查现状和已有修改，不覆盖用户代码。
- 优先完成最小可运行的纵向切片。
- 所有循环和重试必须有上限，外部调用必须有超时。
- 真实设备和付费 API 只能显式开启，普通测试不得调用。
- 如发现任务实际超过一个子任务，停止扩展，把剩余工作加入计划表。

### 4.3 对话收尾

完成开发后必须回到本文件：

1. 测试通过：把当前任务改为 `DONE`。
2. 无法完成：改为 `BLOCKED`，写明原因和解除条件。
3. 只完成一部分：不得标记 `DONE`；拆出剩余任务并保持当前任务 `BLOCKED` 或重新定义验收范围。
4. 在“开发记录”追加一行：日期、任务ID、状态、修改摘要、验证证据。
5. 更新“当前任务”为 `NONE`。
6. 不自动开始下一项任务，等待用户开启下一次对话。

状态定义：

| 状态 | 含义 |
|---|---|
| `TODO` | 尚未开始 |
| `IN_PROGRESS` | 本次对话唯一正在开发的任务 |
| `BLOCKED` | 被设备、接口、数据或决策阻塞 |
| `DONE` | 满足验收条件并完成验证 |

### 4.4 提交与推送

- **禁止主动 commit / push**：用户没有显式说明「push / 推送」时，不执行 `git commit` 和 `git push`。
- 只有用户显式说「push / 推送」后，才允许提交并推送代码。
- 完成子任务后默认把改动保留在工作区，等待用户指示。

## 5. 开发计划

当前任务：`NONE`

| ID | 子任务 | 状态 | 验收条件 |
|---|---|---|---|
| BASE-00 | 现有真机 Demo 基线 | `DONE` | ADB、Flow、VLM定位和计价流程已可演示 |
| CAP-01 | 详情页终点“预约用车”检测 | `DONE` | 进入详细计价页每次滑动后调用LLM判断“预约用车”；检测到即停止滚动并回顶后继续（工作日回顶→休息日，休息日采完退出）；Mock 测试通过 |
| CAP-02 | 测量页面实际滚动位移 | `TODO` | 输出位移、重叠比例和置信度；离线测试通过 |
| CAP-03 | 自适应滑动控制 | `TODO` | 根据实测位移调整手势，循环有上限 |
| CAP-04 | Probe/Keyframe与Manifest | `TODO` | 最少关键帧覆盖完整内容，产物可追踪 |
| CAP-05 | 详情页退出条件：预约用车或页面无变化 | `DONE` | 出现“预约用车”**或**页面不再变化即停止滚动（本地像素比对）；离线测试通过 |
| CAP-06 | 经济型运力商批量采集（目标10/采完即停） | `DONE` | 达到 max_suppliers=10 或「全选经济」下识别到的运力商采完即结束；列表最后一个采完且不够时下滑打车页查看新运力商；找不到问号跳过 |
| ARCH-22 | S2 单屏行级识别 + 每轮点导航锚定 + 无新才滑（CAP-11） | `DONE` | 新提示词按行级特征+标题上方截断；每轮 全选校验→点左侧导航「经济」→识别→采集→无新才小幅下滑；过滤层 _SKIP_EXACT+_SKIP_KEYWORDS 双保险；S2 日志含 屏上/识别到/未采集 |
| OCR-01 | 接入滴滴内部 OCR：v2 详细计价页「预约用车」检测（OCR 优先，VLM 兜底） | `DONE` | ocr_client（自包含）+ OcrTextExtractor 实现 TextExtractor；scroll_until_visible 支持 ocr_first；CLI --no-ocr 开关；OCR_PROFILE=DEV 切环境；离线测试通过 |
| CAP-10 | 截图优化：滚动/check 合并 + collect 只落 result 帧 | `DONE` | 详情页每轮 1 张滚动帧（无 scroll/check 重复）；result 数量随下滑次数；collect 探针不落盘 |
| CAP-09 | 经济型栏边界感知：S2 只识别经济型栏 + 特快车/出租车/优享型以下不再采集 | `DONE` | S2 返回 (suppliers, economy_ended)；栏结束（出现特快车/出租车/优享型）即使不足 10 也停止；快车/拼车入关键词兜底 |
| CAP-08 | 运力商列表过滤：排除的士/出租/优享 | `DONE` | S2 关键词过滤（的士/出租/优享）；找不到问号不崩溃；S3c 失败不计入 collected |
| CAP-07 | 打车页滑动：每次截图 + 距离减半 | `DONE` | 打车页列表每次下滑 1/6 屏（原 1/3 屏的一半）并截图；离线测试通过 |
| VER-01 | 关键页面StateVerifier | `TODO` | 关键动作均有明确后置验证 |
| INT-01 | 接入现有线上识别服务 | `TODO` | 支持超时、重试、幂等和原始响应保存 |
| QC-01 | 测试集、Ground Truth与数据Diff | `TODO` | 可区分采集缺失、识别错误、重复和合并错误 |
| REC-01 | Incident记录与已知恢复策略 | `TODO` | 高频异常可确定性恢复并保留完整上下文 |
| REC-02 | 受限Recovery Agent | `TODO` | 只允许白名单动作，每步由Verifier确认 |
| MOD-01 | 本地Grounding模型评测 | `TODO` | 框架稳定后，用同一测试集对比线上模型 |
| INF-01 | Go设备层必要性评估 | `TODO` | 根据长期运行和多机数据决定，不预先重写 |
| REF-01 | 按 codex.md 目标结构重构代码目录 | `DONE` | 现有模块迁移到 cli/workflows/platform/infrastructure/domain/quality；旧导入路径兼容；离线测试通过 |
| ARCH-05 | Platform 抽象与注册表（多平台接入） | `DONE` | 新增平台零侵入：`--platform` 选择 + 平台注册表 + 通用步骤 handler；gaode 行为不变；离线测试通过 |
| ARCH-06 | 截图与标记输出模式（debug/collect） | `DONE` | 标记图仅 debug 输出；collect 模式仅保存详细计价页截图；离线测试通过 |
| ARCH-07 | collect 模式采集打车页 + 耗时统计 | `DONE` | collect 保存打车页(含滑动)与详细计价页；输出每步/API/等待耗时；离线测试通过 |
| SEL-01 | 目标锚定的幂等全选 ensure_all_selected | `DONE` | 定位/判定拆分，仅判主勾选框ROI；素材100次成功率100%(700/700)；点击后重验；真机验证通过 |
| RES-01 | 采集结果整理（必要截图筛选+聚合） | `DONE` | output 筛选打车页(select_all_after)+每(标签×运力商)前4张滚动截图，聚合到 result/工作日|休息日/{冒泡页,<运力商>}/，打车页入冒泡页（每大文件夹1次共2次）；离线测试+真实 output 验证通过 |
| PERF-03 | 标签坐标复用 + 详情页等待再收敛（方案五） | `DONE` | 工作日/休息日标签坐标首次 LLM 记录后复用（不调 LLM/不截图）；tab_wait 0.5 / detail_scroll_wait 0.3 / scroll_top_wait 0.1 / back_wait 0.8；详见 耗时优化方案-2026-08.md |
| PERF-02 | 返回导航确定性化 + 确定性等待收敛（方案一+二） | `DONE` | 详细页→弹窗→打车页直接 adb.back() 不调 VLM；pricing_page_wait 1.2s / after_confirm_wait 1.5s / tab_wait 0.8s / back_wait 1.0s；详见 耗时优化方案-2026-08.md |
| PERF-01 | 耗时优化（P2） | `TODO` | 减少截图/标注/固定等待与VLM调用开销；collect 模式耗时归因准确 |

计划维护规则：

- 新需求先拆成可独立验收的子任务，再加入表格。
- 不允许把多个任务合并成一个长期 `IN_PROGRESS`。
- 任务顺序可以根据用户要求调整，但必须记录原因。
- `DONE` 任务需求发生变化时，新建任务ID，不篡改历史结果。

## 6. 测试和完成标准

基础离线检查：

```bash
python tests/test_double_check.py
python tests/test_pricing_collect.py
python tests/test_select_all.py
python -m compileall collector tests
```

真实 VLM、线上识别和真机测试必须显式执行，并在开发记录中标明。

任务标记 `DONE` 前至少满足：

- 代码位于正确层级，没有新增反向依赖。
- 验收条件对应的测试通过。
- 正常路径没有新增不必要的LLM或Agent调用。
- 关键动作有验证；失败能够安全退出。
- 配置、README或测试素材按需同步。
- 不包含密钥、Token、账号或个人敏感信息。
- 说明真实设备/API是否验证。

## 7. 安全规则

- 禁止执行叫车、支付、登录、验证码和不可逆提交。
- Agent只允许：返回、等待、重启App、滚动、安全目标点击和失败退出。
- API Key只来自环境变量或密钥服务，不写入代码、Prompt、YAML和日志。
- 不默认清空输出根目录；每次运行使用独立 `run_id`。
- Keyframe是不可变原始证据；标注图另存。
- 不引入Temporal、LangGraph、完整Mobile-Agent或Go重写，除非当前任务明确要求且有数据依据。

## 8. 开发记录

每次对话结束追加一行，保持简短；验证命令较多时只写关键证据。

| 日期 | 任务ID | 状态 | 修改摘要 | 验证证据 |
|---|---|---|---|---|
| 2026-08-03 | DOC-01 | `DONE` | 精简CLAUDE.md，加入单对话单子任务和状态回写机制 | 文档结构检查 |
| 2026-08-03 | REF-01 | `DONE` | 按 codex.md 目标结构重构：cli/workflows/platform/gaode/infrastructure{device,vision}/domain/quality；旧导入路径保留兼容层；flows/profiles 移入 platform/gaode | compileall + test_double_check + test_pricing_collect 通过；旧路径 smoke 通过 |
| 2026-08-03 | DOC-02 | `DONE` | codex.md/CLAUDE.md 第3节改为一次性重构已完成；明确当前有代码目录与预留目录；依赖规则改为 canonical 优先 | 文档检查 |
| 2026-08-03 | ARCH-05 | `DONE` | Platform 抽象与注册表：domain/platform.py + platform/registry.py + gaode/platform.py；flow_engine 去掉对高德直接依赖，pricing_collect 改为平台 handler；cli 增加 --platform | compileall + test_double_check + test_pricing_collect（含 Suite 3b 注册表/零侵入）通过 |
| 2026-08-04 | ARCH-06 | `DONE` | 截图/标记输出模式：--mode debug|collect；标记图仅 debug；collect 仅保存详细计价页截图，其余走临时目录供 VLM 定位 | compileall + test_double_check + test_pricing_collect（含 Suite 3c 模式测试）通过 |
| 2026-08-04 | ARCH-07 | `DONE` | collect 模式改为进入打车页后开始保存（含刚进打车页/滑动/详细计价页）；新增耗时统计（每步/API/等待） | compileall + test_double_check + test_pricing_collect（Suite 3c 含耗时统计）通过 |
| 2026-08-04 | SEL-01 | `DONE` | ensure_all_selected 目标锚定幂等全选：domain/checkbox + infra/vision/checkbox + gaode/select_all(含离线定位启发式) + select_all 平台步骤(v3) + 删 NL 兜底；素材100次成功率700/700 | compileall + test_double_check + test_pricing_collect + test_select_all 通过 |
| 2026-08-04 | RUN-01 | `DONE` | 真机 v2 debug 全流程跑通（起终点→计价采集2家）；S1 全选经济目标锚定：未勾选→点击→已勾选；耗时 250.4s（API 58.3s/等待52.2s） | 真机日志（run_real_test.txt） |
| 2026-08-04 | CAP-01 | `DONE` | 详细计价页每次滑动后调用LLM判断蓝色“预约用车”：检测到即停止滚动并回顶后继续（工作日回顶→休息日，休息日采完退出）；_detect_end_marker 独立方法并计入 vlm_calls；_scroll_to_bottom 返回检测结果 | compileall + test_double_check + test_pricing_collect（Suite1 检测解析 / Suite2 每次滑动检测·终止滚动·回顶）通过；真实素材 VLM 验证：工作日第4张/休息日第3张检测到「预约用车」，流程可完成 |
| 2026-08-04 | CAP-05 | `DONE` | 详情页退出条件改为“预约用车”**或**页面不再变化：新增 _page_unchanged 本地像素比对（缩放灰度+裁状态栏+阈值）；每次滑动后未命中标记即评估页面是否无变化；max_detail_swipes 可配 | test_pricing_collect（CAP-05 页面比对/标记或稳定退出 + FSM 全流程）通过 |
| 2026-08-04 | CAP-06 | `DONE` | 运力商采集循环重构为 _collect_suppliers：目标 max_suppliers=10；终止=达到目标或经济型采完（下滑确认无新列表）；列表最后一个采完不够则下滑打车页看新运力商；找不到问号跳过 | test_pricing_collect（CAP-06 采集循环终止条件 + FSM 全流程回归）通过 |
| 2026-08-04 | CAP-10 | `DONE` | 详情页滚动循环每轮 1 张（scroll_{i+1}，无 check 重复帧，debug/collect 一致）；collect 模式探针帧（q/popup/detail_*/exit/s2/s4 等）进临时目录不落盘，仅保留 select_all_after + scroll_0..N；organizer 取全部滚动帧 | test_pricing_collect（探针不落盘/无 check 帧/数量不固定 + 全流程回归）通过 |
| 2026-08-04 | CAP-09 | `DONE` | S2 提示词按灰线分栏只识别「经济型」栏并返回 economy_ended；终止=出现特快车/出租车/优享型，其下不再采集（不足 10 也停）；_SKIP_KEYWORDS 加 快车/拼车 | test_pricing_collect（S2响应解析/栏结束即停场景 + 全流程回归）通过 |
| 2026-08-04 | CAP-08 | `DONE` | 运力商列表关键词过滤（_SKIP_KEYWORDS=的士/出租/优享）；_s3a 修复 None 解包崩溃；_collect_suppliers 中 S3c 失败不计入 collected | test_pricing_collect（出租车过滤扩展 + 找不到问号不崩溃 + 采集循环 S3c 失败）通过 |
| 2026-08-04 | CAP-07 | `DONE` | _swipe_down 距离 1/3 屏 → 1/6 屏，每次滑动仍截图（s4_next/s4_nomore） | test_pricing_collect（CAP-07 滑动截图+距离断言）通过 |
| 2026-08-04 | DOC-03 | `DONE` | codex.md/CLAUDE.md 增加 §4.4：禁止主动 commit/push，仅用户显式「push」时提交推送 | 文档检查 |
| 2026-08-04 | RES-01 | `DONE` | 新增 screenshot_organizer：必要截图=打车页(select_all_after)+每(标签×运力商)scroll_0..3；聚合 result/工作日|休息日/{冒泡页,<运力商>}/，打车页入冒泡页（每大文件夹1次共2次）；handle_pricing_collect 结束后调用；output 缺失/为空安全跳过；result/ 加入 .gitignore | test_pricing_collect（含 RES-01 聚合测试）通过；真实 output/ 验证 18 张/冒泡页×2+4组结构正确 |
| 2026-08-04 | PERF-03 | `DONE` | 标签坐标复用（_tab_coords 缓存，首次 LLM 记录后续直接点击）+ 详情页等待再收敛（tab_wait 0.5/detail_scroll_wait 0.3/scroll_top_wait 0.1/back_wait 0.8） | test_pricing_collect（PERF-03 标签复用测试 + 全流程断言标签 ground=2）通过 |
| 2026-08-04 | PERF-02 | `DONE` | 返回导航确定性化（adb.back() 替代 VLM ground）+ 确定性等待收敛（back_wait 1.0/tab_wait 0.8/pricing_page_wait 1.2/after_confirm_wait 1.5）；新建 耗时优化方案-2026-08.md | test_pricing_collect（含 PERF-02 返回确定性化测试 + 全流程回归）通过 |
| 2026-08-05 | ARCH-22 | `DONE` | S2 单屏行级识别（新提示词）+ 每轮点左侧导航「经济」锚定 + 无新才小幅下滑；handle_s2_list_suppliers 移除内部下滑；supplier_parse 双保险过滤；S2 日志三层可见 | test_pricing_collect + 全量 7 测试文件通过 |
| 2026-08-04 | OCR-01 | `DONE` | 接入滴滴内部 OCR：infrastructure/vision/ocr_client.py + ocr_adapter.py（实现 TextExtractor）；_do_scroll_until_visible 支持 ocr_first（命中→不调 VLM，未命中/异常→VLM 兜底）；detail_capture_gaode.yaml 开启；ExecutionContext 挂载 + ocr_calls 统计；CLI --no-ocr | tests/test_ocr_client.py 全部通过 |
| 2026-08-04 | PERF-01 | `TODO` | 耗时优化 P2：debug 模式非准确耗时；API/等待/设备+编码三块归因，待优化 | 250.4s 真机日志归因 |

## 9. AI交付格式

```text
结果：本次只完成了哪个任务。

修改：
- 关键文件和内容

验证：
- 实际执行的测试及结果

计划状态：
- TASK-ID: DONE / BLOCKED
- 当前任务已回写为 NONE

风险或下一步：
- 只说明，不自动开始下一任务
```
