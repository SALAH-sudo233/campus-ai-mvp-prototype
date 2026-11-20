# 校园 AI 创作平台 MVP 原型

这个原型演示学生登录、领取 Token 额度，以及从中转站拿到调用 AI 工具用的密钥。所有数据都是演示数据，中转站的上游通道也是假的。

## 协作入口

接下来会有其他共创者一起改。动手前先读 `docs/adr/`：

| 文档 | 内容 |
|---|---|
| `docs/adr/README.md` | ADR 怎么写、什么时候写、目录 |
| `docs/adr/0002-prototype-architecture.md` | 整体架构、数据流、对外边界 |
| `docs/adr/0003-ui-conventions.md` | UI 规范：颜色、字号、组件、交互、文案 |
| `docs/adr/0004-development-conventions.md` | 开发规范：依赖、命名、安全约定、改动流程 |

改架构、UI 规范、开发规范或对外接口前，先加或更新对应 ADR，再改代码。

## 运行

原型现在有后端，需要先启动它。后端只用 Python 标准库，不用安装依赖。

```bash
DEMO_ALLOW_RESET=1 python3 server.py
```

浏览器打开 `http://127.0.0.1:8080`。`DEMO_ALLOW_RESET=1` 只用于演示，开启后页面底部的“重置演示数据”才会生效。

可用环境变量：

| 变量 | 默认值 | 作用 |
|---|---|---|
| PORT | 8080 | 监听端口 |
| HOST | 127.0.0.1 | 监听地址 |
| DEMO_DB | demo-backend.sqlite | 账户、流水、会话数据库 |
| DEMO_RELAY_DB | demo-relay.sqlite | 中转站数据库 |
| DEMO_ALLOW_RESET | 未设置 | 设为 1 才开放 /api/reset |
| MOCK_RELAY_DOWN | 未设置 | 设为 1 模拟中转站不可用 |
| ADMIN_USERNAME | 未设置 | 管理员账号。和 ADMIN_PASSWORD 都设置后才开启管理后台 `/admin` |
| ADMIN_PASSWORD | 未设置 | 管理员密码。任一项未设置，管理功能关闭 |

GitHub Pages 上的公开页面是纯静态的，跑不了后端。登录、注册、领取密钥只能在本地启动 `server.py` 后体验。

## 访问入口

原型已部署在中转站服务器上，入口是 `https://campusgo.aihuanxi.ai/app/`，和中转站共用域名。

- 学生端：`https://campusgo.aihuanxi.ai/app/`
- 中转站管理后台：`https://campusgo.aihuanxi.ai/app/`，在首页切到“管理员”登录。账号和密码只存在目标机的 `/etc/campus-prototype/env`，不进仓库。
- 健康检查：`https://campusgo.aihuanxi.ai/app/api/health`，返回 `{"ok": true}`。

本地开发仍用 `python3 server.py`，后端只监听 `127.0.0.1:8080`，不直接暴露。

当前域名 `campusgo.aihuanxi.ai` 被火山云 webblock 拦截，HTTPS 不可用。域名恢复前，临时入口是 HTTP 直连 `http://14.103.80.52/app/`（浏览器会提示不安全）。拦截与服务器无关，需在火山云控制台核实。

## 怎么试用

学生端：

1. 打开学生端，选“校园邮箱”或“学号”。
2. 输入名册里的学号，例如 `2024001001`。邮箱形式是 `2024001001@mail.hust.edu.cn`。
3. 首次使用先点“注册并领取额度”，密码自设，至少 8 位。注册后赠送 100,000 Token，并发放一枚模拟密钥。
4. 密钥明文只显示一次，之后只显示掩码，可在工作台重新生成。
5. 名册外的学号不能注册，也不能登录。

管理员：在登录页切到“管理员”，输入账号密码，可按学号、邮箱、姓名搜索学生并吊销密钥。

## 中转站管理后台

管理后台面向中转站管理员，不是学生页面：

- 实名台账列表：学生编号、身份类型、登录身份、姓名、密钥掩码、密钥状态。
- 按学号、邮箱或姓名搜索。
- 吊销某学生的当前密钥，吊销后立即失效，学生下次登录会重新发放。

姓名来自内置演示名册（学号 `2024001001` 起连号的 200 人，固定随机种子）。名册同时是学生准入名单：名册外的学号和邮箱不能注册，也不能登录。生产版把演示名册换成学校真实名册，真实名册不进仓库。

名册示例，完整 200 人见 `roster.py`：

| 学号 | 姓名 |
|---|---|
| 2024001001 | 郭英 |
| 2024001002 | 冯平伟 |
| 2024001003 | 吴霞 |
| 2024001004 | 胡杰 |
| 2024001005 | 吴平桂 |
| 2024001200 | 刘艳 |

名册里没有密码，学生首次注册时自设。

## 部署

原型和中转站 New-API 部署在同一台机器上，通过 nginx 的 `/app/` 路径对外。后端只监听 `127.0.0.1:8080`，不直接暴露。

