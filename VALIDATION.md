# 本地验证记录

更新日期：2026-10-11。当前模式为 Agent 规划和生成文字、Jev 每轮判断、Phone Bridge 直接执行；Jev 的 `DONE` 直接推进流程。下方早期记录按时间保留，旧的置信度门槛和逐步验收已经移除。

## 已完成

- Python 3.11 环境和依赖安装完成，`uv.lock` 固定依赖。
- Ruff 静态检查、Python 字节码编译、两个示例任务的参数校验通过。
- Android 的三份 Java 源码使用 Android 35 的 android.jar、NanoHTTPD 2.3.1 和 Java 17 目标编译通过。使用现有 DevEco JBR 编译器；有兼容旧 Android API 的 deprecation 提示。
- 实际 MCP stdio 子进程初始化、五个工具枚举、无设备响应和无效任务错误返回通过；server instructions 正确返回。
- Java 与 Python 的配对承诺摘要及 SAS 摘要结果一致。
- 本地临时 HTTPS 服务验证了提交承诺/揭示、批准前无法领取令牌、批准后配对、HTTP 连接复用和错误证书指纹在发送凭据前中止。服务是模拟端点，不是 Android 设备。
- 模拟设备/决策器验证了输入后只发送一次、未知发送结果交接后不重发、低置信度不执行输入。没有调用真实 Jev，也没有发送实际消息。
- 模拟 Jev HTTP 响应验证了单次请求包含 operation 和各类 target，未选中目标头不参与执行校验，父子控件文字正确映射到操作候选。
- Python wheel 和源码包构建成功。源码包包含 Android 项目与 Gradle wrapper；wheel 包含运行时指令和第三方许可证。

## 未完成的外部验收

手机已完成配对、节点点击、中文与 emoji 输入和一次微信发送；小红书读取及多次完整业务验收尚未进行。

AndroidKeyStore TLS、设置应用的节点采集、截图和消息输入已实机验证；厂商后台限制、长时间截图频率和安全窗口行为仍需检查。未运行 Android lint。

tree RPC P95 <= 300 ms、决策动作循环 P95 <= 1 秒、两个完整任务各 10 次至少成功 9 次，是待验证目标，不是已测结果。后续使用 CLI benchmark 与每次任务报告分段测量，并单列中文判断错误及业务等待时间。

本次未增加永久逻辑或 UI 测试套件；上述临时模拟验证只保留运行记录和 scratch artifacts。

## 环境安装后的验证（2026-10-10）

- Python 3.11.13 和锁定依赖已安装；为当前 SOCKS 代理补充 `httpx[socks]`，锁定 `socksio 1.0.0`。普通 `phone-use devices` 不再因代理依赖缺失失败。
- Jev key 保存在项目 `.env`，权限为 `0600`。最小真实请求返回 HTTP 200、实际模型 `jev-1.13.0`；生产 `JevPolicy.choose` 对合成场景返回 `DONE`、置信度 0.97。未向模型提交真实手机内容，未执行手机动作。
- 已添加全局 `phone-use` MCP，原有 Codex 配置解析后的其他设置保持一致。按配置中的启动命令实际初始化成功，五个工具枚举及 `phone_devices` 查询成功，返回空设备列表。
- Android SDK 位于 `/Users/yilun/Library/Android/sdk`，已安装 Android 35 平台、Build Tools 35.0.0、platform-tools 37.0.1，并配置 `android/local.properties`。
- 使用 Temurin JDK 17.0.19、Gradle 8.11.1 执行 `:app:assembleDebug`，exit 0，32 个任务完成。APK 为 `android/app/build/outputs/apk/debug/app-debug.apk`，110887 bytes。
- `aapt2` 核验包名 `dev.phoneuse.bridge`、minSdk 30、targetSdk 35；`apksigner verify --verbose` exit 0，APK v2 签名有效。
- Ruff、Python 字节码编译和 Python 包构建通过。wheel 与源码包均检查过，不包含 `.env`、`.state` 或提供的 API key。
- `phone-use doctor` 当前显示 key 已配置、手机配对配置不存在，因此 `ready: false`；电脑端环境已就绪，手机安装与配对仍待完成。

