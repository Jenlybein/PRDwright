<script setup lang="ts">
/**
 * MVP 功能行编辑器。
 *
 * 抽出自 RequirementsForm 内联的 `type === 'mvp'` 分支。组件自包含地管理一组功能行：
 * 逐项填写「功能名称 / 描述 / 价值」、增删行、批量粘贴导入；子字段错误键形如
 * `mvp_features.0.name`，与抽出前完全一致，聚焦定位与测试选择器不受任何影响。
 *
 * 数据通过 v-model 整体上行（每次变更 emit 新数组），错误展示读父组件传入的
 * `errors`，需要清错时调用父组件 `clearError`，批量导入委托给父组件 `importFeatures`
 * （它负责解析、回填、提示与清错）。
 */
import { nextTick, ref } from 'vue'

import type { MvpFeatureItem } from './types'
import { rowControlClass, rowErrorKey } from './fieldStyles'

const PREFIX = 'mvp_features'

const props = defineProps<{
  /** 当前功能行数组 */
  modelValue: MvpFeatureItem[]
  /** 父组件共享的校验错误表（只读，用于展示 `PREFIX.{i}.{子字段}` 级错误） */
  errors: Record<string, string>
  /** 清除指定错误键（父组件提供，避免子组件直接改共享状态） */
  clearError: (key: string) => void
  /** 批量粘贴导入：接收多行文本，由父组件解析后追加并提示 */
  importFeatures: (text: string) => void
  /** 编辑器容器的 id（对应原内联渲染时的 `field-mvp_features`），供测试与滚动定位复用 */
  fieldId: string
}>()

const emit = defineEmits<{
  /** 功能行数组变化，载荷为新数组 */
 'update:modelValue': [value: MvpFeatureItem[]]
}>()

/** 批量粘贴导入面板是否展开 */
const showPaste = ref(false)
/** 批量粘贴的原始文本 */
const pasteText = ref('')

/** 子字段错误键 */
function errorKey(index: number, sub: 'name' | 'description' | 'value'): string {
  return rowErrorKey(PREFIX, index, sub)
}

/** 整行更新某子字段后上行新数组，并清掉该子字段的校验错误 */
function updateCell(index: number, sub: 'name' | 'description' | 'value', value: string): void {
  props.clearError(errorKey(index, sub))
  emit(
 'update:modelValue',
    props.modelValue.map((item, i) => (i === index ? { ...item, [sub]: value } : item)),
  )
}

/** 添加一行空白功能，并聚焦新行的功能名称输入框 */
async function add(): Promise<void> {
  emit('update:modelValue', [...props.modelValue, { name: '', description: '', value: '' }])
  const newIndex = props.modelValue.length
  await nextTick()
  document.getElementById(`field-${errorKey(newIndex, 'name')}`)?.focus()
}

/** 删除指定下标的功能行 */
function remove(index: number): void {
  emit(
 'update:modelValue',
    props.modelValue.filter((_, i) => i !== index),
  )
  props.clearError(PREFIX)
}

/** 执行批量粘贴导入：委托父组件解析，并收起面板 */
function runImport(): void {
  props.importFeatures(pasteText.value)
  pasteText.value = ''
  showPaste.value = false
}
</script>

<template>
  <div :id="fieldId" class="space-y-2.5" role="group" aria-label="MVP 功能">
    <!-- 空状态引导 -->
    <div
      v-if="modelValue.length === 0"
      class="rounded-xl border border-dashed border-line-strong bg-surface-2 px-4 py-6 text-center text-xs text-subtle  /40"
    >
      还没有功能项，点击下方「添加功能」逐项填写
    </div>

    <!-- 功能行卡片 -->
    <div
      v-for="(item, index) in modelValue"
      :key="index"
      class="rounded-xl border border-line bg-surface-2/40 p-3.5 /70 /40"
    >
      <div class="mb-2.5 flex items-center justify-between">
        <span class="text-xs font-semibold text-subtle">功能 {{ index + 1 }}</span>
        <button
          type="button"
          title="删除该功能"
          class="rounded-md px-2 py-1 text-xs font-medium text-subtle transition hover:bg-red-50 hover:text-red-600 dark:hover:bg-red-950/40"
          @click="remove(index)"
        >
          删除
        </button>
      </div>

      <div class="space-y-2.5">
        <div>
          <label :for="`field-${errorKey(index, 'name')}`" class="mb-1.5 block text-xs font-medium text-muted ">
            功能名称
            <span class="text-red-500" aria-hidden="true">*</span>
          </label>
          <input
            :id="`field-${errorKey(index, 'name')}`"
            :value="item.name"
            type="text"
            :class="rowControlClass(Boolean(errors[errorKey(index, 'name')]))"
            placeholder="例如：问卷填写"
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

        <div>
          <label :for="`field-${errorKey(index, 'description')}`" class="mb-1.5 block text-xs font-medium text-muted ">
            功能描述
            <span class="text-red-500" aria-hidden="true">*</span>
          </label>
          <input
            :id="`field-${errorKey(index, 'description')}`"
            :value="item.description"
            type="text"
            :class="rowControlClass(Boolean(errors[errorKey(index, 'description')]))"
            placeholder="例如：分基础与进阶两组收集产品信息"
            @input="updateCell(index, 'description', ($event.target as HTMLInputElement).value)"
          />
          <p
            v-if="errors[errorKey(index, 'description')]"
            class="mt-1.5 flex items-start gap-1.5 text-xs font-medium text-red-600 dark:text-red-400"
          >
            <svg class="mt-0.5 h-3.5 w-3.5 shrink-0" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path
                fill-rule="evenodd"
                d="M18 10A8 8 0 1 1 2 10a8 8 0 0 1 16 0Zm-8-4a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z"
                clip-rule="evenodd"
              />
            </svg>
            <span>{{ errors[errorKey(index, 'description')] }}</span>
          </p>
        </div>

        <div>
          <label :for="`field-${errorKey(index, 'value')}`" class="mb-1.5 block text-xs font-medium text-muted ">
            价值（选填）
          </label>
          <input
            :id="`field-${errorKey(index, 'value')}`"
            :value="item.value"
            type="text"
            :class="rowControlClass(false)"
            placeholder="例如：降低填写门槛"
            @input="updateCell(index, 'value', ($event.target as HTMLInputElement).value)"
          />
        </div>
      </div>
    </div>

    <!-- 添加功能行 -->
    <button
      type="button"
      class="w-full rounded-xl border border-dashed border-line-strong px-4 py-2.5 text-xs font-medium text-subtle transition hover:border-primary-400 hover:text-primary-600   dark:hover:border-primary-500 dark:hover:text-primary-400"
      @click="add"
    >
      ＋ 添加功能
    </button>

    <!-- 批量粘贴导入（可选）：兼容「功能名｜描述｜价值」旧格式文本 -->
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
          placeholder="每行一个功能：功能名｜描述｜价值，例如：&#10;问卷填写｜分基础与进阶两组收集产品信息｜降低填写门槛"
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