```text
浏览器 → nginx（HTTPS，限流）→ /app/ 去前缀 → 127.0.0.1:8080 原型后端
                               / 和 /dashboard → New-API 中转站（不动）
```

| 文件 | 作用 |
|---|---|
| deploy/deploy.sh | 建独立系统用户、同步代码、装 systemd unit、做健康检查。不改 nginx |
| deploy/campus-prototype.service | systemd unit，带沙箱和资源上限（内存 256 MB、CPU 50%） |
| deploy/env.example | 环境变量模板。真实的管理员账号和密码只写在目标机的 `/etc/campus-prototype/env` |
| deploy/nginx-campus-http.conf | http 级的限流区和 upstream，放进 `conf.d` |
| deploy/nginx-campus-locations.conf | `/app/` 的 location，封掉公网 `/api/reset`，在 server 块里 include |
| deploy/campus-proxy-common.conf | 代理公共头和超时 |

限流按来源 IP 计，超限返回 429：

| 接口 | 限制 |
|---|---|
| /app/api/register | 10 次/分钟，突发 3 次 |
| /app/api/login | 30 次/分钟，突发 5 次 |
| /app/api/admin/login | 5 次/分钟，突发 1 次 |
| 其他 /app/api/* | 60 次/分钟，突发 20 次 |

**管理员密码不进仓库。** 部署后在目标机编辑 `/etc/campus-prototype/env`，再执行 `systemctl restart campus-prototype`。

### 线上位置和运维

| 项 | 值 |
|---|---|
| 服务器 | 火山云上海，公网 `14.103.80.52`，和中转站 New-API 同机 |
| 对外入口 | `https://campusgo.aihuanxi.ai/app/` |
| 服务 | `campus-prototype.service`，用户 `campus-proto`，监听 `127.0.0.1:8080` |
| 代码目录 | `/opt/campus-prototype` |
| 配置 | `/etc/campus-prototype/env`，权限 0640，属主 root:campus-proto |
| 数据 | `/var/lib/campus-prototype/demo-backend.sqlite`、`demo-relay.sqlite` |
| nginx 片段 | `/etc/nginx/conf.d/campus-http.conf`、`/etc/nginx/campus-locations.conf`、`/etc/nginx/campus-proxy-common.conf` |

更新代码：把仓库同步到目标机后执行 `bash deploy/deploy.sh`。脚本不覆盖已有的 `/etc/campus-prototype/env`，也不改 nginx。

改 nginx 前先备份站点文件，`nginx -t` 通过后用 `systemctl reload nginx`，不要 restart。看日志用 `journalctl -u campus-prototype`。

## 已包含

- 登录页分“学生”和“管理员”两个入口。学生用校园邮箱或学号加密码，管理员用账号加密码。
- 学生必须在名册内才能注册和登录。密码至少 8 位，服务端存加盐哈希。
- 连续 5 次登录失败后锁定 60 秒。
- 注册时一次性赠送 100,000 Token，按账户只发一次。
- 注册和登录成功后，由模拟中转站发放一枚密钥。密钥明文只显示一次，之后只显示掩码。
- 中转站维护实名台账，管理后台 `/admin` 可按学号、邮箱、姓名搜索学生并吊销密钥。
- 工作台可重新生成密钥，旧密钥立即失效。
- 余额、累计消耗、Token 流水和模拟消耗。

## 接口

| 接口 | 作用 |
|---|---|
| POST /api/register | 名册内的邮箱或学号加密码注册，返回令牌和密钥明文。名册外返回 403 |
| POST /api/login | 校验密码，返回令牌和密钥掩码 |
| GET /api/me | 按令牌返回账户视图 |
| POST /api/key/rotate | 作废旧密钥，发新密钥 |
| POST /api/consume | 扣减 Token，带请求编号幂等 |
| POST /api/logout | 令牌失效 |
| POST /api/admin/login | 校验管理员账号和密码，返回管理员令牌 |
| GET /api/admin/accounts | 管理员令牌鉴权，返回实名台账，支持 `?q=` 搜索 |
| POST /api/admin/revoke | 管理员令牌鉴权，按 owner_ref 吊销当前密钥 |

## 生产接入边界

- 原型用账号密码模拟身份认证。密码只是原型临时手段，正式版接华中科技大学官方 SSO 或 CAS，不在平台保存校园密码。
- 中转站按设计维护实名台账：收到学生编号（owner_ref）后关联登录身份与姓名，管理员可据此查看和吊销。原型的中转站是模拟实现，以后接入真实中转站。
- 学生编号当前是登录身份的 SHA-256 截断，只当台账和密钥的关联键用，不是强匿名化。
- 邮箱账号和学号账号在原型里是两个独立账户，不做归并，是已知限制，生产版由学校统一认证解决。
- 名册同时提供姓名和准入判断，名册外的身份不能注册。真实名册以后接入，不进仓库。
- 中转站的真实供应商、计费口径和密钥格式还没有定。
