<script setup lang="ts">
/**
 * 系统配置页：可调参数 + 模型供应商。
 *
 * 这个页面最怕的不是报错，而是**「显示已保存、实际没生效」**——它看起来完全正常，
 * 用户会以为配置改了，直到某天发现生成还在用旧的。所以保存之后必须做三步验证：
 *
 * 1. 把响应里回读的值与提交的值**逐项比对**；
 * 2. 看 `revision`（配置指纹）**是否变化**——没变说明库里其实没变；
 * 3. 再**独立请求一次** `/config/effective` 复核（防「响应体是缓存回声」）。
 *
 * 三步都过才说「已生效」。任何一步对不上，就明说「保存成功但未生效」，
 * 并给出下一步该查什么——而不是给一个让人安心的绿字。
 *
 * 另一个刻意的设计：**启动类配置不显示在这里**（日志级别、落库开关、库文件路径等）。
 * 它们改了必须重启，放进来只会制造「显示已保存、实际没变」的困惑。
 */
import { computed, onMounted, ref } from 'vue'

import {
  activateProvider,
  createProvider,
  deleteProvider,
  fetchConfig,
  fetchProviders,
  messageOf,
  resetConfig,
  testProvider,
  toResult,
  updateConfig,
  updateProvider,
  type ConfigGroup,
  type ConfigItem,
  type Provider,
  type ProviderList,
  type SystemConfig,
} from '@/services/adminService'

const config = ref<SystemConfig | null>(null)
const providerList = ref<ProviderList | null>(null)
const loading = ref(true)
const error = ref('')
const notice = ref('')

// 每个分组一份草稿（键 → 待提交值）。只有动过的项才提交。
const drafts = ref<Record<string, Record<string, unknown>>>({})
const savingGroup = ref('')

const creating = ref(false)
const savedRevision = ref('')

/** 危险项待确认：记住「哪个键、要改成什么」 */
const pendingChange = ref<{ key: string; label: string; value: unknown; group: string } | null>(null)
/** 待确认的供应商操作（激活 / 删除） */
const pendingProvider = ref<{ action: 'activate' | 'delete'; provider: Provider } | null>(null)

// 新增供应商表单
const form = ref({
  id: '',
  name: '',
  protocol: 'openai_compatible',
  base_url: '',
  api_key_env_var: '',
  default_model: '',
  max_tokens: 4096,
  context_window: 0,
})

/** 测试连接的结果（按供应商 id 存） */
const testResults = ref<Record<string, { ok: boolean; detail: string }>>({})
const testingId = ref('')

const groups = computed<ConfigGroup[]>(() => config.value?.groups ?? [])
const providers = computed<Provider[]>(() => providerList.value?.providers ?? [])
const protocols = computed(() => providerList.value?.protocols ?? [])
const presets = computed(() => providerList.value?.presets ?? [])

function itemOf(groupKey: string, key: string): ConfigItem | undefined {
  return groups.value.find((g) => g.key === groupKey)?.items.find((i) => i.key === key)
}

/** 某项的当前草稿值（没改过就是生效值） */
function draftOf(group: ConfigGroup, item: ConfigItem): unknown {
  const bag = drafts.value[group.key]
  return bag && item.key in bag ? bag[item.key] : item.value
}

/** 该组有没有未保存的改动 */
function dirtyOf(group: ConfigGroup): boolean {
  return Object.keys(drafts.value[group.key] ?? {}).length > 0
}

function setDraft(group: ConfigGroup, item: ConfigItem, value: unknown, confirmed = false) {
  // 危险项先弹确认：它们的共同点是「改错了不明显，但影响很大」
  if (item.danger && !confirmed) {
    pendingChange.value = { key: item.key, label: item.label, value, group: group.key }
    return
  }
  drafts.value[group.key] = { ...(drafts.value[group.key] ?? {}), [item.key]: value }
}

function confirmPendingChange() {
  const pending = pendingChange.value
  if (!pending) return
  const group = groups.value.find((g) => g.key === pending.group)
  const item = group?.items.find((i) => i.key === pending.key)
  if (group && item) setDraft(group, item, pending.value, true)
  pendingChange.value = null
}

function cancelPendingChange() {
  pendingChange.value = null
}

function discardGroup(group: ConfigGroup) {
  drafts.value[group.key] = {}
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [cfg, list] = await Promise.all([fetchConfig(), fetchProviders()])
    config.value = cfg
    providerList.value = list
    savedRevision.value = cfg.revision
    drafts.value = {}
  } catch (err) {
    error.value = messageOf(err)
  } finally {
    loading.value = false
  }
}

