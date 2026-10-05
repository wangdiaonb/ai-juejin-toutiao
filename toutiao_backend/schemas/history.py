from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from schemas.base import NewsItemBase


class HistoryAddRequest(BaseModel):
    """记录一次浏览的请求体：前端只传 newsId，userId 由 token 解析，无需前端传"""
    news_id: int = Field(..., alias="newsId", description="新闻ID")


class HistoryNewsItemResponse(NewsItemBase):
    """浏览历史列表里的单条记录 = 新闻基础字段 + 历史记录字段"""
    history_id: int = Field(..., alias="historyId", description="历史记录ID")
    view_time: datetime = Field(..., alias="viewTime", description="浏览时间")

    model_config = ConfigDict(
        populate_by_name=True,
        from_attributes=True
    )


class HistoryListResponse(BaseModel):
    """浏览历史列表响应：列表 + 总数 + 是否还有下一页"""
    list: list[HistoryNewsItemResponse]
    total: int
    has_more: bool = Field(..., alias="hasmore", description="是否还有下一页")

    model_config = ConfigDict(
        populate_by_name=True,
        from_attributes=True
    )
