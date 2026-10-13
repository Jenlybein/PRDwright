<script setup lang="ts">
/**
 * 页面结构卡片编辑器。
 *
 * 抽出自 RequirementsForm 内联的 `type === 'pages'` 分支。组件渲染页面卡片与模块标签，
 * 但「模块」的增删 / 键盘确认 / 粘贴解析 / 批量导入这几种会改变 `form.ui_pages` 内部
 * 结构的操作，仍由父组件传入的函数（addModule / removeModule / onModuleKeydown /
 * handleModulePaste / importPages）执行——因为这些操作参与了父组件的「提交前草稿入库」
 * 校验流程，必须保持单一权威。子字段错误键形如 `ui_pages.0.name`，与原实现一致。
 *
 * 卡片的其它字段（页面名 / 备注 / moduleDraft）通过 v-model 整体上行。
 */
import { nextTick, ref } from 'vue'

import type { UiPageItem } from './types'
import { rowControlClass, rowErrorKey } from './fieldStyles'

const PREFIX = 'ui_pages'

const props = defineProps<{
  /** 当前页面卡片数组 */
  modelValue: UiPageItem[]
  /** 父组件共享的校验错误表（只读，用于展示 `PREFIX.{i}.{子字段}` 级错误） */
  errors: Record<string, string>
  /** 清除指定错误键（父组件提供，避免子组件直接改共享状态） */
  clearError: (key: string) => void
  /** 把某张卡片模块草稿确认为标签（父组件权威操作） */
  addModule: (index: number) => void
  /** 删除某个模块标签（父组件权威操作） */
  removeModule: (index: number, moduleIndex: number) => void
  /** 模块输入框键盘事件（回车 / 逗号即确认） */
  onModuleKeydown: (event: KeyboardEvent, index: number) => void
  /** 模块输入框内粘贴：多行 / 逗号文本拆成多个标签 */
  handleModulePaste: (event: ClipboardEvent, index: number) => void
  /** 批量粘贴导入：接收多行文本，由父组件解析后追加并提示 */
  importPages: (text: string) => void
  /** 编辑器容器的 id（对应原内联渲染时的 `field-ui_pages`），供测试与滚动定位复用 */
  fieldId: string
}>()

const emit = defineEmits<{
  /** 页面卡片数组变化，载荷为新数组 */
 'update:modelValue': [value: UiPageItem[]]
}>()

/** 批量粘贴导入面板是否展开 */
const showPaste = ref(false)
/** 批量粘贴的原始文本 */
const pasteText = ref('')

/** 子字段错误键 */
function errorKey(index: number, sub: 'name' | 'modules' | 'notes'): string {
  return rowErrorKey(PREFIX, index, sub)
}

/** 整张卡更新某字段后上行新数组，并清掉该字段的校验错误 */
function updateCell(index: number, sub: 'name' | 'notes' | 'moduleDraft', value: string): void {
  // moduleDraft 的报错挂在 `modules` 键上，由输入框的 inline handler 清除；
  // 这里只清 name / notes 两个真正的错误键。
  if (sub === 'name' || sub === 'notes') props.clearError(errorKey(index, sub))
  emit(
 'update:modelValue',
    props.modelValue.map((page, i) => (i === index ? { ...page, [sub]: value } : page)),
  )
}

/** 添加一张空白页面卡片，并聚焦页面名称输入框 */
async function add(): Promise<void> {
  emit('update:modelValue', [
    ...props.modelValue,
    { name: '', modules: [], notes: '', moduleDraft: '' },
  ])
  const newIndex = props.modelValue.length
  await nextTick()
  document.getElementById(`field-${errorKey(newIndex, 'name')}`)?.focus()
}

/** 删除指定下标的页面卡片 */
function remove(index: number): void {
  emit(
 'update:modelValue',
    props.modelValue.filter((_, i) => i !== index),
  )
  props.clearError(PREFIX)
}

/** 执行批量粘贴导入：委托父组件解析，并收起面板 */
function runImport(): void {
  props.importPages(pasteText.value)
  pasteText.value = ''
  showPaste.value = false
}
</script>

