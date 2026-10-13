<script setup lang="ts">
/**
 * 统一按钮：吞掉此前散落在各页面的主/次按钮样式，消除 12+ 处重复与 padding 不一致。
 * - variant：primary（品牌主操作）/ secondary（描边次操作）/ ghost（轻量）
 * - 原生 click 通过属性透传落到根 <button>，消费方直接 @click 即可。
 *
 * 颜色只用语义令牌（bg-surface / text-body / border-line-strong …），
 * 于是「暗色下按钮文字与底色糊在一起」这类问题只需在 index.css 修一处。
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
    class="inline-flex items-center justify-center gap-2 rounded-control font-medium transition-[background-color,box-shadow,transform,color] duration-150 ease-smooth focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-1 focus-visible:ring-offset-bg disabled:cursor-not-allowed disabled:opacity-50"
    :class="[
      variant === 'primary'
        ? 'bg-primary-600 text-on-accent shadow-sm hover:bg-primary-700 active:scale-[0.99] disabled:bg-surface-2 disabled:text-subtle disabled:shadow-none'
        : '',
      variant === 'secondary'
        ? 'border border-line-strong bg-surface text-body hover:border-accent hover:text-accent disabled:opacity-50'
        : '',
      variant === 'ghost'
        ? 'text-body hover:bg-surface-2 hover:text-accent disabled:opacity-40'
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
