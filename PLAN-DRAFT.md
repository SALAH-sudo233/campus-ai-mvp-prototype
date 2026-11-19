# 校园 AI 创作平台：登录与 Token MVP 方案草案

## 目标与流程

学生访问平台，选择校园邮箱或学号，输入密码后注册或登录进入工作台。注册时系统创建学生账户，赠送初始额度（默认 100,000 Token），并向中转站申请一枚密钥；重复登录只读取现有账户，不重复赠送，也不再返回密钥明文。工作台展示余额、累计消耗、流水和密钥掩码，可模拟一次 AI 创作扣除；充值先保留入口。登录与密钥发放的完整需求见 `hust-campus-ai-plan-v7.md`。

正式流程为：浏览器跳转 HUST 官方 SSO/CAS → 后端校验回调票据 → 映射学生唯一身份 → 查询或创建 Token 账户 → 以幂等方式发放首次赠送 → 返回平台会话。平台不接触或保存校园密码。

## 数据模型（草案）

`student_accounts`: `id`, `provider_subject`（SSO 唯一标识）, `email`, `student_no`, `created_at`, `last_login_at`。

`token_accounts`: `student_id`, `balance`, `total_granted`, `total_consumed`, `updated_at`。

`token_ledger`: `id`, `student_id`, `change_amount`（赠送为正、消耗为负）, `balance_after`, `reason_code`, `request_id`, `created_at`。`request_id` 用于幂等与追踪。

首次赠送应使用服务端事务和唯一约束（例如 `student_id + reason_code=INITIAL_GRANT`），先锁定账户再写入流水；重复请求返回原赠送结果，不新增流水。所有消费同样必须服务端校验余额、原子扣减并写流水，前端展示不能作为授权依据。

## 接口草案

- `GET /auth/hust/cas/start`：生成官方 SSO/CAS 跳转。
- `GET /auth/hust/cas/callback?ticket=...`：服务端校验 ticket，建立短期 HttpOnly 会话。
- `GET /api/me`：返回当前学生的脱敏身份。
- `POST /api/token/initial-grant`：服务端幂等领取；正式版不信任前端传入的学生 ID 或额度。
- `GET /api/token/account`：返回余额与汇总。
- `GET /api/token/ledger`：分页返回流水。
- `POST /api/token/consume`：携带幂等 `request_id`，原子扣减并记录原因。

## 边界与风险

- 同一学生多标签页、重复回调或网络重试：依靠唯一约束和 request_id 幂等。
- 非法类型、负数额度、超额消费：服务端 schema 校验，余额不足返回明确错误。
- 余额和流水不一致：流水为审计依据，消费与流水必须同一事务；设置对账任务和管理员修复流程。
- SSO 返回的邮箱可能变化：以官方 subject / 学号作为稳定身份键，邮箱仅作展示与通知。
- 充值、支付、资源消耗属于后续范围，当前不产生真实扣费。
- 隐私：最小化保存身份字段，权限分离；校方看板只看聚合数据，不暴露个人流水。

## 验收标准

1. 本地运行 `server.py` 后打开页面，切换邮箱或学号，用密码完成注册和登录。
2. 新身份恰好得到 100,000 Token；刷新或再次登录不重复领取。
3. 工作台正确展示余额、累计消耗和赠送流水；模拟消耗后余额、累计消耗、流水同步变化。
4. 余额不足、空输入、退出登录和重置演示数据均有明确反馈。
5. 生产实现通过 HUST 官方 SSO/CAS 完成身份认证，服务端幂等发放并以服务端 Token 账户和流水为唯一事实来源。
