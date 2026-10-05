# ============================================================
# 大模型配置：用哪个模型来给采集到的新闻「改写 + 写摘要」
# ------------------------------------------------------------
# 走的是 OpenAI 兼容协议（POST /chat/completions），
# 所以 DeepSeek、通义千问、智谱、Kimi、硅基流动、本地 vLLM/Ollama 都能直接用，
# 换厂商只需要改下面三个环境变量，代码一行都不用动。
#
# 密钥不写死在代码里，通过「环境变量」或「项目根目录的 .env 文件」提供：
#
#   方式一：项目根目录新建 .env 文件，内容：
#       LLM_BASE_URL=https://api.deepseek.com/v1
#       LLM_API_KEY=sk-xxxxxxxxxxxxxxxx
#       LLM_MODEL=deepseek-chat
#
#   方式二：在系统里设置同名环境变量（PowerShell：$env:LLM_API_KEY="sk-xxx"）
#
# 【降级说明】没配 LLM_API_KEY 时，程序不会报错崩溃，
# 而是自动改用「截取原文」的方式生成标题和摘要，保证采集链路始终能跑通。
# ============================================================

import os

# python-dotenv 用来读取 .env 文件。它是可选依赖：
# 没装也不影响运行（只是 .env 不生效，改用系统环境变量）。
try:
    from dotenv import load_dotenv
    load_dotenv()          # 从项目根目录加载 .env
except ImportError:
    pass


# ------------------------------------------------------------
# LLM 接口参数
# ------------------------------------------------------------
# base_url：接口地址。注意多数厂商要带 /v1 后缀
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
# api_key：为空时表示「未配置大模型」→ 自动降级为原文截取
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
# model：模型名。DeepSeek 用 deepseek-chat（便宜且中文好）
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
# 单次调用的超时（秒）。批量摘要时会一条一条调，超时别设太短
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "60"))
# 采样温度：摘要任务要稳，不要发挥，所以给低值
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))


def is_llm_enabled() -> bool:
    """是否配置了大模型。没配 → 采集流程自动降级为「原文截取」模式。"""
    return bool(LLM_API_KEY.strip())
