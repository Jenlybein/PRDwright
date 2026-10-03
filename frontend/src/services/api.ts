import axios from 'axios'

import type { QuestionsConfig } from '@/types'

/**
 * 全局 axios 实例。
 * 请求走 Vite 的 /api 代理转发到后端，所以这里保持同源、不写死后端地址。
 */
const http = axios.create({
  baseURL: '/',
  timeout: 30_000,
  headers: {
    'Content-Type': 'application/json',
  },
})

/**
 * 获取需求澄清问卷配置。
 * 对应后端 `GET /api/conversation/questions`。
 */
export async function getQuestions(): Promise<QuestionsConfig> {
  const { data } = await http.get<QuestionsConfig>('/api/conversation/questions')
  return data
}

export default http