function sayOk(text: string) {
  notice.value = text
  error.value = ''
}

function sayFail(message: string) {
  error.value = message
  notice.value = ''
}

/**
 * 保存一组配置，并**验证真的生效**。
 *
 * 三层验证缺一不可：
 * - 回读比对拦住「后端只写了一半」；
 * - `revision` 变化拦住「写进库了但读的是旧快照」；
 * - 独立请求拦住「响应体是缓存回声」。
 */
async function saveGroup(group: ConfigGroup) {
  const patch = drafts.value[group.key] ?? {}
  if (!Object.keys(patch).length) return
  if (savingGroup.value) return

  savingGroup.value = group.key
  const beforeRevision = config.value?.revision ?? ''

  // ① 提交。这一层失败来自后端（校验 400 / 权限 / 网络），原因由 `messageOf` 取原文。
  let saved: SystemConfig
  try {
    saved = await updateConfig(patch)
  } catch (err) {
    const message = messageOf(err)
    savingGroup.value = ''
    // ⚠️ 顺序：`load()` 会清空 error，所以要**先刷新、再设错误**
    await load()
    sayFail(message)
    return
  }

  // ② 三步校验「真的生效了」。⚠️ 这里返回**我们已经组织好的字符串**，
  //    不能再套 `messageOf`——它只认 `ApiError`，会把我们的说明兜底成
  //    「操作失败，请稍后重试」，那就等于什么都没说。
  const problem = await verifyApplied(patch, saved, beforeRevision, () => {
    drafts.value[group.key] = {}
  })
  savingGroup.value = ''

  if (problem) {
    await load()
    sayFail(problem)
    return
  }
  sayOk(`${group.label}已生效（下一次生成起使用新值）`)
}

/**
 * 验证「刚保存的改动真的生效了」。返回问题描述，全部通过时返回 null。
 *
 * 三层缺一不可：
 * - **指纹变化**：没变说明库里没变（可能连到了另一个库）；
 * - **回读一致**：拦「后端只写了一半」；
 * - **独立复核**：再请求一次，拦「响应体是缓存回声」。
 *
 * 返回的是字符串而不是抛异常：见 `saveGroup` 里的说明——抛出去会被
 * `messageOf` 兜底成通用文案，用户就失去唯一的线索了。
 */
async function verifyApplied(
  patch: Record<string, unknown>,
  saved: SystemConfig,
  beforeRevision: string,
  resetDraft: () => void,
): Promise<string | null> {
  config.value = saved
  resetDraft()

  if (saved.revision === beforeRevision) {
    return '已保存但配置指纹没有变化：可能连到了另一个数据库，或后端没有真正写库。'
  }

  const flat = (cfg: SystemConfig) => cfg.groups.flatMap((g) => g.items)
  for (const [key, want] of Object.entries(patch)) {
    const got = flat(saved).find((i) => i.key === key)?.value
    if (got !== want) {
      return `已保存但 ${key} 回读不一致：提交 ${String(want)}，实际 ${String(got)}`
    }
  }

  try {
    const again = await fetchConfig()
    config.value = again
    for (const [key, want] of Object.entries(patch)) {
      const got = flat(again).find((i) => i.key === key)?.value
      if (got !== want) {
        return `复核失败：${key} 仍是 ${String(got)}，可能写到了别的库或另一个进程。`
      }
    }
    savedRevision.value = again.revision
  } catch (err) {
    return `已保存，但复核请求失败：${messageOf(err)}`
  }
  return null
}

async function restoreGroup(group: ConfigGroup) {
  const keys = group.items.map((i) => i.key)
  const result = await toResult(async () => {
    config.value = await resetConfig(keys)
    drafts.value[group.key] = {}
  })
  if (result.ok) sayOk(`${group.label}已恢复默认`)
  else sayFail(result.message)
}

/** 用预设填表：省去手记 base_url 与变量名 */
function applyPreset(presetId: string) {
  const preset = presets.value.find((p) => p.id === presetId)
  if (!preset) return
  form.value = {
    id: preset.id,
    name: preset.name,
    protocol: preset.protocol,
    base_url: preset.base_url,
    api_key_env_var: preset.api_key_env_var,
    default_model: preset.default_model,
    max_tokens: 4096,
    context_window: 0,
  }
}

