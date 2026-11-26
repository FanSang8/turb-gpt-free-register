# Roxy 注册、Remail 采购和账号推送

默认 `REGISTER_PUSH_ONLY=true`。注册取得账号后，关闭并清理本轮创建的
Roxy 环境，再同步推送到接收服务。跳过 Codex 自动授权、注册后停留和
后台套餐查询。新账号的密码、access token、TOTP 不进入账号库或任务记录。
已有账号数据不做自动删除。

在 WebUI 的“账号推送”中配置：

```dotenv
PUSH_API_URL=https://your-chatgpt2api.example
PUSH_API_TOKEN=your-admin-key
PUSH_API_TIMEOUT=15
PUSH_API_VERIFY_TLS=true
PUSH_API_INCLUDE_PROXY=false
```

URL 可填写根地址或完整 `/api/accounts` 地址。兼容参考项目的
`REGISTER_PUSH_API_URL`、`REGISTER_PUSH_API_TOKEN` 等环境变量，
两者同时存在时带 REGISTER 前缀的值优先。

推送使用 `Authorization: Bearer <Key>`，JSON 格式：

```json
{"accounts":[{"email":"example@outlook.com","access_token":"...","type":"free","source_type":"outlook_external"}]}
```

非 Remail 来源为 `source_type=web`。`PUSH_API_INCLUDE_PROXY` 默认 `true`，
兼容原有行为：注册代理存在时增加 `proxy` 字段。设为 `false`，或在
WebUI“账号推送”关闭“推送携带代理”，推送时会完全省略 `proxy` 字段，
包括代理地址和认证信息。此开关不改变注册浏览器的代理配置或当前代理池。
也支持 `REGISTER_PUSH_API_INCLUDE_PROXY` 环境变量；同时配置时该别名优先。
任务列表分别显示任务结果和推送结果。推送失败时不会重新注册同一邮箱，
也不将凭证写入失败队列；本次凭证在请求结束后释放，无法跨进程重推。
未配置有效 URL 时直接拒绝开始注册，不消耗邮箱订单。
`REGISTER_PUSH_ONLY=false` 可恢复旧版账号保存和 Codex 自动授权行为。

## Remail

```dotenv
USE_EMAIL_SERVICE=true
EMAIL_SOURCE=remail
REMAIL_ENABLED=true
REMAIL_API_BASE=https://remail.aishop6.com
REMAIL_API_KEY=rk-your-key
REMAIL_PROJECT_ID=2
REMAIL_EMAIL_SUFFIX=outlook.com
REMAIL_SERVICE_MODE=purchase
REMAIL_PURCHASE_BATCH_SIZE=4
REMAIL_MAX_PURCHASE_PER_ROUND=8
```

采购和分配管理来自 `register-deploy`：

- 按项目和后缀、公开库存长效购买，不要求 productId。
- 默认不足时至少补 4 个主邮箱，单轮最多 8 个；这些设置指邮箱采购数量。
- 每个主邮箱包含一个主地址和一个随机 `+se...` 子地址；主地址任务结束后
  子地址才可领用。Roxy 找到邮箱输入框后才分配地址并按需补货。
- 先保存采购意图，再提交订单。超时使用相同幂等键重试，并按订单时间、
  API Key ID 和下单前快照恢复订单，避免普通重试重复扣费。
- 服务重启后恢复尚未完成的采购意图；已经开始注册的地址保守地按已使用
  处理，避免重复注册。
- 余额不超过 200 积分时告警，默认间隔 600 秒；低余额本身不暂停采购。
- `remail_state.json` 只保存付费邮箱订单、分配状态和必要的取码凭证；
  一个订单的主、子地址均结束后删除该订单的取码 token，不保存注册账号
  的 access token、密码或 TOTP。尚未使用的付费邮箱继续保留。

`GET /api/remail/status` 只读本地状态，不采购且不返回取码凭证。
重新保存开启采购管理或调用 `POST /api/remail/reset-circuit` 可解除采购熔断。
沿用 WebUI 的登录鉴权。

当前采购管理固定使用公开库存；旧 `REMAIL_SUPPLY_POLICY` 和
`REMAIL_ORDER_WAIT_SECONDS` 只供旧单笔客户端兼容，不控制自动采购管理。
请使用 `purchase + outlook.com` 对齐参考项目；其他后缀的子地址兼容性未经验证。
