# Phone Bridge

Android 11+ 配套应用，三份 Java 源码：本机授权页、无障碍执行器、HTTPS 服务。运行时只依赖 NanoHTTPD 2.3.1。

使用 JDK 17/21、Android SDK 35 和 Gradle wrapper 构建 `:app:assembleDebug`。UI 无额外框架；资源只含 Manifest 与无障碍能力声明。

## 最小接口

- `GET /identity`：公开的安装 ID、服务实例、设备名称、Android 版本、协议版本。
- `POST /pair/request`：固定客户端承诺，返回服务器承诺及临时请求 ID。
- `POST /pair/reveal`：核验客户端随机数并返回服务器随机数，两端生成八位核对码。
- `POST /pair/status`：手机本机批准后领取访问令牌，请求有效期两分钟。
- `POST /api`：Bearer 认证，正文为 `{"tool":"observe","arguments":{"device_id":"..."}}`。

TLS 证书与私钥由 AndroidKeyStore 持有。电脑在发送令牌前校验证书 SHA-256 指纹。服务保留 HTTP/1.1 连接，无需每次查询身份或握手。

观察支持 tree、screenshot、both，默认 tree。输出节点路径、文字、描述、可操作能力、边界及必要状态；include_details 增加 view_id 与 class。所有采集最多 1500 个节点、深度 40，观察保存八份且最长 30 秒。父子索引属于单个观察，不能用作永久 ID。

动作支持节点 tap / long_press / scroll / input_text，以及 back、home、launch_app。文字只支持 replace，明确拒绝密码框。截图缺少语义时可使用经映射的 tap / swipe 坐标。节点身份与内容会重新核验；坐标另要求 UI 版本一致。bridge 自身授权页不导出内容，不接受远程节点或坐标操作。

动作参数必须包含 request_id；节点与坐标必须关联 observation_id。动作结果明确区分 action_executed:true / false / null。请求去重最长十分钟、最多 256 个 ID，保留的是动作执行事实，重复查询不重新观察或执行。get_status 查询原请求，cancel 只停止尚未执行的输入；已派发手势可能完成。

变化等待由无障碍事件唤醒。默认动作后观察只取树。尚未实现增量树、任意 WebView DOM、自定义绘制界面解析或完整业务等待表达式；这些属于主 agent 与执行器的协作范围。

此应用可访问无障碍服务公开的节点，不是读取其他应用的全部内部 View。截图受 Android 的频率和安全窗口限制。源码编译通过不能替代 TLS、系统输入、厂商后台行为及真实 App 的设备验证。
