# ============================================================
# 数据模型层：用 SQLAlchemy 定义数据库表结构
# ------------------------------------------------------------
# 这个文件里定义的每个类，都对应数据库中的一张表。
# SQLAlchemy 的「声明式(Declarative)」写法让我们用 Python 类来"画"表：
#   - 类名  -> 表名(通过 __tablename__ 指定)
#   - 类属性 -> 表的字段(列)
# 这样做的好处：不需要手写建表 SQL，还能在代码里像操作对象一样操作数据。
# ============================================================

from datetime import datetime
from typing import Optional
from sqlalchemy import DateTime, Integer, String, Text, ForeignKey, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# ============================================================
# Base：所有模型类的「基类」
# ============================================================
# DeclarativeBase 是 SQLAlchemy 声明式模型的根类。
# 所有表模型都继承它，SQLAlchemy 才能识别"哪些类是要映射成表的"。
class Base(DeclarativeBase):
    # __abstract__ = True 表示：这个类自己「不是一张真实的表」，
    # 它只作为模板存在，把公共字段(创建时间/更新时间)共享给子类。
    # 如果漏了这个，SQLAlchemy 会尝试把 Base 当成一张表去注册，启动就报错。
    __abstract__ = True

    # created_at：记录每条数据的创建时间
    # Mapped[datetime] 是类型注解，表示这个字段对应的 Python 值是 datetime 类型
    # mapped_column(...) 把这个字段配置成数据库里的一列
    created_at: Mapped[datetime] = mapped_column(
        DateTime,              # 数据库列类型：日期时间
        default=datetime.now,  # 默认值：插入时自动填当前时间(注意是函数引用，不带括号)
        comment="创建时间"      # 列的注释(建表后显示在数据库里)
    )

    # updated_at：记录每条数据的最后更新时间
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,   # 插入时的默认值
        onupdate=datetime.now,  # 每次 update 时自动刷新为当前时间
        comment="更新时间"
    )


# ============================================================
# Category：新闻分类表 (news_category)
# ============================================================
class Category(Base):
    # 表名。数据库里实际建的表就叫 news_category
    __tablename__ = "news_category"

    # 主键 id，自增
    # primary_key=True 主键；autoincrement=True 数据库自动生成递增的 id
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="分类ID")

    # 分类名称。unique=True 保证不重复；nullable=False 不允许为空
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, comment="分类名称")

    # 排序字段，default=0 表示默认值 0，用于控制前端展示顺序
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="排序")

    # __repr__：定义对象被打印时显示成什么样子，方便调试
    # 例：print(category) -> <Category(id=1, name=科技, sort_order=0)>
    def __repr__(self):
        return f"<Category(id={self.id}, name={self.name}, sort_order={self.sort_order})>"


# ============================================================
# News：新闻表 (news)
# ============================================================
class News(Base):
    __tablename__ = "news"

    # __table_args__：表级别的额外配置，这里用来创建「索引」
    # 索引可以大幅提升按某列查询的速度(类似书的目录)。
    #   fk_news_category_idx -> 给 category_id 建索引，加快"按分类查新闻"
    #   idx_publish_time     -> 给 publish_time 建索引，加快"按时间排序/筛选"
    __table_args__ = (
        Index('fk_news_category_idx', 'category_id'),
        Index('idx_publish_time', 'publish_time')
    )

    # 主键 id，自增
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="新闻ID")

    # 标题，最长 255 字符，不能为空
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="新闻标题")

    # 简介。Optional[str] 表示这个字段「可以为空」—— 对应数据库里允许 NULL
    description: Mapped[Optional[str]] = mapped_column(String(500), comment="新闻简介")

    # 正文内容。Text 类型适合存长文本(几万字都没问题)
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="新闻内容")

    # 封面图 URL，可以为空
    image: Mapped[Optional[str]] = mapped_column(String(255), comment="封面图片URL")

    # 作者，可以为空
    author: Mapped[Optional[str]] = mapped_column(String(50), comment="作者")

    # 所属分类 id，通过 ForeignKey 关联到 news_category 表的 id 字段
    # ForeignKey('news_category.id') 建立外键关系，保证数据一致性
    category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey('news_category.id'), nullable=False, comment="分类ID"
    )

    # 浏览量，默认 0
    views: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="浏览量")

    # url：新闻原文链接。只有「采集入库」的新闻才有，手工导入的 403 条为空。
    # unique=True 让数据库自己保证「同一个链接只能存一条」，
    # 这样即使采集脚本重复运行，也不可能把同一篇新闻插两次。
    # 注意：MySQL 的唯一索引允许多个 NULL 共存，所以 403 条空值不会互相冲突。
    url: Mapped[Optional[str]] = mapped_column(String(500), unique=True, comment="原文链接")

    # source：来源媒体名（如"量子位"、"36氪"）。采集时写入，手工数据为空。
    source: Mapped[Optional[str]] = mapped_column(String(100), comment="来源媒体")

    # 发布时间，默认当前时间
    publish_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment="发布时间")

    def __repr__(self):
        return f"<News(id={self.id}, title='{self.title}', views={self.views})>"

