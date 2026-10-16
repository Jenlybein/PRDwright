<script setup lang="ts">
import type { EntryMode } from '@/types'
import AppCard from '@/components/ui/AppCard.vue'
import AppIcon from '@/components/ui/AppIcon.vue'

/**
 * 入口选择页（新建方案的第一屏）。
 *
 * ## 为什么是「两组四叶子」而不是四张平铺卡片
 *
 * 四条入口的差别只在**起点**，但起点分两类：一类是**从无到有**（自己把需求说清楚），
 * 一类是**已经有材料**（别人写的、别的工具产出的）。平铺四张卡片时用户要逐张读文案才能
 * 找到自己那一类；先分组再选，第一眼就能排除掉一半——「我是哪种情况」比「我要哪个功能」
 * 更快回答。因此这里是**一屏两组、四个选项都可见**：分组给出语义，叶子给出落点，
 * 不需要多点一屏才看得见选项。
 *
 * 两组的产出也不同：从零创建那一组是完整的生成链路（PRD → 接口文档 → 提示词套件），
 * 导入那一组只补做缺的那份（提示词套件）。二者差别由入口本身与后续流程体现——卡片上不再
 * 标注「产出」（两条导入入口的产出本就一样，标出来是重复信息，也抢占注意力）。
 *
 * 组件自身不持有任何流程状态，选中哪个入口由上层的 store 决定。
 *
 * ## 视觉：一组一层次，只有选项卡片是「盒子」
 *
 * 早先的版本给每组套了一个带边框与底色的面板，面板里再放四张带边框的卡片——两层容器
 * 互相抢边界，用户分不清「这是组还是选项」。现在**只有选项卡片有边框与阴影**，组退化成
 * 一个轻量标题区（一根强调色短线 + 组名 + 一句说明）；组与组的界限交给**留白**（组间距
 * 约为组内选项间距的两倍），用间距本身表达层级，而不是再加一层边框。
 *
 * 卡片内部刻意做出主次，只有两级：标题最大最重，说明次之；
 * 右侧箭头默认用 `text-subtle` 本身（已远弱于标题的 `text-fg`，足够低调又不至于看不见），
 * 悬停才转强调色并右移，不与标题抢注意力。
 * 悬停 / 键盘聚焦的反馈交给 `AppCard`（interactive 渲染为 `<button>`，整卡可点且键盘可达）。
 *
 * ## 图标进标题行（而不是独立一列）
 *
 * 图标若单独占左侧一列，会把正文挤到约 236px 宽，14px 的说明要换 4 行、卡片被撑成一大块
 * 文字，既密也不像「选项」。因此把图标收进**标题行**（`[图标] 标题 …… [箭头]`）：图标与
 * 标题在同一行自然对齐，说明则从卡片左内边距起、**独占整行宽**——换行降到 2 行，
 * 卡片更像选项、也更好读。图标取 40px（`h-10 w-10`），与标题行高度齐平。
 *
 * 说明再给一个**两行高的固定盒**（`min-h`）：四条说明都恰好两行，无论个别句子长短如何，
 * 卡片高度都一致，2×2 网格的底边才齐平——没有外框时，参差的底边最扎眼。文案长度也据此
 * 控制在两行以内、且第二行不会只剩一两个孤字。删掉产出小字后，这个两行盒就是**卡片高度
 * 的唯一基准**（卡片只剩标题行 + 说明两块）。
 *
 * ## 整页重心
 *
 * 入口页内容（页首 + 两组）在较大窗口里只占上半屏，下半屏空一大片会显得「没写完」。
 * 根节点因此用 `min-h` + `justify-center` 让整块内容在**可用高度内垂直居中**；可用高度 =
 * 视口高 − 底部操作栏高（`--shell-action-bar-h`）− 主内容区的上下留白，避免与吸底栏重叠。
 */
const emit = defineEmits<{
  /** 选中某个入口 */
  select: [mode: EntryMode]
}>()

/** 一个可点的入口叶子 */
interface EntryOption {
  mode: EntryMode
  title: string
  description: string
  iconWrap: string
  iconColor: string
  iconPath: string
}

