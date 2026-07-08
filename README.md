# NewAPI Channel Panel

管理多个 NewAPI / One-API 风格中转站渠道的小后端。你可能同时手上有好几个中转站账号，这个东西替你把"这个渠道现在能跑哪些模型、每个模型实际单价是多少（已经按你账号的计费分组换算过，不是官方标价）、上游状态页显不显示故障"这几件事聚合成一个接口。

不是一个完整产品，只是一段可以直接跑起来的后端脚本，方便接自己的前端/dashboard。

## 跑起来

```bash
pip install -r requirements.txt
python app.py          # 默认监听 0.0.0.0:8787
# 或者
uvicorn app:app --host 0.0.0.0 --port 8787
```

数据（渠道配置、当前默认渠道）落在 `./data/*.json`，用 `CHANNEL_PANEL_DATA_DIR` 环境变量可以改到别的目录。

### 鉴权

默认不鉴权。设置 `AUTH_TOKEN` 环境变量后，所有接口都要求请求带 `Authorization: Bearer <AUTH_TOKEN>`，否则 401。渠道配置里存的是各中转站的 api_key（相当于账号密码），建议要么设 `AUTH_TOKEN`，要么自己在前面挡一层反向代理 + 防火墙，不要裸跑在公网上。

## 接口

渠道管理：

- `GET /channels` — 列出所有渠道 + 当前默认渠道 id
- `POST /channels` — 新建渠道 `{name, base_url, api_key, status_url?}`
- `PATCH /channels/{id}` — 改渠道任意字段
- `DELETE /channels/{id}` — 删渠道
- `POST /channels/{id}/active` / `DELETE /channels/active` — 设置/清除"默认渠道"指针（只是给前端高亮用，不影响其它接口的行为）

核心接口：

- `GET /channels/{id}/models` — 拉这个渠道能用的模型列表，每个模型附带：
  - 实际单价（按你账号在这个中转站的计费分组换算过，读的是中转站公开的 `/api/pricing`）
  - 状态页信息（自动探测三种常见格式：Uptime-Kuma 风格状态页、NewAPI 内置的模型状态组件、自定义的 `model-status` 监控接口）

## 没做的事：余额查询

原版里还有一个读取中转站控制台余额的功能，做法是让用户把控制台登录后的 session cookie 和 `New-Api-User` 请求头粘贴进渠道配置，再拿这两样东西去代替登录状态请求控制台接口。这个功能**没有**放进这份开源代码里——往配置文件里存别人的登录 cookie 这件事本身有点危险（cookie 泄漏 = 账号被接管），不想把这种模式写进一个别人可能直接抄的开源脚本里。

如果你确实需要这个功能，思路很简单，自己加一个接口就行：

```
GET {中转站 origin}/api/user/self
Headers:
  Cookie: session=<你登录后拿到的 session cookie>
  New-Api-User: <你的用户 ID，登录后控制台请求里能看到>
```

返回的 JSON 里 `data.quota` 是总额度、`data.used_quota` 是已用额度，相减就是余额。拿到之后自己判断要不要长期保存这个 cookie、要不要设过期提醒。

## License

MIT
