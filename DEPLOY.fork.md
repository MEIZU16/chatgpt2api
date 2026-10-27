# Fork 部署说明（eu-main）

本仓库（`MEIZU16/chatgpt2api`）是 `basketikun/chatgpt2api` 的 fork。本机克隆中 `fork` 远端指向 `MEIZU16/chatgpt2api`，`origin` 指向 `basketikun/chatgpt2api`，日常开发与发布改动推到 `fork` 远端。

线上实例运行在 **eu-main（主机信息见本机 server-ops 文档）**。推送代码或构建镜像**不会**触发自动部署，统一在 eu-main 的 compose 文件中固定镜像 tag 并手动切换。

---

## 线上运行拓扑

`chatgpt2api` 主服务与 `sentinel-solver` 求解服务同属 eu-main（主机信息见本机 server-ops 文档）`/opt/image2/docker-compose.yml` 栈，与 Image2 共用内网 `image2-net`。

### `chatgpt2api`（主服务）

- **当前镜像**：`ghcr.io/meizu16/chatgpt2api:sha-3c2d86f`（revision `3c2d86f3698ba06f33970f92b56db712daa450a9`）
- **端口映射**：`127.0.0.1:18084->80/tcp`
- **持久化挂载**：`/opt/image2/chatgpt2api-data -> /app/data`（包含 `accounts.json`、`auth_keys.json`、`config.json`、`image_index.json`、`images/`、`logs.jsonl` 等）
- **流量路径**：
  - **公网访问**：`https://c2a.oblivionis.net`，经 Cloudflare Tunnel `image2-eu`（同栈容器 `cloudflared-image2`）转发至内网 `http://chatgpt2api:80`。原 Zeabur 域名 `c2a-e93ne1g13k.zeabur.app` 已停用（现返回 502）。
  - **内网调用**：Image2 主服务通过 `image2-net` 内网地址 `http://chatgpt2api:80` 直接调用。

### `sentinel-solver`（Turnstile 与 Cloudflare 求解服务）

- **当前镜像**：`ghcr.io/meizu16/sentinel-solver:sha-6c472f7`（revision `6c472f78171d13200cfc4c33c6dbd2279bf3a405`）
- **端口映射**：`127.0.0.1:18086->8080/tcp`
- **持久化挂载**：无要持久化的数据
- **职责与调用方式**：常驻无头 Chromium 打开 `chatgpt.com`，替 `chatgpt2api` 计算 Turnstile token 并按需获取 Cloudflare cookie；`chatgpt2api` 的文字对话和生图请求均需经过它，在 `image2-net` 内通过别名 `service-6ad01f0a7741ff5769fbac81:8080`（或 `sentinel-solver:8080`）访问。源码位于本仓库 `sentinel_solver/`。

---

## 镜像构建与 Tag 规则

两个服务的镜像均由 GitHub Actions 构建并推送为 GHCR 公开镜像，完整 40 位 commit SHA 写入镜像 label `org.opencontainers.image.revision`。两者均不会自动上线。

| 服务 | Workflow 文件 | 镜像名 | 触发条件 | 产出的 Tag 格式 |
| --- | --- | --- | --- | --- |
| `chatgpt2api` | `.github/workflows/docker-publish.yml`（Publish Docker Image） | `ghcr.io/meizu16/chatgpt2api` | 手动运行（`workflow_dispatch`）或推送 `v*` tag（**普通 push `main` 不触发**） | 手动在分支上运行时仅产出 `sha-<commit 前 7 位>`；推送 `v*` tag 时额外产出 `latest`、`v*`、`<version>`、`<major>.<minor>` |
| `sentinel-solver` | `.github/workflows/sentinel-solver-image.yml`（Sentinel Solver Image） | `ghcr.io/meizu16/sentinel-solver` | push `main` 且改动涉及 `sentinel_solver/**` 或该 workflow 文件，或手动运行（`workflow_dispatch`） | 仅产出 `sha-<commit 前 7 位>`（不产出 `latest`） |