## 实机连接与 TLS 修复后的验证（2026-10-10）

- 设备为 PJZ110、Android 16 / API 36；通过用户提供的无线调试配对码与端口建立 ADB 连接。
- 初版 APK 的 TLS 握手在真实设备失败。限定应用进程的 logcat 显示 `CryptoUpcalls.rsaSignDigestWithPrivateKey` 调用 `RSA/ECB/NoPadding`，AndroidKeyStore 返回 `INCOMPATIBLE_DIGEST`。
- 修复 RSA 密钥授权：增加 `DIGEST_NONE`、SHA-384 和 `ENCRYPTION_PADDING_NONE`，保持仅 `PURPOSE_SIGN | PURPOSE_VERIFY`。使用新 alias `phone-bridge-tls-v2` 及固定 alias 的 `X509ExtendedKeyManager`，保留旧 alias、应用数据及设备 ID。
- 修复版 `:app:assembleDebug` exit 0，32 个任务（4 executed、28 up-to-date）；签名验证通过，ADB `install -r` 成功。
- 实机握手分别通过 TLS 1.2 / `ECDHE-RSA-AES128-GCM-SHA256`、TLS 1.3 / `TLS_AES_256_GCM_SHA384`；两者证书 SHA-256 相同。升级后的限定进程日志未再发现 CryptoUpcalls/Keystore 异常。
- 错误证书 pin 被客户端以 `TLS_IDENTITY_CHANGED` 拒绝，发生在构造 Authorization 头和发送请求之前。新的承诺/揭示配对成功；用户核对全部八位码一致并在手机本机批准后，才执行 `--confirm`。
- `phone-use doctor` 现在显示 `ready: true`，设备查询返回 paired / online；配对配置权限为 `0600`。
- 通过 Phone Bridge API 打开系统设置，返回 `action_executed: true`；观察返回 `com.android.settings`、13 个节点。Jev 对这份真实 UI 选择 `DONE`，置信度 0.92，实际模型 `jev-1.13.0`；未执行模型生成的其他动作。
- 设置页面基准每种模式 5 次，均 0 失败：tree RPC P50 39.97 ms / P95 199.42 ms；tree + screenshot RPC P50 197.26 ms / P95 294.86 ms。报告位于本机 `.state/setup/device-benchmark.json`。这是少量单页面样本，不代表完整业务任务的成功率或长期性能。
- 未添加单测；尚未直接读取 v2 KeyInfo 元数据，也未验收微信、小红书或输入行为。

