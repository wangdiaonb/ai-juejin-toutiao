# ============================================================
# 身份认证工具：根据请求里的 token 查出当前登录用户
# ------------------------------------------------------------
# 这是一个「FastAPI 依赖函数」，供需要登录才能访问的接口使用。
#
# 用法（在路由里写一行即可完成身份认证）：
#   async def 某个接口(user: User = Depends(get_current_user)):
#       # 走到这里说明 token 有效，user 就是当前登录用户
#
# 它做了什么：
#   1. 从 HTTP 请求头 Authorization 里取出 token（形如 "Bearer xxxxx"）
#   2. 去掉 "Bearer " 前缀，拿到纯 token 字符串
#   3. 调 crud 层按 token 查用户
#   4. 查不到或过期 → 抛 401；查到 → 返回 User 对象注入到接口参数里
# ============================================================

from fastapi import Header, Depends, HTTPException
#   Header   —— 从 HTTP 请求头里取值（这里是取 Authorization 请求头）
#   Depends  —— 依赖注入工具
#   HTTPException —— 认证失败时抛 401

from sqlalchemy.ext.asyncio import AsyncSession
from config.db_conf import get_db     # 数据库会话依赖
from crud import users                # 用 crud 层的 get_user_by_token 查用户
from starlette import status          # 状态码常量（status.HTTP_401_UNAUTHORIZED）


async def get_current_user(authorization: str = Header(..., alias="Authorization"),
                           db: AsyncSession = Depends(get_db)
):
    # 参数说明：
    #   authorization —— 从请求头 "Authorization" 取值。
    #       Header(..., alias="Authorization") 里的 ... 表示"必填"，缺了这个请求头直接 422 报错
    #   db —— 数据库会话，由 Depends(get_db) 注入
    #
    # token 形如 "Bearer abc123"，这里把前缀 "Bearer " 替换成空串，得到纯 token "abc123"
    token = authorization.replace("Bearer ", "")

    # 调 crud 层按 token 查用户：token 有效且未过期会返回 User，否则返回 None
    user = await users.get_user_by_token(db, token)
    if not user:
        # token 无效或已过期 → 抛 401，接口函数体不会被执行
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的令牌或已经过期的令牌")

    # 认证通过，返回 User 对象；FastAPI 会把它注入到路由的 user 参数里
    return user
