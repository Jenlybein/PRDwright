<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'

import type { QuestionConfig, QuestionsConfig } from '@/types'

/**
 * 需求澄清问卷表单。
 *
 * 只负责「按题目类型渲染控件 + 必填校验 + 数据收集」，不做任何请求，
 * 数据通过 v-model 与父组件双向同步，提交时通过 submit 事件抛出。
 */
const props = defineProps<{
  /** 问卷配置，包含基础题与进阶题两组 */
  questions: QuestionsConfig
}>()

const emit = defineEmits<{
  /** 点击提交且必填校验通过后触发，载荷为表单数据的副本 */
  submit: [formData: Record<string, string>]
}>()

/** 表单数据，key 为 QuestionConfig.name；与父组件通过 v-model 双向绑定 */
const model = defineModel<Record<string, string>>({ default: () => ({}) })

/** 进阶问题是否展开 */
const showAdvanced = ref(false)

/** 校验错误提示，key 为 QuestionConfig.name */
const errors = reactive<Record<string, string>>({})

/** 把两组题目整理成统一的分组结构，便于用同一段模板渲染；no 为标题徽章编号 */
const groups = computed(() => [
  {
    no: 1,
    key: 'base',
    title: '基础问题',
    hint: '建议全部填写，AI 主要依据这些信息生成文档',
    collapsible: false,
    items: props.questions?.base_questions ?? [],
    visible: true,
  },
  {
    no: 2,
    key: 'advanced',
    title: '进阶问题',
    hint: '选填，补充后可以明显提升生成内容的准确度',
    collapsible: true,
    items: props.questions?.advanced_questions ?? [],
    visible: showAdvanced.value,
  },
])

/** 所有题目，用于提交时统一校验 */
const allQuestions = computed<QuestionConfig[]>(() =>
  groups.value.flatMap((group) => group.items),
)

/** 读取某题当前值，未填写时返回空字符串，避免 undefined 参与判空 */
function valueOf(name: string): string {
  return model.value[name] ?? ''
}

/** 清空某题的错误提示（用户开始输入时调用） */
function clearError(name: string): void {
  if (errors[name]) {
    delete errors[name]
  }
}

/** 校验单题必填，返回是否通过 */
function validateOne(question: QuestionConfig): boolean {
  if (question.required && !valueOf(question.name).trim()) {
    errors[question.name] = '此题为必填项'
    return false
  }
  clearError(question.name)
  return true
}

/** 提交：先校验全部必填项，通过后再把数据抛给父组件 */
function handleSubmit(): void {
  const passed = allQuestions.value.map(validateOne).every(Boolean)

  if (!passed) {
    // 错误可能落在折叠的进阶题里，展开以便用户看到
    showAdvanced.value = true
    return
  }

  emit('submit', { ...model.value })
}

// 配置到位后为每题初始化空值，保证下拉框默认停在「请选择」而不是空白项
watch(
  () => props.questions,
  (config) => {
    const items: QuestionConfig[] = [
      ...(config?.base_questions ?? []),
      ...(config?.advanced_questions ?? []),
    ]
    for (const question of items) {
      if (model.value[question.name] === undefined) {
        model.value[question.name] = ''
      }
    }
  },
  { immediate: true, deep: true },
)
</script>