async function onCreate() {
  if (creating.value) return
  creating.value = true
  const payload = { ...form.value, max_tokens: Number(form.value.max_tokens) || 4096 }
  const result = await toResult(async () => {
    providerList.value = await createProvider(payload)
    await load()
  })
  creating.value = false

  if (!result.ok) {
    sayFail(result.message)
    return
  }
  form.value = {
    id: '',
    name: '',
    protocol: 'openai_compatible',
    base_url: '',
    api_key_env_var: '',
    default_model: '',
    max_tokens: 4096,
    context_window: 0,
  }
  sayOk('供应商已添加。记得在 .env 里配好密钥变量，然后点「测试连接」验证')
}

async function toggleEnabled(provider: Provider) {
  const result = await toResult(async () => {
    providerList.value = await updateProvider(provider.id, { enabled: !provider.enabled })
  })
  if (result.ok) sayOk(provider.enabled ? '已停用' : '已启用')
  else sayFail(result.message)
}

async function confirmProviderAction() {
  const pending = pendingProvider.value
  if (!pending) return
  // 先收起面板：即使后端拒绝，也不该让人停在确认态（错误改由顶部横幅显示）
  pendingProvider.value = null

  const result = await toResult(async () => {
    if (pending.action === 'activate') {
      const list = await activateProvider(pending.provider.id)
      providerList.value = list
      if (list.warning) sayFail(list.warning)
      else sayOk(`已切换到「${pending.provider.name}」，下一次生成生效`)
    } else {
      providerList.value = await deleteProvider(pending.provider.id)
      sayOk(`已删除「${pending.provider.name}」`)
    }
    await load()
  })
  if (!result.ok) sayFail(result.message)
}

async function onTest(provider: Provider) {
  if (testingId.value) return
  testingId.value = provider.id
  const result = await toResult(async () => {
    const outcome = await testProvider(provider.id)
    testResults.value = { ...testResults.value, [provider.id]: outcome }
  })
  testingId.value = ''
  if (!result.ok) sayFail(result.message)
  else {
    const outcome = testResults.value[provider.id]
    if (outcome?.ok) sayOk(`「${provider.name}」连通正常`)
    else sayFail(`「${provider.name}」连接失败：${outcome?.detail ?? ''}`)
  }
}

function sourceText(item: ConfigItem): string {
  return item.source === 'db' ? '后台覆盖' : '默认'
}

function protocolOptionsLabel(value: string): string {
  return protocols.value.find((p) => p.value === value)?.label ?? value
}

/** 供应商状态：使用中 / 已停用 / 待命，三态各自带一个标签样式 */
function providerState(p: Provider): { text: string; cls: string } {
  if (p.active) return { text: '使用中', cls: 'brut-tag-sky' }
  if (!p.enabled) return { text: '已停用', cls: 'brut-tag-slab' }
  return { text: '待命', cls: 'brut-tag-ok' }
}

onMounted(load)
</script>

