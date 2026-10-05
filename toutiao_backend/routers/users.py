# ============================================================
# 路由层（Router）：用户相关的 HTTP 接口
# ------------------------------------------------------------
# 职责：接收前端请求 → 调用 CRUD 层处理 → 用统一格式 success_response 返回。
# 这里每个 @router.xxx 装饰的函数，都对应一个前端能访问的 URL 接口。
#
# 调用链（数据怎么流动）：
#   前端请求 → routers/users.py（接请求、拼响应）
#             ├─ 查库逻辑        → crud/users.py
#             ├─ 请求/响应模型    → schemas/users.py
#             ├─ 数据库会话依赖   → config/db_conf.py 的 get_db
#             └─ 统一返回格式     → utils/response.py 的 success_response
#
# 本文件定义的接口（前缀统一是 /api/users）：
#   POST /api/users/register   注册
#   POST /api/users/login      登录
#   GET  /api/users/info       获取当前登录用户信息
#   PUT  /api/users/update     更新用户资料
#   PUT  /api/users/password   修改密码
# ============================================================

# FastAPI 的路由 / 依赖注入 / 异常工具
from fastapi import APIRouter, Depends, HTTPException
#   APIRouter —— 路由分组工具：把同一类接口拆到单独文件
#   Depends   —— 依赖注入：Depends(xxx) 让 FastAPI 自动调用 xxx 并把结果注入参数
#   HTTPException —— 主动抛 HTTP 错误（如 400/401）

from starlette import status                  # HTTP 状态码常量（status.HTTP_400_BAD_REQUEST 等，比手写数字更可读）

# 数据库异步会话类型 + 会话依赖：Depends(get_db) 会给每个请求注入一个可用的 db 会话
from sqlalchemy.ext.asyncio import AsyncSession
from config.db_conf import get_db
from models.users import User                 # User ORM 模型（用作依赖注入的参数类型注解）

# 请求体/响应模型：校验入参、规定返回的 JSON 结构
from schemas.users import UserRequest, UserAuthResponse, UserInfoResponse, UserUpdateRequest, UserChangePasswordRequest

# 业务数据操作（查用户 / 建用户 / 发 token / 登录校验 / 更新 / 改密）
from crud import users
from utils.auth import get_current_user        # 身份认证依赖：从请求头解析 token 并查出当前用户
# 统一成功响应包装：{code, message, data}
from utils.response import success_response


# 创建本模块的路由组：
#   prefix="/api/users" —— 下面所有接口的 URL 自动拼上这个前缀
#   tags=["users"]       —— 在 /docs 文档里归类到 "users" 分组
router = APIRouter(prefix="/api/users", tags=["users"])


# ------------------------------------------------------------
# 接口 1：用户注册   POST /api/users/register
# 流程：查重 → 建用户 → 签发 token → 返回 {token, userInfo}
# ------------------------------------------------------------
# @router.post 表示：用 HTTP POST 方法访问 /api/users/register 时，会调用下面这个函数
@router.post("/register")
# user_data 是请求体（前端 POST 的 JSON 会自动校验成 UserRequest）
# db 是数据库会话，由 Depends(get_db) 注入；请求结束后会自动关闭
async def register(user_data: UserRequest, db: AsyncSession = Depends(get_db)):
    # 1) 先按用户名查一次库：账号已存在就直接抛 400，不再往下执行
    existing_user = await users.get_user_by_username(db, user_data.username)
    if existing_user:
        # raise HTTPException 会让 FastAPI 返回 400，detail 是给前端看的提示语
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="用户已存在")

    # 2) 用户名没被占用 → 创建用户（密码在 CRUD 的 create_user 里已做 bcrypt 加密）
    user = await users.create_user(db, user_data)

    # 3) 为新用户签发 token 写进 user_token 表（注册成功即相当于自动登录）
    token = await users.create_token(db, user.id)

    # 4) 用响应模型把「token + 用户信息」组装成标准结构，再套上统一返回格式
    response_data = UserAuthResponse(token=token, userInfo=UserInfoResponse.model_validate(user))
    # success_response 会包成 {code:200, message:"注册成功", data:{token, userInfo}} 返回
    return success_response(message="注册成功", data=response_data)


# ------------------------------------------------------------
# 接口 2：用户登录   POST /api/users/login
# 流程：校验账号密码 → 通过则签发 token → 返回 {token, userInfo}
# ------------------------------------------------------------
@router.post("/login")
async def login(user_data: UserRequest, db: AsyncSession = Depends(get_db)):
    # 1) 在 CRUD 层校验「用户名存在 + 密码正确」；任一不通过返回 None
    user = await users.authenticate_user(db, user_data.username, user_data.password)
    if not user:
        # 校验失败 → 抛 401（未授权）
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")

    # 2) 登录成功后签发 token（已有 token 就刷新，没有就新增）
    token = await users.create_token(db, user.id)

    # 3) 组装并返回与注册完全相同的 {code, message, data:{token, userInfo}}
    response_data = UserAuthResponse(token=token, userInfo=UserInfoResponse.model_validate(user))
    return success_response(message="登录成功", data=response_data)


# ------------------------------------------------------------
# 接口 3：获取当前登录用户信息   GET /api/users/info
# 流程：从请求头解析 token → 查出用户 → 返回用户资料
# ------------------------------------------------------------
@router.get("/info")
# Depends(get_current_user) 是身份认证依赖：
#   它会从请求头 Authorization 里取 token、查出对应用户，把 User 对象注入到 user 参数；
#   token 无效/过期会自动抛 401，根本走不到函数体。
async def get_user_info(user: User = Depends(get_current_user)):
    # model_validate 把 ORM 的 user 对象转成 UserInfoResponse（不含 password 等敏感字段）
    return success_response(message="获取用户信息成功", data=UserInfoResponse.model_validate(user))


# ------------------------------------------------------------
# 接口 4：更新用户资料   PUT /api/users/update
# 流程：验证 token 拿到当前用户 → 更新用户提交的字段 → 返回更新后的资料
# ------------------------------------------------------------
@router.put("/update")
async def update_user_info(user_data: UserUpdateRequest, user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)):
    # 用「当前登录用户」的用户名定位要改的人（而不是前端随便传个用户名，避免改到别人）
    # user.username 来自 Depends(get_current_user)，是认证通过后拿到的
    user = await users.update_user(db, user.username, user_data)
    # 返回更新后的用户信息
    return success_response(message="更新用户信息成功", data=UserInfoResponse.model_validate(user))


# ------------------------------------------------------------
# 接口 5：修改密码   PUT /api/users/password
# 流程：验证 token → 校验旧密码 → 加密新密码 → 更新
# ------------------------------------------------------------
@router.put("/password")
async def update_password(
        password_data: UserChangePasswordRequest,      # 请求体：{oldPassword, newPassword}
        user: User = Depends(get_current_user),        # 当前登录用户（从 token 解析出来）
        db: AsyncSession = Depends(get_db)):           # 数据库会话
    # 调 CRUD 层改密码：返回 True 表示成功；旧密码错误返回 False
    res_change_pwd = await users.change_password(db, user, password_data.old_password, password_data.new_password)
    if not res_change_pwd:
        # 旧密码错误 → 抛 401
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="旧密码错误，修改密码失败")
    # 成功 → 返回统一成功格式（这里不需要返回数据，所以 success_response 只传 message）
    return success_response(message="修改密码成功")