密钥配置依据：[Android KeyGenParameterSpec.Builder](https://developer.android.com/reference/android/security/keystore/KeyGenParameterSpec.Builder)、[Conscrypt CryptoUpcalls](https://github.com/google/conscrypt/blob/master/common/src/main/java/org/conscrypt/CryptoUpcalls.java)。

## 直接执行循环收尾（2026-10-11）

- 已移除新增的 `RecipientVerification`、目标完成谓词、置信度门槛和三次无进展回交。Jev 选中操作后立即执行，返回 `DONE` 直接完成当前子目标；明确失败的动作及错误保留在近期历史中，重观察后由 Jev 继续判断。
- `WAIT` 只等待当前窗口的有效变化，没有变化时不会再次请求 Jev。代码仍处理真实通信故障、缺少输入信息和 Jev 自己返回的 `BLOCKED` / `NEEDS_CONTEXT`。
- 中文输入乱码的原因是 NanoHTTPD 2.3.1 在缺少 charset 时默认按 ASCII 解码。请求已改为 `application/json; charset=utf-8`；实机输入的中文与 emoji 均保持原文。
- 桥接服务已取消 10 秒空闲 socket 超时。实机检查空闲 12 秒后仍为同一个 HTTPSConnection，不重新握手；非当前窗口的内容刷新不再使当前窗口观察失效。
- 一次真实微信发送已记录在 `.state/inspection/direct-wechat-send.json`：Jev 选择 `COMMIT`，置信度 0.68，模型 `jev-1.13.0`，Jev 耗时 498.18 ms；手机返回 `action_executed: true`，随后 UI 树中出现 1 个相同文本的非编辑消息节点。没有重复发送该测试消息。
- 请求压缩使用同一份设置快照实际比较：输入从 3783 降到 2405 tokens，约减少 36%；该单次比较不能证明稳定延迟提升。
- 通过公开 SDK 进行了四个临时离线冒烟场景，均 `completed`：低置信度动作直接执行（1 次动作 / 2 次决策）；界面不变时连续 4 次决策动作不回交（4 次动作 / 5 次决策）；明确动作失败后重观察恢复（2 次动作 / 3 次决策）；WAIT 连续 2 次未变化、第 3 次变化，期间没有额外模型调用（0 次动作 / 2 次决策 / 3 次等待）。未新增单测文件。
- 使用真实 Jev 和冻结的设置快照运行新 SDK 循环，返回 `completed`、0 次物理动作、0 次回交；模型 `jev-1.13.0`，单次模型耗时 336 ms。该验证没有再次操作或发送手机消息。
- Ruff、Python 字节码编译和两个示例请求参数校验通过。旧 `options.confidence` 仅兼容接受，不再参与执行决策。

## 决策回交循环修复（2026-10-11）

- 最后一次微信实验中，Jev 两次主动要求上下文，两次返回 `TAP + NONE`。原代码立即退出后台循环；首个任务 158069 ms 中有 151857 ms 等待上层处理，Jev 调用合计 2268.74 ms。第二个任务已经点击发送成功、UI 出现消息且输入框清空，但仍以 `needs_context` 结束。
- `NEEDS_CONTEXT`、`BLOCKED`、目标 `NONE` 和暂时不可读的树现在在 worker 内短暂等待后重采集详细树、再次交给 Jev，直到 `DONE`、取消或达到原有预算。技术故障和执行结果未知仍停止并保留原请求，不替代派发。
- Jev 请求增加总体目标、当前子目标、已完成步骤、前一决策、发送执行状态和消息/输入框证据；动作历史保留观察到的目标文字。正确草稿不再提供重复输入候选，发送尝试后关闭输入与发送候选。消息任务本次发送未执行时不开放 `DONE`；runner 也会拒绝过早的 `DONE` 并继续内部判断。
- 对两份保存的真实微信树调用实际 `jev-1.13.0`：草稿返回 `COMMIT`、置信度 0.95；发送后返回 `DONE`、置信度 0.99。报告在 `.state/loop-fix/real-jev-draft-result.json` 和 `real-jev-recorded-result.json`；没有执行手机动作。
- 通过公开 SDK、真实 Jev 和冻结的发送前后观察完成完整循环：`completed`、2 次 Jev 请求、模拟发送 1 次、物理手机动作 0 次、调用方续跑 0 次。报告在 `.state/loop-fix/real-frozen-loop-result.json`。这验证了记录场景中的草稿发送和完成衔接，不代表新任务的实机成功率。
- Ruff、Python 字节码编译、wheel/源码包构建及新 MCP 服务初始化均 exit 0；五个工具枚举正常，初始化指令包含内部重试循环。未添加单测，未修改 Android APK 或安装新依赖。
- 独立临时离线冒烟验证 13 项通过、0 失败，命令 `PYTHONPATH=src .venv/bin/python .state/loop-fix/verification/verify_loop_fix.py` exit 0。覆盖目标为空/主动要求上下文/阻塞后的内部恢复、连续不确定性、树恢复、单次输入发送、过早 DONE 恢复、草稿与旧消息不提前完成、发送后候选限制、WAIT、取消/预算、未知执行结果及真实 JevPolicy 的模拟 HTTP 流程。同一合法 `TAP + NONE → DONE` 响应序列在修改前为 `needs_context`、修改后为 `completed`。记录位于 `.state/loop-fix/verification/loop-verification-report.json`，未新增永久单测文件。
