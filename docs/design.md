# AutoFree — 设计文档

> 批量自动注册 OpenAI free 账号的独立 web 应用。从 AutoTeam-F 项目里的 freegen 模块抽出,重写前端为 React,采用苹果风视觉。

## 1. 目标 & 非目标

### 目标
- 浏览器面板:配置 → 一键启动批量注册 → 实时进度 → 账号库下载
- 多 cloud-mail 域名池(平铺、可启用/禁用、按需轮选)
- SMS provider 可切(5sim / hero-sms)
- 注册成功后:**始终**生成本地 CPA-importable JSON;CPA 配了则**额外**自动 push
- 单密码全站锁(.env 引导,设置页可改)
- 默认 SQLite,支持切 PG/MySQL

### 非目标(v1)
- 多用户 / RBAC
- 并发 batch / 多浏览器并行
- AutoTeam-F 历史数据迁移
- WebSocket(SSE 已够用)
- i18n(中文 only)

## 2. 总体架构

```
┌──────────────────────────────┐
│ React SPA (Apple style)      │
│  Login / Dashboard / Batch   │
│  Accounts / Pending / Settings│
└──────────────┬───────────────┘
       HTTP + SSE (cookie auth)
               │
┌──────────────▼───────────────┐
│ FastAPI                      │
│  /api/auth      /api/freegen │
│  /api/settings  /api/domains │
│  /api/accounts  /api/sse/*   │
└──────────────┬───────────────┘
               │
┌──────────────▼───────────────┐
│ autofree.core (← freegen 抽) │
│  browser/identity/mail/oauth │
│  register/batch/storage/sms  │
│  cpa_sync (← autoteam 抽)    │
└──────────────┬───────────────┘
               │
        SQLite / PG / MySQL
        + 本地 JSON 输出目录
```

## 3. 项目结构

```
/data/dev/py/AutoFree/
├── backend/
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── src/autofree/
│   │   ├── __init__.py
│   │   ├── main.py              # FastAPI app + SPA static mount + lifespan
│   │   ├── settings.py          # Pydantic Settings (env)
│   │   ├── deps.py              # auth/db DI
│   │   ├── auth/
│   │   │   ├── routes.py        # /login /logout /change-password /me
│   │   │   ├── service.py       # bcrypt + session token
│   │   │   └── bootstrap.py     # 首次启动写 User 表
│   │   ├── api/
│   │   │   ├── settings.py      # cloud-mail / sms / cpa  KV CRUD
│   │   │   ├── domains.py       # 域名池 CRUD
│   │   │   ├── freegen.py       # /start /stop /status
│   │   │   ├── accounts.py      # 账号列表 / 下载 / pending
│   │   │   └── sse.py           # SSE 任务事件流
│   │   ├── db/
│   │   │   ├── base.py          # SQLAlchemy 2.0 + session factory
│   │   │   ├── models.py        # User Setting Domain Batch Account PendingAccount
│   │   │   └── alembic/         # 迁移
│   │   └── core/                # ← 抽自 freegen
│   │       ├── browser.py
│   │       ├── identity.py
│   │       ├── mail.py
│   │       ├── oauth.py
│   │       ├── register.py
│   │       ├── batch.py
│   │       ├── storage.py       # 改为 DB + 文件双写
│   │       ├── sms.py
│   │       ├── sms_providers/
│   │       ├── control.py       # stop signal
│   │       ├── errors.py
│   │       └── cpa_sync.py      # ← 内联自 autoteam
│   └── tests/
├── frontend/
│   ├── package.json             # React 19 + Vite + TS + Tailwind
│   ├── vite.config.ts
│   ├── tailwind.config.ts
│   ├── tsconfig.json
│   ├── index.html
│   └── src/
│       ├── main.tsx
│       ├── App.tsx
│       ├── routes.tsx
│       ├── api/                 # axios client + endpoints
│       ├── store/               # zustand
│       ├── pages/
│       │   ├── LoginPage.tsx
│       │   ├── DashboardPage.tsx
│       │   ├── BatchPage.tsx
│       │   ├── AccountsPage.tsx
│       │   ├── PendingPage.tsx
│       │   └── SettingsPage.tsx
│       ├── components/
│       │   ├── Sidebar.tsx
│       │   ├── Card.tsx
│       │   ├── Button.tsx
│       │   ├── Input.tsx
│       │   ├── ProgressRing.tsx
│       │   ├── Toast.tsx
│       │   └── Modal.tsx
│       └── styles/
│           └── globals.css      # tailwind base + 苹果字体
├── docker/
│   ├── Dockerfile               # multi-stage: node build → python runtime
│   └── entrypoint.sh
├── docker-compose.yml           # app (+ 可选 postgres)
├── docs/
│   └── design.md                # 本文档
├── .env.example
├── .gitignore
└── README.md
```

