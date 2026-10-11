# Jev ultrafast 决策链路与 PhonePilot 对照

> 状态更新（2026-10-11）：以下源码对照描述改造前的 PhonePilot。按用户明确选择，当前已采用 Jev 判断后直接执行的循环：取消置信度门槛、逐步完成验收和三次无进展回交；`DONE` 直接推进，`WAIT` 等待有效界面变化而不重复提交同一状态。Agent 负责流程规划和生成原文，Jev 负责每轮动作及目标，Bridge 负责执行。收尾结果见 [VALIDATION.md](../VALIDATION.md)。

## 范围和源码快照

这份报告只做源码对照，没有调用手机、TypeSafe/Jev 或其他模型，也没有修改生产代码。

- 官方仓库：[`browser-use/jev-ultrafast`](https://github.com/browser-use/jev-ultrafast)
- 固定源码提交：`1231850a0bf1a0c0341fe408ef1668dbbfdfac46`
- 固定 tree：`987b481ed8ef400911ca471bce38c7adfdabc45c`
- 本地只读副本：`.state/reference/jev-ultrafast/`

## 先给结论

Jev-ultrafast 的速度来源不是“每一步少做一次模型判断”。它仍然在自动循环中每轮调用一次 `choose`；速度来自把当前页面压缩成有限、带操作类型的候选集合，然后在一次 TypeSafe 请求中同时询问“下一种操作”和“该操作的目标”，最后由代码校验和执行。官方 README 也明确描述为每轮一个请求、操作和目标头共享同一份观察状态。[`model.py`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/jev_ultrafast/model.py#L48-L148) [`README.md`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/README.md#L31-L51)

PhonePilot 的 `JevPolicy.choose` 已经有同一条核心路径：一次请求包含 operation 和各 operation 的 target head，普通决策默认只传紧凑树，不传截图，且 `JevPolicy` 持有复用的 HTTP/2 client。[`jev.py`](../src/phone_use_runner/jev.py#L44-L199) [`runner.py`](../src/phone_use_runner/runner.py#L269-L282)

当前真正的行为差异是安全策略，而不是缺一套全新的决策框架：

1. 官方 Agent 每轮只要求返回合法且概率最高的选择，没有 confidence 门槛；PhonePilot 默认要求 `confidence >= 0.7`，连续两次低于门槛才交接给上层。[`model.py`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/jev_ultrafast/model.py#L30-L45) [`agent.py`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/jev_ultrafast/agent.py#L86-L100) [`runner.py`](../src/phone_use_runner/runner.py#L518-L533)
2. 官方 Agent 选择 `DONE` 后，在页面仍 fresh 时会直接把本地状态改成 `done`；独立结果校验放在 Flights 示例等业务层。PhonePilot 选择 `DONE` 后固定进入 `verification_required`，不会自动完成；`completion.kind="agent"` 在 PhonePilot 的本地 `_completion` 中也没有可自动满足的谓词，因此会保留人工/上层验证停顿。[`agent.py`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/jev_ultrafast/agent.py#L86-L100) [`README.md`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/README.md#L118-L126) [`runner.py`](../src/phone_use_runner/runner.py#L326-L347) [`runner.py`](../src/phone_use_runner/runner.py#L537-L542)
3. 官方有浏览器 DOM 节点身份、可见性和点击遮挡校验；这些是 Web 执行器的实现细节，不能直接移植为 Android 规则。PhonePilot 应继续使用已有的 `observation_id`、`element_id`、窗口和 Bridge 端重新核验。[`snapshot.js`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/jev_ultrafast/snapshot.js#L3-L11) [`browser.py`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/jev_ultrafast/browser.py#L135-L186) [`README.md`](../README.md#L99-L105)

## 官方源码的一轮循环

### 1. 观察变成有限动作空间

`snapshot.js` 在浏览器内一次读取可见、启用且位于视口内的常见 HTML/ARIA 控件，保留 role、可访问名称、当前值和状态；可见正文最多 6000 字符，动作候选最多保留 250 个，并额外加入滚动和等待控制。[`snapshot.js`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/jev_ultrafast/snapshot.js#L24-L27) [`snapshot.js`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/jev_ultrafast/snapshot.js#L55-L106)

同一 DOM 节点只分配一个 code-owned identity；WeakMap/Map 维护实际节点引用，断开的引用会清理。真正执行前还会重新读几何和 `elementFromPoint`，所以模型只选择观察到的节点，不能把模型输出当成 selector、坐标或 JavaScript。[`snapshot.js`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/jev_ultrafast/snapshot.js#L3-L8) [`browser.py`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/jev_ultrafast/browser.py#L141-L164)

### 2. 一次请求同时问 operation 和 speculative targets

`action_space` 先按节点去重，再按 `CLICK`、`TYPE_TEXT`、`SELECT` 等 operation 建立各自的 target 集合；`choose` 把 operation choice 和每个 target head 放进同一个请求。target 问题彼此独立，因此它们可以对每一种 operation 都给出候选，但执行器只验证并消费与已选 operation 对应的 target head。官方 README 将这种未选 target 称为 speculative，并明确为一次网络往返。[`model.py`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/jev_ultrafast/model.py#L48-L78) [`model.py`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/jev_ultrafast/model.py#L81-L148) [`README.md`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/README.md#L33-L51)

PhonePilot 已经这样做：`action_space` 按 Android 节点建立 table 和 operation groups，`choose` 把所有 target head 放入一个 `/v1/systemone` 请求；它还加入 `NONE`，使模型在目标不明确时转为 `NEEDS_CONTEXT`。因此这里不需要再复制一套 JEV fan-out 框架。[`jev.py`](../src/phone_use_runner/jev.py#L44-L106) [`jev.py`](../src/phone_use_runner/jev.py#L149-L195)

### 3. 合法性校验不是 confidence 门槛

官方 `validate_choice` 检查：choice 必须属于候选；probability 的 key 必须与候选完全相同；所有数值必须是有限的 0 到 1；概率和接近 1；被选项必须是最高概率项。这个函数并没有要求 confidence 达到某个阈值。[`model.py`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/jev_ultrafast/model.py#L30-L45)

`choose` 会把 operation confidence、target confidence 和概率带回结果，但 `Agent.command("act")` 只检查 decision 是否存在、fingerprint 是否匹配、页面是否 fresh，然后执行选择；没有 `low_confidence` 分支，也没有 handoff。PhonePilot 的 `validate_choice` 其实已经采用同样的严格概率校验，额外增加的是运行时阈值和两次失败交接。[`model.py`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/jev_ultrafast/model.py#L118-L148) [`agent.py`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/jev_ultrafast/agent.py#L86-L100) [`jev.py`](../src/phone_use_runner/jev.py#L26-L41) [`runner.py`](../src/phone_use_runner/runner.py#L527-L533)

### 4. 自动循环每轮都 choose

`Agent.run` 是一个很小的循环：只要状态不是 `done` 或 `blocked`，就执行一次 `tick`；`tick` 先 `predict`，再把同一个页面 fingerprint 交给 `act`。`predict` 每轮都会调用 `choose`；如果页面已经变化就先重新观察。动作执行后先记录 history，再重新观察，因此观察失败不会把已执行动作抹掉。连续三次非 wait 动作没有页面变化时才把状态置为 `blocked`。[`agent.py`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/jev_ultrafast/agent.py#L52-L85) [`agent.py`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/jev_ultrafast/agent.py#L101-L165)

所以官方项目并没有一个“相同页面直接复用上次模型决策”的 decision cache。它缓存的是 DOM 节点身份、freshness guard 和 HTTP 连接，不是 `choose` 的结果；同一页面是否继续推进仍由下一轮模型选择决定。[`snapshot.js`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/jev_ultrafast/snapshot.js#L3-L8) [`browser.py`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/jev_ultrafast/browser.py#L20-L42) [`agent.py`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/jev_ultrafast/agent.py#L65-L85)

### 5. HTTP 复用和 text helper 边界

模型层建立一个全局 HTTP/2 client，429、529、503 最多重试三次并退避；这属于连接和请求复用，不是决策缓存。[`model.py`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/jev_ultrafast/model.py#L12-L27)

`TYPE_TEXT` 才会进入独立的 text helper。helper 收到目标、字段、可见页面文本和最近动作，必须返回只含一个 `text` 字段的 JSON；缺 key、空值、非字符串或超过 2000 字符都会停止输入。一次 stale retry 只有在完整 helper context 未变时才重用生成值，成功 mutation 后清空。[`model.py`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/jev_ultrafast/model.py#L151-L198) [`agent.py`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/jev_ultrafast/agent.py#L105-L118)

PhonePilot 目前使用上层准备好的 `input_text`，不会让 Jev 生成消息正文；这是更适合联系人消息的安全边界。PhonePilot 的 Bridge 连接也已经按设备地址和 pin 缓存 `HTTPSConnection`，而 `JevPolicy` 持有复用的 HTTP/2 client。[`agent.md`](../src/phone_use_runner/agent.md#L6-L17) [`transport.py`](../src/phone_use_runner/transport.py#L35-L118) [`jev.py`](../src/phone_use_runner/jev.py#L109-L115)

## PhonePilot 当前已经具备的部分

| 能力 | 当前实现 | 对照结论 |
| --- | --- | --- |
| 一轮决策 | `_work` 每个运行循环调用一次 `policy.choose`；请求同时含 operation 和 targets | 已具备 JEV 的核心 fan-out，不需要复制新框架。 [`runner.py`](../src/phone_use_runner/runner.py#L518-L567) [`jev.py`](../src/phone_use_runner/jev.py#L149-L195) |
| 紧凑输入 | `_observe` 请求 `output=decision`、`compact=True`；只有 selector 需要 view_id 时才请求 details | 已有 Android 版 compact path。 [`runner.py`](../src/phone_use_runner/runner.py#L269-L288) |
| 候选边界 | 支持的 targets 超过 254 或元素超过 500 时显式失败，不静默截断 | 已有安全上限，语义不同于官方 Web 的 250 截断。 [`jev.py`](../src/phone_use_runner/jev.py#L44-L106) |
| 响应校验 | choice、概率 key、有限数值、概率和、最高概率项均校验 | 与官方一致；差异在运行时 confidence threshold。 [`jev.py`](../src/phone_use_runner/jev.py#L26-L41) |
| 连接复用 | `JevPolicy` 复用 HTTP/2 client；Phone Bridge 按地址/pin 复用 HTTPS connection | 已有 HTTP 复用，不应把它误判成缺失。 [`jev.py`](../src/phone_use_runner/jev.py#L109-L115) [`transport.py`](../src/phone_use_runner/transport.py#L38-L42) |
| 防重复发送 | COMMIT 只有 recipient、输入框精确匹配且 send selector 可验证时才可用；发送后只验证原请求 | 比官方通用 Web demo 更保守，适合消息发送。 [`runner.py`](../src/phone_use_runner/runner.py#L368-L382) [`runner.py`](../src/phone_use_runner/runner.py#L508-L515) |
| 本地完成校验 | element、records、message 有代码校验；agent 只有 JEV 判断后交接 | 这是当前人工停顿的直接来源。 [`runner.py`](../src/phone_use_runner/runner.py#L326-L347) [`runner.py`](../src/phone_use_runner/runner.py#L537-L542) |

## 最小、可落地的优化顺序

不建议把 Web DOM 的 snapshot/cache 设计搬进 Android，也不建议为了追求官方 demo 的速度直接移除消息安全边界。PhonePilot 已经拥有一次 RPC 的 operation+target 结构，最小改动应集中在决策前的本地确定性分支：

### P0：减少确定性场景的 Jev 请求

在 `_work` 调用 `policy.choose` 前增加一个很窄的 `_fast_decision`，只处理代码已经证明的情况：

- 若 `_commit_ready(task)` 为真，直接使用当前唯一 `commit_selector` 节点生成 `COMMIT`，不要再让 Jev 选择发送按钮；
- 若 `_completion(task)` 已经返回证据，保持现有 `_advance`，不再请求 Jev；当前代码已经做到这一点；
- 不要把“任意场景只有一个候选”直接当成可点击，除非同时满足操作类型、当前 subgoal、输入值和 commit 状态的约束。

这样可以缩短微信发送这类末端动作的延迟，同时保留导航阶段的 Jev 判断。`_commit_ready` 已经检查了发送按钮、收件人和输入框精确值，因此它是最适合作为本地 fast path 的现成边界。[`runner.py`](../src/phone_use_runner/runner.py#L368-L382)

### P0：把 `completion.kind="agent"` 视为明确的上层验证边界

若目标能表达成 element、records 或 message，应直接提交对应的可验证 completion；不要让 `agent` 作为“Jev 说完成即可”的隐式完成类型。若确实需要语义完成，再为该业务补一个代码拥有的 verifier，验证通过后才调用 `_advance`。这样能消除不必要的 `verification_required`，同时不把模型 confidence 当成完成证据。

官方参考也把 DONE 的独立结果校验放在业务示例，而不是把模型回答当作事实；PhonePilot 现在把这个边界交给上层是正确方向，只是 `agent` 类型没有本地 verifier。[`README.md`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/README.md#L92-L126) [`runner.py`](../src/phone_use_runner/runner.py#L326-L347)

### P1：按操作类型区分 confidence 策略

不要全局删除 `0.7` 门槛。建议保留 `COMMIT`、收件人识别和首次高风险动作的严格门槛；对已由本地 selector 限定、无副作用的导航/等待动作，可以允许一次重观察后继续，避免因单次 Jev 置信度不足就保存截图并交接。这个方案只改 handoff 策略，不改 `validate_choice`，也不需要引入第二个 planner。

### P1：记录决策耗时和停顿原因

现有 timings 已记录 Jev latency，但建议在汇总中单独统计 `low_confidence`、`verification_required`、`context_not_verified` 和 `message_verification_required`。先确认停顿主要来自 `completion.kind="agent"`、低 confidence 还是 UI 结构缺失，再决定阈值。官方性能报告也把模型请求、浏览器操作、等待和最终独立验证分开计时，而不是只看总耗时。[`performance.md`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/docs/performance.md#L22-L32) [`runner.py`](../src/phone_use_runner/runner.py#L524-L567)

### 不建议现在做的事

- 不要实现一个跨多轮的“相同 fingerprint 直接复用 decision”缓存：官方也没有这样做，PhonePilot 的 `recent_actions`、commit 状态和上层输入会改变安全语义。
- 不要把 DOM 的 `WeakMap`、`checkVisibility`、`elementFromPoint` 当成 Android 的通用方案；Android Bridge 已经有自己的节点和窗口校验边界。
- 不要为了减少 handoff 让 `DONE` 直接变成已完成；没有本地 completion evidence 时，模型 confidence 只能说明选择质量，不能证明微信消息或任务结果已经成立。

## 验收标准

如果后续只实施 P0，建议只看三项：

1. `COMMIT` 末端是否少一次 Jev 请求，且仍然只发送一次；
2. `completion.kind="agent"` 是否被显式替换为可验证 completion 或明确保留上层验证；
3. 低 confidence 和 UI 标题缺失是否能从 timings/handoff reason 中区分，而不是混在一次 `verification_required` 里。

本次没有运行测试或真实设备实验，符合“只研究并写报告”的边界。
