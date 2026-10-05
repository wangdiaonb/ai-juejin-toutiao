# ============================================================
# AI 问答的数据契约（Pydantic）
# ------------------------------------------------------------
# 前端 POST 上来的对话历史用什么格式、字段有什么限制，都在这里定义。
# FastAPI 会自动拿它做校验：字段缺失、类型不对、超长都会直接返回 422，
# 不用在路由里手写一堆 if 判断。
# ============================================================

from typing import List, Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    """一条对话消息。"""

    # Literal 限定取值范围：只能填这三个角色，填别的直接被校验拦下
    role: Literal["system", "user", "assistant"] = Field(..., description="消息角色")

    # min_length 防止空消息；max_length 防止有人塞一篇小说进来刷 token
    content: str = Field(..., min_length=1, max_length=8000, description="消息内容")


class AIChatRequest(BaseModel):
    """
    AI 问答请求体。

    【设计要点】这里刻意 **不接收** model 和 api_key 字段：
      - 模型用哪个，由后端 config/llm_conf.py 决定（换模型不用改前端）
      - API Key 只存在服务器的 .env 里
    这样即使有人在浏览器里伪造请求，也没法指定模型或拿到密钥。
    """

    # max_length=50：最多带 50 条上下文，防止对话历史无限增长把 token 撑爆
    messages: List[ChatMessage] = Field(
        ..., min_length=1, max_length=50, description="对话历史（最后一条是本次用户提问）"
    )
