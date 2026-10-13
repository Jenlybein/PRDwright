<script setup lang="ts">
/**
 * 统一输入框：支持 v-model，统一边框 / 聚焦环 / 错误态。
 * 颜色由令牌驱动，label 关联通过透传的 id 完成。
 */
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    modelValue?: string
    type?: string
    placeholder?: string
    disabled?: boolean
    error?: boolean
    id?: string
  }>(),
  { modelValue: '', type: 'text', placeholder: '', disabled: false, error: false, id: undefined },
)

const emit = defineEmits<{ 'update:modelValue': [value: string] }>()

const value = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})
</script>

<template>
  <input
    v-model="value"
    :type="type"
    :placeholder="placeholder"
    :disabled="disabled"
    :id="id"
    class="w-full rounded-xl border bg-white px-3 py-2 text-sm text-slate-900 outline-none transition placeholder:text-slate-400 focus:ring-2 disabled:bg-slate-50 disabled:text-slate-400 dark:bg-slate-800 dark:text-slate-100 dark:disabled:bg-slate-900"
    :class="
      error
        ? 'border-danger-300 focus:border-danger-400 focus:ring-danger-100'
        : 'border-slate-300 focus:border-primary-400 focus:ring-primary-100 dark:border-slate-700'
    "
  />
</template>
