<script setup lang="ts">
/**
 * 统一按钮：吞掉此前散落在各页面的主/次按钮样式，消除 12+ 处重复与 padding 不一致。
 * - variant：primary（品牌渐变主操作）/ secondary（描边次操作）/ ghost（轻量）
 * - 原生 click 通过属性透传落到根 <button>，消费方直接 @click 即可。
 */
import AppSpinner from './AppSpinner.vue'

withDefaults(
  defineProps<{
    variant?: 'primary' | 'secondary' | 'ghost'
    size?: 'sm' | 'md' | 'lg'
    type?: 'button' | 'submit' | 'reset'
    block?: boolean
    disabled?: boolean
    loading?: boolean
  }>(),
  { variant: 'secondary', size: 'md', type: 'button', block: false, disabled: false, loading: false },
)
</script>

<template>
  <button
    :type="type"
    :disabled="disabled || loading"
    :aria-busy="loading || undefined"
    class="inline-flex items-center justify-center gap-2 rounded-xl font-medium transition-[background-color,box-shadow,transform,color] duration-150 ease-[cubic-bezier(0.16,1,0.3,1)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-300 focus-visible:ring-offset-1 focus-visible:ring-offset-white dark:focus-visible:ring-offset-slate-950 disabled:cursor-not-allowed disabled:opacity-50"
    :class="[
      variant === 'primary'
        ? 'bg-primary-600 text-white shadow-sm hover:bg-primary-700 dark:hover:bg-primary-500 active:scale-[0.99] disabled:bg-slate-300 disabled:text-slate-400 disabled:shadow-none'
        : '',
      variant === 'secondary'
        ? 'border border-slate-300 bg-white text-slate-600 hover:border-primary-400 hover:text-primary-700 disabled:opacity-50 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300 dark:hover:border-primary-400 dark:hover:text-primary-300'
        : '',
      variant === 'ghost'
        ? 'text-slate-600 hover:bg-slate-100 hover:text-primary-700 disabled:opacity-40 dark:text-slate-300 dark:hover:bg-slate-800 dark:hover:text-primary-300'
        : '',
      size === 'sm' ? 'px-3 py-1.5 text-xs' : '',
      size === 'md' ? 'px-4 py-2 text-sm' : '',
      size === 'lg' ? 'px-5 py-2.5 text-sm' : '',
      block ? 'w-full' : '',
    ]"
  >
    <AppSpinner v-if="loading" size="sm" />
    <slot />
  </button>
</template>