<template>
  <form class="space-y-6" novalidate @submit.prevent="handleSubmit">
    <section
      v-for="group in groups"
      :key="group.key"
      class="rounded-2xl border border-slate-200/80 bg-white shadow-sm shadow-slate-200/50"
    >
      <!-- 分组标题：可折叠组用按钮切换，普通组用静态标题 -->
      <button
        v-if="group.collapsible"
        type="button"
        class="flex w-full items-center justify-between gap-4 rounded-t-2xl px-6 py-5 text-left transition hover:bg-slate-50"
        :aria-expanded="showAdvanced"
        @click="showAdvanced = !showAdvanced"
      >
        <span class="flex items-center gap-3">
          <span
            class="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-primary-100 text-sm font-bold text-primary-700"
          >
            {{ group.no }}
          </span>
          <span>
            <span class="block text-base font-semibold text-slate-900">{{ group.title }}</span>
            <span class="block text-xs text-slate-500">{{ group.hint }}（{{ group.items.length }} 题）</span>
          </span>
        </span>
        <span class="flex shrink-0 items-center gap-1 rounded-full bg-slate-100 px-3 py-1 text-xs font-medium text-slate-600">
          {{ showAdvanced ? '收起' : '展开' }}
          <svg
            class="h-3.5 w-3.5 transition-transform"
            :class="showAdvanced ? 'rotate-180' : ''"
            viewBox="0 0 20 20"
            fill="currentColor"
            aria-hidden="true"
          >
            <path
              fill-rule="evenodd"
              d="M5.23 7.21a.75.75 0 0 1 1.06.02L10 11.17l3.71-3.94a.75.75 0 1 1 1.08 1.04l-4.25 4.5a.75.75 0 0 1-1.08 0l-4.25-4.5a.75.75 0 0 1 .02-1.06Z"
              clip-rule="evenodd"
            />
          </svg>
        </span>
      </button>

      <div v-else class="flex items-center gap-3 px-6 pt-5">
        <span
          class="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-primary-100 text-sm font-bold text-primary-700"
        >
          {{ group.no }}
        </span>
        <span>
          <span class="block text-base font-semibold text-slate-900">{{ group.title }}</span>
          <span class="block text-xs text-slate-500">{{ group.hint }}（{{ group.items.length }} 题）</span>
        </span>
      </div>

      <div v-show="group.visible" class="space-y-6 px-6 pb-6 pt-5">
        <div v-for="question in group.items" :key="question.name">
          <label
            :for="`question-${question.name}`"
            class="mb-2 block text-sm font-medium leading-relaxed text-slate-800"
          >
            {{ question.label }}
            <!-- 必填项用红色星号标记 -->
            <span v-if="question.required" class="ml-0.5 text-red-500" aria-hidden="true">*</span>
          </label>

          <!-- 单行文本 -->
          <input
            v-if="question.type === 'text'"
            :id="`question-${question.name}`"
            v-model="model[question.name]"
            type="text"
            class="w-full rounded-xl border bg-slate-50/50 px-3.5 py-2.5 text-sm text-slate-900 outline-none transition placeholder:text-slate-400 hover:border-slate-300 focus:border-primary-500 focus:bg-white focus:ring-4 focus:ring-primary-100"
            :class="errors[question.name] ? 'border-red-400 focus:border-red-500 focus:ring-red-100' : 'border-slate-200'"
            placeholder="请输入"
            @input="clearError(question.name)"
          />

          <!-- 多行文本 -->
          <textarea
            v-else-if="question.type === 'textarea'"
            :id="`question-${question.name}`"
            v-model="model[question.name]"
            rows="4"
            class="w-full resize-y rounded-xl border bg-slate-50/50 px-3.5 py-2.5 text-sm leading-relaxed text-slate-900 outline-none transition placeholder:text-slate-400 hover:border-slate-300 focus:border-primary-500 focus:bg-white focus:ring-4 focus:ring-primary-100"
            :class="errors[question.name] ? 'border-red-400 focus:border-red-500 focus:ring-red-100' : 'border-slate-200'"
            placeholder="请输入"
            @input="clearError(question.name)"
          />

          <!-- 下拉选择（自定义箭头） -->
          <div v-else-if="question.type === 'select'" class="relative">
            <select
              :id="`question-${question.name}`"
              v-model="model[question.name]"
              class="w-full appearance-none rounded-xl border bg-slate-50/50 px-3.5 py-2.5 pr-10 text-sm text-slate-900 outline-none transition hover:border-slate-300 focus:border-primary-500 focus:bg-white focus:ring-4 focus:ring-primary-100"
              :class="[
                errors[question.name]
                  ? 'border-red-400 focus:border-red-500 focus:ring-red-100'
                  : 'border-slate-200',
                model[question.name] ? '' : 'text-slate-400',
              ]"
              @change="clearError(question.name)"
            >
              <option value="">请选择</option>
              <option v-for="option in question.options" :key="option" :value="option">
                {{ option }}
              </option>
            </select>
            <svg
              class="pointer-events-none absolute right-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400"
              viewBox="0 0 20 20"
              fill="currentColor"
              aria-hidden="true"
            >
              <path
                fill-rule="evenodd"
                d="M5.23 7.21a.75.75 0 0 1 1.06.02L10 11.17l3.71-3.94a.75.75 0 1 1 1.08 1.04l-4.25 4.5a.75.75 0 0 1-1.08 0l-4.25-4.5a.75.75 0 0 1 .02-1.06Z"
                clip-rule="evenodd"
              />
            </svg>
          </div>

          <!-- 单选（卡片式选项，选中高亮） -->
          <div v-else-if="question.type === 'radio'" class="grid gap-2 sm:grid-cols-2">
            <label
              v-for="option in question.options"
              :key="option"
              class="flex cursor-pointer items-start gap-2.5 rounded-xl border border-slate-200 bg-slate-50/50 px-3.5 py-2.5 text-sm text-slate-700 transition hover:border-primary-300 hover:bg-primary-50/50 has-[:checked]:border-primary-500 has-[:checked]:bg-primary-50 has-[:checked]:ring-1 has-[:checked]:ring-primary-500"
            >
              <input
                v-model="model[question.name]"
                type="radio"
                :name="question.name"
                :value="option"
                class="mt-0.5 h-4 w-4 shrink-0 accent-primary-600"
                @change="clearError(question.name)"
              />
              <span>{{ option }}</span>
            </label>
          </div>

          <p v-if="errors[question.name]" class="mt-2 flex items-center gap-1.5 text-xs font-medium text-red-600">
            <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path
                fill-rule="evenodd"
                d="M18 10A8 8 0 1 1 2 10a8 8 0 0 1 16 0Zm-8-4a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z"
                clip-rule="evenodd"
              />
            </svg>
            {{ errors[question.name] }}
          </p>
        </div>
      </div>
    </section>

    <!-- 提交区 -->
    <div class="pt-1">
      <button
        type="submit"
        class="w-full rounded-xl bg-gradient-to-r from-primary-500 to-sky-600 px-4 py-3 text-sm font-semibold text-white shadow-lg shadow-sky-500/25 transition hover:from-primary-600 hover:to-sky-700 hover:shadow-sky-500/35 focus:outline-none focus:ring-4 focus:ring-primary-200 active:scale-[0.99]"
      >
        提交并生成文档
      </button>
      <p class="mt-3 text-center text-xs text-slate-400">
        提交前会自动校验必填项 · 填写内容已实时保存为本地草稿
      </p>
    </div>
  </form>
</template>