/** 一组入口：同属一类起点，组标题与组说明是这一层的语义 */
interface EntryGroup {
  id: string
  title: string
  description: string
  options: EntryOption[]
}

/**
 * 四叶子两组的静态配置。
 *
 * 每条只留「标题 + 一句说明」：入口页的任务是让用户快速选起点，卡片越少元素越好比较。
 */
const GROUPS: EntryGroup[] = [
  {
    id: 'from-scratch',
    title: '从零创建 PRD',
    description: '还没有任何材料，从这里把需求说清楚。',
    options: [
      {
        mode: 'structured',
        title: '完整流程',
        description: '逐项填表 + 和 AI 对话把细节问清楚，再依次生成三份文档。',
        iconWrap:
          'bg-primary-50 group-hover:bg-primary-100 dark:bg-primary-950/40 dark:group-hover:bg-primary-900/60',
        iconColor: 'text-primary-600 dark:text-primary-300',
        iconPath:
          'M4 4a2 2 0 0 1 2-2h5.586a1 1 0 0 1 .707.293L15.707 5.5a1 1 0 0 1 .293.707V16a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4Zm7 1.5V7h1.5L11 5.5ZM7 9a1 1 0 0 0 0 2h6a1 1 0 1 0 0-2H7Zm0 3a1 1 0 1 0 0 2h6a1 1 0 1 0 0-2H7Z',
      },
      {
        mode: 'prd-quick',
        title: '快捷流程',
        description: '一段话说清想法，跳过表单与对话，直接逐章生成 PRD。',
        iconWrap:
          'bg-amber-50 group-hover:bg-amber-100 dark:bg-amber-950/40 dark:group-hover:bg-amber-900/60',
        iconColor: 'text-amber-600 dark:text-amber-300',
        iconPath:
          'M11.3 1.046A1 1 0 0 1 12 2v5h4a1 1 0 0 1 .82 1.573l-7 10A1 1 0 0 1 8 18v-5H4a1 1 0 0 1-.82-1.573l7-10a1 1 0 0 1 1.12-.381Z',
      },
    ],
  },
  {
    id: 'with-material',
    title: '导入已有素材',
    description: '手头已经有写好的材料，别重复劳动。',
    options: [
      {
        mode: 'prompt-debug',
        title: '我有 PRD',
        description: '贴一份现成 PRD，直接生成提示词套件，之后可用 AI 优化反复调。',
        iconWrap:
          'bg-emerald-50 group-hover:bg-emerald-100 dark:bg-emerald-950/40 dark:group-hover:bg-emerald-900/60',
        iconColor: 'text-emerald-600 dark:text-emerald-300',
        // 尖括号：提示词套件本质上是给模型看的「代码」
        iconPath:
          'M6.28 5.22a.75.75 0 0 1 0 1.06L2.56 10l3.72 3.72a.75.75 0 0 1-1.06 1.06L.97 10.53a.75.75 0 0 1 0-1.06l4.25-4.25a.75.75 0 0 1 1.06 0Zm7.44 0a.75.75 0 0 1 1.06 0l4.25 4.25a.75.75 0 0 1 0 1.06l-4.25 4.25a.75.75 0 0 1-1.06-1.06L17.44 10l-3.72-3.72a.75.75 0 0 1 0-1.06Z',
      },
      {
        mode: 'prd-and-api-paste',
        title: '我有 PRD + 接口文档',
        description: '两份都贴进来，提示词因此能对齐真实的接口路径与字段名。',
        iconWrap:
          'bg-violet-50 group-hover:bg-violet-100 dark:bg-violet-950/40 dark:group-hover:bg-violet-900/60',
        iconColor: 'text-violet-600 dark:text-violet-300',
        // 两条叠起来的块：这份入口比上一个多带一份材料
        iconPath:
          'M4 3h12a1 1 0 0 1 1 1v3a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Zm0 9h12a1 1 0 0 1 1 1v3a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-3a1 1 0 0 1 1-1Z',
      },
    ],
  },
]

