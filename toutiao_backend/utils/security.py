# ============================================================
# 安全工具：密码的加密与校验
# ------------------------------------------------------------
# 为什么需要这个文件：
#   数据库里绝不能存明文密码（一旦泄露，用户所有账号都危险），
#   所以要存「不可逆」的哈希串。bcrypt 就是一种安全的密码哈希算法。
#
# 供 crud/users.py 调用，两个函数：
#   get_hash_password(明文) → 返回 bcrypt 哈希串     （注册 / 改密时存库）
#   verify_password(明文, 哈希) → True / False       （登录 / 改密时比对）
#
# 用法示例（在 crud/users.py 里）：
#   hashed = security.get_hash_password("123456")        # 得到一串看不懂的哈希
#   security.verify_password("123456", hashed)  # → True
#   security.verify_password("错密码",  hashed)  # → False
#
# 注意：底层用 passlib 的 bcrypt。passlib 1.7.4 与 bcrypt>=4.1 不兼容，
# 因此环境里必须保持 bcrypt==4.0.1，否则加密会直接抛错。
# ============================================================

from passlib.context import CryptContext
# passlib 是密码哈希库；CryptContext 是它的"配置对象"，用来指定用哪种算法、如何处理旧格式


# 创建一个全局的「密码上下文」对象，声明使用 bcrypt 算法
#   schemes=["bcrypt"]   —— 使用 bcrypt 算法
#   deprecated="auto"    —— 遇到旧版本格式的哈希时，自动按新方案重新处理
# 用法：整个项目共用这一个 pwd_context，不要重复创建
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# 密码加密：把用户明文密码转成不可逆的 bcrypt 哈希
# 用法：注册时(create_user)、改密时(change_password)调用
# 入参：password —— 用户输入的明文密码
# 返回：加密后的哈希串（直接存进数据库的 password 字段）
def get_hash_password(password: str):
    # pwd_context.hash() 是 passlib 提供的加密方法，内部会加随机"盐"，所以同一密码每次结果都不同
    return pwd_context.hash(password)


# 密码校验：比对用户输入的明文与库里存的哈希是否一致
# 用法：登录时(authenticate_user)、改密时(change_password)调用
# 入参：plain_password —— 用户输入的明文密码
#       hashed_password —— 数据库里存的 bcrypt 哈希串
# 返回：bool —— 一致返回 True，不一致返回 False
def verify_password(plain_password, hashed_password):
    # pwd_context.verify() 负责把明文重新哈希后和库里哈希比对，返回布尔值
    return pwd_context.verify(plain_password, hashed_password)
