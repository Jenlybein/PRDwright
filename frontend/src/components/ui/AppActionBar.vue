<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'

/**
 * 页面级操作栏。
 *
 * 全站**唯一**的流程操作落点：每个阶段的主操作与次操作都收在这一条栏里，位置与顺序固定。
 * 此前各屏各自摆放——主操作时而右对齐、时而整宽居中，次操作时而底部左侧、时而卡片头部右上，
 * 「清空 / 重新开始」这类操作更是四屏四种位置——用户每换一屏都要重新找「下一步」在哪。
 *
 * ## 两行结构
 *
 * ```
 *   ┌─ 内容行（可选，有 #tools 插槽时才出现）──────────────────────┐
 *   │  [⚡ AI 优化] [⧉ 复制]                        [ 下载 ]      │
 *   ├─ 流程行（永远存在）─────────────────────────────────────────┤
 *   │  [返回对话]  说明文字 ………………  [生成接口文档]  [ 完成 ]      │
 *   └────────────────────────────────────────────────────────────┘
 * ```
 *
 * - **内容行**（`#tools`）：对「当前这份内容」做的事——文档工具（AI 优化 / 复制 / 下载）、
 *   对话输入框（composer）。它用 `bg-surface-2/60` 微沉并以下方一条细线与流程行分隔，
 *   视觉层级低于流程行：**先做完内容，再决定流程往哪走**。
 *   ⚠️ 这一行**没有「保存」**：手改正文靠「编辑即防抖落库」自动写回后端，不需要用户记得点一下。
 * - **流程行**：三段式，顺序不可变——
 *   `[破坏性操作] │ [返回 / 次操作] [说明文字]  ………  [主操作（永远最右且唯一）]`。
 *
 * 之所以不把二者混成一行：审阅页一行要塞 6 个按钮（3 个工具 + 3 个流程），窄屏必然换行失控；
 * 对话页更是要塞下一个输入框，物理上放不下。分两行后每一行的职责都单一，宽度也稳定。
 *
 * ## 吸底（`sticky` 默认开启）为什么用 `fixed` 而不是 `sticky`
 *
 * `position: sticky` 的活动范围被**包含块**（这里是各阶段根元素）框住：滚到页面最底时它只能
 * 停在包含块的底边，而包含块之上还压着 `<main>` 的 `py-10`，于是底栏「到末尾就松开」，
 * 与窗口下缘裂出一条 40px 的缝——正是「滚到底又不吸底」的观感来源。`fixed` 脱离文档流、
 * 恒定贴住视口底部，任何滚动位置都不松手。
 *
 * ## 水平铺满：两侧不再「断裂」
 *
 * 吸底态从**侧栏右缘**一直铺到**窗口右缘**（`--shell-sidebar-w` 即侧栏宽度），
 * 分隔线与底色于是接在侧栏与窗口边界上，而不是悬在内容区中间被切出两截断口；
 * 内层再用与 `<main>` 完全相同的 `mx-auto w-full max-w-3xl px-4`，保证按钮与上方卡片列对齐。
 *
 * 传入 `:sticky="false"` 表示「嵌在已有卡片里的面板底栏」（两道确认闸门）：此时回到文档流，
 * 不吸底、不出血，内边距对齐面板内其它区块。
 */
const props = withDefaults(
  defineProps<{
    /** 是否吸底常驻；嵌在卡片内部的面板底栏应显式传 false */
    sticky?: boolean
  }>(),
  { sticky: true },
)

/**
 * 吸底态把自身高度同步到 `--shell-action-bar-h`。
 *
 * 底栏现在最多两行，高度随内容变（内容行有无、按钮是否换行），宿主 `<main>` 的底部留白
 * 若写死一个值，要么在长文档里压住最后一行，要么在只有单行的页面底部留出一大段空白。
 * 写成一个几何变量后，`<main>` 的留白与窄屏浮动按钮的避让都按它算，两处都不必知道底栏此刻多高。
 */
const root = ref<HTMLElement | null>(null)

/** 尺寸监听器；测试环境（jsdom）没有 ResizeObserver，保持 undefined */
let observer: ResizeObserver | undefined

/**
 * 当前在写这个变量的那条底栏。
 *
 * 同一屏里**可能同时挂着不止一条底栏**：表单页用 `v-show` 常驻（失配时不卸载），
 * 于是审阅阶段的 DOM 里既有可见的审阅底栏、也有一条 `display: none` 的表单底栏。
 * 隐藏那条的高度恒为 0，若任其写入，就会把可见底栏刚写好的值覆盖成 0——页面底部于是
 * 白白少了一截留白，长文档的最后一行被压住。因此只允许「渲染出来的那条」写，
 * 并用这个归属标记决定谁在卸载时负责清理。
 */
let owner: HTMLElement | null = null

/** 把当前高度写进 CSS 变量；未渲染（display:none / 尚未接入文档）时跳过 */
function syncHeight(): void {
  const el = root.value
  if (!el) return
  // getClientRects 为空 = 这个盒子没有被渲染，此时 offsetHeight 恒为 0，写了就是污染
  if (!el.getClientRects().length) return
  owner = el
  document.documentElement.style.setProperty('--shell-action-bar-h', `${el.offsetHeight}px`)
}

onMounted(() => {
  if (!props.sticky) return

  // 首次同步必须等下一帧：组件挂载时父链可能还没接进 document，此刻量不到真实高度
  // （Vue 是自底向上挂载的，子组件的 onMounted 早于根节点插入页面）。
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(syncHeight)
  else syncHeight()

  // jsdom 没有 ResizeObserver：测试环境到此为止，只靠 :root 的默认值
  if (typeof ResizeObserver === 'undefined') return
  observer = new ResizeObserver(syncHeight)
  if (root.value) observer.observe(root.value)
})

onBeforeUnmount(() => {
  observer?.disconnect()
  // 只有当前在写的那条负责清理；隐藏底栏卸载时不要去动别人的值
  if (owner === root.value) {
    owner = null
    // 回到 :root 里的默认 0px，下一页若没有底栏就不会平白多出留白
    document.documentElement.style.removeProperty('--shell-action-bar-h')
  }
})
</script>

<template>
  <div
    ref="root"
    :class="
      sticky
        ? 'shell-action-bar fixed bottom-0 left-0 right-0 z-30 border-t border-line bg-surface/90 backdrop-blur lg:left-[var(--shell-sidebar-w)]'
        : 'border-t border-line bg-surface'
    "
  >
    <!-- 内容行（可选）：对「当前这份内容」的工具或输入，与下面的流程操作分层 -->
    <div v-if="$slots.tools" class="border-b border-line bg-surface-2/60">
      <div :class="sticky ? 'mx-auto w-full max-w-3xl px-4 py-2.5' : 'px-4 py-2.5 sm:px-6'">
        <slot name="tools" />
      </div>
    </div>

    <!-- 流程行：左「破坏性操作 │ 次操作 · 说明」，右「主操作」 -->
    <div
      class="flex flex-wrap items-center justify-between gap-x-4 gap-y-2"
      :class="sticky ? 'mx-auto w-full max-w-3xl px-4 py-3' : 'px-4 py-3 sm:px-6'"
    >
      <div class="flex min-w-0 flex-wrap items-center gap-2">
        <slot name="danger" />
        <span v-if="$slots.danger" class="h-5 w-px shrink-0 bg-line-strong" aria-hidden="true" />
        <slot name="secondary" />
        <slot name="hint" />
      </div>

      <div class="flex flex-wrap items-center gap-2">
        <slot />
      </div>
    </div>
  </div>
</template>
