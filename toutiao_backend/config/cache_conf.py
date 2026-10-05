# ============================================================
# 缓存配置：基于 Redis 的缓存工具
# ------------------------------------------------------------
# 作用：把「查数据库很慢、但结果短期内不变」的数据（如新闻列表）存进 Redis，
#       下次直接读 Redis，减少数据库压力、加快响应。
#
# 提供 4 个函数：
#   get_cache(key)                —— 取缓存，返回原始字符串
#   get_json_cache(key)           —— 取缓存，并把 JSON 字符串还原成 dict/list
#   set_cache(key, value, expire) —— 写缓存（支持自动转 JSON、设置过期时间）
#   delete_cache_pattern(pattern) —— 按通配符批量删缓存（采集入库后清列表用）
#
# 用法示例：
#   data = await get_json_cache("news_list_1")   # 先试着读缓存
#   if data is None:                             # 缓存没有（None）
#       data = await 查数据库()                   # 就去查库
#       await set_cache("news_list_1", data, 600) # 再写回缓存，10 分钟过期
#
# 【设计原则】缓存永远是「加速器」，不是「必需品」。
#   Redis 挂了、连不上、超时了，业务都必须能继续跑（降级为直接查数据库），
#   绝不能因为缓存故障把请求拖死、或者把错误抛给前端。
# ============================================================

import asyncio       # 给 Redis 操作套一层硬超时（asyncio.wait_for）
import json          # 用于把 dict/list 和 JSON 字符串互相转换（json.dumps / json.loads）
import time          # 记录熔断时间戳

from typing import Any                   # Any 表示"任意类型"（用于给 value 参数做类型注解）
import redis.asyncio as redis            # Redis 的异步客户端，起别名叫 redis 方便书写


# ------------------------------------------------------------
# Redis 连接参数
# ------------------------------------------------------------
REDIS_HOST = "localhost"   # Redis 服务地址（本机）
REDIS_PORT = 6379          # Redis 默认端口
REDIS_DB = 0               # 使用第 0 号数据库（Redis 默认有 16 个库：0~15）


# ------------------------------------------------------------
# 创建全局的 Redis 客户端（整个项目共用这一个，不要重复创建）
# ------------------------------------------------------------
redis_client = redis.Redis(
    host=REDIS_HOST,          # Redis 主机地址
    port=REDIS_PORT,          # Redis 端口号
    db=REDIS_DB,              # Redis 数据库编号
    decode_responses=True,    # 是否将字节数据解码为字符串（True 则读出来是 str 而不是 b"xx"）

    # 【必须设置】连接与读写超时（秒）。
    # 踩过的坑：不设置时，如果 Redis 没启动，连接尝试会一直等到操作系统的 TCP 默认超时——
    # 实测本机一次失败要等约 48 秒（Windows 会先在 IPv6 上试、再回退到 IPv4）。
    # AI 新闻采集任务里「清缓存」这一步因此白等了 96 秒，整个任务从 1 秒变成 98 秒。
    socket_connect_timeout=2,
    socket_timeout=2,
)


# ============================================================
# Redis 故障熔断：防止「缓存挂了拖死业务」
# ------------------------------------------------------------
# 为什么光设 socket 超时还不够？
#   redis-py 内部自带重试逻辑，实测即使 socket_connect_timeout=2，
#   一次失败仍然耗掉 26 秒。如果每个请求都白等 26 秒，
#   Redis 没启动时整个服务等于不可用。
#
# 所以再加两道保险：
#   1. 硬超时：用 asyncio.wait_for 给每次操作设上限，超时立刻放弃
#   2. 熔断：失败一次后，接下来 60 秒内所有缓存操作直接跳过（不再尝试连接），
#            只付一次超时代价；60 秒后再试一次，Redis 恢复了就自动恢复
# ============================================================
REDIS_OP_TIMEOUT = 5        # 单次缓存操作的硬超时（秒）
REDIS_FAIL_COOLDOWN = 60    # 失败后的熔断时长（秒）

_redis_down_until = 0.0     # 熔断截止时间戳；0 表示当前未熔断


def _redis_should_skip() -> bool:
    """当前是否处于熔断期。是 → 直接跳过缓存操作，交给数据库兜底。"""
    return time.time() < _redis_down_until


def _mark_redis_down() -> None:
    """标记 Redis 不可用，进入熔断期。"""
    global _redis_down_until
    _redis_down_until = time.time() + REDIS_FAIL_COOLDOWN