<template>
  <div :id="fieldId" class="space-y-3" role="group" aria-label="页面结构">
    <!-- 空状态引导 -->
    <div
      v-if="modelValue.length === 0"
      class="rounded-xl border border-dashed border-line-strong bg-surface-2 px-4 py-6 text-center text-xs text-subtle"
    >
      还没有页面，点击下方「添加页面」逐张填写
    </div>

    <!-- 页面卡片 -->
    <div
      v-for="(page, index) in modelValue"
      :key="index"
      class="rounded-xl border border-line bg-surface-2/40 p-3.5"
    >
      <div class="mb-2.5 flex items-center justify-between">
        <span class="text-xs font-semibold text-subtle">页面 {{ index + 1 }}</span>
        <button
          type="button"
          title="删除该页面"
          class="rounded-md px-2 py-1 text-xs font-medium text-subtle transition hover:bg-red-50 hover:text-red-600 dark:hover:bg-red-950/40"
          @click="remove(index)"
        >
          删除
        </button>
      </div>

      <div class="space-y-2.5">
        <!-- 页面名称 -->
        <div>
          <label :for="`field-${errorKey(index, 'name')}`" class="mb-1.5 block text-xs font-medium text-muted ">
            页面名称
            <span class="text-red-500" aria-hidden="true">*</span>
          </label>
          <input
            :id="`field-${errorKey(index, 'name')}`"
            :value="page.name"
            type="text"
            :class="rowControlClass(Boolean(errors[errorKey(index, 'name')]))"
            placeholder="例如：首页"
            @input="updateCell(index, 'name', ($event.target as HTMLInputElement).value)"
          />
          <p
            v-if="errors[errorKey(index, 'name')]"
            class="mt-1.5 flex items-start gap-1.5 text-xs font-medium text-red-600 dark:text-red-400"
          >
            <svg class="mt-0.5 h-3.5 w-3.5 shrink-0" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path
                fill-rule="evenodd"
                d="M18 10A8 8 0 1 1 2 10a8 8 0 0 1 16 0Zm-8-4a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z"
                clip-rule="evenodd"
              />
            </svg>
            <span>{{ errors[errorKey(index, 'name')] }}</span>
          </p>
        </div>

        <!-- 模块标签 -->
        <div>
          <label :for="`field-${errorKey(index, 'modules')}`" class="mb-1.5 block text-xs font-medium text-muted ">
            模块 / 区块
            <span class="text-red-500" aria-hidden="true">*</span>
            <span class="ml-1 font-normal text-subtle">回车或逗号确认一个模块</span>
          </label>

          <!-- 已确认的模块标签 -->
          <div v-if="page.modules.length > 0" class="mb-2 flex flex-wrap gap-1.5">
            <span
              v-for="(module, moduleIndex) in page.modules"
              :key="`${module}-${moduleIndex}`"
              class="inline-flex items-center gap-1 rounded-lg bg-primary-50 px-2.5 py-1 text-xs font-medium text-primary-700 ring-1 ring-primary-100 dark:bg-primary-950/40 dark:text-primary-300 dark:ring-primary-800/60"
            >
              {{ module }}
              <button
                type="button"
                :title="`移除模块：${module}`"
                class="text-primary-400 transition hover:text-red-600 dark:text-primary-300/70 dark:hover:text-red-400"
                @click="removeModule(index, moduleIndex)"
              >
                ×
              </button>
            </span>
          </div>

          <div class="flex items-center gap-2">
            <input
              :id="`field-${errorKey(index, 'modules')}`"
              :value="page.moduleDraft"
              type="text"
              :class="rowControlClass(Boolean(errors[errorKey(index, 'modules')]))"
              placeholder="例如：产品介绍（可连续输入，用逗号分隔）"
              @input="updateCell(index, 'moduleDraft', ($event.target as HTMLInputElement).value); clearError(errorKey(index, 'modules'))"
              @keydown="onModuleKeydown($event, index)"
              @paste="handleModulePaste($event, index)"
            />
            <button
              type="button"
              class="shrink-0 rounded-lg border border-line-strong bg-surface px-3 py-2 text-xs font-medium text-muted transition hover:border-primary-400 hover:text-accent     "
              @click="addModule(index)"
            >
              添加模块
            </button>
          </div>
          <p
            v-if="errors[errorKey(index, 'modules')]"
            class="mt-1.5 flex items-start gap-1.5 text-xs font-medium text-red-600 dark:text-red-400"
          >
            <svg class="mt-0.5 h-3.5 w-3.5 shrink-0" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path
                fill-rule="evenodd"
                d="M18 10A8 8 0 1 1 2 10a8 8 0 0 1 16 0Zm-8-4a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z"
                clip-rule="evenodd"
              />
            </svg>
            <span>{{ errors[errorKey(index, 'modules')] }}</span>
          </p>
        </div>

        <!-- 备注 -->
        <div>
          <label :for="`field-${errorKey(index, 'notes')}`" class="mb-1.5 block text-xs font-medium text-muted ">
            备注（选填）
          </label>
          <input
            :id="`field-${errorKey(index, 'notes')}`"
            :value="page.notes"
            type="text"
            :class="rowControlClass(false)"
            placeholder="例如：面向新用户"
            @input="updateCell(index, 'notes', ($event.target as HTMLInputElement).value)"
          />
        </div>
      </div>
    </div>

    <!-- 添加页面卡片 -->
    <button
      type="button"
      class="w-full rounded-xl border border-dashed border-line-strong px-4 py-2.5 text-xs font-medium text-subtle transition hover:border-primary-400 hover:text-primary-600   dark:hover:border-primary-500 dark:hover:text-primary-400"
      @click="add"
    >
      ＋ 添加页面
    </button>

    <!-- 批量粘贴导入（可选）：兼容「页面名｜模块1,模块2｜备注」旧格式文本 -->
    <div class="pt-0.5">
      <button
        type="button"
        class="text-xs text-subtle underline-offset-2 transition hover:text-accent hover:underline"
        @click="showPaste = !showPaste"
      >
        {{ showPaste ? '收起批量粘贴' : '批量粘贴导入（可选）' }}
      </button>
      <div v-if="showPaste" class="mt-2 space-y-2">
        <textarea
          v-model="pasteText"
          rows="3"
          class="w-full resize-y rounded-xl border border-line bg-surface-2 px-3.5 py-2.5 text-sm leading-relaxed text-fg outline-none transition placeholder:text-subtle hover:border-line-strong focus:border-accent focus:bg-surface focus:ring-4 focus:ring-accent-soft-line      "
          placeholder="每行一个页面：页面名｜模块1,模块2｜备注，例如：&#10;首页｜产品介绍,快速开始｜面向新用户"
        ></textarea>
        <button
          type="button"
          class="rounded-lg border border-line-strong bg-surface px-3 py-1.5 text-xs font-medium text-muted shadow-sm transition hover:border-primary-400 hover:text-accent disabled:cursor-not-allowed disabled:opacity-50     "
          :disabled="!pasteText.trim()"
          @click="runImport"
        >
          解析并导入
        </button>
      </div>
    </div>
  </div>
</template>