/** 叶子右侧的箭头图标 */
const ARROW_ICON =
  'M7.22 4.22a.75.75 0 0 1 1.06 0l5.25 5.25a.75.75 0 0 1 0 1.06l-5.25 5.25a.75.75 0 1 1-1.06-1.06L11.94 10 7.22 5.28a.75.75 0 0 1 0-1.06Z'
</script>

<template>
  <!-- 页首与两组之间、组与组之间都用留白拉开：层级靠间距表达，不再靠外层盒子。
       根节点用 min-h + justify-center 把整块内容在可用高度内垂直居中（可用高度 = 视口高
       − 底部操作栏高 − 主内容区上下留白），避免较大窗口里下半屏空一大片显得没写完。 -->
  <section
    class="flex min-h-[calc(100vh_-_var(--shell-action-bar-h)_-_5rem)] flex-col justify-center space-y-9"
  >
    <header class="text-center">
      <h1 class="text-2xl font-bold tracking-tight text-fg">新建 PRD 方案</h1>
      <p class="mx-auto mt-2.5 max-w-lg text-sm leading-relaxed text-subtle">
        选择本次工作的起点。四条路最终都通向同一套写作规范与生成链路，只是起点不同；
        选错了也没关系，随时可以重新开始。
      </p>
    </header>

    <!-- 两组并排：一眼看出「两条路，每条路两个入口」。组间距（gap）约为组内选项间距的两倍 -->
    <div class="grid gap-x-6 gap-y-9 sm:grid-cols-2">
      <section
        v-for="group in GROUPS"
        :key="group.id"
        class="flex flex-col"
        :aria-label="group.title"
      >
        <!-- 轻量组标题区：一根强调色短线 + 组名 + 一句说明，取代原来的整块边框面板 -->
        <header class="mb-3.5">
          <div class="flex items-center gap-2">
            <span class="h-4 w-1 rounded-full bg-accent" aria-hidden="true"></span>
            <h2 class="text-sm font-semibold text-fg">{{ group.title }}</h2>
          </div>
          <p class="mt-1.5 pl-3 text-xs leading-relaxed text-muted">{{ group.description }}</p>
        </header>

        <div class="flex flex-1 flex-col gap-3">
          <AppCard
            v-for="option in group.options"
            :key="option.mode"
            interactive
            :padded="false"
            class="group p-4"
            @click="emit('select', option.mode)"
          >
            <!--
              标题行：图标 + 标题 + 箭头同属一行，图标与标题在此行内自然对齐（不再独立成列
              把说明挤窄）。图标 40px、色调底，悬停整块加深（色调由 iconWrap 的 group-hover 提供）。
            -->
            <span class="flex items-center gap-3">
              <span
                class="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl transition-colors"
                :class="option.iconWrap"
              >
                <AppIcon :path="option.iconPath" size="lg" :class="option.iconColor" />
              </span>
              <span class="min-w-0 flex-1 text-[15px] font-semibold leading-snug text-fg">
                {{ option.title }}
              </span>
              <!-- 箭头待在标题行内，默认最弱；悬停才转强调色并右移一点，不与标题同权重 -->
              <svg
                class="h-4 w-4 shrink-0 text-subtle transition group-hover:translate-x-0.5 group-hover:text-accent"
                viewBox="0 0 20 20"
                fill="currentColor"
                aria-hidden="true"
              >
                <path fill-rule="evenodd" :d="ARROW_ICON" clip-rule="evenodd" />
              </svg>
            </span>

            <!--
              说明：从卡片左内边距起、独占整行宽。固定为两行高（2.875rem = 两行 leading-relaxed 的
              高度），四条说明都恰好两行，个别句子再短也占同样高度，四张卡因此自动等高、底边齐平。
              `text-balance` 让两行长度尽量均衡（`text-wrap: balance`），避免第二行只剩三四个字
              的「吊尾」；不支持该属性的浏览器会回退为普通换行，无副作用。
            -->
            <span class="mt-2 block min-h-[2.875rem] text-balance text-sm leading-relaxed text-muted">
              {{ option.description }}
            </span>
          </AppCard>
        </div>
      </section>
    </div>
  </section>
</template>