async def _redis_call(op) -> Any:
    """
    Redis 操作的统一入口：套硬超时 + 失败熔断。

    op 是「一个返回协程的函数」，例如 lambda: redis_client.get(key)。
    为什么不直接传协程？因为协程一旦创建就必须马上 await，
    超时和熔断的判断就没机会在它创建之前拦住了。
    """
    if _redis_should_skip():
        return None
    try:
        return await asyncio.wait_for(op(), timeout=REDIS_OP_TIMEOUT)
    except Exception as e:
        # 缓存出错不能影响业务：记录日志 + 熔断 + 返回 None（上层当作"没缓存"）
        _mark_redis_down()
        print(f"缓存操作失败（已熔断 {REDIS_FAIL_COOLDOWN}s，期间直接走数据库）：{e}")
        return None


# ------------------------------------------------------------
# 取缓存（返回原始字符串）
# 用法：`value = await get_cache("some_key")`
# 返回：key 存在 → 字符串；不存在 / 出错 / 熔断 → None
# ------------------------------------------------------------
async def get_cache(key: str):
    # redis_client.get(key) 是异步操作；key 不存在时 Redis 返回 None
    return await _redis_call(lambda: redis_client.get(key))


# ------------------------------------------------------------
# 取缓存（并把 JSON 字符串还原成 Python 对象）
# 用法：`data = await get_json_cache("news_list_1")` → 直接拿到 dict / list
# 返回：key 存在 → 还原后的 dict/list；不存在 / 出错 / 熔断 → None
# ------------------------------------------------------------
async def get_json_cache(key: str):
    data = await _redis_call(lambda: redis_client.get(key))
    if not data:
        return None
    try:
        # json.loads 把字符串还原成 Python 的 dict / list
        return json.loads(data)
    except (TypeError, json.JSONDecodeError) as e:
        # 缓存里存了脏数据（不是合法 JSON）也不能让业务挂掉
        print(f"缓存内容解析失败：{e}")
        return None


# ------------------------------------------------------------
# 写缓存
# 用法：`await set_cache("news_list_1", data, 600)` → 存 600 秒后自动过期
# 参数：
#   key    —— 缓存键名（取的时候要用同一个 key）
#   value  —— 要缓存的值，可以是 dict / list / 字符串 / 数字
#   expire —— 过期时间（秒），默认 3600 秒 = 1 小时
# 返回：成功 True；出错 / 熔断 False
# ------------------------------------------------------------
async def set_cache(key: str, value: Any, expire: int = 3600):
    # Redis 只能存字符串，所以如果 value 是 dict / list，先转成 JSON 字符串
    # ensure_ascii=False 让中文原样存储（否则中文会被转成 \uXXXX 转义，可读性差）
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    # setex = SET + EXpire：写入 key 并同时设置过期秒数
    # 参数顺序是 (名字, 过期秒数, 值)，写反会导致缓存立即失效甚至报错
    result = await _redis_call(lambda: redis_client.setex(key, expire, value))
    return result is not None


# ------------------------------------------------------------
# 按通配符批量删除缓存
# 用法：`n = await delete_cache_pattern("news_list:*")` → 删掉所有新闻列表缓存
# 返回：删除的 key 数量；出错 / 熔断返回 0
# ------------------------------------------------------------
# 为什么用 scan_iter 而不是 keys？
#   keys 会一次性遍历整个 Redis 的 key 空间，key 多了会阻塞 Redis（生产事故常见原因）。
#   scan_iter 是游标式渐进遍历，每次只取一小批，对线上服务更友好。
async def delete_cache_pattern(pattern: str) -> int:
    if _redis_should_skip():
        return 0

    async def _do_scan() -> int:
        count = 0
        async for key in redis_client.scan_iter(match=pattern, count=100):
            await redis_client.delete(key)
            count += 1
        return count

    try:
        # 批量删除可能涉及多个 key，给的时间比普通读写宽一点
        return await asyncio.wait_for(_do_scan(), timeout=REDIS_OP_TIMEOUT * 2)
    except Exception as e:
        # 和读写缓存一样：缓存出问题不能让业务挂掉，记日志 + 熔断即可
        _mark_redis_down()
        print(f"批量删除缓存失败（已熔断 {REDIS_FAIL_COOLDOWN}s）：{e}")
        return 0
