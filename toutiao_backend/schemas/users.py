# ============================================================
# Schemas 层（Pydantic 模型）：用户相关的请求体 / 响应体
# ------------------------------------------------------------
# 这些类做两件事：
#   1. 校验前端发来的请求体（比如注册时传的 username / password）
#   2. 规定接口返回给前端的 JSON 结构（比如登录返回的 token / userInfo）
#
# 与 models/users.py 的区别：
#   models 里的类是"数据库表结构"，schemas 里的类是"接口收发的数据结构"。
#   数据库有 password 字段，但返回给前端时绝不能让 password 泄露出去，
#   所以响应用的是这里的 UserInfoResponse（不含 password）。
#
# 字段命名约定：返回给前端的字段用 camelCase（token / userInfo / oldPassword）。
# ============================================================

from typing import Optional            # Optional[str] 表示字段可为 None（可空）

from pydantic import BaseModel, ConfigDict, Field
# 上面一行分别导入：
#   BaseModel —— 所有 Pydantic 模型的基类，继承它才能做数据校验/序列化
#   ConfigDict —— 用来写 model_config 配置项（比如允许从 ORM 对象读取属性）
#   Field      —— 给字段加约束：必填(用 ...)、最大长度、默认值、别名等


# ------------------------------------------------------------
# UserRequest：注册 / 登录的请求体
# ------------------------------------------------------------
# 用法：前端 POST /api/users/register 或 /login 时，body 传 {"username":"xxx","password":"xxx"}，
#       FastAPI 会自动用这个类校验：两个字段都必须有，类型必须是字符串。
class UserRequest(BaseModel):
    username: str    # 用户名，必填（没有默认值 = 必填）
    password: str    # 密码，必填


# ------------------------------------------------------------
# UserinfoResponse：userInfo 里可空的基础资料字段
# ------------------------------------------------------------
# 这是「子模型」，把用户资料里那几个可为空的字段抽出来，
# 供下面的 UserInfoResponse 继承，避免重复写。
# 用法：不单独使用，只作为父类被继承。
class UserinfoResponse(BaseModel):
    # Field(None, max_length=50, ...) 的含义：
    #   第一个参数 None = 默认值 None（即"允许为空"）
    #   max_length = 限制字符串最大长度，超长会校验失败
    nickname: Optional[str] = Field(None, max_length=50, description="昵称")
    avatar: Optional[str] = Field(None, max_length=255, description="头像URL")
    gender: Optional[str] = Field(None, description="性别")
    bio: Optional[str] = Field(None, max_length=500, description="个人简介")


# ------------------------------------------------------------
# UserInfoResponse：完整的用户信息（含 id / username）
# ------------------------------------------------------------
# 用法：把查到的 User ORM 对象转成这个模型：
#       UserInfoResponse.model_validate(user)
#       （前提是开了下面 model_config 里的 from_attributes）
class UserInfoResponse(UserinfoResponse):
    id: int           # 用户 ID
    username: str     # 用户名

    # ConfigDict(from_attributes=True) 表示：允许直接从 ORM 对象读取同名属性，
    # 这样 model_validate(orm对象) 就能自动把 ORM 对象里的字段"搬"进这个 Pydantic 模型
    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------
# UserAuthResponse：注册 / 登录接口的 data 部分
# ------------------------------------------------------------
# 用法：登录成功后返回给前端的结构：
#       { code, message, data: { token: "xxx", userInfo: {...} } }
#       token 给前端之后每次请求时带上，userInfo 是当前登录的用户资料
class UserAuthResponse(BaseModel):
    token: str    # 访问令牌字符串
    # Field(..., alias="userInfo")：
    #   第一个参数 ... 表示必填；
    #   alias="userInfo" 表示序列化输出时，这个字段在 JSON 里叫 "userInfo"（camelCase）
    userInfo: UserInfoResponse = Field(..., alias="userInfo")

    # ConfigDict 两个开关：
    #   populate_by_name=True —— 允许构造时既用字段名 userInfo、也用别名 "userInfo" 传参
    #   from_attributes=True  —— 允许从 ORM 对象读取属性（这里 userInfo 是子模型，其实用不上，但无妨）
    model_config = ConfigDict(
        populate_by_name=True,
        from_attributes=True
    )


# ------------------------------------------------------------
# UserUpdateRequest：更新用户资料的请求体
# ------------------------------------------------------------
# 用法：前端 PUT /api/users/update 时，body 里只想改哪个字段就传哪个，
#       所有字段都"可选"（默认 None），没传的字段不会被覆盖。
#       比如只传 {"nickname":"新昵称"}，就只改昵称，其它资料不变。
class UserUpdateRequest(BaseModel):
    nickname: Optional[str] = Field(None, max_length=50, description="昵称")
    avatar: Optional[str] = Field(None, max_length=255, description="头像URL")
    gender: Optional[str] = Field(None, description="性别")
    bio: Optional[str] = Field(None, max_length=500, description="个人简介")
    phone: Optional[str] = Field(None, max_length=20, description="手机号")


# ------------------------------------------------------------
# UserChangePasswordRequest：修改密码的请求体
# ------------------------------------------------------------
# 用法：前端 PUT /api/users/password 时，body 传 {"oldPassword":"旧密码","newPassword":"新密码"}。
#       alias 让 JSON 里的 camelCase 字段名映射到 Python 里的 snake_case 属性名。
class UserChangePasswordRequest(BaseModel):
    # Field(..., alias="oldPassword")：... 表示必填；alias 指定 JSON 里的字段名
    old_password: str = Field(..., alias="oldPassword", description="旧密码")
    # min_length=6 表示新密码至少 6 位，不满足会被 FastAPI 自动拒绝（返回 422）
    new_password: str = Field(..., min_length=6, alias="newPassword", description="新密码")
