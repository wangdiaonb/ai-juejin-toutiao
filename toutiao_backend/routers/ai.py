# ============================================================
# 路由层：AI 问答（后端转发给大模型）
# ------------------------------------------------------------
# 【为什么要做这个转发接口？】
# 前端原来的写法是「浏览器直连大模型」，这意味着 API Key 必须写在
# 前端代码里。而前端代码最终会打包成 JS 文件发给每一个访问者，
# 任何人按 F12 就能看到密钥并盗用刷额度——这是很典型的前端泄密。
#
# 改成后端转发之后：
#   浏览器 → 本项目后端（持有密钥）→ 大模型
# 密钥只存在服务器的 .env 里，浏览器完全接触不到。
#
# 顺带还获得三个好处：
#   1. 换模型 / 换厂商只改后端 .env，前端一行不用动
#   2. 可以在这里加限流、鉴权、用量统计
#   3. 可以统一注入系统提示词，控制助手的人设和边界
# ============================================================

import json

import requests
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from config.llm_conf import (
    LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_TEMPERATURE, is_llm_enabled,
)
from schemas.ai import AIChatRequest

router = APIRouter(prefix="/api/ai", tags=["ai"])

# 系统提示词：定义助手的身份和回答风格。
# 放在后端而不是前端，好处是前端改不了（防止被人用参数覆盖成"万能越狱助手"）。
SYSTEM_PROMPT = (
    "你是「AI掘金头条」新闻客户端的智能助手，擅长解答科技与人工智能领域的问题。"
    "回答使用简体中文，简洁准确、条理清晰，避免冗长。"
)


def _sse_stream(payload: dict):
    """
    同步生成器：一边从大模型读流，一边把数据块 yield 给前端。

    为什么这里可以用同步的 requests 而不担心卡住事件循环？
      因为 FastAPI 的 StreamingResponse 遇到「同步生成器」时，
      会自动把它丢到线程池里迭代（iterate_in_threadpool），
      所以阻塞的是线程池里的工作线程，不会堵住 asyncio 的事件循环。

    yield 出去的格式是 SSE（Server-Sent Events）：
      每行以 "data: " 开头，行尾用两个换行表示一条事件结束。
      大模型返回什么我们基本原样透传，前端按同样的格式解析即可。
    """
    url = f"{LLM_BASE_URL.rstrip('/')}/chat/completions"
    try:
        with requests.post(
            url,
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            # stream=True 是关键：不等大模型把整段话写完，边生成边往回读
            stream=True,
            # timeout 是个元组：(建立连接超时, 两次数据间隔超时)。
            # 流式接口不能用单个超时，否则长回答会被误判断开。
            timeout=(10, 120),
        ) as resp:
            if resp.status_code != 200:
                # 把大模型的错误（如 401 鉴权失败、429 限流）透传给前端，
                # 否则前端只会看到"没有回复"，无从排查
                detail = resp.text[:300]
                error = {"error": f"大模型返回 {resp.status_code}：{detail}"}
                yield f"data: {json.dumps(error, ensure_ascii=False)}\n\n"
                return

            # iter_lines 按行读取流。这里拿 bytes 自己 decode，
            # 而不是用 decode_unicode=True——后者依赖 requests 猜编码，中文容易乱码。
            for raw_line in resp.iter_lines():
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8", errors="ignore")
                # 已经是 "data: {...}" 格式的行，再加一个换行补成合法 SSE 事件
                yield f"{line}\n\n"

            # 告诉前端"流结束了"，前端的解析循环靠它收尾
            yield "data: [DONE]\n\n"

    except Exception as e:
        # 网络异常等也要以 SSE 形式返回，否则前端读到一半断了不知道原因
        error = {"error": f"调用大模型失败：{type(e).__name__}: {e}"}
        yield f"data: {json.dumps(error, ensure_ascii=False)}\n\n"


@router.post("/chat")
async def ai_chat(req: AIChatRequest):
    """
    AI 问答接口：接收对话历史，转发给大模型，以 SSE 流式返回回答。

    访问地址：POST /api/ai/chat

    请求体：
        {"messages": [{"role": "user", "content": "你好"}]}

    响应：text/event-stream 流，每条形如
        data: {"choices":[{"delta":{"content":"你"}}]}
    """
    # 没配密钥就直接告诉前端，比让它空等到超时友好得多
    if not is_llm_enabled():
        raise HTTPException(
            status_code=503,
            detail="后端未配置大模型密钥，请在项目根目录的 .env 中设置 LLM_API_KEY",
        )

    # 把系统提示词拼到历史前面：不管前端传了什么，人设都由后端说了算
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(m.model_dump() for m in req.messages)

    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "stream": True,                 # 必须开流式，否则前端拿不到打字机效果
        "temperature": LLM_TEMPERATURE,
    }

    return StreamingResponse(
        _sse_stream(payload),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",     # 禁止中间层缓存流
            "X-Accel-Buffering": "no",       # 若前面挂了 Nginx，让它别缓冲
            "Connection": "keep-alive",
        },
    )
