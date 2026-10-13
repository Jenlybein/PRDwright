<script setup lang="ts">
/**
 * 统一卡片容器。interactive=true 时渲染为 <button>，保留键盘可达性（卡片整体可点），
 * 用于入口选择等场景；默认渲染 <div>。
 *
 * 需要一提的是 `dark:shadow-none`：阴影本身就是「浅色底上做层级」的手段，
 * 深色底上投影几乎不可见、只是把边界弄脏，所以这里保留一处 dark 变体
 * —— 语义令牌管颜色，明暗差异里「有没有阴影」这类结构判断仍归变体管。
 */
withDefaults(
  defineProps<{
    interactive?: boolean
    padded?: boolean
  }>(),
  { interactive: false, padded: true },
)
</script>

<template>
  <component
    :is="interactive ? 'button' : 'div'"
    :type="interactive ? 'button' : undefined"
    class="block w-full rounded-2xl border border-line bg-surface text-left shadow-card transition dark:shadow-none"
    :class="[
      padded ? 'p-5' : '',
      interactive
        ? 'cursor-pointer hover:border-accent hover:shadow-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent'
        : '',
    ]"
  >
    <slot />
  </component>
</template>
