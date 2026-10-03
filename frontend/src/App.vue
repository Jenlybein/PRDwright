<script setup lang="ts">
import { onMounted, reactive, ref, watch } from 'vue'

import FormStep from '@/components/FormStep.vue'
import { getQuestions } from '@/services/api'
import type { QuestionsConfig } from '@/types'

/** localStorage 中保存表单草稿的键名 */
const STORAGE_KEY = 'harnessprd:form-data'

const questions = ref<QuestionsConfig | null>(null)
const loading = ref(true)
const loadError = ref('')
const savedTip = ref('')

/** 读取本地草稿；草稿缺失或损坏时返回空对象 */
function loadDraft(): Record<string, string> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    const parsed: unknown = raw ? JSON.parse(raw) : {}
    return typeof parsed === 'object' && parsed !== null
      ? (parsed as Record<string, string>)
      : {}
  } catch {
    return {}
  }
}

/** 是否已有本地草稿（用于页面提示「已为你恢复上次填写的内容」） */
const hasDraft = Object.keys(loadDraft()).length > 0

// 表单数据：初始取自本地草稿，填写过程中由 FormStep 通过 v-model 双向写入。
// 这里用 let 是因为 v-model 编译后需要可赋值的绑定；FormStep 只修改字段、不整体替换对象，
// 所以 formData 的响应式引用不会被改写。
let formData = reactive<Record<string, string>>(loadDraft())

/** 拉取问卷配置 */
async function fetchQuestions(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    questions.value = await getQuestions()
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : String(error)
  } finally {
    loading.value = false
  }
}

onMounted(fetchQuestions)

// 表单内容变化时写入 localStorage，刷新或误关页面都不丢草稿
watch(formData, (value) => {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(value))
})

/** 接收子组件提交的数据并落盘 */
function handleSubmit(data: Record<string, string>): void {
  Object.assign(formData, data)
  localStorage.setItem(STORAGE_KEY, JSON.stringify(formData))
  savedTip.value = '表单已保存到本地'
  window.setTimeout(() => {
    savedTip.value = ''
  }, 2000)
}
</script>

<template>
  <div class="min-h-screen bg-gradient-to-b from-sky-50 via-slate-50 to-slate-100 text-slate-900">
    <!-- 顶部导航栏 -->
    <header class="sticky top-0 z-10 border-b border-slate-200/70 bg-white/80 backdrop-blur">
      <div class="mx-auto flex max-w-3xl items-center justify-between px-4 py-3">
        <div class="flex items-center gap-3">
          <div
            class="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-primary-500 to-sky-600 text-sm font-bold text-white shadow-md shadow-sky-500/30"
          >
            H
          </div>
          <div>
            <div class="text-sm font-bold leading-tight text-slate-900">HarnessPRD</div>
            <div class="text-xs leading-tight text-slate-500">需求澄清 · 生成 PRD / 接口文档 / 提示词</div>
          </div>
        </div>

        <!-- 草稿保存状态徽章，自动淡出 -->
        <Transition
          enter-active-class="transition duration-200"
          enter-from-class="opacity-0 translate-y-1"
          leave-active-class="transition duration-500"
          leave-to-class="opacity-0"
        >
          <span
            v-if="savedTip"
            class="flex items-center gap-1 rounded-full bg-emerald-50 px-3 py-1 text-xs font-medium text-emerald-600 ring-1 ring-emerald-200"
          >
            <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path
                fill-rule="evenodd"
                d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm3.857-9.809a.75.75 0 0 0-1.214-.882l-3.483 4.79-1.88-1.88a.75.75 0 1 0-1.06 1.061l2.5 2.5a.75.75 0 0 0 1.137-.089l4-5.5Z"
                clip-rule="evenodd"
              />
            </svg>
            {{ savedTip }}
          </span>
        </Transition>
      </div>
    </header>

    <main class="mx-auto max-w-3xl px-4 py-10">
      <!-- 页面标题区 -->
      <div class="mb-8">
        <h1 class="text-2xl font-bold tracking-tight text-slate-900 sm:text-3xl">
          告诉我们你的产品想法
        </h1>
        <p class="mt-2 max-w-xl text-sm leading-relaxed text-slate-600">
          花几分钟填写下面的问卷，AI 会据此生成
          <span class="font-medium text-slate-800">PRD</span>、
          <span class="font-medium text-slate-800">接口文档</span>与
          <span class="font-medium text-slate-800">提示词文档</span>。
          <template v-if="hasDraft">已为你恢复上次填写的草稿，可以继续作答。</template>
        </p>
      </div>

      <!-- 加载中：骨架屏 -->
      <div v-if="loading" class="space-y-6" aria-busy="true" aria-label="正在加载问卷">
        <div class="rounded-2xl border border-slate-200/80 bg-white p-6 shadow-sm">
          <div class="h-4 w-28 animate-pulse rounded bg-slate-200"></div>
          <div class="mt-6 space-y-5">
            <div v-for="i in 3" :key="i" class="space-y-2">
              <div class="h-3.5 w-2/5 animate-pulse rounded bg-slate-100"></div>
              <div class="h-10 w-full animate-pulse rounded-xl bg-slate-100"></div>
            </div>
          </div>
        </div>
      </div>

      <!-- 加载失败，提供重试 -->
      <div v-else-if="loadError" class="rounded-2xl border border-red-200 bg-red-50 p-6 text-center">
        <div class="mx-auto flex h-10 w-10 items-center justify-center rounded-full bg-red-100">
          <svg class="h-5 w-5 text-red-500" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
            <path
              fill-rule="evenodd"
              d="M18 10A8 8 0 1 1 2 10a8 8 0 0 1 16 0Zm-8-4a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z"
              clip-rule="evenodd"
            />
          </svg>
        </div>
        <p class="mt-3 text-sm font-medium text-red-700">问卷加载失败</p>
        <p class="mt-1 text-xs text-red-500">{{ loadError }}（请确认后端服务已启动）</p>
        <button
          type="button"
          class="mt-4 rounded-lg bg-red-500 px-4 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-red-600 focus:outline-none focus:ring-4 focus:ring-red-200"
          @click="fetchQuestions"
        >
          重新加载
        </button>
      </div>

      <!-- 问卷表单，表单数据与 formData 双向绑定 -->
      <FormStep
        v-else-if="questions"
        v-model="formData"
        :questions="questions"
        @submit="handleSubmit"
      />
    </main>

    <footer class="pb-10 text-center text-xs text-slate-400">
      HarnessPRD · 由 AI 驱动的产品文档生成器
    </footer>
  </div>
</template>
