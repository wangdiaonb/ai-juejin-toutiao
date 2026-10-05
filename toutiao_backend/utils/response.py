# ============================================================
# 统一响应工具
# ------------------------------------------------------------
# 让所有接口返回同一种 JSON 结构：{code, message, data}
# 供各 routers/*.py 调用，例如：
#   return success_response(message="登录成功", data=response_data)
# 前端约定：只用 code === 200 判断接口是否成功。
# ============================================================

from fastapi.encoders import jsonable_encoder  # 把 Pydantic / ORM 对象递归转成可 JSON 化的普通类型
from fastapi.responses import JSONResponse      # 能直接作为 FastAPI 返回值的响应对象


# 把业务数据包进统一结构，转成 JSONResponse 返回
def success_response(message: str = "success", data=None):
    content = {
        "code": 200,        # 业务码：前端用 code === 200 判断成功
        "message": message, # 给用户看的提示语
        "data": data        # 具体业务数据(token、用户信息等)，可为 None
    }

    # 目标：把 content 里混入的 FastAPI / Pydantic / ORM 对象统一编码成普通类型，
    # 再包成 JSONResponse，避免直接返回对象时报序列化错误
    return JSONResponse(content=jsonable_encoder(content))
