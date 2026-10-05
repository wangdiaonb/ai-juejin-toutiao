<template>
  <div class="news-item" @click="goToDetail">
    <div class="news-content">
      <h3 class="news-title">{{ news.title }}</h3>
      <p class="news-desc">{{ news.description }}</p>
      <div class="news-info">
        <span>{{ news.author }}</span>
        <span>{{ news.publishTime }}</span>
        <span>{{ news.views }} 阅读</span>
      </div>
    </div>
    <div class="news-image">
      <!--
        referrerpolicy="no-referrer" 是必须的，不是可选项：
        澎湃新闻的图片 CDN（imgpai.thepaper.cn）做了 Referer 防盗链 ——
        带「非 thepaper 域」的 Referer 一律返回 403 + HTML 错误页，
        浏览器会因为「图片请求收到 HTML」直接报 ERR_BLOCKED_BY_ORB 显示裂图。
        实测：无 Referer → 200；Referer=thepaper.cn → 200；Referer=localhost → 403。
        所以这里显式声明「不发送 Referer」，其它站点的图片也一并受益（它们本就不校验）。
      -->
      <img :src="news.image" :alt="news.title" referrerpolicy="no-referrer">
    </div>
  </div>
</template>

<script setup>
import { defineProps } from 'vue'
import { useRouter } from 'vue-router'

const props = defineProps({
  news: {
    type: Object,
    required: true
  }
})

const router = useRouter()

const goToDetail = () => {
  router.push(`/news/detail/${props.news.id}`)
}
</script>

<style scoped>
.news-item {
  display: flex;
  padding: 12px 16px;
  border-bottom: 1px solid #f2f2f2;
  background-color: #fff;
}

.news-content {
  flex: 1;
  margin-right: 12px;
  overflow: hidden;
}

.news-title {
  font-size: 16px;
  font-weight: 500;
  margin: 0 0 8px;
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.news-desc {
  font-size: 14px;
  color: #666;
  margin: 0 0 8px;
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.news-info {
  font-size: 12px;
  color: #999;
  display: flex;
}

.news-info span {
  margin-right: 10px;
}

.news-image {
  width: 110px;
  height: 80px;
  flex-shrink: 0;
}

.news-image img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  border-radius: 4px;
}
</style>