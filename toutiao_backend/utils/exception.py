# ============================================================
# 全局异常处理器：把异常统一转成 {code, message, data} 结构的 JSON
# ------------------------------------------------------------
# 这些 handler 要配合 FastAPI 注册使用，例如：
#   app.add_exception_handler(HTTPException, http_exception_handler)
# 开发模式(DEBUG_MODE=True)会返回详细错误信息方便排查，
# 生产环境应把 DEBUG_MODE 设为 False，避免把内部细节暴露给用户。
# ============================================================

import traceback

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette import status

# 教学项目保持开启：True 时返回详细错误信息
DEBUG_MODE = True


# ------------------------------------------------------------
# 处理 HTTPException：业务逻辑主动抛出的异常(如"用户已存在")
# ------------------------------------------------------------
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.status_code,
            "message": exc.detail,
            "data": None,
        },
    )


# ------------------------------------------------------------
# 处理数据库完整性约束错误(唯一键冲突 / 外键失败等)
# ------------------------------------------------------------
async def integrity_error_handler(request: Request, exc: IntegrityError):
    error_msg = str(exc.orig)

    # 判断具体的约束错误类型，转成用户能看懂的话
    if "username_UNIQUE" in error_msg or "Duplicate entry" in error_msg:
        detail = "用户名已存在"
    elif "FOREIGN KEY" in error_msg:
        detail = "关联数据不存在"
    else:
        detail = "数据约束冲突，请检查输入"

    # 开发模式：额外返回详细错误信息
    error_data = None
    if DEBUG_MODE:
        error_data = {
            "error_type": "IntegrityError",
            "error_detail": error_msg,
            "path": str(request.url),
        }

    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "code": 400,
            "message": detail,
            "data": error_data,
        },
    )


# ------------------------------------------------------------
# 处理 SQLAlchemy 数据库错误(除约束外的通用错误)
# ------------------------------------------------------------
async def sqlalchemy_error_handler(request: Request, exc: SQLAlchemyError):
    # 开发模式：返回详细错误信息
    error_data = None
    if DEBUG_MODE:
        error_data = {
            "error_type": type(exc).__name__,
            "error_detail": str(exc),
            "traceback": traceback.format_exc(),
            "path": str(request.url),
        }

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "code": 500,
            "message": "数据库操作失败，请稍后重试",
            "data": error_data,
        },
    )


# ------------------------------------------------------------
# 处理所有未捕获的异常：兜底
# ------------------------------------------------------------
async def general_exception_handler(request: Request, exc: Exception):
    # 开发模式：返回详细错误信息
    error_data = None
    if DEBUG_MODE:
        error_data = {
            "error_type": type(exc).__name__,
            "error_detail": str(exc),
            # 把异常堆栈格式化成字符串，方便日志记录和排查
            "traceback": traceback.format_exc(),
            "path": str(request.url),
        }

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "code": 500,
            "message": "服务器内部错误",
            "data": error_data,
        },
    )
