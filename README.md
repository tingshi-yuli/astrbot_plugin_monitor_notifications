# 哈工深官网新通知提醒

只做一件事：**自动登录学校官网，定时检测新通知，并推送到指定会话。**

固定监听[哈工深办公信息网通知栏目](http://info.hitsz.edu.cn/list.jsp?urltype=tree.TreeTempUrl&wbtreeid=1053)，默认每 1 小时检查前 3 页，间隔至少 15 分钟。首次成功抓取只建立基线，以后按通知 ID 判断新增，发送标题、日期和原文链接。

## 配置

在 AstrBot 安装插件并安装 `requirements.txt` 中的依赖，然后填写插件配置：

| 配置项 | 填写内容 |
| --- | --- |
| `username` | 学校统一身份认证账号（学号/工号） |
| `password` | 学校统一身份认证密码 |
| `target_umo` | 接收通知的完整会话 UMO |
| `poll_interval_seconds` | 检查间隔，默认 3600 秒（1 小时），最小 900 秒（15 分钟） |

私聊机器人发送 AstrBot 自带的 [`/sid`](https://docs.astrbot.app/use/command.html#sid)，复制返回的 **UMO**，不要只填 QQ 号或用户 ID。保存配置并重载插件后自动运行。使用 AstrBot 的插件启停功能控制运行；插件自身不注册聊天命令。

已有间隔配置不会被覆盖；低于 900 秒的值实际按 900 秒执行。若旧配置为 180 秒，升级后实际每 15 分钟检查；需要每小时检查时将其改为 3600。

## 自动登录与七天续期

账号密码由 AstrBot 保存在**本地插件配置文件**中；密码框遮罩不会加密磁盘文件。Cookie 自动保存于：

```text
data/plugin_data/astrbot_plugin_monitor_notifications/cookies.txt
```

每次检测先复用 Cookie。七天会话到期或提前失效时，如果跳转到统一认证平台，或门户返回 401/403，插件会重新获取登录表单，使用最新的 `execution` 和动态盐加密密码，提交 `rememberMe=true`，完成 CAS 回调后继续抓取。以网站实际返回判断失效，不依赖人工每七天更新 Cookie。

登录失败时，会向 `target_umo` 指定的会话推送提醒，说明失败原因、无法检查新通知以及下次重试时间；认证过程中的网络失败也会提醒。插件按检查间隔自动重试。同一次持续故障每 24 小时最多提醒一次，重启仍保留提醒记录；提醒发送失败则下一轮重试。成功恢复抓取后重置提醒限制，下次登录失败立即提醒。消息平台本身不可用时无法保证提醒送达，错误同时写入 AstrBot 日志。

若学校要求滑块、App、短信或其他二次验证，当前不能全自动完成。进入学校的 `reAuthCheck` 页面时，提醒会明确说明需要二次认证。需在同一登录会话中完成人工验证，再将验证后的 Cookie 保存给插件；在其他浏览器单独登录不会自动同步。学校提供“信任此设备”选项，该操作产生的 Cookie 会与登录会话一起保存和复用；是否再次要求验证仍由学校策略决定。

## 本地状态

同目录的 `state.json` 保存已见通知 ID、待推送通知和上次登录失败提醒时间。重启后继续去重；发送成功后移出待推送列表，发送失败留到下一轮重试。Cookie、状态文件权限为 `0600`，数据目录为 `0700`。源码中不包含真实帐密。

固定扫描前 3 页，长时间停机可能错过窗口外的通知；发送完成但进程尚未保存状态就退出时，可能重发一次。

v0.2 已删除订阅/取消、状态查询、历史列表、手动检查、多栏目设置、Cookie 导入和独立 demo。升级后重新填写上述四项配置；旧 demo 的账号文件和 SQLite 数据库不再读取，也不会被自动删除，首次运行重新建立基线。

## 验证

```bash
python3 -m unittest discover -s tests -v
```

仅做离线验证，覆盖通知解析、重启去重、失败重试，以及模拟七天到期后的自动重新登录；不启动现有 AstrBot，不发送真实消息。

解析依据为 HITA 的 `CampusNoticeParser` 和[本地参考说明](HITA_CAMPUS_NOTICE_IMPLEMENTATION.md)。登录依据为学校公开的 [login.js](https://ids.hit.edu.cn/authserver/cusHitnew/static/web/js/login.js) 与 [encrypt.js](https://ids.hit.edu.cn/authserver/cusHitnew/static/common/encrypt.js)。
