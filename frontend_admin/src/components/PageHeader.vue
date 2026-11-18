<script setup lang="ts">
/**
 * 页面报头：所有后台页共用的标题区。
 *
 * 只有标题与页面级动作，**没有栏目名小字**。栏目名与顶栏标题重复，
 * 多一行字只是把眼睛往上看一次，不增加任何信息。
 */
withDefaults(
  defineProps<{
    /** 页面大标题 */
    title: string
  }>(),
  {},
)
</script>

<template>
  <header class="page-head">
    <div class="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
      <div class="min-w-0">
        <h1 class="page-title">{{ title }}</h1>
      </div>

      <!-- 页面级动作（刷新 / 新建 / 计数标签）永远在这一行的右下，与标题同基线 -->
      <div v-if="$slots.actions" class="flex flex-wrap items-center gap-2 pb-0.5">
        <slot name="actions" />
      </div>
    </div>

    <div v-if="$slots.desc" class="page-head-desc">
      <slot name="desc" />
    </div>
  </header>
</template>