注意：
- 只改主服务代码的提交不会触发 `Sentinel Solver Image`，因此 `sentinel-solver` 的最新镜像 tag 对应的是最后一次改到 `sentinel_solver/` 的 commit，不一定是 `main` 的最新 commit。
- 线上 `/opt/image2/docker-compose.yml` 中两个服务统一固定写死 `sha-<commit 前 7 位>` tag。

---

## 发布 `chatgpt2api` 主服务

### 1. 准备

1. 将代码推到 `fork` 的 `main` 分支后，手动触发 `Publish Docker Image` 并等待构建成功（构建 `linux/amd64,linux/arm64`，含前端 `web/` 与后端，约 1–3 分钟）：
   ```bash
   gh workflow run docker-publish.yml -R MEIZU16/chatgpt2api --ref main
   gh run list -R MEIZU16/chatgpt2api --workflow docker-publish.yml --limit 5
   ```
2. 用 `git diff <线上 commit> <新 commit>` 检查本次改动范围（重点关注 `Dockerfile`、`pyproject.toml`、`uv.lock`、`services/`、`api/`、`web/`）。
3. 在 eu-main 上预拉新镜像并核对 `org.opencontainers.image.revision`：
   ```bash
   docker pull ghcr.io/meizu16/chatgpt2api:sha-xxxxxxx
   docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' ghcr.io/meizu16/chatgpt2api:sha-xxxxxxx
   ```

### 2. 切换

1. 先查 Image2 是否有进行中的任务，挑空闲时段切换（c2a 任务的 `upstream_url` 是 `http://chatgpt2api:80`）：
   ```bash
   docker exec image2-postgres sh -c 'psql -U "$POSTGRES_USER" -d zeabur -c "select count(*) from tasks where status in ('\''pending'\'','\''running'\'');"'
   ```
2. 备份 `/opt/image2/docker-compose.yml` 及持久化目录中的关键配置与账号文件：
   ```bash
   cd /opt/image2
   cp -p docker-compose.yml docker-compose.yml.bak-<日期>-before-chatgpt2api-sha-xxxxxxx
   cp -p chatgpt2api-data/accounts.json chatgpt2api-data/accounts.json.bak-<日期>-before-sha-xxxxxxx
   cp -p chatgpt2api-data/config.json chatgpt2api-data/config.json.bak-<日期>-before-sha-xxxxxxx
   ```
3. 只把 `/opt/image2/docker-compose.yml` 中 `chatgpt2api` 服务的 `image:` 改成新 tag `ghcr.io/meizu16/chatgpt2api:sha-xxxxxxx`（compose 里有明文密钥，不要整份打印），再单独重建 `chatgpt2api`：
   ```bash
   cd /opt/image2 && docker compose up -d --no-deps chatgpt2api
   ```

### 3. 验证

1. 检查容器状态与版本接口：
   ```bash
   docker inspect -f '{{.State.Status}}' chatgpt2api
   curl -fsS http://127.0.0.1:18084/version
   ```
2. 把 `chatgpt2api` 的访问密钥读进变量（不要打印），分别发一次文字请求与生图请求验证完整链路：
   ```bash
   K=$(docker exec chatgpt2api printenv CHATGPT2API_AUTH_KEY)

   # 文字请求：走公网隧道，model 设为 auto，提示词带随机串以绕过对话缓存、确保走到 sentinel-solver
   curl -fsS https://c2a.oblivionis.net/v1/chat/completions \
     -H "Authorization: Bearer $K" \
     -H "Content-Type: application/json" \
     -d "{\"model\":\"auto\",\"messages\":[{\"role\":\"user\",\"content\":\"ping $(date +%s)-$RANDOM reply pong only\"}]}"

   # 生图请求：model 设为 gpt-image-2，n=1，b64_json，会消耗一张生图额度
   curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18084/v1/images/generations \
     -H "Authorization: Bearer $K" \
     -H "Content-Type: application/json" \
     -d '{"model":"gpt-image-2","prompt":"a minimal white cube on gray background","n":1,"response_format":"b64_json"}'

   unset K
   ```
   两次请求均应返回 200。
