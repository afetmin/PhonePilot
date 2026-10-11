# PhonePilot

项目名称为 PhonePilot；Python 包和命令沿用 `phone-use-runner`、`phone-use`，手机配套应用为 Phone Bridge。

Codex 负责规划、生成输入文字、审阅采集结果与总结，Jev 根据 UI 树、当前流程和近期动作选择下一步，Phone Bridge 直接执行。每轮由 Jev 同时选择操作和目标；可验证的控件与条目完成条件由执行器直接检查，其余完成条件由 Jev 判断。第一版支持 Android 11+，提供精简 Android 应用、Python SDK、CLI 和后台任务 MCP。

## 本地运行

```sh
cd ~/Desktop/PhonePilot
uv sync --python 3.11
uv run phone-use doctor
uv run phone-use-mcp
```

项目 `.env` 支持 `TYPESAFE_API_KEY` 和 `TYPESAFE_MODEL`，格式见 `.env.example`。默认模型固定为 `jev-1.13.0`。不需要额外的规划模型 API key，调用方 Codex 即规划器。Python 依赖已由 `uv.lock` 锁定。

手机端源码在 `android/`，只包含无障碍桥接、HTTPS 服务和本机授权页。安装本项目的 APK，开启无障碍并保持亮屏解锁，使电脑能访问手机显示的 HTTPS 地址。原 PhoneUse APK 的协议与本项目不同。

Android 构建需要 JDK 17 或 21、Android SDK 35 及 Build Tools。配置 `ANDROID_HOME` 或 `android/local.properties` 后：

```sh
cd android
./gradlew :app:assembleDebug
```

APK 位于 `android/app/build/outputs/apk/debug/app-debug.apk`。不需要 Root、ADB 服务或额外手机输入法；安装 APK 可自行选择通常使用的方式。再完成一次配对：

本机已安装 SDK 35、Build Tools 35.0.0、platform-tools 和 JDK 17，并已生成上述 APK。由于当前默认 Java 仍为 8，macOS 上重新构建时显式选择 JDK 17：

```sh
cd android
JAVA_HOME="$(/usr/libexec/java_home -v 17)" ./gradlew :app:assembleDebug
```

```sh
uv run phone-use pair --address https://PHONE:8443
# 核对手机与电脑显示的全部八位码，在手机批准后：
uv run phone-use pair --device-id DEVICE_ID --confirm
uv run phone-use devices
```

`--confirm` 表示用户已经确认核对码一致，不能由 agent 从网络状态推断。配对数据默认保存在项目 `.state/bridge.json`，可以用 `PHONE_USE_CONFIG` 改路径。TLS 证书指纹在发送凭据前核验，核对码由双方的承诺与随机数计算，授权在手机本机完成。手机只保存访问令牌摘要，可在本机撤销；重新配对会替换旧授权。

## 接入 Codex

本机已添加名为 `phone-use` 的全局 MCP，使用项目 `.env` 读取 Jev 凭据。其他机器可在安装环境后添加：

```sh
codex mcp add phone-use -- uv --directory /ABSOLUTE/PATH/PhonePilot run --locked phone-use-mcp
```

服务初始化会返回 agent 使用指令，无需额外安装 Skill。也提供了 `skills/phone-use/SKILL.md`，供其他 agent 使用。

| 工具 | 作用 |
| --- | --- |
| `phone_devices` | 已配对设备、可用能力与可启动应用 |
| `phone_run` | 接收自然语言目标和上层生成的子目标，启动后台任务 |
| `phone_wait` | 最多等待 20 秒，在完成、异常交接或超时后返回 |
| `phone_resume` | 更新子目标、提供文字、执行视觉动作、提交完成证据或批量审阅采集结果 |
| `phone_cancel` | 停止后续动作，按原请求 ID 尝试取消未完成输入 |

Codex 生成计划后提交一次 `phone_run`，执行器内部连续操作；每次点击不会返回 Codex。遇到 `needs_context`，Codex 根据现场自动调整，或分批审阅原文后调用 `phone_resume`。服务初始化时加载工具 schema 和流程指令；修改后需重新连接 `phone-use` MCP 才能使用新字段。MCP 不会递归调用父 Codex，也不会自行启动另一个大模型。

## 任务与完成条件

