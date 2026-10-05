# AI掘金头条

一个 **FastAPI + Vue3 全栈新闻资讯应用**：后端每天自动从 15 个新闻源抓取新闻、按 9 个分类各入库 10 条；前端提供分类浏览、新闻详情、相关推荐、收藏、浏览历史与 AI 问答。

---

## 目录

- [功能特性](#功能特性)
- [技术栈](#技术栈)
- [目录结构](#目录结构)
- [快速开始](#快速开始)
- [接口一览](#接口一览)
- [每日自动采集](#每日自动采集)
- [配置速查](#配置速查)
- [常见问题](#常见问题)
- [说明与免责](#说明与免责)
- [更多文档](#更多文档)

---

## 功能特性

**后端**

- 用户体系：注册 / 登录 / 查询信息 / 修改资料 / 修改密码，密码 bcrypt 加密，登录后签发 UUID token
- 新闻模块：分类列表、分页新闻列表、新闻详情（浏览量自增 + 同类相关推荐）
- 收藏与浏览历史：两套结构对称的 CRUD，均支持分页查询
- AI 问答：`/api/ai/chat` 由后端转发大模型，**密钥只留在服务端**，浏览器拿不到
- Redis 旁路缓存：新闻分类/列表/详情走缓存，Redis 挂了自动降级直连数据库
- 统一响应格式：`{"code": 200, "message": "...", "data": ...}`，全局异常处理器兜底
- 自动采集流水线：多源抓取 → 关键词分类 → 补原文配图 → 先清后插 → 清理缓存

**前端**

- Vue3 + Vite + Pinia + Vant，适配移动端
- 首页 / 分类 / 详情 / 收藏 / 历史 / AI 问答 / 我的 / 登录注册 / 设置
- Pinia 持久化（`pinia-plugin-persistedstate`），中英文双语（vue-i18n）
- 详情页支持相关推荐切换（监听路由参数变化重新拉取，而非依赖组件重建）

---

## 技术栈

| 层 | 技术 |
| --- | --- |
| Web 框架 | FastAPI 0.141 + Uvicorn 0.52 |
| ORM / 驱动 | SQLAlchemy 2.0（异步）+ aiomysql |
| 数据库 | MySQL 8（库名 `news_app`） |
| 缓存 | Redis（本地 `localhost:6379`，可选） |
| 数据校验 | Pydantic v2 |
| 认证 | passlib + bcrypt + UUID token |
| 定时任务 | APScheduler（进程内）+ Windows 任务计划程序（系统级） |
| 前端 | Vue 3 + Vite 7 + Pinia 3 + Vant 4 + Axios + vue-router 4 |

---

## 目录结构

```
AI掘金头条/
├── toutiao_backend/                 # 后端（FastAPI）
│   ├── main.py                      # 应用入口：挂 CORS、注册异常处理、挂载 5 个路由、启动定时器
│   ├── requirements.txt             # 依赖清单（版本已按实测锁定）
│   ├── .env.example                 # 环境变量模板（复制为 .env 后填值）
│   ├── start-all.bat                # Windows 一键启动（后端 + 前端 + 打开浏览器）
│   ├── routers/                     # 路由层：只做参数校验与组装响应
│   │   ├── news.py  users.py  favorite.py  history.py  ai.py
│   ├── crud/                        # 数据访问层：所有 SQL 都写在这里
│   │   ├── news.py  users.py  favorite.py  history.py  news_cache.py
│   ├── models/                      # ORM 模型：user / user_token / news_category / news / favorite / history
│   ├── schemas/                     # Pydantic 契约：入参校验 + 出参序列化（不含 password）
│   ├── services/                    # 业务服务
│   │   ├── news_fetcher.py          # 采集流水线主流程 fetch_and_save()
│   │   ├── rss_source.py            # RSS 源抓取
│   │   ├── pp_source.py             # 澎湃新闻频道抓取
│   │   ├── sina_source.py           # 新浪滚动 JSON 源抓取
│   │   ├── excerpt.py               # 原文摘录（不调大模型时的保底方案）
│   │   ├── llm_client.py            # 大模型客户端（OpenAI 兼容协议）
│   │   └── scheduler.py             # APScheduler 每日定时器
│   ├── scripts/scheduled_fetch.py   # 给「Windows 任务计划程序」调用的采集入口
│   ├── cache/news_cache.py          # 缓存策略：拼 key + 过期时间
│   ├── config/                      # db_conf（数据库）/ cache_conf（Redis）/ fetcher_conf（采集）/ llm_conf（大模型）
│   ├── utils/                       # security（bcrypt）/ auth（token 依赖）/ response（统一响应）/ exception*
│   └── docs/项目架构说明文档.html    # 单文件架构说明（分层图、请求链路、问题清单）
└── xwzx-news/                       # 前端（Vue3 + Vite）
    ├── src/views/                   # 页面：Home / Category / NewsDetail / Favorite / History / AIChat / My / Login / Register / Profile / Settings
    ├── src/components/              # NewsItem / TabBar
    ├── src/store/                   # Pinia：user / news / favorite / history / language / theme
    ├── src/router/index.js          # 路由表
    ├── src/config/api.js            # 后端地址 http://127.0.0.1:8000（AI 问答已改为走后端转发）
    └── vite.config.js
```

---

## 快速开始

### 环境要求

| 依赖 | 版本 | 说明 |
| --- | --- | --- |
| Python | 3.11+ | 建议 3.12 / 3.13 |
| Node.js | 18+ | 建议 20+ |
| MySQL | 8.x | 需要提前建库 |
| Redis | 任意 | **可选**，不装也能跑（自动降级） |

### 1. 准备数据库

先创建数据库（库名默认 `news_app`）：

```sql
CREATE DATABASE news_app DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
```

代码会用到的 6 张表：`user`、`user_token`、`news_category`、`news`、`favorite`、`history`。

> ⚠️ **本仓库未包含建表 SQL**。请自行准备这 6 张表的 DDL，并往 `news_category` 里插入 9 个分类（`头条 / 社会 / 国内 / 国际 / 娱乐 / 体育 / 科技 / 财经 / 人工智能`，字段 `id / name / sort_order`）。`news` 表可以为空 —— 启动后跑一次采集就会自动填充。

### 2. 配置环境变量（必需）

```bash
cd toutiao_backend
cp .env.example .env      # Windows: copy .env.example .env
```

编辑 `.env`，至少填上数据库连接串：

```ini
DATABASE_URL=mysql+aiomysql://root:你的密码@localhost:3306/news_app?charset=utf8mb4
```

大模型相关字段**可以留空**：留空时采集走「原文摘录」模式（一字不改地摘原文标题和正文，不调用大模型，零编造风险）。想启用 AI 改写，填上 `LLM_API_KEY` 并打开 `config/fetcher_conf.py` 里的 `USE_LLM_REWRITE`。

> 源码中不包含任何密码或密钥；`DATABASE_URL` 未配置时，后端会**直接启动失败并提示**，不会静默连到错误的库。

### 3. 启动后端

```bash
cd toutiao_backend

# 创建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\activate                     # macOS / Linux: source .venv/bin/activate
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

# 启动（默认 http://127.0.0.1:8000）
.venv\Scripts\python.exe -u -m uvicorn main:app --host 127.0.0.1 --port 8000
```

访问 <http://127.0.0.1:8000/docs> 可看到自动生成的 Swagger 交互文档。

> 注意：这条命令**没有 `--reload`**，改后端代码需要手动重启。开发时可自行加上 `--reload`。

### 4. 启动前端

```bash
cd xwzx-news
npm install
npm run dev          # http://127.0.0.1:5173
```

前端在后端地址上写死了 `http://127.0.0.1:8000`，见 `src/config/api.js`；改了后端端口要同步修改这里。

### 5. Windows 一键启动（可选）

双击 `toutiao_backend\start-all.bat`：会依次检查虚拟环境与前端依赖 → 拉起后端（8000）与前端（5173）各一个窗口 → 等约 8 秒 → 打开浏览器。端口已被占用时会自动跳过对应服务。

```bat
start-all.bat                :: 启动并打开浏览器
start-all.bat --no-browser   :: 只启动，不开浏览器
```

---

## 接口一览

统一响应体：`{ "code": 200, "message": "ok", "data": ... }`；需要登录的接口通过请求头 `Authorization: <token>` 传 UUID token。

| 方法 | 路径 | 说明 | 需登录 |
| --- | --- | --- | --- |
| GET | `/` | 健康检查 | 否 |
| POST | `/api/users/register` | 注册 | 否 |
| POST | `/api/users/login` | 登录，返回 token | 否 |
| GET | `/api/users/info` | 获取当前用户信息 | 是 |
| PUT | `/api/users/update` | 修改资料（昵称/头像等） | 是 |
| PUT | `/api/users/password` | 修改密码 | 是 |
| GET | `/api/news/categories` | 新闻分类列表 | 否 |
| GET | `/api/news/list` | 分页新闻列表（可按分类筛选） | 否 |
| GET | `/api/news/detail` | 新闻详情（浏览量 +1，返回相关推荐） | 否 |
| POST | `/api/news/fetch` | 手动触发一轮采集（后台任务 + 运行锁） | 否 |
| POST | `/api/favorite/add` | 添加收藏 | 是 |
| DELETE | `/api/favorite/remove` | 取消收藏 | 是 |
| GET | `/api/favorite/check` | 查询某新闻是否已收藏 | 是 |
| GET | `/api/favorite/list` | 收藏列表（分页） | 是 |
| DELETE | `/api/favorite/clear` | 清空收藏 | 是 |
| POST | `/api/history/add` | 写入浏览历史 | 是 |
| GET | `/api/history/list` | 浏览历史列表（分页） | 是 |
| DELETE | `/api/history/delete/{history_id}` | 删除单条历史 | 是 |
| DELETE | `/api/history/clear` | 清空历史 | 是 |
| POST | `/api/ai/chat` | AI 问答（后端转发大模型） | 否 |

---

## 每日自动采集

采集逻辑全部在 `services/news_fetcher.py` 的 `fetch_and_save()` 里，由**两个入口**触发：

| 入口 | 位置 | 触发时间 | 特点 |
| --- | --- | --- | --- |
| APScheduler | `services/scheduler.py`，寄在 uvicorn 进程内 | 每天 **08:00**（`FETCH_HOUR` / `FETCH_MINUTE`） | 服务没启动就错过，不补跑 |
| Windows 任务计划 | `scripts/scheduled_fetch.py`，系统级 | 每天 **08:30**，勾选「错过则尽快启动」 | 关机后开机自动补跑 |

两个入口共用同一个数据目录与「间隔判断」：采集前查 `MAX(news.created_at)`，**距上次不足 6 小时就跳过**，因此同一天不会重复采两轮。

**流水线五个阶段**

1. **多源抓取** —— RSS 源（36氪、极客公园、雷锋网、IT之家、快科技、华尔街见闻、中新网各频道）+ 澎湃新闻频道 + 新浪滚动 JSON 源，共 15 个来源
2. **关键词分类** —— 按 `CATEGORY_RULES` 把候选新闻分发到 9 个分类，每类最多留 10 条（`PER_CATEGORY_LIMIT`）；优先只取当天（24 小时），不够时放宽到 48 小时
3. **补原文配图** —— 缺图的候选去原文页抓首图，全部失败才用占位图兜底（`REQUIRE_ORIGINAL_IMAGE`）
4. **先清后插** —— 每个分类内先按条目去重、再筛掉不合格内容，然后删旧插新（`REPLACE_OLD_NEWS`），保证库里每类只有当天的最新 10 条
5. **清缓存** —— 删除相关 Redis 缓存键，避免前端继续读到旧列表

**手动采集**

```bash
# 方式一：接口（需要后端在跑）
curl -X POST http://127.0.0.1:8000/api/news/fetch

# 方式二：命令行（不依赖任何进程）
.venv\Scripts\python.exe scripts\scheduled_fetch.py            # 正常跑，间隔不足会自动跳过
.venv\Scripts\python.exe scripts\scheduled_fetch.py --force    # 忽略间隔，强制采一轮
.venv\Scripts\python.exe scripts\scheduled_fetch.py --status   # 只看「上次采于何时」
```

---

## 配置速查

`config/fetcher_conf.py`

| 配置项 | 默认值 | 含义 |
| --- | --- | --- |
| `FETCH_HOUR` / `FETCH_MINUTE` | `8` / `0` | 每日采集时间（东八区） |
| `RUN_ON_STARTUP` | `False` | 服务启动时是否立即采一轮 |
| `PER_CATEGORY_LIMIT` | `10` | 每个分类最多入库条数 |
| `FRESH_HOURS` / `FRESH_HOURS_LENIENT` | `24` / `48` | 只取当天的严格/兜底时间窗 |
| `REPLACE_OLD_NEWS` | `True` | 分类内「先清后插」 |
| `USE_LLM_REWRITE` | `False` | 关闭 = 原文摘录（零编造）；开启 = 大模型改写，需配 `LLM_API_KEY` |
| `REQUIRE_ORIGINAL_IMAGE` | `True` | 必须有原文配图才入库 |
| `REQUEST_TIMEOUT` | `15` | 单源抓取超时（秒） |

`.env`（大模型，OpenAI 兼容协议，换厂商只改这两项）

| 厂商 | `LLM_BASE_URL` | `LLM_MODEL` |
| --- | --- | --- |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 阿里通义百炼 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| 月之暗面 Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 硅基流动 | `https://api.siliconflow.cn/v1` | `Qwen/Qwen2.5-7B-Instruct` |

---

## 常见问题

**Q：启动直接报「环境变量 DATABASE_URL 未设置」？**
`.env` 没建或没填。复制 `.env.example` 为 `.env` 并填写 `DATABASE_URL`。

**Q：前端页面能开，但新闻一片空白？**
按顺序排查：① 后端是否在 8000 端口运行（访问 `/docs`）；② `.env` 的库名/密码是否正确；③ `news` 表是不是空的 —— 空表调一次 `POST /api/news/fetch` 即可。

**Q：接口第一次响应特别慢（约 5 秒）？**
没装 Redis 时，缓存操作会等连接超时后自动降级为直连数据库，属预期行为。装了 Redis（`localhost:6379`）就不会。

**Q：改完后端代码不生效？**
启动命令里没有 `--reload`，需要手动重启后端进程。

**Q：采集到的娱乐分类内容看着不像娱乐？**
分类靠关键词规则匹配，边界题材（文化、社会新闻）会误判，属已知的规则局限。

---

## 说明与免责

- 新闻内容、标题与配图的**版权归原媒体所有**（中新网、新浪、澎湃新闻、36氪、极客公园、雷锋网、IT之家、快科技、华尔街见闻、环球网等），本项目为个人非商业项目，仅用于演示与技术交流，不做任何商业用途。
- 采集遵循「原文摘录」优先的策略：默认不调用大模型、不改写原文，只摘取标题与正文片段并保留来源标注。
- 请自行遵守目标站点的 `robots.txt` 与访问频率限制；`fetcher_conf.py` 中已对易封禁的来源设置抓取间隔与连续失败熔断。

**已知问题**

- `main.py` 的 CORS 使用 `allow_origins=["*"]` 且同时开启 `allow_credentials`，生产环境应改为白名单域名
- `history.py` 的删除接口把路径参数 `history_id` 当 `news_id` 使用，删除逻辑需要修正
- 重复收藏会撞唯一索引抛错，被全局异常处理器误报为「用户名已存在」，应在业务层用 `is_news_favorite()` 先行拦截
- 四个 `models/*.py` 各自定义了独立的 `Base`（4 套 metadata），后期应统一到单一 Base
- `crud/news.py` 与 `crud/news_cache.py` 存在重复代码，且两者的 `get_related_news()` 返回结构不一致

---

## 更多文档

`toutiao_backend/docs/项目架构说明文档.html`（浏览器直接打开）包含：分层架构图、文件总览、4 条完整请求链路、表关系图与问题清单。