3. 检查日志：`docker logs --since <切换时间> chatgpt2api` 里不应出现 `SentinelSolverError`、`Model not found`、`Unusual activity`（后台刷新账号时出口代理偶发 `CONNECT tunnel failed, response 403` 是代理自身波动，切换前就有）。

### 4. 回滚

1. 把 `/opt/image2/docker-compose.yml` 中 `chatgpt2api` 的 `image:` 改回切换前的 tag（即第 2 步备份的 compose 里写的 tag，旧镜像仍留在主机本地；写这份文档时线上为 `ghcr.io/meizu16/chatgpt2api:sha-3c2d86f`），再执行：
   ```bash
   cd /opt/image2 && docker compose up -d --no-deps chatgpt2api
   ```
2. 若新版本改动过 `accounts.json` 或 `config.json` 格式且需恢复备份文件，必须先 `docker compose stop chatgpt2api`（防止容器在运行或退出时把内存里的账号写回磁盘覆盖备份），拷回第 2 步备份的 `.bak` 文件后再用旧 tag 启动。

---

## 发布 `sentinel-solver` 服务

### 1. 准备

1. 确认目标 commit 的 `Sentinel Solver Image` workflow 是绿的：
   ```bash
   gh run list -R MEIZU16/chatgpt2api --workflow sentinel-solver-image.yml --limit 5
   ```
2. 用 `git diff <线上 commit> <新 commit> -- sentinel_solver/` 查看改动。`sentinel_solver/requirements.txt` 钉住的 `playwright` 版本决定内置 Chromium 版本和页面 UA（反映在 `/health` 的 `browser_version`、`user_agent`），`chatgpt2api` 会按 `/health` 生成请求里的浏览器字段；若升了 `playwright`，第 3 步验证要重点观察。
3. 在 eu-main 上先拉取新镜像并核对 `org.opencontainers.image.revision`：
   ```bash
   docker pull ghcr.io/meizu16/sentinel-solver:sha-xxxxxxx
   docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' ghcr.io/meizu16/sentinel-solver:sha-xxxxxxx
   ```

### 2. 切换

1. 先查 Image2 是否有进行中的任务，挑空闲时段切换：
   ```bash
   docker exec image2-postgres sh -c 'psql -U "$POSTGRES_USER" -d zeabur -c "select count(*) from tasks where status in ('\''pending'\'','\''running'\'');"'
   ```
2. 备份 compose 文件，只把 `sentinel-solver` 服务的 `image:` 改成新 tag `ghcr.io/meizu16/sentinel-solver:sha-xxxxxxx`（compose 里有明文密钥，不要整份打印），再单独重建 `sentinel-solver`：
   ```bash
   cd /opt/image2
   cp -p docker-compose.yml docker-compose.yml.bak-<日期>-before-sentinel-solver-sha-xxxxxxx
   docker compose up -d --no-deps sentinel-solver
   ```
   旧容器停止约 10 秒，新容器启动后约 6 秒 Chromium 页面就绪（合计约 17 秒），这期间 `chatgpt2api` 的文字和生图请求会失败。

### 3. 验证

1. 检查 `/health` 中 `ready` 为 `true`、`last_error` 为 `null`：
   ```bash
   curl -fsS http://127.0.0.1:18086/health
   ```
2. 用 `K=$(docker exec chatgpt2api printenv CHATGPT2API_AUTH_KEY)` 读入密钥（不要打印），按上节「发布 `chatgpt2api` 主服务 → 3. 验证」的同一组命令各发一次文字请求（`POST https://c2a.oblivionis.net/v1/chat/completions`，`model` 为 `auto`，提示词带随机串绕过对话缓存）和生图请求（`POST http://127.0.0.1:18084/v1/images/generations`，`model` 为 `gpt-image-2`，`n=1`，`response_format=b64_json`，会用掉一张额度），两次都要返回 200。
3. 新求解器第一次求解后，`docker logs sentinel-solver` 里会出现 `turnstile asset: …`（镜像用 `python app.py` 启动，没有 HTTP access log）；`docker logs --since <切换时间> chatgpt2api` 里不应有 `SentinelSolverError`、`Model not found`、`Unusual activity`。

