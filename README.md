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

- `GET /channels/{id}/models` — 拉这个渠道能用的模型列表，每个模型附带实际单价和状态页信息，具体逻辑见下面两节。

## 价格是怎么算的

价格数据来自中转站公开的 `{base_url 的 origin}/api/pricing`——这是 NewAPI / One-API 这套生态里的标准接口，不需要登录就能看（大部分站是这样，例外见下面）。

**两种计费方式**（看接口返回的 `quota_type`）：

- `quota_type == 1`：按次计费，直接用 `model_price` 字段，显示成 `按次 X`
- 其它情况：按量计费，用 `model_ratio`（输入的基准倍率）分别乘上 `completion_ratio` / `cache_ratio` / `create_cache_ratio` 算出输出/缓存/写入相对输入的倍数，拼成 `输入 X / 输出 Y / 缓存 Z / 写入 W`

**按你的计费分组调价**：`/api/pricing` 的返回里还有个 `group_ratio` 字典，是站方给不同计费分组（比如 `vip`/`default`）设的整体折扣倍率。脚本会尝试猜你这个渠道属于哪个分组——做法是把渠道名字按常见分隔符（` · ` / ` \| ` / ` / ` / `｜`）拆开，和 `group_ratio` 里的分组名做匹配（先找完全一样的，找不到再找互相包含的）。**这意味着渠道名字最好把分组名带上才能生效**——比如渠道叫"小鸡农场 · 小鸡 default"，脚本就是靠拆出来的 "default" 这个词去匹配 `group_ratio` 里的 `default` 分组的。一个都没匹配上就当倍率是 1，显示官方原价。

返回里 `base_price` 是没调整过的官方标价，`price` 是套上你分组倍率之后的价格——两个都给，方便你对比"官方写多少 / 我实际会被扣多少"。模型列表（`/models`）和价格表（`/api/pricing`）用的命名不一定是同一套，所以匹配的时候优先找原始名字完全一致的价格行，找不到再按型号名/路由前缀/分组名做模糊打分，取分最高的一条。

**有些站的 `/api/pricing` 本身需要登录态才能看**——这跟渠道有没有配 `api_key` 是两码事：`api_key` 是给 `/chat/completions` 这类正常调用用的，`/api/pricing` 是给它自己控制台网页用的接口，不少站直接要求带 session cookie 才让看。脚本探测到这种情况（接口返回 401/403）时不会想办法硬闯，就如实报 `pricing_requires_auth: true`——那个渠道下所有模型都会正常出现在列表里（`/models` 本来就是 api_key 鉴权的，跟这个无关），只是不带 `price` 字段。实测过：`treegpt.cc` 这个渠道就是这样，39 个模型全部正常返回，但没有一个带上价格。真要把这类站的价格也拉出来，只能让用户额外提供登录 cookie 去请求 `/api/pricing`——这跟余额查询是同一类"需要 cookie"的功能，出于同样的安全考虑没有做进开源版（见下一节）。

## 状态页是怎么兜底探测的

目标是把这个渠道对应的监控/状态页（如果有的话）抓下来，跟每个模型对上号，标出在线/波动/挂了。中转站搭状态页常见的至少有三种不同的软件/格式，脚本按下面顺序挨个试，命中一个就用，不会同时套用好几种：

1. **Uptime-Kuma 风格状态页**（最常见，很多站直接拿开源的 Uptime Kuma 搭）。找法：
   - 如果渠道配了 `status_url` 且链接里能认出 `/status/<slug>` 这种模式，直接算出对应的 `/api/status-page/<slug>` + 心跳接口 `/api/status-page/heartbeat/<slug>`
   - 没配或者认不出来，就拿链接最后一段路径当 slug 猜，外加几个常见 slug（`api` / `tree` / `status` / `main`）挨个试
   - 同时会去请求中转站首页的 `{origin}/api/status`，把返回 JSON 里所有字符串值递归扫一遍，只要发现里面嵌着 `/status/xxx` 这样的链接也当候选加进去
   - 还会猜"status."子域名（比如 `api.xxx.com` 猜 `status.xxx.com`）配合那几个常见 slug 再试一轮
   - 所有候选按顺序试，只要请求到的数据里带 `publicGroupList` 这个字段（Uptime Kuma 的特征字段），就认定找对了，把每个监控项的名字、最近一次心跳的状态/时间/延迟拼成结果，不再往下试
2. **NewAPI 自带的"模型状态"嵌入组件**（站点没用独立的 Uptime Kuma，而是 NewAPI 自己那套监控组件）。请求 `/api/model-status/embed/config/selected` 拿到这个站选中监控的模型和自定义分组，再拿模型名去 `/api/model-status/embed/status/batch` 批量查状态
3. **自定义的 model-status 接口**（少数站用自己的一套私有监控 API，比如 msuicode 那几个渠道）。先 `GET /api/v1/model-status/models` 拿监控的模型目录，再 `POST /api/v1/model-status` 批量查每个模型的可用率/耗时

整个探测过程给了 10 秒总预算，每一步请求根据剩余时间动态收窄超时，某一步超时就直接放弃、试下一种格式，不会把请求拖到没响应。三种格式都没探测到东西的话，`status_summary` 就是空数组、`status_source` 是 `null`，不会报错，只是没有状态信息。

拿到状态数据之后，怎么知道某一条状态属于哪个模型：拿"模型 id / 模型名 / 路由前缀 / 价格行的分组名 / 渠道名"这几个标签，跟状态条目的名字做宽松匹配（忽略大小写、空格、破折号、竖线、括号这些符号，只要互相包含就算命中）。所以监控页上监控项的命名跟你实际模型名越接近，匹配就越准；命名差太远可能匹配不上，那个模型就没有 `status` 字段——不代表真的没监控到，只是脚本没认出这是同一个东西。

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
