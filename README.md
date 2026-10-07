# 哈工深官网通知监控 Demo

在当前目录独立开发的 AstrBot 插件。默认监听[哈工深办公信息网通知栏目](http://info.hitsz.edu.cn/list.jsp?urltype=tree.TreeTempUrl&wbtreeid=1053)，每 180 秒检查一次，推送标题、发布日期和原文链接。本次开发不安装、不同步到现有 AstrBot。

## 已实现

- 按 HITA 的 `.Newslist`、`wbnewsid` 和日期规则解析；默认抓取前 3 页，跨页去重。
- 使用本地账号密码自动登录 `ids.hit.edu.cn`，复用 Cookie；过期后重新登录。
- 适配当前学校 `pwdFromId` 表单、动态 `execution`/`pwdEncryptSalt`、AES-CBC/PKCS7 加密及 CAS `service` 回调；账号密码只提交至学校 HTTPS 认证域名。
- SQLite 保存通知、订阅和每个接收会话的待发送记录。默认首次订阅只建立基线，成功发送后才标记已送达；失败保留并重试。
- 管理员私聊订阅、取消、手动检查、查看最近 5 条缓存和运行状态。
- 独立命令行抓取与控制台监控，不依赖运行中的 AstrBot。

## 先独立运行

Python 3.10+，在本目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

# 在本机终端输入学号/密码，密码不回显；不要在聊天中发送密码。
python demo.py credentials

# 单次真实抓取；不建立或改变监控基线。
python demo.py fetch

# 控制台监控，首次只建立基线，新增通知打印到终端。
python demo.py watch --interval 180
```

需要首次就看到当前窗口内的通知时，在第一次 `watch` 时加 `--push-existing`。该选项不重置已建立的基线。`fetch` 始终可以直接查看当前列表。

数据默认保存在 `.demo-data/`，可以把 `--data-dir /path/to/dedicated-data` 放在子命令**之前**指定专用目录。不要让两份运行实例同时使用同一数据目录。

```bash
python demo.py --data-dir /path/to/dedicated-data credentials
python demo.py --data-dir /path/to/dedicated-data fetch
```

`credentials.json` 是只在本地保存的**明文**文件，结构为 `{"username": "学号或工号", "password": "密码"}`；Linux 下目录权限为 `0700`，账号和 Cookie 文件为 `0600`。不在仓库内附带真实凭据，也不向日志输出密码、Cookie 或 CAS ticket。重新运行 `credentials` 会清除旧会话和登录冷却记录。

HTTP 请求使用运行环境中的代理设置，保留 TLS 证书验证。网络需能访问学校门户与认证平台。

## 验证码与手动 Cookie

学校当前使用滑块验证码。登录前先请求 `checkNeedCaptcha.htl`；若要求验证，demo 会停止自动提交并提示。错误密码、滑块或二次认证未完成时，至少冷却 15 分钟，避免频繁尝试。**不自动解决验证码、短信验证或 MFA**。

在浏览器完成统一认证，并确认能打开通知列表后，用浏览器 Cookie 导出功能/扩展导出 JSON（数组中包含 `name`、`value`、`domain`、`path`、`secure`，可含 `expirationDate` 或 `expires`；也支持含 `cookies` 数组的 Playwright storage state）：

```bash
python demo.py import-cookies /path/to/exported-cookies.json
python demo.py fetch
```

只导入 `info.hitsz.edu.cn` / `ids.hit.edu.cn` 的未过期 Cookie。仅导入 Cookie 也可运行，无需账号文件；会话过期后需再次导入，或配置账号进行自动登录。浏览器导出的原始文件也包含会话凭据，请保存在仓库外。

## 后续接入 AstrBot

当前未部署。准备试用时，将本目录作为插件安装到一个测试 AstrBot，安装 `requirements.txt` 的依赖。插件数据目录为 `data/plugin_data/astrbot_plugin_monitor_notifications/`，与独立 demo 的 `.demo-data/` 分离。

使用同一 CLI 为插件的**专用数据目录**创建账号文件，或在 WebUI 的 `credentials_file` 填写既有账号文件的绝对路径。容器部署时，这个文件必须能从容器内访问。也可用 CLI `--data-dir` 向插件数据目录导入 Cookie；写入时先停用插件，完成后重载。

把自己配置为 AstrBot 管理员，然后私聊机器人：

| 命令 | 行为 |
| --- | --- |
| `/校园通知 订阅` | 将当前私聊订阅到所有已配置栏目 |
| `/校园通知 检查` | 立即抓取并投递到期的待发送任务 |
| `/校园通知 最新` | 查看每个栏目最近 5 条缓存，不联网 |
| `/校园通知 状态` | 查看订阅、基线、最近成功抓取、失败原因及待推送数量 |
| `/校园通知 取消` | 取消当前私聊订阅及其未发送任务 |

别名 `/notice`。所有命令限 AstrBot 管理员私聊，推送目标直接记录 `event.unified_msg_origin`。后台使用 `context.send_message`；接入的平台需要支持主动消息。

WebUI 参数见 [_conf_schema.json](_conf_schema.json)：默认自动监控开启、180 秒轮询、最多 3 页、25 秒读取超时、首次不推送历史。没有订阅时不自动请求学校。修改配置后重载插件；关闭 `enabled` 只停止自动轮询，手动检查仍可运行。

## 扩展与边界

`source_urls` 可增加同一门户的其他 `wbtreeid` 栏目，只要页面采用相同列表结构。增加其他学校时，在 `notice_monitor/sources.py` 新增数据源适配器，并扩展网络/认证适配；当前不会假装支持任意网址。

- 按栏目和通知 ID 去重，不靠日期或列表位置判断新增；已有通知改标题/正文不会再次推送。
- 扫描前 N 页，不限制为 HITA 的 30 条；长时间停机超过窗口仍可能漏掉通知。
- 某页解析或网络失败时整次抓取不更新基线，缓存保留；已排队通知仍可继续发送。
- 每轮每栏目最多发送 20 条，失败按 60 秒起步退避至最长 1 小时。抓取失败独立退避。
- 数据库能保证已确认成功的推送不重复；如果平台已收到消息、但进程在落库前退出，下次可能重发，demo 不保证跨平台严格“仅一次”。
- 只抓取列表，不下载正文/附件；点击原文可能仍需要浏览器认证。
- 已用独立 demo 验证真实未登录请求会跳转认证平台，并正确提示缺少本地账号。未持有你的账号，**尚未实测带账号的登录和真实 AstrBot 推送**。

## 最小验证

```bash
python -m unittest discover -s tests -v
python demo.py parse ../hita/HITA_Android/app/src/test/resources/hitsz_notice_list_p1.html
```

测试仅覆盖解析、学校 AES 格式、登录表单、基线与重启去重、失败推送重试，使用假数据且不访问学校、不发消息。

本次验证：6 项离线检查通过，Python 编译和配置格式检查通过；HITA 的真实 HTML 样本解析得到 11 条唯一通知。密码加密校验向量由学校原始 JavaScript 生成，与 Python 结果一致。

实现依据：[本地 HITA 实现说明](HITA_CAMPUS_NOTICE_IMPLEMENTATION.md)、HITA `CampusNoticeParser.kt`；学校公开的 [login.js](https://ids.hit.edu.cn/authserver/cusHitnew/static/web/js/login.js) 和 [encrypt.js](https://ids.hit.edu.cn/authserver/cusHitnew/static/common/encrypt.js)（2026-10-07 核对）。AstrBot 接口参考[主动消息](https://docs.astrbot.app/dev/star/guides/send-message.html)、[配置](https://docs.astrbot.app/dev/star/guides/plugin-config.html)和[存储](https://docs.astrbot.app/dev/star/guides/storage.html)。