### 4. 回滚

把 `/opt/image2/docker-compose.yml` 中 `sentinel-solver` 的 `image:` 改回切换前的 tag（即第 2 步备份的 compose 里写的 tag，旧镜像仍留在主机本地；写这份文档时线上为 `ghcr.io/meizu16/sentinel-solver:sha-6c472f7`，再往前的 Zeabur 构建 `sentinel-solver:local` 也还在本地），再执行：

```bash
cd /opt/image2 && docker compose up -d --no-deps sentinel-solver
```

`sentinel-solver` 没有持久化数据和迁移，直接换回镜像即可。

---

## 哪些情况要多看看

1. **升级 `sentinel-solver` 的 `playwright`、基础镜像或字体**
   - `sentinel_solver/requirements.txt` 里的 `playwright` 版本决定 Chromium 版本和页面 UA，基础镜像安装的字体包会影响页面可探测到的字体与 canvas 文字渲染。升级后需对比 `/health` 的 `browser_version`、`user_agent`、`environment`，并确认生图与对话请求不会触发伪装 404（`Model not found`）或 403（`Unusual activity`）。
   - **`chatgpt2api` 对 `/health` 的 10 分钟内存缓存**：`services/sentinel_solver_client.py` 会将 `/health` 返回的页面 UA 与浏览器环境缓存 600 秒（探测失败则缓存空值 30 秒）。若升级 `playwright` 改变了 Chromium 版本或 UA，在 `sentinel-solver` `ready: true` 后应顺手 `docker compose restart chatgpt2api`，避免 10 分钟内主服务仍用旧 UA 构造 `requirements/proof` token。
2. **改动 `chatgpt2api` 的 TLS 指纹、PoW 或请求头（`curl-cffi` / `OAI-*`）**
   - 主服务使用 `curl_cffi` 模拟新版 Chrome TLS 指纹（如 `chrome146`），且首页 bootstrap 与对话接口的请求头要求不同（首页 bootstrap 不带 `OAI-*` 头）。升级 `curl-cffi`（修改 `uv.lock`）或改动 `services/openai_backend_api.py`、`utils/pow.py`、`services/cf_cookie_cache.py` 后，需同时观察账号定时刷新/首页预热日志和实际对话/生图请求。
3. **手工编辑或恢复 `/opt/image2/chatgpt2api-data/` 下的文件**
   - `chatgpt2api` 在内存中维护账号状态，并按 `refresh_account_interval_minute` 定期把内存里的账号写回 `accounts.json`。手工修改或恢复 `accounts.json` 前必须先停掉 `chatgpt2api` 容器，否则内存旧状态会覆盖磁盘文件。
4. **改动前端 `web/` 或根 `Dockerfile`**
   - 根 `Dockerfile` 会先在 `node:22-alpine` 中构建 `web/` 静态产物再拷入 `/app/web_dist`，且 `docker-publish.yml` 同时构建 `linux/amd64` 与 `linux/arm64` 双架构。改动前端后构建耗时明显变长，上线后需额外验证管理后台静态资源访问正常。
5. **主机镜像清理与旧 compose 备份禁忌**
   - **切勿在 eu-main 上执行 `docker image prune -a`**：主机本地保留着未推 GHCR 的历史回滚镜像（如 `sentinel-solver:local`、`image2:local`）。
   - **从旧备份恢复 `/opt/image2/docker-compose.yml` 时**：不要把已删除的 `cloudflared-tunnel` 服务（原 `cutover` profile）带回来，更不要用 `--profile cutover` 启动，否则会接入另一条正在使用的隧道并导致部分请求 502。
