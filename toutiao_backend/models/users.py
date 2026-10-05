# ============================================================
# 数据模型层（Model）：用户相关的两张数据库表
# ------------------------------------------------------------
# 本文件用 SQLAlchemy 的「声明式」写法，用 Python 类来"画"数据库表：
#   - 每个类对应数据库里的一张表
#   - 类的每个属性对应表里的一个字段（列）
#   - 类名不直接等于表名，真正的表名由 __tablename__ 指定
#
# 本文件定义两个类：
#   User      对应 user 表        —— 存账号、密码、昵称、头像等用户资料
#   UserToken 对应 user_token 表  —— 存登录后签发的访问令牌(token)
#
# 用法：其它模块（crud/users.py）通过 `from models.users import User, UserToken`
#       拿到这两个类，再用它们做增删改查。
# ============================================================

from datetime import datetime          # 日期时间类型，用于 created_at / updated_at / expires_at 字段
from typing import Optional            # Optional[str] 表示"这个字段可以是 None"（即数据库里允许 NULL）

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String
# 上面一行分别导入：
#   DateTime  —— 数据库的日期时间列类型
#   Enum      —— 数据库的枚举列类型（值只能从给定集合里选）
#   ForeignKey—— 声明"外键"，表示这一列的值指向另一张表的主键
#   Index     —— 声明"索引"，加快按某列的查询速度（类似书的目录）
#   Integer   —— 数据库的整数列类型
#   String    —— 数据库的字符串列类型（需要指定最大长度）

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
# 上面一行分别导入：
#   DeclarativeBase —— 所有模型类的"基类"，继承它才能被识别成一张表
#   Mapped          —— 类型注解工具：Mapped[int] 表示这个字段的 Python 值是 int
#   mapped_column   —— 把字段配置成数据库里的一列（指定类型、主键、默认值等）


# ------------------------------------------------------------
# Base：所有模型类的公共基类
# ------------------------------------------------------------
class Base(DeclarativeBase):
    # __abstract__ = True 表示：这个类自己「不是一张真实的表」，
    # 只作为模板存在，让下面的 User / UserToken 继承它的能力。
    # 如果漏了这行，SQLAlchemy 会尝试把 Base 本身也当成一张表去注册，启动就报错。
    __abstract__ = True


# ------------------------------------------------------------
# User：用户表 (user)
# ------------------------------------------------------------
# 用法示例：
#   user = User(username="张三", password="加密后的哈希")
#   db.add(user)   # 把新用户加入会话，之后 commit 才会真正写库
class User(Base):
    # 指定这个类对应数据库里的哪张表
    __tablename__ = "user"

    # __table_args__ 是表级别的额外配置，这里用来建「唯一索引」：
    #   唯一索引 = 该列的值不允许重复
    # username 唯一 → 保证登录名不重复
    # phone    唯一 → 保证手机号不重复（注册时也靠这个约束兜底）
    __table_args__ = (
        Index("username_UNIQUE", "username"),
        Index("phone_UNIQUE", "phone"),
    )

    # ---- 字段定义开始 ----

    # 主键 id：primary_key=True 表示主键；autoincrement=True 表示数据库自动生成递增的 id
    # 用法：插入时不用手动填 id，数据库会自动分配 1、2、3……
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="用户ID")

    # 用户名：最长 50 字符、不能为空、唯一（unique=True 与上面的唯一索引等效）
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, comment="用户名")

    # 密码：最长 255 字符（因为存的是 bcrypt 加密后的长哈希串，不是明文）
    # 用法：绝不存明文，crud 层会先调用 utils/security.py 的 get_hash_password 加密
    password: Mapped[str] = mapped_column(String(255), nullable=False, comment="密码（加密存储）")

    # 昵称：Optional[str] 表示可以为空（注册时可以不填）
    nickname: Mapped[Optional[str]] = mapped_column(String(50), comment="昵称")

    # 头像 URL：可为空；default= 表示插入时如果不填，自动用这张占位图
    avatar: Mapped[Optional[str]] = mapped_column(
        String(255),
        default="https://fastly.jsdelivr.net/npm/@vant/assets/cat.jpeg",
        comment="头像URL",
    )

    # 性别：Enum("male","female","unknown") 表示值只能是这三个之一；默认 "unknown"
    # 用法：数据库里存的是字符串 "male" / "female" / "unknown"
    gender: Mapped[Optional[str]] = mapped_column(
        Enum("male", "female", "unknown"),
        default="unknown",
        comment="性别",
    )

    # 个人简介：可为空；默认给一句占位文案
    bio: Mapped[Optional[str]] = mapped_column(
        String(500),
        default="这个人很懒，什么都没留下",
        comment="个人简介",
    )

    # 手机号：可为空、唯一
    phone: Mapped[Optional[str]] = mapped_column(String(20), unique=True, comment="手机号")

    # 创建时间：default=datetime.now 表示插入时自动填"当前时间"
    # 注意：传的是函数引用 datetime.now（不带括号），SQLAlchemy 会在插入那一刻才调用它
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment="创建时间")

    # 更新时间：default=插入时填当前时间；onupdate=每次更新记录时自动刷新为当前时间
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
        onupdate=datetime.now,
        comment="更新时间",
    )

    # __repr__：定义这个对象被 print() 打印时显示成什么样，方便调试
    # 用法：print(user) → <User(id=1, username='admin')>
    def __repr__(self):
        return f"<User(id={self.id}, username='{self.username}')>"


# ------------------------------------------------------------
# UserToken：用户令牌表 (user_token)
# ------------------------------------------------------------
# 用法：登录/注册成功后，crud/users.py 的 create_token 会给用户生成一个随机 token，
#       存到这张表里；之后前端每次请求带上这个 token，后端据此识别"你是谁"。
class UserToken(Base):
    __tablename__ = "user_token"

    # 表级配置：token 唯一 + 给 user_id 建普通索引（加快"按用户查令牌"的速度）
    __table_args__ = (
        Index("token_UNIQUE", "token"),
        Index("fk_user_token_user_idx", "user_id"),
    )

    # 主键 id，自增
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="令牌ID")

    # 所属用户：外键指向 user 表的 id 字段，表示"这个 token 是哪个用户的"
    # 用法：根据 token 查到这条记录后，用 user_id 再去 user 表查出完整用户信息
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey(User.id), nullable=False, comment="用户ID")

    # 令牌值：随机生成的长字符串，最长 255 字符，不能为空且唯一
    token: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, comment="令牌值")

    # 过期时间：不能为空；校验 token 时判断当前时间是否已超过它
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="过期时间")

    # 创建时间
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment="创建时间")

    def __repr__(self):
        return f"<UserToken(id={self.id}, user_id={self.user_id}, token='{self.token}')>"
