# ============================================================
# CRUD 层：用户表(user / user_token)的所有数据库操作
# ------------------------------------------------------------
# 「CRUD」= Create(增) / Read(查) / Update(改) / Delete(删)。
# 这一层职责很单一：只负责拼 SQL、和数据库打交道，把查到的数据返回给上层。
# 它不关心 HTTP 请求，也不拼返回格式（那是 routers 层的活）。
#
# 用法：本文件里的函数都被 routers/users.py 通过 `users.xxx(...)` 调用，
#       例如 routers 里写 `await users.get_user_by_username(db, name)`。
#
# 每个函数第一个参数都是 db（数据库会话），由上层通过 Depends(get_db) 注入。
# ============================================================

import uuid                              # 生成 token 用的随机唯一串（uuid4 几乎不会重复）
from datetime import datetime, timedelta  # datetime 算当前时间；timedelta 算"7 天后"这类时间偏移

from fastapi import HTTPException          # 更新失败时主动抛 404 异常（会交给全局异常处理器返回给前端）
from sqlalchemy import select, update      # select=构造 SELECT 查询；update=构造 UPDATE 更新语句
from sqlalchemy.ext.asyncio import AsyncSession  # 异步数据库会话类型（函数参数的类型注解用）

from models.users import User, UserToken   # ORM 模型：对应 user / user_token 两张表
from schemas.users import UserRequest, UserUpdateRequest  # 请求体类型：注册/登录用 UserRequest，更新资料用 UserUpdateRequest
from utils import security                 # 密码加密(get_hash_password)/校验(verify_password)工具


# ------------------------------------------------------------
# 按用户名查用户
# 用法：`user = await get_user_by_username(db, "admin")`
# 使用场景：注册时查重、登录时找账号、更新资料后取回最新数据
# 返回：命中返回 User 对象；查不到返回 None（不会抛异常）
# ------------------------------------------------------------
async def get_user_by_username(db: AsyncSession, username: str):
    # select(User).where(...) 拼出 "SELECT * FROM user WHERE username = ?" 这条查询语句（此时还没真正执行）
    query = select(User).where(User.username == username)
    # db.execute(query) 真正把 SQL 发给数据库执行（异步，所以必须 await）
    result = await db.execute(query)
    # scalar_one_or_none()：取出"单个结果对象"；没有结果就返回 None
    # （注意：如果用 scalar_one()，查不到时会直接抛异常，这里用 or_none 更安全）
    return result.scalar_one_or_none()


# ------------------------------------------------------------
# 创建新用户（注册时调用）
# 用法：`user = await create_user(db, user_data)`，user_data 是 UserRequest（含 username/password）
# 返回：创建好的 User 对象（此时已经拿到了数据库分配的自增 id）
# ------------------------------------------------------------
async def create_user(db: AsyncSession, user_data: UserRequest):
    # 1) 密码绝不能明文存库：先调用 security 工具加密成 bcrypt 哈希串
    hashed_password = security.get_hash_password(user_data.password)
    # 2) 构造 ORM 对象（只填了用户名和加密后的密码，其余字段走模型里定义的默认值）
    user = User(username=user_data.username, password=hashed_password)
    db.add(user)             # 把新对象"登记"进会话，此时还没写进数据库
    await db.commit()        # 提交事务，才真正执行 INSERT 把数据写进数据库
    await db.refresh(user)   # 从数据库读回最新数据（拿到自增生成的 id 字段）
    return user


# ------------------------------------------------------------
# 为用户签发 / 刷新 token
# 用法：`token = await create_token(db, user.id)`
# 使用场景：注册成功、登录成功都要调用（注册成功即相当于自动登录）
# 返回：token 字符串（上层会把它放进响应里给前端）
# 逻辑：一个用户始终只保留一条有效 token，登录会"刷新"旧 token
# ------------------------------------------------------------
async def create_token(db: AsyncSession, user_id: int):
    # 1) 生成随机 token 串（uuid4 转成字符串），过期时间设为"7 天后"
    token = str(uuid.uuid4())
    expires_at = datetime.now() + timedelta(days=7)

    # 2) 查该用户当前是否已经有 token 记录
    query = select(UserToken).where(UserToken.user_id == user_id)
    result = await db.execute(query)
    user_token = result.scalar_one_or_none()   # 有记录返回对象，没有返回 None

    if user_token:
        # 已有记录 → 不新建，直接刷新 token 值和过期时间
        user_token.token = token
        user_token.expires_at = expires_at
        await db.commit()         # 提交，把更新写进数据库
        await db.refresh(user_token)
    else:
        # 没有记录 → 在 user_token 表新增一行
        user_token = UserToken(user_id=user_id, token=token, expires_at=expires_at)
        db.add(user_token)
        await db.commit()
        await db.refresh(user_token)
    return token   # 把 token 字符串返回给上层，放进响应里


