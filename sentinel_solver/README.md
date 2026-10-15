# sentinel 求解服务

从 2026-10-14 起，chatgpt.com 的对话接口（文字和生图）开始严格校验 sentinel：

- `chat-requirements` 必须带上在**真实 chatgpt.com 页面**里算出的 Turnstile token。纯 Python 或 Node 模拟算出的结果会被拒，缺失时返回 403 "Unusual activity"。
- 请求的 TLS 指纹必须是新版 Chrome（主服务默认已改为 curl_cffi `chrome146`）。
- requirements/proof token 里的浏览器字段（UA、时区、语言、核数、内存上限等）要像真实页面，并和算 Turnstile 的页面一致；PoW 使用网页同款 FNV-1a 算法（结果带 `~S` 后缀）。

只要校验质量不够，对话接口不会直接返回 403，而是返回伪装成 404 的 `{"detail":"Model not found"}`。

本服务常驻一个无头 Chromium（带 playwright-stealth），一直开着 chatgpt.com 页面，替主服务计算 Turnstile。主服务不需要打包浏览器。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 页面状态、页面 UA 和浏览器环境（时区、语言、核数等），主服务据此生成 requirements/proof token |
| POST | `/v1/turnstile` | `{"p", "dx", "requirements"}` → `{"token"}`。p 是 prepare 请求提交的 requirements token，dx 和 requirements（完整 prepare 响应）取自 prepare 响应；带 requirements 时优先调用页面自带的 turnstile 模块，失败再用移植的 VM。结果会校验，算不出有效 token 时返回 503 |
| POST | `/v1/so` | `{"collector_dx", "snapshot_dx", "p"}` → `{"token"}`，目前对话接口还不强制要求 |

设置了 `SENTINEL_SOLVER_KEY` 时，请求需要带 `Authorization: Bearer <key>` 头。

## 部署

```bash
docker compose up -d --build sentinel-solver
```

主服务通过环境变量（或 `config.json` 里的 `sentinel_solver_url` / `sentinel_solver_key`）指向本服务：

```
CHATGPT2API_SENTINEL_SOLVER_URL=http://sentinel-solver:8080
CHATGPT2API_SENTINEL_SOLVER_KEY=<与 SENTINEL_SOLVER_KEY 相同>
```

未配置或求解失败时，主服务直接报错（SentinelSolverError），不再退回本地求解器：它已跟不上新版挑战程序，算出的 token 只会换来伪装 404。

环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SENTINEL_SOLVER_KEY` | 空 | 访问密钥 |
| `SENTINEL_SOLVER_PROXY` | 空 | 浏览器出网代理，应与主服务访问 chatgpt.com 的出口一致 |
| `SENTINEL_SOLVER_USER_AGENT` | 按内置 Chromium 版本生成 | 页面 UA |
| `SENTINEL_SOLVER_TIMEZONE` | 跟随系统 | 页面时区（如 `Asia/Tokyo`），建议与出口 IP 所在地一致 |
| `SENTINEL_SOLVER_REFRESH_SECONDS` | 1800 | 页面定期重开的间隔 |
| `SENTINEL_SOLVER_HEADLESS` | true | 调试时可设为 false |
| `PORT` | 8080 | 监听端口 |

## 注意

- 必须使用完整 Chromium 的新版无头模式（`channel="chromium"`），并加上 stealth；否则页面会停在 Cloudflare「请稍候…」，算出的 token 无效。
- 求解时优先调用页面自带的 turnstile 模块（做法参考 basketikun/chatgpt2api#418 与 239573049/WebImage），前端更新后无需改代码；`vm.js` 移植自 chatgpt.com 前端，只作兜底，OpenAI 更新指令集后需要同步更新。
- 本服务只解决请求质量问题。由脚本批量注册的账号，即使在真实网页里也会提示 "Model not found"，属于账号层面的限制。
