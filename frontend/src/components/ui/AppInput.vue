<script setup lang="ts">
/**
 * 统一输入框：支持 v-model，统一边框 / 聚焦环 / 错误态。
 * 颜色由语义令牌驱动，label 关联通过透传的 id 完成。
 *
 * 聚焦可见性由「边框」承担（accent 在两种主题下都 >= 4.5:1），
 * ring 只作为柔和光晕；错误态的边框用 --ui-danger 实体色，不依赖浅色描边。
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
    class="w-full rounded-control border bg-field px-3 py-2 text-sm text-fg outline-none transition placeholder:text-subtle focus:ring-2 disabled:bg-surface-2 disabled:text-subtle"
    :class="
      error
        ? 'border-danger focus:border-danger focus:ring-danger-line'
        : 'border-line-strong focus:border-accent focus:ring-accent-soft-line'
    "
  />
</template>
