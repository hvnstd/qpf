# 部署 QwenPaw 到 Maritime

[Maritime](https://maritime.sh) 是专为 AI Agent 打造的云平台:每个 Agent 运行在独立的
micro-VM 中,闲时睡眠(接近零成本)、来消息自动唤醒,`/data` 目录持久化。
本目录实现了 Maritime 自定义容器的三端点契约,让 QwenPaw 作为一个完整的 Agent 运行。

## 架构

```
Maritime 平台(front door / CLI / Telegram / 定时触发器)
        │  POST /chat {"message": "...", "source": "..."}
        ▼  0.0.0.0:$PORT (Maritime 注入, VM 内当前为 18789)
┌─────────────────────────────────────────────────┐
│ adapter.py (FastAPI)                            │
│   GET  /health → 代理 /api/healthz              │
│   POST /chat   → 提交后台任务并轮询结果          │
└──────────────┬──────────────────────────────────┘
               ▼  http://127.0.0.1:8088 (仅回环, 不对外暴露)
┌─────────────────────────────────────────────────┐
│ qwenpaw app (Console API + 渠道 + Skills)        │
│ supervisord 同时管理 dbus / Xvfb / xfce4          │
│ (浏览器、computer-use 等技能可用)                 │
└─────────────────────────────────────────────────┘
               ▼
        /data (持久化: config, 工作区, 记忆, 模型缓存)
```

## 部署步骤

### 方式一(推荐):GitHub Actions 构建 → GHCR → Maritime

仓库已内置 workflow `.github/workflows/maritime-image.yml`:push 到 main 即在
GitHub 构建机上构建镜像(内存充足,不受 Maritime 构建机规格限制),冒烟测试
`/health` 通过后推送到 GHCR。

```bash
# 1. 推送代码, 等 workflow 跑完 (Actions 标签页可查看)
git add Dockerfile deploy/maritime .github/workflows/maritime-image.yml
git commit -m "Add Maritime deployment" && git push origin main

# 2. 【仅首次】把 GHCR 包设为 Public (Maritime 只支持公共 registry):
#    https://github.com/users/<你>/packages -> qpf -> Settings
#    -> Change visibility -> Public

# 3. 安装 CLI 并配置 token (在 https://maritime.sh/developers 申请 mk_ 开头的 key)
npm install -g maritime-cli
export MARITIME_TOKEN=mk_xxxxxxxxxxxx

# 4. 创建 agent 并用预构建镜像部署
maritime create qwenpaw --json
maritime deploy qwenpaw --source docker --image ghcr.io/<你>/qpf:latest --wait

# 5. 配置模型: 在 agent 页面的 Env 标签点 "Use Maritime LLM"
#    (自动注入 OPENAI_API_KEY + OPENAI_BASE_URL, 首次启动时 bootstrap
#     会把它配成 QwenPaw 的 openai provider 并激活 QWENPAW_MODEL, 默认 gpt-5.4)
#    或者自带 key:
maritime env set qwenpaw OPENAI_API_KEY=sk-... --json
maritime env set qwenpaw OPENAI_BASE_URL=https://api.openai.com/v1 --json   # 或任意 OpenAI 兼容端点
maritime env set qwenpaw QWENPAW_MODEL=gpt-4o --json

# 6. 验证
maritime status qwenpaw --json
maritime chat qwenpaw "你好,介绍一下你自己" --json
maritime logs qwenpaw --json
```

**可选:全自动部署**。在仓库 Settings → Secrets and variables → Actions 中设置
secret `MARITIME_TOKEN` 和 variable `MARITIME_AGENT`(agent 名)后,每次构建成功
workflow 会自动 `maritime deploy` 并等待完成,实现 push 即发布。

### 方式二(备选):Maritime 直接从仓库构建

Maritime 会构建仓库根目录的 `Dockerfile`(不支持 build args):

```bash
maritime create qwenpaw --json
maritime deploy qwenpaw --source github --repo https://github.com/<你>/<仓库> --branch main --wait
```

私有仓库需先在 Maritime Settings 安装 GitHub App。注意构建机内存规格未知,
本镜像较大(3.4GB),方式一更稳妥。

## 定时任务 (triggers)

休眠中的 VM 内部定时器不会触发,定时任务交给 Maritime 平台执行,
到点唤醒并把 prompt 投递到 `/chat`:

```bash
maritime triggers create qwenpaw --type cron --cron "0 9 * * 1-5"   # 每个工作日 9 点
# 在 dashboard 的 trigger 面板或 API config.prompt 里设置要投递的消息
maritime triggers list qwenpaw
```

QwenPaw 自带的 cron skill 在 VM 休眠期间不会运行;需要准点唤醒的任务请用 Maritime triggers。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `OPENAI_API_KEY` | — | LLM API key;启用 Maritime LLM 后自动注入 |
| `OPENAI_BASE_URL` | OpenAI 官方 | OpenAI 兼容端点;Maritime LLM 为 `https://api.maritime.sh/api/llm/v1` |
| `QWENPAW_MODEL` | `gpt-5.4` | bootstrap 激活的模型 id |
| `ADAPTER_DEADLINE_SECONDS` | `27` | `/chat` 应答期限 (Maritime 硬上限 30s) |
| `QWENPAW_TASK_TIMEOUT` | `900` | QwenPaw 后台任务超时(秒) |
| `QWENPAW_DISABLED_CHANNELS` | `imessage` | 禁用的渠道列表 |

> 模型生效规则:
> - **不设 `OPENAI_API_KEY`**:不做任何模型配置,`qwenpaw init --defaults`
>   激活的内置免费模型 (`kilo/kilo-auto/free`) 开箱可用。
> - **设了 `OPENAI_API_KEY`**:环境变量即事实来源,每次启动都会把
>   `openai/$QWENPAW_MODEL` 配好并激活。想换模型改 `QWENPAW_MODEL` 即可;
>   想完全手动管理,请取消设置 `OPENAI_API_KEY`。

## 已知限制

- **30 秒应答上限**:Maritime 要求 `/chat` 在 30 秒内回复。简单对话没问题;
  长任务(如大型代码改造)会收到"仍在处理中"的提示,任务在后台继续运行。
- **渠道长连接**:钉钉/Discord 等 QwenPaw 自带渠道在 VM 休眠时断开。
  推荐使用 Maritime 的渠道(Telegram 等,消息会唤醒 agent),或购买 always-on add-on。
- **不包含 Console Web UI**:Maritime 镜像跳过了 console 前端构建
  (vite 构建需要 >3GB 内存,且 Web 控制台在 agent 模式下不可达),
  根路径会返回 JSON 提示而非控制台页面。可通过
  `maritime exec qwenpaw ...` 进入 VM 操作,文件在 `/data/working`。

## 本地验证

```bash
docker build -t qwenpaw-maritime .
docker run --rm -e PORT=8080 -e OPENAI_API_KEY=sk-... \
  -v qp-data:/data -p 8080:8080 qwenpaw-maritime

curl localhost:8080/health
curl -X POST localhost:8080/chat -H 'Content-Type: application/json' \
  -d '{"message": "你好", "source": "cli"}'
```
