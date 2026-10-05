/**
 * API配置文件
 * 包含后端API基础URL和AI问答接口地址
 */

// API基础URL配置
export const apiConfig = {
  // 后端API基础URL
  baseURL: 'http://127.0.0.1:8000',
}

/**
 * AI 问答配置
 *
 * 【安全提醒】
 *   这里原来写的是「浏览器直连大模型」，并且把 API Key 硬编码在了本文件里
 *   （把密钥明文写在 apiKey 字段里）。这种做法很危险：
 *   前端代码会被打包成 JS 文件发给每一个访问者，任何人按 F12 就能看到密钥，
 *   然后拿去盗刷你的额度。
 *
 *   现在的做法：请求自己后端的 /api/ai/chat 接口，
 *   由后端带上密钥去调用大模型（密钥存在后端项目的 .env 文件里）。
 *   浏览器端从头到尾接触不到密钥。
 *
 *   因此本文件不再需要 apiKey 和 model ——
 *   模型由后端 config/llm_conf.py 决定，换模型不用改前端。
 */
export const aiChatConfig = {
  // 后端 AI 问答转发接口（完整地址）
  apiEndpoint: `${apiConfig.baseURL}/api/ai/chat`,
}
