<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import AppActionBar from '@/components/ui/AppActionBar.vue'
import AppButton from '@/components/ui/AppButton.vue'

/**
 * 快捷流程入口的录入页。
 *
 * 只有两个字段：产品名称（可选）与一段话描述（必填）。刻意不做任何格式要求——
 * 用户在文本框里怎么写都行，剩下的结构化工作由后端的技能包与写作规范承担；
 * 这正是不走表单的理由。
 *
 * 需求文本的完整性由用户负责，因此这里明说「没提到的会被标成待确认」，
 * 让「一段话」的边界是可预期的，而不是靠模型猜。
 *
 * 空需求时**按钮直接禁用**，并把缺失提示常驻在字段下方：禁用态点不动，
 * 指望点击后再报错是收不到任何反馈的，所以提示必须提前给。
 */
const props = defineProps<{
  /** 产品名称；为空时文档头退化为「未命名产品」 */
  productName: string
  /** 用户写下的一段话需求 */
  requirements: string
}>()

const emit = defineEmits<{
  /** 产品名称变化 */
 'update:productName': [value: string]
  /** 需求文本变化 */
 'update:requirements': [value: string]
  /** 提交极简需求并直接生成 PRD */
  generate: []
  /** 返回入口选择页 */
  back: []
}>()

/** 需求文本框；本页只有这一个必填项，挂载时直接聚焦，省去一次点击 */
const textareaRef = ref<HTMLTextAreaElement | null>(null)

onMounted(() => textareaRef.value?.focus())

/** 需求文本去掉空白后是否为空 */
const requirementMissing = computed(() => props.requirements.trim() === '')

/** 校验通过才允许生成 */
const canGenerate = computed(() => !requirementMissing.value)

/** 示例内容：一键填充后自己改，比对着空框发呆快 */
const SAMPLE = {
  productName: '小账本',
  requirements:
 '做一个给家庭用的记账 App，主要解决两口子各花各的、月底算不清的问题。\n'
    + '希望支持：快速记一笔（金额、分类、备注）、按月和按分类看统计图、两个人共享同一个账本。\n'
    + '平台先做 iOS 和 Android，数据要能云端同步。',
}

/** 一键填充示例 */
function fillSample(): void {
  emit('update:productName', SAMPLE.productName)
  emit('update:requirements', SAMPLE.requirements)
}

/** 清空两个字段 */
function clearAll(): void {
  emit('update:productName', '')
  emit('update:requirements', '')
}
</script>

<template>
  <section class="space-y-4">
    <header>
      <h1 class="text-lg font-bold text-fg ">直接生成 PRD</h1>
      <p class="mt-1 text-sm text-subtle">
        用一段话把想法说清楚就行，不需要按字段填、也不会再追问一轮。
        提交后直接逐章撰写，没提到的信息会标注「待确认」而不是替你编。
      </p>
    </header>

    <div class="space-y-3 rounded-2xl border border-line bg-surface p-5 shadow-sm  ">
      <!-- 产品名称（可选） -->
      <div>
        <label for="field-quick-product-name" class="mb-1.5 block text-xs font-medium text-muted ">
          产品名称
          <span class="ml-1 font-normal text-subtle">可选，会用于文档标题与下载文件名</span>
        </label>
        <input
          id="field-quick-product-name"
          type="text"
          class="w-full rounded-lg border border-line-strong px-3 py-2 text-sm text-fg outline-none transition placeholder:text-subtle focus:border-accent focus:ring-2 focus:ring-accent-soft-line    "
          placeholder="例如：小账本"
          :value="productName"
          @input="emit('update:productName', ($event.target as HTMLInputElement).value)"
        />
      </div>

      <!-- 需求描述（必填） -->
      <div>
        <label for="field-quick-requirements" class="mb-1.5 block text-xs font-medium text-muted ">
          需求描述
          <span class="ml-1 text-rose-500">*</span>
        </label>
        <textarea
          id="field-quick-requirements"
          ref="textareaRef"
          rows="8"
          class="w-full resize-y rounded-lg border px-3 py-2 text-sm leading-relaxed text-fg outline-none transition placeholder:text-subtle focus:ring-2  "
          :class="
            requirementMissing
              ? 'border-line-strong focus:border-accent focus:ring-accent-soft-line  '
              : 'border-accent focus:border-accent focus:ring-accent-soft-line  '
 "
          placeholder="干什么用的、给谁用、要有哪些功能、有什么技术或范围的约束……没提到的会被标成「待确认」。"
          :value="requirements"
          @input="emit('update:requirements', ($event.target as HTMLTextAreaElement).value)"
        ></textarea>
        <div class="mt-1.5 flex items-start justify-between gap-3">
          <p v-if="requirementMissing" class="text-xs text-amber-600 dark:text-amber-400" role="status">
            请至少写一句需求描述，否则没有可依据的内容。
          </p>
          <p v-else class="text-xs text-subtle">
            纯文本即可，换行、列表、甚至粘一整段会议记录都可以。
          </p>
          <span class="shrink-0 text-xs text-subtle">{{ requirements.length }} 字</span>
        </div>
      </div>

      <!-- 快捷操作（工具类，留在卡片内；「清空」属重置类，下沉到页面级操作栏） -->
      <div class="flex flex-wrap items-center gap-2 border-t border-line pt-3 ">
        <AppButton variant="secondary" size="sm" @click="fillSample">
          填充示例
        </AppButton>
      </div>
    </div>

    <!-- 页面级操作栏：左「清空」│「返回入口」，右主操作 -->
    <AppActionBar>
      <template #danger>
        <AppButton
          variant="danger"
          size="md"
          :disabled="!productName && !requirements"
          @click="clearAll"
        >
          清空
        </AppButton>
      </template>
      <template #secondary>
        <AppButton variant="secondary" size="md" @click="emit('back')">返回入口</AppButton>
      </template>

      <AppButton variant="primary" size="md" :disabled="!canGenerate" @click="emit('generate')">
        生成 PRD
      </AppButton>
    </AppActionBar>
  </section>
</template>

