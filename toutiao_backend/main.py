# ============================================================
# 应用入口：创建 FastAPI 应用实例，配置中间件，挂载路由
# ------------------------------------------------------------
# 这是整个后端程序的「启动文件」，运行这个文件就能启动 Web 服务。
# 它的职责：把各个模块(路由、中间件等)组装起来。
# ============================================================

import asyncio                          # 用于把首次采集丢到后台执行，不阻塞服务启动
from contextlib import asynccontextmanager   # 把「启动/关闭」逻辑写成一个上下文管理器

from fastapi import FastAPI
from routers import news,users,favorite,history,ai
from fastapi.middleware.cors import CORSMiddleware

from config.fetcher_conf import RUN_ON_STARTUP
from services.scheduler import run_fetch_job, start_scheduler, stop_scheduler
from utils.exception_handlers import register_exception_handlers


# ------------------------------------------------------------
# lifespan：应用「生命周期」钩子
# ------------------------------------------------------------
# FastAPI 在服务启动前会执行 yield 之前的代码，在服务关闭时执行 yield 之后的代码。
# 定时任务的启停就挂在这里：
#   启动时 start_scheduler()  → 注册并开启每日采集任务
#   关闭时 stop_scheduler()   → 优雅停掉调度器，避免残留线程
#
# 注意 yield 的位置：它把函数切成「启动段」和「关闭段」两半，
# 服务运行期间就停在 yield 这一行。
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---------- 启动 ----------
    start_scheduler()

    if RUN_ON_STARTUP:
        # create_task 把采集丢到后台跑：采集要抓网页+调大模型，可能几十秒，
        # 如果直接 await 会让服务迟迟起不来，所以不阻塞启动流程。
        asyncio.create_task(run_fetch_job())

    yield

    # ---------- 关闭 ----------
    stop_scheduler()


# ------------------------------------------------------------
# 创建 FastAPI 应用实例
# ------------------------------------------------------------
# app 是整个应用的核心对象，所有路由、中间件都要挂到它上面。
# 启动方式(命令行)：uvicorn main:app --reload
# lifespan=lifespan：把上面那个生命周期函数交给 FastAPI 托管
app = FastAPI(lifespan=lifespan)
register_exception_handlers(app)
# ------------------------------------------------------------
# 配置 CORS(跨域资源共享)中间件
# ------------------------------------------------------------
# 问题背景：前端通常跑在另一个地址/端口(比如 localhost:5173)，
# 浏览器出于安全会「阻止」它请求后端(这叫跨域限制)。
# CORS 中间件就是告诉浏览器：允许这些来源访问后端。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],    # 允许的源。开发时填 "*" 表示允许所有来源；生产环境要改成具体域名
    allow_credentials=True, # 是否允许携带 cookie 等凭证
    allow_methods=["*"],    # 允许的 HTTP 方法(GET/POST/PUT/DELETE...)
    allow_headers=["*"],    # 允许的请求头
)

# ------------------------------------------------------------
# 根路径接口(用于快速验证服务是否启动成功)
# ------------------------------------------------------------
@app.get("/")
async def root():
    return {"message": "Hello World"}

# ------------------------------------------------------------
# 挂载 news 路由
# ------------------------------------------------------------
# include_router 把 routers/news.py 里定义的所有接口注册到 app 上。
# 由于 news.router 自带 prefix="/api/news"，最终路径形如 /api/news/list
app.include_router(news.router)
app.include_router(users.router)
app.include_router(favorite.router)
app.include_router(history.router)
# AI 问答路由：转发给大模型，密钥保存在后端 .env，不暴露给浏览器
app.include_router(ai.router)