## 4. 数据模型

```python
class User(Base):
    id: int                       # 单行 (id=1)
    password_hash: str            # bcrypt
    updated_at: datetime

class Setting(Base):              # KV 配置
    key: str (PK)
    value: str (JSON)
    updated_at: datetime

# Setting keys (规范):
#   cloud_mail.base_url           cloud-mail 服务 URL
#   cloud_mail.password           cloud-mail 邮箱通用密码
#   sms.provider                  "5sim" | "hero-sms"
#   sms.api_key                   provider 的 API key
#   sms.service                   "openai"
#   sms.country                   "france" / "uk" / ...
#   sms.operator                  "any" / "virtual51" / ...
#   cpa.url                       CPA 平台 URL
#   cpa.key                       CPA API key
#   cpa.enabled                   true/false

class Domain(Base):
    id: int
    domain: str (unique)          # 不带 @ 的纯域名
    enabled: bool
    success_count: int
    fail_count: int
    last_used_at: datetime | None
    created_at: datetime

class Batch(Base):
    id: str (PK, 12 hex)
    domain: str
    count: int                    # 计划数
    status: str                   # pending|running|finished|stopped|failed
    started_at: datetime | None
    finished_at: datetime | None
    ok: int                       # 成功数
    failed: int                   # 失败数
    created_at: datetime

class Account(Base):
    id: int
    batch_id: str (FK)
    email: str (unique)
    password: str                 # 明文 (注册时生成,本地保存,与本地 JSON 一致)
    account_id: str
    plan_type: str                # "free" 通常
    access_token: str (Text)
    refresh_token: str (Text)
    id_token: str (Text)
    expires_at: datetime
    last_refresh: datetime
    auth_json_path: str           # 本地输出 JSON 的相对路径
    cpa_synced: bool
    cpa_synced_at: datetime | None
    cpa_error: str | None         # 同步失败原因
    created_at: datetime

class PendingAccount(Base):       # 注册成功但 OAuth 拿 token 失败
    id: int
    batch_id: str (FK)
    email: str
    password: str
    error_kind: str               # phone_gate_timeout / oauth_failed / ...
    error: str (Text)
    created_at: datetime
    resolved_at: datetime | None
    resolved_via: str | None      # "manual_import" / "retry_oauth"
```

