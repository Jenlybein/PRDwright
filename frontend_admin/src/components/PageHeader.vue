<script setup lang="ts">
/**
 * 页面报头：所有后台页共用的标题区。
 *
 * 为什么它**不是**一张卡
 * ----------------------
 * 之前每个页面的页头也是一张 `brut-card-lg`：白底、硬边、6px 偏移投影，
 * 和下面真正的内容卡一模一样。结果整页变成一串同重量、同尺寸的白盒子，
 * 眼睛没有落点——这就是「看着累」的来源。
 *
 * 现在页头是一条**实色表头带**：只有底色与下沿一条 3px 线，上、左、右都不描边，
 * 也没有投影。层级由三件分开的信号给出——底色（带 vs 白）、材质（无投影 vs 有投影）、
 * 边框（无框 vs 硬框），眼睛一眼就能分出「上面是标题、下面是内容」。
 * 同时满页的黑框少掉一整圈：边框太多正是「看着累」的主因。
 *
 * ⚠️ 亮色主题用墨色、暗色主题用天蓝（`.dark .page-head` 覆盖）。天蓝在这套体系里
 * 只表示「强调」，不表示「结构」——亮色若也用天蓝，会与内容里的天蓝强调块
 * （如配置页「当前供应商」）连成一片，反而分不出层次。
 *
 * 说明文字的取舍
 * --------------
 * 页头里的说明常常有三五句（口径、边界、为什么不这么做）。印成正文会和内容抢读，
 * 所以统一收进 `desc` 槽：左侧一条竖线、字号降一档、略降透明度。
 * 它成了「注解」，读不读都不影响操作。
 */
withDefaults(
  defineProps<{
    /** 左上角的小字：栏目 / 用途，例如「Jobs / 现场」 */
    kicker: string
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
        <p class="page-head-kicker">{{ kicker }}</p>
        <h1 class="page-title mt-2.5">{{ title }}</h1>
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