任务包括 `device_id`、`goal`、`subgoals` 和可选 `options`。每个子目标提供 `goal`、可选应用包名 `app`、由调用方大语言模型生成的 `input_text`、`completion`，以及是否采集原文 `collect`。默认最多 120 次动作、总计 300 秒；连续无法决策或动作无进展各限 3 次，单次等待最多 3 秒。可用 `max_decision_retries`、`max_no_progress`、`wait_timeout_seconds` 调整。不按置信度门槛拦截 Jev 的动作。旧 `options.confidence` 参数仍接受，但不再控制执行。计时包含向规划器交接后的时间。

执行器只用已有 UI 树和计数做轻量完成检查，不额外调用模型、截图或刷新页面。指定的 `package_name` 必须匹配；不采集原文的 `element` 在控件条件满足后直接完成，`items` 达到审阅通过的条目数后直接完成，旧 `records` 只检查已采集页数并等待 Jev 判断。条件未满足时不提供 `DONE`。消息任务只有本次发送动作已执行后才开放 `DONE`，避免把草稿或旧的同文消息当成本次发送。`completion.kind` 支持：

- `element`：描述目标控件的 text、description 或 view_id，以及可选状态/action。
- `records`：用 `collect: true` 采集原文，达到 `min_pages` 后才能完成。页数按界面文本去重，不等于不同房源数；内容是否相关仍由 Jev 判断。
- `items`：用 `collect: true` 连续采集，声明 `min_items`、`required_fields` 和 `review_every`（默认 5）。Codex 分批筛选去重、提交原文字段证据；达到合格条目数后自动停止。
- `message`：描述收件人、准确消息和发送按钮。大语言模型生成一次文案，执行器直接填入原文，Jev 决定何时输入、发送和完成。
- `agent`：直接由 Jev 根据当前子目标判断完成，无需逐步交回 Codex 验收。

`context_selector` 可以描述目标页面上下文，供 Jev 判断；执行器不再据此强制回交核验。选择器使用原始字段而非决策 label；需要 view_id 的任务自动请求详细树。对浏览任务，`READ` 按需展开文字，保留原文与来源，不生成标题、作者或链接。

需要“10 条不同房源”等计数结果时，使用一次导航加一个连续采集子目标，不复制 10 个相同阅读子目标。参考 `examples/xhs-rentals.json`。导航不采集，采集阶段每批返回 `needs_context / records_review`：调用方读取 `context.records`，按任务要求排除无关、重复、失效记录，再用 `phone_resume.record_review` 提交全部批次 `record_ids` 和合格 `items`。每个 item 提供稳定 `key`；每个字段提供 `value`、`record_id`、`quote`，其中 quote 必须来自对应原文，value 必须包含在 quote 内。同 key 合并字段，不重复计数；不合格批次可提交空 items 后继续。相关性与条目身份由调用方判断，执行器验证来源、必填字段和数量。日期、费用、链接等缺失字段保持未知，不能补猜。具体请求格式见 `src/phone_use_runner/agent.md`。

示例在 `examples/`，其中设备 ID、联系人及页面条件需要由调用方结合实际界面填写：

```sh
uv run phone-use validate examples/wechat.json
uv run phone-use run examples/search-and-read.json
```

CLI `run` 是单次执行入口，遇到交接时输出现场并结束进程。需要自动重规划和续跑时使用持久 MCP 或以下 SDK：

```python
from phone_use_runner import PhoneRunner, RunRequest

with PhoneRunner() as phone:
    request = RunRequest.model_validate(task_dict)
    state = phone.run(request)
    while state["status"] == "running":
        state = phone.wait(state["task_id"])
    # 上层根据 needs_context 的现场准备补充内容：
    # phone.resume({"task_id": state["task_id"], "input_text": "prepared literal text"})
```

## 自动恢复与证据

普通循环仅取树；Jev 选择动作后立即执行。打开页面、点击和返回后，执行器通过实际 `wait_for_change` 事件做最多 400ms 的短暂稳定等待；桥接端未实现 `stable_wait_ms`，不能依靠该参数。明确失败、`BLOCKED` / `NEEDS_CONTEXT`、目标为 `NONE` 或树暂时不可读时，先短暂等待并重新观察，连续 3 次仍无法前进就进入 `needs_context`，由 Codex 调整后 `phone_resume`。连续 3 次动作未改变界面且没有新采集页，也会交接。短窗口内重复返回同一界面触发 `navigation_loop`；采集时连续至少 12 次循环没有新增长文本触发 `no_new_content`。有待审阅条目时优先回交批次，避免丢弃已经采集的内容。低置信度本身不触发验收。Jev 选择 `WAIT` 时，最多等待 3 秒；超时后刷新树重新判断，连续超时同样按重试上限交接。正常决策不增加模型调用；轻量交接保留当前 UI 树，不自动截图。滚动候选附带已有的容器类型和位置，帮助 Jev 区分列表、分类容器和图片轮播。所有模型目标来自当前候选，Jev 不生成选择器、代码或坐标。

