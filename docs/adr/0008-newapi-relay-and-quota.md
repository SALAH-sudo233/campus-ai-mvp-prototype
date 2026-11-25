# 0008 New-API 中转对接与额度口径

状态：草案
日期：2026-11-24

## 背景

0002 的决策是原型用模拟中转站 `relay.py` 发假钥，并写明「不改同机 New-API」「真实中转站不在本版范围」。现在要把发钥对接到同机的真实 New-API（`34.146.187.183`，标准版 v1.0.0-rc.51），正式名单成员注册时在 New-API 建一个 token 并精确绑定到成员。这一步推翻 0002 的上述两条边界。

## 决策

### 对接形态

- New-API 里所有成员挂在一个 New-API 用户下，每个平台成员建一个 token。
- `relay.py` 从本地假钥存储改为 New-API 的影子存储：平台侧只存 token 的远端 id、SHA-256 摘要、掩码和状态，上游真实 key 只在 New-API 内，发放时返回一次明文给成员。
- 发钥入口仍是 `app_server.py` 的 `issue_key_for_member`，是注册、登录、轮换的唯一发钥点，签名不变。

### token 命名与幂等

- token 名用确定性格式 `campus:{owner_ref}:{request_id}`，只用 ASCII。中文标签不写进 New-API。
- 发钥崩溃或响应丢失后，按 token 名在 New-API 列表里回查认领，不重复建。沿用现有 `request_id` 幂等和 `one_active_key_per_owner` 唯一约束。

### 状态机与并发

- 影子记录状态：creating、pending、active、revoking、revoked、failed。
- New-API 的 HTTP 调用不放进全局发钥锁。锁内只预占影子状态，出锁做网络请求，再回锁写最终状态。慢上游不阻塞其他成员的发钥。
- 轮换先在 New-API 建新 token 再删旧 token，中间短暂两把钥有效，避免断供。旧 token 删除失败进 revoking 状态补偿重试。
- 吊销与发钥共用串行，最终不留 active 残留钥。

### 额度

- 额度的唯一事实源是平台数据库的本地记账。New-API 成员 token 一律按 `unlimited_quota` 建，不设硬限额、不缓存上游额度。平台的 `students.balance` 与 `/api/consume` 是本地账面展示与演示扣减。
- 组织总额度记在 `organizations.quota_limit`，成员份额 = 总额度 ÷ 名册人数，由平台 `analytics` 按此分摊展示。试点华中科技大学软件学院 200 人，总额度默认 2000 万，人均 10 万。份额只在平台记账，不写进 New-API：建 token 时设 `unlimited_quota=true`、`remain_quota=0`。

### 不可用与失败

- New-API 临时不可用时，注册照常成功（201），密钥标 pending，不连带注册失败。
- pending 补发走惰性方式：在 `/api/me`、登录、教师端成员列表、管理端账户页等读路径上，每次最多重试一条到期 pending，单次超时不超过 5 秒，退避 30s、2m、10m、1h、6h，5 次后标 failed。组织管理员有手动重发入口。不新增后台定时任务。
- 上游失败只记类别码（auth_failed、rate_limited、quota_exhausted、bad_request、timeout、network、upstream_5xx），不记 token 和响应体。禁止上游失败时回滚注册或发假钥。

### 凭据

- New-API 管理凭据从服务器环境变量读（`NEW_API_BASE_URL`、`NEW_API_TOKEN` 等），不进代码、日志、提交、文档。启动时校验非空，缺失则打日志并降级为 pending 模式，不退出进程。

### 三种凭据与明文可见性

本方案涉及三种不同凭据，不能混为一谈：

- 供应商通道密钥：New-API 连上游模型厂商用的密钥，始终只在 New-API 内，平台和成员都接触不到。本次不改变这一点。
- New-API 成员 token：平台为每个成员建的调用 token，本来就要交给成员本人使用。平台服务进程在发放那一次会短暂处理它的明文，但不持久化，只存 SHA-256 摘要和掩码。平台管理接口和学院管理员不能再次读取它的明文（延续 0005）。New-API 后台的中转站管理员能看到。
- 平台个人访问令牌 PAT：见 0009，与成员 token 是两回事。

这一条收窄 0002、0004 的措辞：原文「真实上游密钥不会返回给平台和学生」仍然成立（指供应商通道密钥）；本次新增的是 New-API 成员 token 的摘要存储与一次性发放约定。

### 演示登录入口

- 现有 `/api/demo/login` 用服务端管理员凭据免密登进预选演示账号。接真实 New-API 后它是提权面：演示会话可取真实成员 token，演示管理员会话可铸 PAT。
- 新增环境变量 `DEMO_ALLOW_LOGIN` 门控，与 `DEMO_ALLOW_RESET` 同套路。线上默认关：接口返回 404、前端隐藏按钮。仅本地演示可开。
- 开启时演示账号也不发真实成员 token、不能铸 PAT，只用于界面演示。

### 破坏性切换

- 切到 New-API 时重置中转站库重新发钥。重置只删 New-API 里带 `campus:` 前缀的 token，不碰共享用户下的其他 token。用独立脚本 `migrate.py --relay-cutover`，先 dry-run 列出要删的 token、备份、人工确认后执行，不放进运行时自动跑。

## 后果

- 0002 的「不改 New-API」「真实中转站不在本版范围」两条边界被本条取代，日期 2026-11-24，替代者 0008。仅放宽 token 管理与真实额度接入；0002 的独立系统用户 campus-proto、只占 nginx /app/ 路径、不碰 New-API 监听地址等隔离约束继续有效。
- 0004 中「账户、流水、密钥的唯一事实来源是服务端数据库」就额度部分继续成立：额度权威是平台数据库的本地记账，New-API token 不设限额、不作额度事实源。密钥摘要存储与幂等约定保留。
- 明文口径见「三种凭据与明文可见性」，不是简单的「平台看不到」。
- 发钥链路依赖同机 New-API 的可用性，但注册不受其故障阻塞。
- 端点证书 2026-11-29 到期，部署跨过该窗口须先确认续期。
