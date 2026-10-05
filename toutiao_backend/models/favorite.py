# ============================================================
# 数据模型层：收藏表 (favorite)
# ------------------------------------------------------------
# 记录用户收藏的新闻。同一用户对同一条新闻只能收藏一次，
# 由 (user_id, news_id) 的联合唯一约束保证。
# ============================================================

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from models.news import News
from models.users import User


class Base(DeclarativeBase):
    # 抽象基类：只作为模板，不对应真实表
    __abstract__ = True


class Favorite(Base):
    """收藏表 ORM 模型"""
    __tablename__ = "favorite"

    # 表级配置：联合唯一约束 + 两个外键索引
    __table_args__ = (
        UniqueConstraint("user_id", "news_id", name="user_news_unique"),
        Index("fk_favorite_user_idx", "user_id"),
        Index("fk_favorite_news_idx", "news_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="收藏ID")
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey(User.id), nullable=False, comment="用户ID")
    news_id: Mapped[int] = mapped_column(Integer, ForeignKey(News.id), nullable=False, comment="新闻ID")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False, comment="收藏时间")

    def __repr__(self):
        return f"<Favorite(id={self.id}, user_id={self.user_id}, news_id={self.news_id})>"
