# ============================================================
# 大模型服务：把抓到的新闻原文「改写成新闻客户端要的样子」
# ------------------------------------------------------------
# 为什么需要这一步？
#   RSS 里给的是一整篇网页摘要（还夹着 HTML、推广语、外文），
#   直接塞进 news 表会出现：标题平淡、正文几百字全是广告、语言混杂。
#   所以这里让大模型做一次「编辑加工」，产出三段固定格式的内容：
#       title        中文标题（20 字内）
#       description  一句话简介（40~60 字）
#       content      新闻摘要（100~150 字，与库里现有 403 条数据风格一致）
#
# 【降级设计】这是本模块最重要的思想：
#   大模型不是永远可用的（没配 key、网络抖动、限流、返回格式跑偏都可能发生），
#   而「采集入库」这条链路不应该因为大模型挂掉就整体失败。
#   所以：调用失败一律返回 None，由上层用的 fallback 逻辑兜底。
# ============================================================

import asyncio
import json
import re
from typing import Any, Dict, Optional

import requests

from config.llm_conf import (
    LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_TEMPERATURE, LLM_TIMEOUT, is_llm_enabled,
)

# 系统提示词：给模型定「角色 + 约束」。把规则写在 system 里比写在 user 里更稳。
SYSTEM_PROMPT = (
    "你是一名专业的中文科技新闻编辑，为新闻客户端整理 AI/科技领域的资讯。"
    "你的任务是把抓取到的原文改写成规范的中文新闻条目。"
    "必须遵守：语言客观中立、只使用原文中出现的事实、不得编造数据或引用、"
    "不得保留广告和推广话术、不得输出任何解释。"
)

# 用户提示词模板：明确字段、字数、输出格式
#
# 关于字数控制的实测经验：
#   最初只写「content：100~150 字」，结果模型平均写到 200 字（最长 281 字），明显超标。
#   原因是没有给出「压缩取舍规则」——模型不知道该丢掉什么，就倾向把原文细节全保留。
#   改成「硬性约束 + 明确告诉他舍弃哪些内容 + 宁短勿长」之后，字数就稳住了。
#   这也是提示词工程的通用技巧：**给模型的负面约束要配一条可执行的行动指令**。
USER_PROMPT_TEMPLATE = """请把下面这篇新闻改写成 JSON，包含三个字段：

1. title：中文标题，不超过 20 个字，保留核心信息，不做标题党
2. description：一句话简介，40~60 字，概括最关键的结论
3. content：新闻摘要，100~150 字，讲清楚"发生了什么、涉及哪些公司或人物、有什么影响"

原文来源：{source}
原文标题：{title}
原文内容：
{body}

严格要求：
1. 只输出一个 JSON 对象，不要 markdown 代码块，不要任何解释文字。
2. content 必须控制在 150 字以内，写完后自己数一遍，超了就删减再输出。
3. 压缩时优先保留「谁、做了什么、结果或影响」，
   果断舍弃型号参数、背景铺垫、股价涨跌等次要细节。

输出长度请严格参照下面这个示例（它的 content 是 118 字）：
{{"title": "英伟达发布新一代AI芯片", "description": "英伟达发布Blackwell架构新芯片，推理性能大幅提升，微软谷歌等云厂商将首批部署。", "content": "英伟达正式发布基于Blackwell架构的新一代AI芯片，推理性能较上代大幅提升，同时降低了单位算力能耗。该芯片主要面向大模型训练与推理场景，微软、谷歌等云厂商已宣布将首批部署。业内人士认为，这将进一步拉大头部厂商在算力上的差距，并推高对先进封装产能的需求。"}}

【注意】上面示例里的花括号写成了双花括号 {{ }}，
因为这段模板要经过 str.format() 渲染，单花括号会被当成占位符而报 KeyError。
以后往模板里加示例（few-shot）时，记得转义。"""


def _post_chat(payload: Dict[str, Any]) -> Dict[str, Any]:
    """同步调用 /chat/completions。用 asyncio.to_thread 包装后即可在异步项目里用。"""
    # base_url 可能带或不带结尾斜杠，统一处理再拼路径
    url = f"{LLM_BASE_URL.rstrip('/')}/chat/completions"
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=LLM_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """
    从模型回复里抠出 JSON 对象。
    为什么不直接用 json.loads？
      因为模型经常会在 JSON 外面裹一层 ```json ... ``` 或者加一句"好的，以下是结果"，
      直接 loads 会失败。所以退一步：找第一个 { 到最后一个 } 之间的内容再解析。
    """
    if not text:
        return None
    cleaned = text.strip()
    # 去掉可能存在的 markdown 代码块围栏
    cleaned = re.sub(r"^```(?:json)?|```$", "", cleaned, flags=re.M).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


async def rewrite_news(title: str, raw_text: str, source: str) -> Optional[Dict[str, str]]:
    """
    调用大模型改写一条新闻。

    返回：{"title": ..., "description": ..., "content": ...}
        或 None —— 表示「大模型不可用或结果不合法」，请调用方走降级逻辑。
    """
    # 没配 api_key 就不用白跑一趟网络请求
    if not is_llm_enabled():
        return None

    # 正文太长会浪费 token（也没必要），截断到 3000 字符
    body = (raw_text or "")[:3000]
    if not body.strip():
        return None

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT_TEMPLATE.format(
                source=source, title=title, body=body,
            )},
        ],
        "temperature": LLM_TEMPERATURE,
        # 要求返回 JSON 对象。DeepSeek、通义等 OpenAI 兼容服务都支持这个参数；
        # 万一某家不支持会在 _post_chat 抛错，被下面的 except 兜住 → 降级。
        "response_format": {"type": "json_object"},
    }

    try:
        data = await asyncio.to_thread(_post_chat, payload)
        content = data["choices"][0]["message"]["content"]
    except Exception as e:
        # 网络错误、鉴权失败、限流、格式不符……全部走这里，统一降级
        print(f"  [LLM] 调用失败，本条降级为原文截取：{type(e).__name__}: {str(e)[:120]}")
        return None

    obj = _extract_json(content)
    if not obj:
        print("  [LLM] 返回内容无法解析成 JSON，本条降级")
        return None

    # 字段校验：三个字段必须都是非空字符串，且 content 得有实际内容
    result = {}
    for key in ("title", "description", "content"):
        value = obj.get(key)
        if not isinstance(value, str) or not value.strip():
            print(f"  [LLM] 返回缺少字段 {key}，本条降级")
            return None
        result[key] = value.strip()
    if len(result["content"]) < 30:
        print("  [LLM] content 过短，判定为无效结果，本条降级")
        return None

    # 数据库字段长度限制：title 255 / description 500，这里主动截断防报错
    result["title"] = result["title"][:255]
    result["description"] = result["description"][:500]
    return result
