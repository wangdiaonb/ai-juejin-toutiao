import { defineStore } from 'pinia'
import axios from 'axios'
import { apiConfig } from '../../config/api'

// ============================================================
// 新闻 store：分类、列表、详情
// ------------------------------------------------------------
// 本次清理（2026-10-05）：
//   1. actions 里原本有**两个同名 changeCategory**。JS 对象字面量的重复 key
//      是「后面的覆盖前面的」，所以实际生效的一直是文件末尾那版 —— 现在只留它。
//   2. 删掉了一大段「只注释了一半」的模拟数据代码。那段注释靠结尾一个 `},`
//      恰好把语法闭合上，能跑但极其脆弱：改动一行就会连带语法错误。
//      真实接口早就跑通了，模拟数据不再需要。
//   3. 去掉 getNewsDetail 里的 `console.log('使用模拟新闻详情数据')`，
//      它走的是真实接口，那行日志是误导。
// ============================================================

export const useNewsStore = defineStore('news', {
  state: () => ({
    newsList: [],
    newsDetail: {},
    categories: [],
    currentCategory: 1,
    loading: false,
    refreshing: false,
    finished: false,
    categoriesLoading: false
  }),

  actions: {
    // 获取新闻分类
    async getCategories() {
      // 防并发重复请求：已经在加载就直接返回
      if (this.categoriesLoading) return

      this.categoriesLoading = true

      try {
        const response = await axios.get(`${apiConfig.baseURL}/api/news/categories`)

        if (response.data && response.data.code === 200) {
          // 「更多」不是后端的真实分类，是前端自己加的入口，固定排在最后
          this.categories = [...response.data.data, { id: 10, name: '更多' }]

          // 还没选过分类时，默认选第一个
          if (!this.currentCategory && this.categories.length > 0) {
            this.currentCategory = this.categories[0].id
          }
        }
      } catch (error) {
        console.error('获取新闻分类失败:', error)
        // 接口失败时给一份兜底分类，至少别让页面空着。
        // 注意：这份兜底表是历史遗留，只有 7 个分类，比后端的 9 个少
        //（缺「财经」「人工智能」），只影响接口挂掉时的极端情况。
        this.categories = [
          { id: 1, name: '头条' },
          { id: 2, name: '社会' },
          { id: 3, name: '国内' },
          { id: 4, name: '国际' },
          { id: 5, name: '娱乐' },
          { id: 6, name: '体育' },
          { id: 7, name: '科技' }
        ]
      } finally {
        this.categoriesLoading = false
      }
    },

    // 切换新闻分类
    //
    // 行为（与清理前实际生效的那版完全一致）：
    //   · 点的还是当前分类 → 直接返回，不重复发请求
    //   · 换了分类 → 清空列表，再走 getNewsList(true) 的「刷新」分支
    //     （固定取第 1 页，同时把 refreshing 置为 true，驱动下拉刷新的 loading）
    changeCategory(categoryId) {
      if (this.currentCategory !== categoryId) {
        this.currentCategory = categoryId
        this.newsList = []
        this.finished = false
        this.getNewsList(true)
      }
    },

    // 获取新闻列表
    async getNewsList(isRefresh = false) {
      if (isRefresh) {
        this.refreshing = true
        this.newsList = []
        this.finished = false
      }

      this.loading = true

      try {
        const params = {
          categoryId: this.currentCategory,
          // 追加模式：按「已经有几条」推算下一页；刷新模式固定第 1 页
          page: isRefresh ? 1 : Math.ceil(this.newsList.length / 10) + 1,
          pageSize: 10
        }

        const response = await axios.get(`${apiConfig.baseURL}/api/news/list`, { params })

        if (response.data && response.data.code === 200) {
          const newsData = response.data.data.list

          // 刷新时直接替换，追加时拼接在已有列表后面
          this.newsList = isRefresh ? newsData : [...this.newsList, ...newsData]

          // 后端返回的条数不足一页 → 说明后面没有了，停止上拉加载
          if (newsData.length < params.pageSize) {
            this.finished = true
          }
        }
      } catch (error) {
        console.error('获取新闻列表失败:', error)
      } finally {
        this.loading = false
        this.refreshing = false
      }
    },

    // 获取新闻详情
    // 成功后写入 newsDetail（含 relatedNews），失败时保留原有数据不动
    async getNewsDetail(id) {
      try {
        const response = await axios.get(`${apiConfig.baseURL}/api/news/detail?id=${id}`)

        if (response.data && response.data.code === 200) {
          this.newsDetail = response.data.data
        } else {
          console.error('获取新闻详情失败: 接口返回错误')
        }
      } catch (error) {
        console.error('获取新闻详情失败:', error)
      }
    },

    // 把分类 id 转成中文名（模板里可能用到）
    getCategoryName(categoryId) {
      const category = this.categories.find(item => item.id === categoryId)
      return category ? category.name : '未知'
    }
  }
})
