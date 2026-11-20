# 展示组件契约

组件集中放在 `components.js`，页面通过 `window.UI` 使用。组件只接收数据和事件回调，不直接请求接口，不读取全局登录态。作品卡、指标卡、趋势图、热力格和组织树可以跨页面复用。

## 通用约定

- 输入是普通对象或数组，字段名与接口响应保持一致。
- 输出是一个 DOM 节点，或通过回调返回用户动作。
- 所有外部文本写入 `textContent` 或 `createElement`，不使用 `innerHTML` 拼接。
- 动画遵守 `prefers-reduced-motion`。
- 颜色和圆角使用 `styles.css` 的 CSS 变量与既有类。

## MetricCard

| 项 | 契约 |
|---|---|
| 输入 | `{label, value, unit, hint, tone}` |
| 输出 | 一张统计卡 DOM |
| 事件 | 无 |
| 复用 | 学生工作台、教师总览、平台组织总览、导入结果 |

`value` 可以是数字或字符串。数字进入页面后按 `zh-CN` 格式化，并从小到大滚动；字符串直接显示，适合组织名等非数量字段。`tone` 可取 `accent`。

## RoleBadge

| 项 | 契约 |
|---|---|
| 输入 | `{role}` 和组织文案 `{admin, member}` |
| 输出 | 角色徽标 DOM |
| 事件 | 无 |
| 复用 | 组织树、成员表、个人信息 |

`role` 只接受 `platform_admin`、`org_admin` 或 `org_member`。组织内显示名来自组织配置。

## WorkCard

| 项 | 契约 |
|---|---|
| 输入 | `work`、`{density, showAuthor, showStatus, actions}` |
| 输出 | 作品卡 DOM |
| 事件 | `onOpen(work)`；`actions[].onClick(work)` |
| 复用 | 产品广场、我的作品、教师精选、平台作品管理 |

`work` 采用作品摘要：`workId`、`title`、`summary`、`authorDisplayName`、`category`、`sourceKind`、`featured`、`status`、`visibilityLabel`。演示标记来自 `sourceKind=seed_demo`，页面不自行猜测。

## TrendChart

| 项 | 契约 |
|---|---|
| 输入 | `{series:[{date,value}], unit, range, truncated}` |
| 输出 | 手写 SVG、文本摘要和可展开数据表 |
| 事件 | 无；容器可聚焦并带 `aria-label` |
| 复用 | 教师用量趋势，未来平台汇总 |

服务端负责把范围截断到 90 天并返回 `truncated`。组件不再二次截断。

## Heatmap

| 项 | 契约 |
|---|---|
| 输入 | `{cells:[{date,value}], unit}` |
| 输出 | 90 格热力图和颜色图例 |
| 事件 | 每格可聚焦，标题和 `aria-label` 提供日期与数值 |
| 复用 | 教师活跃度、后续组织教学分析 |

## OrgTree

| 项 | 契约 |
|---|---|
| 输入 | `{id,name,count,adminLabel,memberLabel,children[]}`；可以有平台 → 组织 → 成员两层 `children` |
| 输出 | 可展开的组织树 DOM |
| 事件 | `onSelect(child)`，只对成员节点触发 |
| 复用 | 平台组织树、后续组织切换 |

成员节点包含 `id`、`name`、`role`、`studentId`、`balance`、`spent`、`keyHint`、`admissionStatus`。组织节点可以继续嵌套成员节点，折叠状态由原生 `details` 维护。组件不判断权限，调用方只传授权数据。

## ImportPreview

| 项 | 契约 |
|---|---|
| 输入 | `{batchId,counts,rows,expiresAt}` |
| 输出 | 计数卡、逐行结果表和操作按钮 |
| 事件 | `onConfirm(data)`、`onDownload(data)`、`onCancel(data)` |
| 复用 | 名单导入页 |

行结果字段为 `rowNo`、`result`、`reasonCode`、`message`、`fields`。邮箱和学号在服务端脱敏后返回，组件不再还原。

## 数据层

页面数据函数集中在各页面的脚本里，统一使用 `fetch` 和 Bearer 令牌。页面脚本负责：

- 解析部署前缀，保持 `/app/` 与根路径都能工作。
- 处理 401、403、409 和 410。
- 把接口数据交给组件。
- 保存引导当前步骤和有限草稿。

组件不持有令牌，不写 `localStorage`，不发起网络请求。