# ------------------------------------------------------------
# 登录校验：用户名存在 + 密码正确，才算通过
# 用法：`user = await authenticate_user(db, "admin", "123456")`
# 返回：校验通过返回 User 对象；不通过返回 None（上层据此抛 401）
# ------------------------------------------------------------
async def authenticate_user(db: AsyncSession, username: str, password: str):
    # 1) 先按用户名找账号：不存在直接返回 None（不泄露"是用户名错还是密码错"，更安全）
    user = await get_user_by_username(db, username)
    if not user:
        return None
    # 2) 比对用户输入的明文密码和库里存的哈希：不一致也返回 None
    if not security.verify_password(password, user.password):
        return None
    # 3) 两个条件都通过 → 返回该用户对象，交给上层生成 token
    return user


# ------------------------------------------------------------
# 根据 token 查询用户（验证 token → 返回对应用户）
# 用法：`user = await get_user_by_token(db, token)`（主要在 utils/auth.py 里被调用）
# 返回：token 有效且未过期 → 返回 User 对象；否则返回 None
# ------------------------------------------------------------
async def get_user_by_token(db: AsyncSession, token: str):
    # 1) 先按 token 值查 user_token 表，看这个 token 是否存在
    query = select(UserToken).where(UserToken.token == token)
    result = await db.execute(query)
    db_token = result.scalar_one_or_none()

    # 2) token 不存在，或已经过期（过期时间早于当前时间）→ 都返回 None
    if not db_token or db_token.expires_at < datetime.now():
        return None

    # 3) token 有效 → 再按 user_id 去 user 表查出完整用户信息返回
    query = select(User).where(User.id == db_token.user_id)
    result = await db.execute(query)
    return result.scalar_one_or_none()


# ------------------------------------------------------------
# 更新用户资料（按用户名定位，只更新前端实际提交的非空字段）
# 用法：`user = await update_user(db, user.username, user_data)`
#       user_data 是 UserUpdateRequest，里面只放了用户想改的字段
# 返回：更新后的 User 对象；若没匹配到用户则抛 404
# ------------------------------------------------------------
async def update_user(db: AsyncSession, user_name: str, user_data: UserUpdateRequest):
    # model_dump(exclude_none=True, exclude_unset=True)：
    #   把 Pydantic 请求体转成"只包含前端实际提交字段"的字典，
    #   没传的字段（值为 None）会被排除掉，避免把数据库里已有的值覆盖成 None
    query = update(User).where(User.username == user_name).values(**user_data.model_dump(
        exclude_none=True,
        exclude_unset=True
    ))
    result = await db.execute(query)   # 执行 UPDATE
    await db.commit()                  # 提交事务，让修改真正落库

    # rowcount 表示"受影响的行数"：0 说明没有匹配到该用户
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="用户不存在")

    # 更新成功 → 重新查出更新后的用户返回给上层
    updated_user = await get_user_by_username(db, user_name)
    return updated_user


# ------------------------------------------------------------
# 修改密码：验证旧密码 → 新密码加密 → 更新
# 用法：`ok = await change_password(db, user, old_password, new_password)`
# 返回：旧密码正确且修改成功返回 True；旧密码错误返回 False（不抛异常）
# ------------------------------------------------------------
async def change_password(db: AsyncSession, user: User, old_password: str, new_password: str):
    # 1) 先校验旧密码是否正确：不对直接返回 False，由路由层抛 401
    if not security.verify_password(old_password, user.password):
        return False

    # 2) 新密码加密后写回 user 对象
    user.password = security.get_hash_password(new_password)
    db.add(user)             # user 是已存在的对象，add 后 commit 会触发 UPDATE 更新该行
    await db.commit()        # 提交，把新密码写进数据库
    await db.refresh(user)   # 读回最新数据
    return True