视觉交接返回真实 MCP 图片及坐标映射。`visual_action.points` 使用所给图片的像素，执行器映射到屏幕物理像素，带原 observation_id 和窗口校验。图片与 UI 树不是同帧原子数据，节点和坐标最终仍由手机校验。快照过期会被拒绝。

发送前保留原请求 ID 和意图；Jev 选择观察到的发送按钮后直接执行。已填写正确的输入框不会再提供重复输入候选；发送尝试后关闭输入和发送候选。每轮请求明确包含发送是否执行、发送前后的消息数量和输入框状态，Jev 据此判断完成。近期动作保留观察到的目标文字，帮助判断标题缺失时的导航结果。普通消息流程不再要求收件人截图证明或人工验收。发送结果因通信故障未知时只查询原请求，避免自动重复派发；旧 `phone_resume.verification` 入口仅保留给这类异常状态处理。系统接受发送不等于服务器确认送达。

记录保存在 `runs/TASK_ID/`：`result.json` 包含所有原文、计时和执行证据，以及 `decision_retries` 和 `last_decision`；`events.jsonl` 包含动作意图、请求 ID、目标、滚动方向、动作错误码、决策、重试原因及采集/合格条目计数，`handoff.jpg` 是最近技术交接图片。旧流程工具每次最多返回 20 条原文记录；`items` 流程通过 `context.records` 返回当前审阅批次，避免每次等待重复传输旧原文，结构化结果在 `items`。`report_path` 提供全部原始数据。文件默认只对当前用户开放；它们可能包含聊天或笔记内容。

任务状态在 MCP 进程内维护。关闭进程会停止后续动作；重启后不会自动恢复或重发。先检查旧报告的 pending_action 并核对原结果，再开始新的消息任务。取消不能撤回已注入的手势。结果未知的任务保留设备占用，防止替代任务与未确认动作重叠。

## 性能与验收

```sh
uv run phone-use benchmark --device-id DEVICE_ID --samples 20
uv run --extra dev ruff check src
uv run python -m compileall -q src
uv build
```

基准分别测 tree 与 both，输出客户端 RPC 的 P50/P95、手机 collection_ms 和失败率。截图模式遵循 Android 调用频率，采样间隔不计入 RPC 时间。任务记录 Jev 实际模型版本、输入 token、动作和决策循环耗时。

自定义客户端复用 HTTPS 长连接；桥接服务不再因空闲 10 秒关闭连接。每个新连接在发送令牌前核验证书指纹；每个 API 响应校验设备 ID，动作绑定服务实例和观察引用。手机端根据无障碍事件唤醒变化等待，当前窗口之外的内容刷新不会使该窗口的观察失效。默认只输出当前活动窗口的可见有效节点；未实现增量 UI 树或局部子树采集。

工程目标为 tree-only RPC P95 <= 300 ms、普通决策动作循环 P95 <= 1 秒。任务中的 cycle 含动作后观察；应用加载和业务等待单列分析。中文任务表现需要真实页面评估。

实机验收目标：微信发送、小红书搜索与阅读各 10 次，至少 9 次自动完成，错联系人与重复发送为零。本机已完成 Android 16 手机配对、真实 API、TLS、UI 树和截图验证，以及一次由 Jev 直接决定的微信测试消息发送；多轮业务验收及决策循环性能目标仍待测，具体结果见 `VALIDATION.md`。使用编译、静态检查、MCP 协议与冒烟验证，不新增单测或 UI 测试套件。

来源：[PhoneUse](https://github.com/nidhoggfgg/PhoneUse)、[jev-ultrafast](https://github.com/browser-use/jev-ultrafast)、[TypeSafe API](https://docs.typesafe.ai/api)、[Codex MCP](https://developers.openai.com/codex/mcp/)。第三方版本与许可证见 `THIRD_PARTY_NOTICES.md`。
