from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# 新闻条目的公共字段（收藏列表、浏览历史等地方都要用，抽出来作为基类复用）
class NewsItemBase(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    image: Optional[str] = None
    author: Optional[str] = None
    category_id: int = Field(alias="categoryId")                              # 数据库字段 category_id → JSON 输出 categoryId
    views: int
    publish_time: Optional[datetime] = Field(None, alias="publishTime")       # 数据库字段 publish_time → JSON 输出 publishTime
    # 下面两个是「AI 新闻采集」新增的字段：手工导入的老数据为空，所以是 Optional
    url: Optional[str] = None                                                 # 原文链接
    source: Optional[str] = None                                              # 来源媒体（如"量子位"）

    model_config = ConfigDict(
        from_attributes=True,      # 允许从 ORM 对象直接读取同名属性
        populate_by_name=True      # 允许用字段名（category_id）构造，而不必非用别名（categoryId）
    )