## 5. API

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/auth/me` | 当前 session 是否有效 |
| POST | `/api/auth/login` | `{password}` → set httpOnly cookie |
| POST | `/api/auth/logout` | 清 cookie |
| POST | `/api/auth/change-password` | `{old, new}` → 更新 + 踢旧 session |
| GET/PUT | `/api/settings/cloud-mail` | base_url, password |
| GET/PUT | `/api/settings/sms` | provider, api_key, service, country, operator |
| POST | `/api/settings/sms/balance` | 拉余额 |
| GET/PUT | `/api/settings/cpa` | url, key, enabled |
| GET | `/api/domains` | 列表 |
| POST | `/api/domains` | `{domain}` 加一条 |
| PATCH | `/api/domains/{id}` | 改 enabled |
| DELETE | `/api/domains/{id}` | 删 |
| GET | `/api/freegen/strategy` | 拉域名选择策略选项(round-robin / random / lowest-fail) |
| POST | `/api/freegen/start` | `{count, domain?}` — domain 不传则按策略自动选 |
| POST | `/api/freegen/stop` | 中断当前 batch |
| GET | `/api/freegen/status` | 轮询兜底 |
| GET | `/api/sse/task/{task_id}` | SSE 推 stage / event |
| GET | `/api/accounts` | 列表 + 筛选 + 分页 |
| GET | `/api/accounts/{email}/auth.json` | 下载 |
| GET | `/api/accounts/export.zip` | 打包下载 |
| GET | `/api/pending` | pending 列表 |
| POST | `/api/pending/{email}/manual-import` | 上传外部拿到的 JSON |
| DELETE | `/api/pending/{email}` | 放弃 |

所有 `/api/*`(除 `/api/auth/login`)需要 cookie session。

## 6. 注册流程(继承自 freegen,改两处)

```
1. identity.gen_email → 从 enabled domain 池按策略选 1 个域名(默认轮询)
                        + 随机 username + 随机 password
2. register.cmd_register (Playwright):
     - 打开 chatgpt.com signup
     - 填 email + password
     - 收 cloud-mail OTP → 提交
     - 填 first/last name + birthday
     - 5sim/herosms 申请号 → 填 phone → 收 OTP → 提交
3. oauth.fetch_personal_bundle:
     - PKCE + state
     - GET /oauth/authorize?prompt=login&codex_cli_simplified_flow=true&id_token_add_organizations=true&...
     - _login_form_walk:走 /log-in 表单 (email + password + 可能邮件 OTP)
     - 走 consent → /callback → 拿 code
     - POST /oauth/token → access_token + refresh_token + id_token
4. storage:
     - 写 output/auth/<email>.json (CPA-importable 格式,**始终**)
     - INSERT Account
5. 如果 cpa.enabled:
     - cpa_sync.upload_to_cpa(bundle)
     - 失败:写 cpa_error 到 Account,不阻塞
6. 失败:
     - INSERT PendingAccount + write output/pending.jsonl
     - SSE 推失败事件
SSE 实时推 stage 事件给前端
```

**和 freegen 当前实现的差异:**
- `oauth._build_auth_url` 已切 `prompt=login` 修复 `no_valid_organizations`(已在 AutoTeam-F 主分支验证)
- `storage` 从"只写文件"变"DB + 文件双写";读 pending 从 jsonl 改读 DB
- `config.get_sms_config` 从读 autoteam runtime_config 改读自己的 Setting 表
- `cpa_push` 从调 autoteam.cpa_sync 改调内联的 autofree.core.cpa_sync
- 域名选择从"传 domain 入参"扩展为"传或不传 — 不传按策略选"

## 7. 域名选择策略(支持多域名核心)

- **round-robin**(默认):按 `last_used_at` 升序选最久没用的 enabled 域名
- **random**:enabled 域名里随机选
- **lowest-fail**:按 `fail_count / (success_count + fail_count)` 升序

策略名存 `Setting.key = "freegen.domain_strategy"`,默认 `round-robin`。

`POST /api/freegen/start` 入参 `domain` 可选:
- 传了 = 强制用该域名
- 不传 = 按策略选

## 8. 认证模型

**首次启动 bootstrap:**
1. 读 `APP_PASSWORD` env(必填,不配置启动报错)
2. 检查 User 表:无行 → bcrypt(APP_PASSWORD) 写入(id=1)
3. 有行 → 跳过(用户已经在设置页改过密码,以 DB 为准)

**Session:**
- 登录成功签发随机 token(64 字节 hex)写入 `Session` 表(简化:也可只用 JWT 不存 DB)
- 设置 `httpOnly + Secure(prod) + SameSite=Lax` cookie,7 天有效
- 改密码 → 删该用户所有 Session → 强制重新登录

**FastAPI dependency:**
```python
async def require_auth(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get("autofree_session")
    user = await session_service.lookup(token, db)
    if not user:
        raise HTTPException(401)
    return user
```

## 9. SSE 实时进度

```
GET /api/sse/task/{task_id}
  → text/event-stream
  → 每个事件:
       event: stage
       data: {"stage": "account_started", "index": 1, "email": "..."}
  → 当 task 状态进入 finished/stopped/failed,服务端 send 一个 close event 然后关
  → 客户端 EventSource 重连机制处理网络断开
```

进度事件类型(沿用 freegen 现有):
- `started` — batch 开始 + batch_id
- `account_started` — `{index, email}`
- `account_done` — `{index, email, ok, error_kind?}`
- `phone_gate` — `{phone, provider}`
- `oauth_started` / `oauth_done`
- `cpa_synced` — `{ok, error?}`
- `finished` — `{ok, failed}`

## 10. UI 设计

### 视觉系统(苹果风)

```css
:root {
  --font-sans: -apple-system, BlinkMacSystemFont, "SF Pro Text",
               "Helvetica Neue", "PingFang SC", "Microsoft YaHei", sans-serif;
  --bg: #F5F5F7;
  --surface: #FFFFFF;
  --text-primary: #1D1D1F;
  --text-secondary: #6E6E73;
  --accent: #007AFF;
  --danger: #FF3B30;
  --success: #34C759;
  --warning: #FF9500;
  --border: rgba(0, 0, 0, 0.06);
  --shadow-sm: 0 1px 3px rgba(0,0,0,.04);
  --shadow-md: 0 4px 24px rgba(0,0,0,.06);
}
```

字阶:36 / 22 / 17 / 13(对应 large title / title / body / caption)。
卡片圆角 16,按钮 12,输入框 10。
按钮 hover scale-98 + 100ms ease;卡片 hover 抬升阴影。
图标用 lucide-react(线性,与 SF Symbols 神似)。

### 页面布局

- **左 Sidebar 240px** + **右主内容** max-width 1100,两侧 32px padding
- Sidebar:Logo + 6 个菜单项(Dashboard / 注册批次 / 账号 / Pending / 设置 / 退出)
- 移动端:Sidebar 折叠成 hamburger

### 各页面重点

- **Login**:居中卡片,大标题 "AutoFree",密码输入,登录按钮
- **Dashboard**:4 张统计卡(总账号 / 今日新增 / pending / CPA 同步率)+ 最近 batch 列表
- **Batch**:顶部"新建批次"卡(域名选择 + 数量),下方"当前任务"卡(SVG 进度环 + 实时事件流 + 停止按钮),历史 batch 列表
- **Accounts**:表格 + 筛选(batch / 域名 / 同步状态)+ 分页 + 单个/全部下载按钮
- **Pending**:表格 + 每行"重试 OAuth" / "上传 JSON" / "删除"操作
- **Settings**:分组卡片(应用密码 / Cloud-Mail / SMS / CPA / 域名池),每组卡保存按钮独立

## 11. 部署

`Dockerfile` multi-stage:
```dockerfile
# Stage 1: 构建前端
FROM node:20-alpine AS web
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: Python runtime + Playwright
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libdbus-1-3 libxcb1 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install uv
WORKDIR /app
COPY backend/pyproject.toml ./
RUN uv sync --frozen
COPY backend/ ./
COPY --from=web /web/dist ./static
RUN python -m playwright install chromium
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "autofree.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

`docker-compose.yml`:
```yaml
services:
  app:
    build:
      context: .
      dockerfile: docker/Dockerfile
    ports: ["8000:8000"]
    volumes:
      - ./data:/app/data         # SQLite + 输出 JSON + screenshots
    env_file: .env
    restart: unless-stopped
```

可选 postgres 通过 `DATABASE_URL=postgresql+psycopg://...` 切换,compose 加 service 即可。

## 12. 环境变量(`.env.example`)

```bash
# 必填
APP_PASSWORD=changeme           # 首次启动 bootstrap 用,之后可在设置页改

# 可选 — 数据库 (默认 sqlite)
# DATABASE_URL=sqlite:////app/data/autofree.db
# DATABASE_URL=postgresql+psycopg://user:pass@host:5432/db
# DATABASE_URL=mysql+pymysql://user:pass@host:3306/db

# 可选 — Session
SESSION_SECRET=                 # 不填则启动时随机生成 + 持久化到 .session_secret

# 可选 — 输出目录
DATA_DIR=/app/data              # SQLite + auth 输出 + screenshots 都在这下面

# 可选 — Playwright headless
PLAYWRIGHT_HEADLESS=true
```

业务配置(cloud-mail / SMS / CPA / 域名池)**全部在 web 设置页配**,不进 .env。

## 13. 测试

- `tests/test_db_models.py` — ORM 基础
- `tests/test_auth.py` — bootstrap + login + change-password
- `tests/test_settings_api.py` — settings CRUD
- `tests/test_domain_strategy.py` — 选择策略
- `tests/test_oauth.py` — 复用 freegen 现有 OAuth 单元测(若有)
- E2E 注册流程不写自动化(需要外部 SMS / cloud-mail / OpenAI)

## 14. 迁移路径(out of scope but 留个钩)

未来若需从 AutoTeam-F 导入历史 freegen 账号:
- 写 `scripts/import_from_autoteam.py` 读旧 `freegen_output/auth/*.json` + `accounts.txt` + `pending_accounts.jsonl`,INSERT 到 AutoFree DB
- 一次性命令,不进 web UI
