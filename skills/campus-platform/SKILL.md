---
name: campus-platform
description: 用 campus-cli 读取校园平台资料、发布作品，或按当前角色管理组织作品。
---

# 校园平台工具

用 campus-cli 操作校园平台。先配置环境，再确认写操作，最后核对返回结果。

## 配置与安全

| 项目 | 用法 |
| --- | --- |
| CLI 获取 | 从个人中心工具包下载 cli/campus-cli，用 Python 3 运行，或赋予执行权限后直接运行。 |
| CAMPUS_PAT | 由用户在受保护的进程环境中注入个人访问令牌；不从参数或文件读取。 |
| CAMPUS_BASE_URL | 填平台根地址；也可用 --base-url 指定，不要填推理服务的 /v1 地址。 |
| --json | 输出单个 JSON 结果，便于 agent 读取。 |
| --data | 传 JSON 对象；用 @文件路径读取复杂请求，字段以现有平台接口为准。 |
| --request-id | 写请求的幂等键；重试同一操作时复用，只有后端支持幂等的接口才保证去重。 |

**不要把凭据放进命令行、URL、对话、日志或作品内容。** 用户应在个人中心签发限时令牌，并在任务结束后撤销。

## 命令清单

所有命令都支持 --json、--base-url、--timeout。以下命令省略 campus-cli 前缀。

| 命令 | 参数与用途 |
| --- | --- |
| me | 查看本人资料、组织和额度。 |
| onboarding get / update | 读取或更新引导；更新时传 --data。 |
| works list / mine | 读取广场或本人作品；list 可传 --org-id。 |
| works get | --id 作品编号，读取作品。 |
| works submit | --data 作品 JSON，提交作品。 |
| works update / withdraw | --id 作品编号；更新时传 --data。 |
| works comments / comment / upvote | --id 作品编号；评论时传 --data。 |
| works upload | --file 本地文件 --software-type web、app 或 pc，上传交付物。 |
| works preview-image | --file 本地图片，上传作品预览图。 |
| works download | --id 作品编号 --output 目标文件；不跟随重定向，以免把凭据转发到外站。 |
| key rotate | 轮换自己的推理密钥；旧钥将失效。 |
| consume | --amount 正整数，调用现有额度消费接口；不是推理请求。 |
| logout | 只结束当前 session；使用 PAT 调用不会撤销 PAT。 |
| org tree / summary / trends / dashboard | --org-id 组织编号；趋势和看板可传 --days。 |
| org members / member | --org-id 组织编号；单个成员加 --id；列表可加 --query。 |
| org featured / audit / works | --org-id 组织编号，读取精选、审计或组织作品。 |
| org review / feature / unpublish | --org-id 组织编号 --id 作品编号；审核或精选传 --data。 |
| accounts list | 列出有权查看的账户；可加 --org-id 和 --query。 |

## 操作步骤

1. 执行 campus-cli --json me，确认当前用户和组织。
2. 先读取目标作品或组织成员，核对编号和操作范围。
3. 向用户说明写操作及影响。尤其是审核、精选、下架和密钥轮换。
4. 用户确认后执行写命令。交互终端会再次询问；agent 在获得确认后才加 --yes。
5. 回读相关资源，核对服务端结果。出现未知结果时先查询，不盲目重放。

示例：

```sh
campus-cli --json me
campus-cli --json works mine
campus-cli works submit --data @work.json --yes
campus-cli org review --org-id college-id --id work-id --data '{"decision":"approve"}' --yes
campus-cli org feature --org-id college-id --id work-id --data '{"featured":true}' --yes
campus-cli org unpublish --org-id college-id --id work-id --yes
```

**二次确认只是客户端约定，不是安全保证。** 服务端按当前角色和准入状态判权。组织管理员不能导入名单或吊销他人密钥。降权或停用后旧 PAT 会失效；待复核用户仅保留既有允许范围。

## 错误与退出码

| 退出码 | 含义与处理 |
| --- | --- |
| 0 | 请求成功，核对返回内容。 |
| 2 / 3 | 参数或环境错误 / 未确认写操作。 |
| 10 / 11 / 12 | HTTP 401 / 403 / 404：重新签发凭据、检查权限或核对目标。 |
| 13 / 14 / 15 | HTTP 409 / 413 / 429：核对冲突、缩小上传或稍后重试。 |
| 16 / 17 / 18 | 服务端错误 / 传输错误 / 其他 HTTP 错误；先核对操作是否生效。 |
| 130 | 用户中断。 |

CLI 不输出认证头、PAT、服务端错误正文或堆栈。不要为了排错打印环境变量。
