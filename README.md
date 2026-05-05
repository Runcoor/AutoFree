# AutoFree

批量自动注册 OpenAI free 账号的独立 web 应用。FastAPI 后端 + React 前端。

**功能**
- 浏览器面板:配置 → 一键启动批量注册 → 实时进度 → 账号库下载
- cloud-mail 多域名池(平铺、可启用/禁用、轮询)
- SMS provider 可切(5sim / hero-sms)
- 注册成功:**始终**生成本地 CPA-importable JSON;CPA 配了则**额外**自动 push
- 单密码全站锁(`.env` 引导,设置页可改)
- 默认 SQLite,支持切 PostgreSQL / MySQL

## 快速开始

### Docker(生产)

```bash
git clone <this-repo> AutoFree
cd AutoFree
cp .env.example .env
# 改 .env 把 APP_PASSWORD 设成你的密码
docker compose up -d --build
# 访问 http://localhost:8000
```

首次登录用 `.env` 里的 `APP_PASSWORD`,登录后到「设置」页配置 cloud-mail / SMS / CPA / 域名。

### 本地开发

**后端**:
```bash
cd backend
uv sync
APP_PASSWORD=dev DATA_DIR=./data uv run autofree run --reload
# http://127.0.0.1:8000/docs (Swagger)
```

**前端**(单独跑 dev server,自动 proxy 到 8000):
```bash
cd frontend
npm install
npm run dev
# http://localhost:5173
```

## 架构

```
React SPA (Apple 风) ── HTTP + SSE ──► FastAPI ──► autofree.core ──► SQLite/PG/MySQL
                                          │
                                          └──► CPA push (可选 webhook)
```
配置(cloud-mail / SMS / CPA / 域名池)全部走 DB Setting 表 + web 设置页;`.env` 只管系统级(数据库 URL、应用密码、数据目录)。

## 项目结构

```
AutoFree/
├── backend/            FastAPI + SQLAlchemy + Alembic + autofree.core
├── frontend/           React 19 + Vite + TS + Tailwind
├── docker/             Dockerfile (multi-stage: node build → python runtime)
├── docker-compose.yml
└── .env.example
```

## 数据库迁移

切到非 sqlite 时:

1. 改 `.env` 的 `DATABASE_URL`(注释掉 docker-compose 里 db service 的注释行)
2. 重启 `docker compose up -d --build`,首启自动跑 `alembic upgrade head` 建表
3. 数据迁移自己写脚本(目前不在 v1 范围)

