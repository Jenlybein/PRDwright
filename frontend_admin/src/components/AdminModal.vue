<script setup lang="ts">
/**
 * 通用弹窗。管理端所有「点一下才需要看见」的内容都走它。
 *
 * 为什么不再就地展开
 * ------------------
 * 卡片内展开会把下面的内容整体推开：配置项一多、日志一长，整页高度会跳，
 * 滚动位置也跟着漂。而在表格里就地展开更糟——它会撑高整行，同一行的其它列
 * 被迫留出大片空白。弹窗把这些内容挪出文档流，卡片高度保持稳定。
 *
 * 三个实现上的硬点
 * ----------------
 * 1. **不带 Teleport**：弹窗由各页挂在视图根上，祖先链里没有 transform / filter，
 *    `position: fixed` 因此仍然相对视口；表格外层的 `overflow-x-auto` 也不会裁剪它
 *    （CSS 规定 overflow 不裁剪 fixed 定位元素）。省掉 Teleport 的代价是要保证
 *    宿主不把弹窗塞进带 transform 的容器里——按钮 hover 的位移只作用在按钮自身，无碍。
 * 2. **打开时锁 body 滚动**：否则滚轮会把底下的页面带着走，关闭后位置就丢了。
 * 3. **Esc 关闭 + 打开后聚焦面板**：键盘用户不必先去摸鼠标。
 */
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    open: boolean
    /** 弹窗标题（也用作无障碍名称） */
    title: string
    /** 标题上方的小字，例如「Delete」「Quota」 */
    kicker?: string
    /** 宽度档位：sm 用于确认框，md 用于表单，lg 用于长内容 */
    size?: 'sm' | 'md' | 'lg'
    /** danger 会把标题与顶线染成危险色，用于删除这类不可逆操作 */
    tone?: 'default' | 'danger'
  }>(),
  { kicker: '', size: 'md', tone: 'default' },
)

const emit = defineEmits<{ close: [] }>()

const panelEl = ref<HTMLElement | null>(null)

/** 关掉页面滚动：只动 body，不动 <html>，避免布局整体位移 */
function lockScroll(lock: boolean) {
  document.body.style.overflow = lock ? 'hidden' : ''
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') emit('close')
}

watch(
  () => props.open,
  async (open) => {
    lockScroll(open)
    if (open) {
      document.addEventListener('keydown', onKeydown)
      await nextTick()
      panelEl.value?.focus()
    } else {
      document.removeEventListener('keydown', onKeydown)
    }
  },
  { immediate: true },
)

// 组件被路由切走时也要解锁，否则整页再也滚不动
onBeforeUnmount(() => {
  lockScroll(false)
  document.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <div
    v-if="open"
    class="admin-modal"
    role="dialog"
    aria-modal="true"
    :aria-label="title"
    @click.self="emit('close')"
  >
    <div
      ref="panelEl"
      class="admin-modal-panel"
      :class="[`admin-modal-${size}`, tone === 'danger' ? 'admin-modal-danger' : '']"
      tabindex="-1"
    >
      <header class="admin-modal-head">
        <div class="min-w-0">
          <p v-if="kicker" class="kicker">{{ kicker }}</p>
          <h2 class="admin-modal-title">{{ title }}</h2>
          <!-- 副标题放标识信息（用户名 / 任务号 / 时间），不占正文重量 -->
          <slot name="subtitle" />
        </div>
        <button type="button" class="brut-btn brut-btn-xs shrink-0" @click="emit('close')">
          关闭
        </button>
      </header>

      <div class="admin-modal-body">
        <slot />
      </div>

      <footer v-if="$slots.footer" class="admin-modal-foot">
        <slot name="footer" />
      </footer>
    </div>
  </div>
</template>
