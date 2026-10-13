<script setup lang="ts">
/**
 * 通用字段渲染器：接管 input / textarea / select / list 四类字段。
 *
 * 此前这些字段的渲染模板在 RequirementsForm 内部重复出现，抽成组件后父表单只需
 * 一行 `<FormFieldRenderer v-model=... :field=... :error=... />`。
 *
 * 数据通过 v-model 双向同步；列表类字段在组件内部维护数组的新副本后整体 emit，
 * 不做「就地改父组件数组」的跨组件突变，父组件在 update 时顺手清掉字段级错误。
 */
import { computed, nextTick } from 'vue'

import type { FieldConfig } from './types'
import { controlClass, rowControlClass } from './fieldStyles'

const props = defineProps<{
  /** 字段渲染配置 */
  field: FieldConfig
  /** 当前值：input/textarea/select 为 string，list 为 string[] */
  modelValue: string | string[]
  /** 字段级错误信息（用于红框与聚焦环），无错误时为空 */
  error?: string
}>()

const emit = defineEmits<{
  /** 值变化（任意子控件输入、列表增删等），载荷为新值 */
 'update:modelValue': [value: string | string[]]
}>()

/** 列表类字段的数组视图（便于模板遍历与类型收窄） */
const items = computed<string[]>(() => (Array.isArray(props.modelValue) ? props.modelValue : []))

/** 是否处于错误态 */
const hasError = computed(() => Boolean(props.error))

/** 列表：追加一个空项并把焦点交给新行 */
async function addItem(): Promise<void> {
  const next = [...items.value, '']
  emit('update:modelValue', next)
  const newIndex = next.length - 1
  await nextTick()
  document.getElementById(`field-${props.field.key}.${newIndex}`)?.focus()
}

/** 列表：删除指定下标 */
function removeItem(index: number): void {
  emit(
 'update:modelValue',
    items.value.filter((_, i) => i !== index),
  )
}

/** 列表：回写指定下标的内容 */
function setItem(index: number, value: string): void {
  emit(
 'update:modelValue',
    items.value.map((item, i) => (i === index ? value : item)),
  )
}

/**
 * 列表：行内粘贴多行文本，自动拆成多条；单行粘贴不拦截，交给浏览器默认行为。
 */
function handlePaste(event: ClipboardEvent): void {
  const text = event.clipboardData?.getData('text') ?? ''
  if (!text || !/[\r\n]/.test(text)) return
  event.preventDefault()
  const parts = text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
  if (parts.length === 0) return
  emit('update:modelValue', [...items.value, ...parts])
}
</script>

<template>
  <div>
    <!-- 单行文本（含输入建议，仍可自由填写） -->
    <template v-if="field.type === 'input'">
      <input
        :id="`field-${field.key}`"
        :value="(modelValue as string)"
        type="text"
        :class="controlClass(hasError)"
        :placeholder="field.placeholder"
        :list="field.suggestions ? `${field.key}-suggestions` : undefined"
        @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)"
      />
      <!-- 常见选项建议：点击选择或直接输入自定义值 -->
      <datalist v-if="field.suggestions" :id="`${field.key}-suggestions`">
        <option v-for="option in field.suggestions" :key="option" :value="option" />
      </datalist>
    </template>

    <!-- 多行文本 -->
    <textarea
      v-else-if="field.type === 'textarea'"
      :id="`field-${field.key}`"
      :value="(modelValue as string)"
      :rows="field.rows ?? 4"
      :class="controlClass(hasError)"
      :placeholder="field.placeholder"
      @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)"
    ></textarea>

    <!-- 下拉选择（自定义箭头） -->
    <div v-else-if="field.type === 'select'" class="relative">
      <select
        :id="`field-${field.key}`"
        :value="(modelValue as string)"
        :class="controlClass(hasError) + ' appearance-none pr-10'"
        @change="emit('update:modelValue', ($event.target as HTMLSelectElement).value)"
      >
        <option value="">{{ field.placeholder }}</option>
        <option v-for="option in field.options ?? []" :key="option" :value="option">
          {{ option }}
        </option>
      </select>
      <svg
        class="pointer-events-none absolute right-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-subtle"
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

    <!--
      字符串列表编辑器（目标用户 / 未来规划 / 关键交互 / 范围）。
      一条一行、逐条添加，用户不需要关心换行或分隔符；粘贴多行会自动拆分。
    -->
    <div
      v-else-if="field.type === 'list'"
      :id="`field-${field.key}`"
      class="space-y-2"
      role="group"
      :aria-label="field.label"
    >
      <p
        v-if="items.length === 0"
        class="rounded-xl border border-dashed border-line-strong bg-surface-2 px-4 py-5 text-center text-xs text-subtle"
      >
        {{ field.emptyText }}
      </p>

      <div v-for="(item, index) in items" :key="index" class="flex items-center gap-2">
        <span class="w-5 shrink-0 text-center text-xs font-medium text-subtle">
          {{ index + 1 }}
        </span>
        <input
          :id="`field-${field.key}.${index}`"
          :value="item"
          type="text"
          :class="rowControlClass(hasError)"
          :placeholder="field.itemPlaceholder"
          @input="setItem(index, ($event.target as HTMLInputElement).value)"
          @paste="handlePaste"
        />
        <button
          type="button"
          title="删除该条"
          class="shrink-0 rounded-md px-2 py-1 text-xs font-medium text-subtle transition hover:bg-red-50 hover:text-red-600 dark:hover:bg-red-950/40"
          @click="removeItem(index)"
        >
          删除
        </button>
      </div>

      <button
        type="button"
        class="w-full rounded-xl border border-dashed border-line-strong px-4 py-2.5 text-xs font-medium text-subtle transition hover:border-primary-400 hover:text-primary-600   dark:hover:border-primary-500 dark:hover:text-primary-400"
        @click="addItem"
      >
        ＋ {{ field.addLabel }}
      </button>
    </div>
  </div>
</template>
