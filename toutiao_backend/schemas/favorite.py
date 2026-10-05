from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from schemas.base import NewsItemBase


class FavoriteCheckResponse(BaseModel):
    """检查收藏状态的响应：某条新闻是否已被收藏"""
    is_favorite: bool = Field(..., alias="isFavorite", description="是否已收藏")

    # populate_by_name=True：允许用字段名 is_favorite 构造（而不必非用别名 isFavorite）
    model_config = ConfigDict(populate_by_name=True)


class FavoriteRequest(BaseModel):
    """添加收藏的请求体：前端只传 newsId，userId 由 token 解析，无需前端传"""
    news_id: int = Field(..., alias="newsId", description="新闻ID")


class FavoriteNewsResponse(NewsItemBase):
    """收藏列表里的单条新闻 = 新闻基础字段 + 收藏记录字段"""
    favorite_id: int = Field(..., alias="favoriteId", description="收藏记录ID")
    favorite_time: Optional[datetime] = Field(None, alias="favoriteTime", description="收藏时间")

    model_config = ConfigDict(
        populate_by_name=True,
        from_attributes=True
    )


class FavoriteListResponse(BaseModel):
    """收藏列表响应：列表 + 总数 + 是否还有下一页"""
    list: list[FavoriteNewsResponse]
    total: int
    has_more: bool = Field(..., alias="hasmore", description="是否还有下一页")

    model_config = ConfigDict(
        populate_by_name=True,
        from_attributes=True
    )