<template>
  <div class="space-y-5">
    <!-- 页头 -->
    <header class="brut-card-lg p-5">
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div class="min-w-0">
          <p class="kicker">Config / 运行时</p>
          <h1 class="page-title mt-2">系统配置</h1>
        </div>
        <span v-if="config" class="brut-tag brut-tag-slab">REV {{ config.revision }}</span>
      </div>
      <p class="mt-3 max-w-3xl text-sm text-body">
        改完保存后立即作用于<b class="text-ink">下一次生成</b>；正在跑的任务不受影响。
        日志级别、落库开关这类「改了必须重启」的配置不在此页——它们改了不重启只会造成错觉。
      </p>
    </header>

    <p v-if="error" class="brut-note brut-note-fail">{{ error }}</p>
    <p v-if="notice" class="brut-note brut-note-ok">{{ notice }}</p>

    <div v-if="loading" class="brut-card px-4 py-8 text-sm text-subtle">加载中…</div>

    <template v-else-if="config">
      <!-- 当前生效状态：排查「改了没生效」的第一眼就该落在这里 -->
      <section class="brut-slab-sky p-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div class="min-w-0">
            <p class="kicker text-on-sky/75">当前供应商 · Active Provider</p>
            <p class="mt-1 text-xl font-extrabold tracking-tight">
              {{ config.active_provider?.name || '（无）' }}
            </p>
            <p class="mt-0.5 font-mono text-xs text-on-sky/80">
              {{ config.active_provider?.protocol_label || '—' }} ·
              {{ config.active_provider?.default_model || '未填模型' }}
            </p>
          </div>

          <div class="text-right">
            <p class="kicker text-on-sky/75">配置来源</p>
            <p class="mt-1 text-sm font-bold">
              {{
                config.config_source === 'db'
                  ? '数据库（含后台覆盖）'
                  : config.config_source === 'env'
                    ? '环境变量默认值'
                    : '降级（表不可用）'
              }}
            </p>
          </div>
        </div>

        <p
          v-if="config.missing_key"
          class="mt-3 border-2 border-on-sky/50 bg-card px-3 py-2 text-xs font-bold text-danger-fg"
        >
          ⚠ {{ config.missing_key }}
        </p>
      </section>

      <!-- 供应商 -->
      <section class="brut-card-lg">
        <header class="flex flex-wrap items-end justify-between gap-2 border-b-2 border-brut px-4 py-3">
          <div>
            <p class="kicker">Providers</p>
            <h2 class="mt-1 text-base font-extrabold tracking-tight text-ink">模型供应商</h2>
          </div>
          <p class="max-w-md text-xs text-subtle">
            密钥只按<b class="text-muted">变量名</b>登记，值留在 .env 里，不会进数据库。
            「测试连接」会真发一次最小调用（约 16 token）——填对参数与真的能用是两件事。
          </p>
        </header>

        <!-- 供应商卡片：一块一家，比表格多出的那一列正好放动作按钮 -->
        <div class="grid gap-3 px-4 py-3 lg:grid-cols-2">
          <article
            v-for="p in providers"
            :key="p.id"
            class="brut-card flex flex-col p-3"
            :class="p.active ? 'border-[3px] border-brut' : ''"
          >
            <div class="flex flex-wrap items-start justify-between gap-2">
              <div class="min-w-0">
                <div class="flex flex-wrap items-center gap-1.5">
                  <h3 class="text-sm font-extrabold tracking-tight text-ink">{{ p.name }}</h3>
                  <span class="brut-tag" :class="providerState(p).cls">
                    {{ providerState(p).text }}
                  </span>
                  <span v-if="p.builtin" class="brut-tag brut-tag-slab">内置</span>
                </div>
                <p class="mt-0.5 font-mono text-[11px] text-subtle">{{ p.id }}</p>
              </div>
            </div>

            <dl class="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
              <div>
                <dt class="kicker">协议</dt>
                <dd class="mt-0.5">{{ p.protocol_label }}</dd>
              </div>
              <div>
                <dt class="kicker">默认模型</dt>
                <dd class="mt-0.5 truncate font-mono text-[11px]">{{ p.default_model || '—' }}</dd>
              </div>
              <div class="col-span-2">
                <dt class="kicker">密钥变量</dt>
                <dd class="mt-0.5 font-mono text-[11px]">
                  <span :class="p.key_present ? 'text-ink' : 'text-danger-fg'">
                    {{ p.api_key_env_var || '—（无需）' }}
                  </span>
                  <span v-if="p.api_key_env_var && !p.key_present" class="ml-1 font-bold text-danger-fg">
                    尚未配置
                  </span>
                </dd>
              </div>
            </dl>

            <p
              v-if="testResults[p.id]"
              class="mt-2 border-l-4 pl-2 text-[11px] font-bold"
              :class="
                testResults[p.id]!.ok
                  ? 'border-success-line text-success-fg'
                  : 'border-danger-line text-danger-fg'
              "
            >
              {{ testResults[p.id]!.ok ? '连通正常' : '连接失败' }} · {{ testResults[p.id]!.detail }}
            </p>

            <div class="mt-3 flex flex-wrap gap-1.5 border-t-2 border-line pt-2">
              <button
                v-if="!p.active"
                type="button"
                class="brut-btn brut-btn-xs"
                :disabled="!p.enabled"
                @click="pendingProvider = { action: 'activate', provider: p }"
              >
                激活
              </button>
              <button
                type="button"
                class="brut-btn brut-btn-xs"
                :disabled="testingId === p.id"
                @click="onTest(p)"
              >
                {{ testingId === p.id ? '测试中…' : '测试连接' }}
              </button>
              <button
                type="button"
                class="brut-btn brut-btn-xs"
                @click="toggleEnabled(p)"
              >
                {{ p.enabled ? '停用' : '启用' }}
              </button>
              <button
                v-if="!p.builtin && !p.active"
                type="button"
                class="brut-btn brut-btn-xs border-danger-line text-danger-fg"
                @click="pendingProvider = { action: 'delete', provider: p }"
              >
                删除
              </button>
            </div>
          </article>
        </div>

        <!-- 新增供应商 -->
        <div class="border-t-2 border-brut px-4 py-4">
          <h3 class="text-sm font-extrabold tracking-tight text-ink">新增供应商</h3>
          <p class="mt-0.5 text-xs text-subtle">
            绝大多数厂商都是 OpenAI 兼容：填对 <code class="font-mono">base_url</code> 即可接入，
            不需要装新依赖。地址会变，以官方文档为准。
          </p>

          <div class="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <label class="block">
              <span class="brut-label">从预设填充</span>
              <select
                class="brut-field"
                @change="applyPreset(($event.target as HTMLSelectElement).value)"
              >
                <option value="">（手动填写）</option>
                <option v-for="preset in presets" :key="preset.id" :value="preset.id">
                  {{ preset.name }}
                </option>
              </select>
            </label>

            <label class="block">
              <span class="brut-label">id（身份，创建后不可改）</span>
              <input v-model="form.id" type="text" placeholder="如 deepseek" class="brut-field font-mono" />
            </label>

            <label class="block">
              <span class="brut-label">协议</span>
              <select v-model="form.protocol" class="brut-field">
                <option v-for="opt in protocols" :key="opt.value" :value="opt.value">
                  {{ opt.label }}
                </option>
              </select>
            </label>

            <label class="block">
              <span class="brut-label">展示名</span>
              <input v-model="form.name" type="text" placeholder="如 DeepSeek" class="brut-field" />
            </label>

            <label class="block sm:col-span-2">
              <span class="brut-label">base_url（OpenAI 兼容类通常要带 /v1）</span>
              <input
                v-model="form.base_url"
                type="text"
                placeholder="https://api.deepseek.com/v1"
                class="brut-field font-mono"
              />
            </label>

            <label class="block">
              <span class="brut-label">密钥变量名（不是密钥本身）</span>
              <input
                v-model="form.api_key_env_var"
                type="text"
                placeholder="DEEPSEEK_API_KEY"
                class="brut-field font-mono"
              />
            </label>

            <label class="block">
              <span class="brut-label">默认模型</span>
              <input
                v-model="form.default_model"
                type="text"
                placeholder="deepseek-chat"
                class="brut-field font-mono"
              />
            </label>

            <label class="block">
              <span class="brut-label">输出上限（max_tokens）</span>
              <input v-model.number="form.max_tokens" type="number" class="brut-field" />
            </label>

            <label class="block">
              <span class="brut-label">上下文窗口（0=用内置默认）</span>
              <input v-model.number="form.context_window" type="number" class="brut-field" />
            </label>
          </div>

          <div class="mt-3 flex flex-wrap items-center gap-3">
            <button
              type="button"
              class="brut-btn brut-btn-primary"
              :disabled="creating"
              @click="onCreate"
            >
              {{ creating ? '添加中…' : '添加供应商' }}
            </button>
            <p class="text-xs text-subtle">
              自定义模型建议填「上下文窗口」：不填会按 32k 保守估算，长文可能被误判为超出预算。
            </p>
          </div>
        </div>
      </section>

      <!-- 配置分组 -->
      <section v-for="group in groups" :key="group.key" class="brut-card-lg">
        <header
          class="flex flex-wrap items-start justify-between gap-3 border-b-2 border-brut px-4 py-3"
        >
          <div class="min-w-0">
            <div class="flex flex-wrap items-center gap-2">
              <h2 class="text-base font-extrabold tracking-tight text-ink">{{ group.label }}</h2>
              <span v-if="dirtyOf(group)" class="brut-tag brut-tag-warn">有未保存改动</span>
            </div>
            <p class="mt-0.5 text-xs text-subtle">{{ group.help }}</p>
          </div>

          <div class="flex flex-wrap items-center gap-1.5">
            <button
              type="button"
              class="brut-btn brut-btn-xs"
              :disabled="!dirtyOf(group)"
              @click="discardGroup(group)"
            >
              撤销改动
            </button>
            <button type="button" class="brut-btn brut-btn-xs" @click="restoreGroup(group)">
              恢复默认
            </button>
            <button
              type="button"
              class="brut-btn brut-btn-xs brut-btn-primary"
              :disabled="!dirtyOf(group) || savingGroup === group.key"
              @click="saveGroup(group)"
            >
              {{ savingGroup === group.key ? '保存中…' : '保存' }}
            </button>
          </div>
        </header>

        <div>
          <div
            v-for="item in group.items"
            :key="item.key"
            class="grid grid-cols-1 gap-2 border-b border-line px-4 py-3 last:border-b-0 sm:grid-cols-[minmax(0,0.85fr)_minmax(0,1.3fr)]"
          >
            <div class="min-w-0">
              <div class="flex flex-wrap items-center gap-1.5">
                <span class="text-sm font-bold text-ink">{{ item.label }}</span>
                <span v-if="item.danger" class="brut-tag brut-tag-warn">危险</span>
              </div>
              <div class="mt-0.5 font-mono text-[11px] text-subtle">{{ item.key }}</div>
            </div>

            <div class="min-w-0">
              <div class="flex flex-wrap items-center gap-2">
                <!-- 布尔 -->
                <label
                  v-if="item.type === 'bool'"
                  class="flex items-center gap-2 border-2 border-brut bg-field px-2.5 py-1.5 text-sm font-bold text-ink"
                >
                  <input
                    type="checkbox"
                    class="brut-check"
                    :checked="Boolean(draftOf(group, item))"
                    @change="setDraft(group, item, ($event.target as HTMLInputElement).checked)"
                  />
                  {{ draftOf(group, item) ? '开启' : '关闭' }}
                </label>

                <!-- 枚举 -->
                <select
                  v-else-if="item.type === 'enum'"
                  class="brut-field w-auto"
                  :value="String(draftOf(group, item))"
                  @change="setDraft(group, item, ($event.target as HTMLSelectElement).value)"
                >
                  <option v-for="opt in item.options ?? []" :key="opt" :value="opt">
                    {{ opt }}
                  </option>
                </select>

                <!-- 数值 -->
                <input
                  v-else-if="item.type === 'number'"
                  type="number"
                  class="brut-field w-32"
                  :value="draftOf(group, item)"
                  :min="item.min ?? undefined"
                  :max="item.max ?? undefined"
                  @change="setDraft(group, item, Number(($event.target as HTMLInputElement).value))"
                />

                <!-- 文本 -->
                <input
                  v-else
                  type="text"
                  class="brut-field font-mono"
                  :value="String(draftOf(group, item) ?? '')"
                  @change="setDraft(group, item, ($event.target as HTMLInputElement).value)"
                />

                <span class="brut-tag brut-tag-slab">{{ sourceText(item) }}</span>
                <span class="text-[11px] text-subtle">
                  <template v-if="item.min !== null || item.max !== null">
                    范围 {{ item.min ?? '−∞' }} ~ {{ item.max ?? '+∞' }}
                  </template>
                  <template v-if="item.min_exclusive !== null">
                    需大于 {{ item.min_exclusive }}
                  </template>
                </span>
              </div>
              <p class="mt-1 text-xs text-subtle">{{ item.help }}</p>
            </div>
          </div>
        </div>
      </section>
    </template>

    <!-- 危险项确认 -->
    <section v-if="pendingChange" class="brut-note brut-note-warn">
      <p class="text-sm font-bold">
        确定把「{{ pendingChange.label }}」改成 <b>{{ String(pendingChange.value) }}</b>？
      </p>
      <p class="mt-1 text-xs">
        这项属于「改错了不明显、但影响很大」的一类。保存后下一次生成即生效。
      </p>
      <div class="mt-3 flex items-center gap-2">
        <button type="button" class="brut-btn brut-btn-primary" @click="confirmPendingChange">
          确认修改
        </button>
        <button type="button" class="brut-btn" @click="cancelPendingChange">取消</button>
      </div>
    </section>

    <!-- 供应商操作确认 -->
    <section v-if="pendingProvider" class="brut-note brut-note-fail">
      <template v-if="pendingProvider.action === 'activate'">
        <p class="text-sm font-bold">
          切换到「{{ pendingProvider.provider.name }}」？下一次生成起所有模型调用都会走这家。
        </p>
        <p v-if="!pendingProvider.provider.key_present" class="mt-1 text-xs">
          注意：{{ pendingProvider.provider.api_key_env_var }} 当前没有值，切过去后生成会直接报错。
        </p>
      </template>
      <p v-else class="text-sm font-bold">
        确定删除「{{ pendingProvider.provider.name }}」？该配置将被移除。
      </p>
      <div class="mt-3 flex items-center gap-2">
        <button type="button" class="brut-btn brut-btn-danger" @click="confirmProviderAction">
          {{ pendingProvider.action === 'activate' ? '确认切换' : '确认删除' }}
        </button>
        <button type="button" class="brut-btn" @click="pendingProvider = null">取消</button>
      </div>
    </section>
  </div>
</template>
