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
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import AdminModal from '@/components/AdminModal.vue'
import PageHeader from '@/components/PageHeader.vue'

import {
  activateProvider,
  createProvider,
  deleteProvider,
  fetchConfig,
  fetchProviders,
  messageOf,
  resetConfig,
  revealProviderKey,
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

/**
 * 新增配置表单。
 *
 * ⚠️ **没有 `id`**：它由后端生成（`cfg_xxx`）。同一条协议可以有多条配置，
 * 让管理员手填 id 只会撞名；而撞名后唯一可做的决定是「谁让」——没有业务价值。
 * `presetKey` 只是下拉框的选中值，用于「从模板填表」，不参与提交。
 */
const showCreate = ref(false)
const form = ref({
  presetKey: '',
  name: '',
  protocol: 'openai_compatible',
  base_url: '',
  api_key: '',
  default_model: '',
  max_tokens: 4096,
  context_window: 0,
})

/** 正在编辑的那一条（`null` = 没在编辑）。编辑面板独立于新增表单。 */
const editing = ref<Provider | null>(null)
const savingEdit = ref(false)
const editForm = ref({
  name: '',
  base_url: '',
  api_key: '',
  clear_key: false,
  default_model: '',
  max_tokens: 4096,
  context_window: 0,
  max_output: 0,
  thinking_enabled: false,
})

/** 测试连接的结果（按供应商 id 存） */
const testResults = ref<Record<string, { ok: boolean; detail: string }>>({})
/** 已查看过明文的密钥（按 id 存；只在本页内存里，刷新即消失） */
const revealedKeys = ref<Record<string, string>>({})
const testingId = ref('')

/**
 * 测试连接**成功**时的悬浮提示。
 *
 * 成功是「看一眼就够」的信息，不该在列表行里长期占一行；失败则继续用行内展示
 * ——原因往往很长（域名解析失败 / 401 / base_url 少了 /v1），需要停下来读、照着改。
 * 所以这个浮层只承载成功，4 秒后自动收起。
 */
const toast = ref<{ text: string } | null>(null)
let toastTimer: ReturnType<typeof setTimeout> | null = null

function showToast(text: string) {
  toast.value = { text }
  if (toastTimer) clearTimeout(toastTimer)
  toastTimer = setTimeout(() => {
    toast.value = null
  }, 4000)
}

onBeforeUnmount(() => {
  if (toastTimer) clearTimeout(toastTimer)
})

const groups = computed<ConfigGroup[]>(() => config.value?.groups ?? [])
const providers = computed<Provider[]>(() => providerList.value?.providers ?? [])
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

/**
 * 用模板填表：把「这家怎么接」一次带齐。
 *
 * 模板只是**表单初值**，不是一条已存在的配置——`preset.key` 与配置 id（`cfg_xxx`）
 * 完全是两回事。选完仍可改地址与模型，改完才由后端生成 id。
 *
 * 「供应商」这一栏是整张表的**驱动项**：协议、地址、密钥变量名、建议模型都由它带出来。
 *
 * 于是有两项**不进表单**：
 * - **协议**：由供应商决定，创建后也不可改。
 * - **密钥变量名**：由供应商决定（`ARK_API_KEY` / `DEEPSEEK_API_KEY` …），
 *   它只是「去哪个环境变量取密钥」的指针，不是需要人来做的决定。
 *
 * 都不能改的东西就不该摆在表单里冒充可改项——灰掉的框同样在暗示「这里本该能改」。
 * 需要核对的场合看得到：列表行的元信息里有变量名，协议在列表与编辑标题上。
 */
function onPickPreset(event: Event) {
  const presetKey = (event.target as HTMLSelectElement).value
  if (!presetKey) {
    // 回到「还没选」：不动已填的值，只把驱动项清掉
    form.value.presetKey = ''
    return
  }

  const preset = presets.value.find((p) => p.key === presetKey)
  if (!preset) return

  // 配置名只在这两种情况下跟着模板走：还没填、或还是**上一个模板**带出来的名字。
  // 否则会把用户自己起的「方舟-生产」冲掉。
  const previousName = presets.value.find((p) => p.key === form.value.presetKey)?.name ?? ''
  const keepName = form.value.name.trim() && form.value.name !== previousName

  form.value = {
    presetKey,
    name: keepName ? form.value.name : preset.name,
    protocol: preset.protocol,
    base_url: preset.base_url,
    // 换供应商时保留已填的密钥（它跟供应商模板无关，由管理员自己录入）。
    api_key: form.value.api_key,
    default_model: preset.default_model,
    max_tokens: form.value.max_tokens,
    context_window: form.value.context_window,
  }
}

function resetCreateForm() {
  form.value = {
    presetKey: '',
    name: '',
    protocol: 'openai_compatible',
    base_url: '',
    api_key: '',
    default_model: '',
    max_tokens: 4096,
    context_window: 0,
  }
}

/** 打开「新增配置」弹窗：每次从空白表单开始，避免上一次的残留值被误提交。 */
function openCreate() {
  resetCreateForm()
  showCreate.value = true
}

/** 关闭新增弹窗并丢弃未提交内容。 */
function closeCreate() {
  showCreate.value = false
  resetCreateForm()
}

async function onCreate() {
  if (creating.value) return
  // 只做「必填」这一层提示，真正的规则仍在后端（名字为空它会 400）。
  // 这里拦一下是为了不让一次明显的空填跑一趟网络、再回一句同样的话。
  //
  // 供应商也是必选：协议、地址、变量名全靠它带出来。没选就提交，得到的是
  // 一条「协议默认 openai_compatible、地址空着」的配置——看着建成了，第一次
  // 生成才报错，而报错里还看不出是当初少选了一步。
  if (!form.value.presetKey) {
    sayFail('请先选供应商——协议和地址都由它决定')
    return
  }
  if (!form.value.name.trim()) {
    sayFail('请先填配置名——列表里就靠它认出这是哪一条')
    return
  }
  creating.value = true
  const payload = {
    name: form.value.name.trim(),
    protocol: form.value.protocol,
    base_url: form.value.base_url,
    // 密钥是明文，只用于本次加密入库；留空表示暂不配。
    ...(form.value.api_key ? { api_key: form.value.api_key } : {}),
    default_model: form.value.default_model,
    max_tokens: Number(form.value.max_tokens) || 4096,
    context_window: Number(form.value.context_window) || 0,
  }
  let createdWarning = ''
  const result = await toResult(async () => {
    const created = await createProvider(payload)
    createdWarning = created.warning
    providerList.value = created
    await load()
  })
  creating.value = false

  if (!result.ok) {
    sayFail(result.message)
    return
  }
  resetCreateForm()
  showCreate.value = false
  // 后端会在「这是第一条、已自动设为使用中」或「密钥还没配」时给一句 warning。
  // 直接用它，而不是自己拼一句固定话：判断在服务端，前端再拼一遍就会漂移。
  sayOk(createdWarning || '配置已添加。填入密钥后点「测试连接」验证')
}

/**
 * 打开编辑面板：把这一条**可改**的值抄进表单。
 *
 * 协议与密钥变量名都不抄——它们由供应商决定、创建后不可改（换供应商请新建一条）。
 * 不抄也就不会出现在请求体里，这两项保持原值。
 */
function openEdit(provider: Provider) {
  editing.value = provider
  editForm.value = {
    name: provider.name,
    base_url: provider.base_url,
    api_key: '',
    clear_key: false,
    default_model: provider.default_model,
    max_tokens: provider.max_tokens,
    context_window: provider.context_window,
    max_output: provider.max_output,
    thinking_enabled: provider.thinking_enabled,
  }
}

function cancelEdit() {
  editing.value = null
}

/**
 * 保存编辑。
 *
 * ⚠️ **不发 `protocol`**：它由供应商决定、创建后不可改。密钥单独用 `api_key` 字段提交
 * （换供应商等于换一家厂商，参数语义与密钥变量全变）。它们不在表单里，也就不会出现在
 * 请求体里——发了相同值虽然会放行，但那份「看起来能改」的请求体会让下一个人以为它真的能改。
 */
async function onSaveEdit() {
  const target = editing.value
  if (!target || savingEdit.value) return
  if (!editForm.value.name.trim()) {
    sayFail('配置名不能为空')
    return
  }

  savingEdit.value = true
  const result = await toResult(async () => {
    providerList.value = await updateProvider(target.id, {
      name: editForm.value.name.trim(),
      base_url: editForm.value.base_url,
      ...(editForm.value.api_key ? { api_key: editForm.value.api_key } : {}),
      ...(editForm.value.clear_key ? { clear_key: true } : {}),
      default_model: editForm.value.default_model,
      max_tokens: Number(editForm.value.max_tokens) || 4096,
      context_window: Number(editForm.value.context_window) || 0,
      max_output: Number(editForm.value.max_output) || 0,
      thinking_enabled: editForm.value.thinking_enabled,
    })
    await load()
  })
  savingEdit.value = false

  if (!result.ok) {
    sayFail(result.message)
    return
  }
  editing.value = null
  sayOk(`「${target.name}」已更新（下一次生成生效）`)
}

/** 查看密钥明文：点一次取一次，并写进审计（后端负责）。 */
async function revealKey(provider: Provider) {
  const result = await toResult(async () => {
    const data = await revealProviderKey(provider.id)
    revealedKeys.value = { ...revealedKeys.value, [provider.id]: data.api_key }
  })
  if (!result.ok) sayFail(result.message)
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
    // 成功走悬浮提示（一眼即够）；失败仍进顶部横幅 + 行内原因（便于细读与对照修改）。
    if (outcome?.ok) showToast(`「${provider.name}」${outcome.detail || '连通正常'}`)
    else sayFail(`「${provider.name}」连接失败：${outcome?.detail ?? ''}`)
  }
}

function sourceText(item: ConfigItem): string {
  return item.source === 'db' ? '后台覆盖' : '默认'
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
    <PageHeader kicker="Config / 运行时" title="系统配置">
      <template #actions>
        <span v-if="config" class="brut-tag brut-tag-slab">REV {{ config.revision }}</span>
      </template>
      <template #desc>
        <p>
          改完保存后立即作用于<b>下一次生成</b>；正在跑的任务不受影响。
          可调项<b>全在这一页</b>——<code class="font-mono">.env</code>
          里只剩启动项，没有第二份。
          日志级别、落库开关这类「改了必须重启」的配置不在此页——它们改了不重启只会造成错觉。
        </p>
      </template>
    </PageHeader>

    <p v-if="error" class="brut-note brut-note-fail">{{ error }}</p>
    <p v-if="notice" class="brut-note brut-note-ok">{{ notice }}</p>

    <!-- 测试连接的悬浮提示（成功）：固定右上角，不挤占列表行，4 秒自动收起。 -->
    <div
      v-if="toast"
      class="brut-card fixed right-4 top-4 z-[60] max-w-sm px-4 py-3 text-sm font-extrabold text-success-fg"
      role="status"
      aria-live="polite"
    >
      ✓ {{ toast.text }}
    </div>

    <div v-if="loading" class="brut-card px-4 py-8 text-sm text-subtle">加载中…</div>

    <template v-else-if="config">
      <!-- 当前生效状态：排查「改了没生效」的第一眼就该落在这里 -->
      <section class="brut-slab-sky p-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div class="min-w-0">
            <p class="kicker text-on-sky/75">当前供应商 · Active Provider</p>
            <p class="mt-1 text-xl font-extrabold tracking-tight">
              {{ config.active_provider?.name || '还没有模型配置' }}
            </p>
            <p class="mt-0.5 font-mono text-xs text-on-sky/80">
              <template v-if="config.active_provider">
                {{ config.active_provider.protocol_label }} ·
                {{ config.active_provider.default_model || '未填模型' }}
              </template>
              <template v-else>在下方「模型配置」里新增一条即可，生成会立刻可用</template>
            </p>
          </div>

          <div class="text-right">
            <p class="kicker text-on-sky/75">配置来源</p>
            <p class="mt-1 text-sm font-bold">
              {{
                config.config_source === 'db'
                  ? '数据库（含后台覆盖）'
                  : config.config_source === 'env'
                    ? '默认值（未在后台改过）'
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

      <!-- 模型配置：一条 = 一个接口 + 一套参数，用列表而不是「一家一张卡」 -->
      <section class="brut-card-lg">
        <header class="flex flex-wrap items-center justify-between gap-2 border-b-2 border-brut px-4 py-3">
          <div>
            <p class="kicker">Providers</p>
            <h2 class="mt-1 text-base font-extrabold tracking-tight text-ink">模型配置</h2>
          </div>
          <div class="flex flex-wrap items-center justify-end gap-2">
            <p class="hidden max-w-sm text-xs text-subtle sm:block">
              密钥由后端加密入库，列表只显示<b class="text-muted">已配置 / 未配置</b>。
              同一家可以有多条配置（生产/测试各一条）。
            </p>
            <button type="button" class="brut-btn brut-btn-xs brut-btn-primary" @click="openCreate">
              + 新增配置
            </button>
          </div>
        </header>

        <!-- 一条都没有：这不是异常状态，是刚装完的样子。
             ⚠️ 这里不放按钮——右上角那个「+ 新增配置」就在同一屏，
             两个入口做同一件事只会让人先愣一下「这两个是不是不一样」。 -->
        <div v-if="!providers.length" class="px-4 py-6">
          <p class="text-sm font-extrabold tracking-tight text-ink">还没有任何模型配置</p>
          <p class="mt-1 max-w-xl text-xs text-subtle">
            点右上角<b class="text-muted">「+ 新增配置」</b>建第一条：选供应商、写个配置名就够。
            在此之前生成无法进行——地址、模型、密钥全部由这里的配置决定，
            .env 里不再有第二份。
          </p>
        </div>

        <ul v-else class="divide-y-2 divide-brut">
          <li
            v-for="p in providers"
            :key="p.id"
            class="grid grid-cols-1 items-start gap-2 px-4 py-3 lg:grid-cols-[6.5rem_minmax(0,1fr)_auto]"
          >
            <!-- 状态：实心点＝使用中，空心点＝待命/停用。
                 形状与颜色同时变化，不依赖单一通道（色弱也能分清）。 -->
            <div class="flex items-center gap-1.5">
              <span
                class="inline-block h-2.5 w-2.5 shrink-0 rounded-full border-2 border-brut"
                :class="p.active ? 'bg-sky' : 'bg-card'"
              />
              <span class="brut-tag" :class="providerState(p).cls">
                {{ providerState(p).text }}
              </span>
            </div>

            <div class="min-w-0">
              <div class="flex flex-wrap items-baseline gap-1.5">
                <span class="text-sm font-extrabold tracking-tight text-ink">{{ p.name }}</span>
                <span class="text-subtle">·</span>
                <span class="font-mono text-xs text-muted">{{ p.default_model || '未填模型' }}</span>
              </div>

              <!-- 机器可读的那一行：协议 / 密钥状态 / 输出上限。
                   密钥变量名之后只跟「有没有值」，绝不显示值本身。
                   协议写中文名、上限写人话——`ark`、`max_tokens` 这类是代码里的叫法，
                   管理员在界面上找不到对应的东西。 -->
              <p class="mt-1 flex flex-wrap items-center gap-x-2 text-[11px] text-subtle">
                <span>{{ p.protocol_label }}</span>
                <span>｜</span>
                <span>密钥</span>
                <span v-if="p.key_present" class="font-bold text-success-fg">✓ 已配置</span>
                <span v-else class="font-bold text-danger-fg">⚠ 未配置</span>
                <button
                  v-if="p.key_present"
                  type="button"
                  class="brut-btn brut-btn-xs"
                  @click="revealKey(p)"
                >
                  查看密钥
                </button>
                <span>｜</span>
                <span>输出上限 {{ p.max_tokens }}</span>
              </p>

              <p
                v-if="revealedKeys[p.id]"
                class="mt-1 break-all border-l-4 border-brut pl-2 font-mono text-[11px]"
              >
                密钥：{{ revealedKeys[p.id] }}
              </p>

              <!-- 只保留**失败**的行内原因：成功已改由右上角悬浮提示展示，
                   不再在这里长期占一行。原因写全，方便照着细节直改配置。 -->
              <p
                v-if="testResults[p.id] && !testResults[p.id]!.ok"
                class="mt-1 border-l-4 border-danger-line pl-2 text-[11px] font-bold text-danger-fg"
              >
                连接失败 · {{ testResults[p.id]!.detail }}
              </p>
            </div>

            <!-- 动作：主操作在最右。
                 ⚠️ 「使用中的不能删」这条**不在这里判断**——按钮照给，让后端用 403 + 原话
                 回答。前端藏起按钮只会让人以为「这行坏了」，而且规则一改两边就不一致。 -->
            <div class="flex flex-wrap items-center gap-1.5 lg:justify-end">
              <button
                v-if="!p.active"
                type="button"
                class="brut-btn brut-btn-xs"
                :disabled="!p.enabled"
                @click="pendingProvider = { action: 'activate', provider: p }"
              >
                设为使用中
              </button>
              <button type="button" class="brut-btn brut-btn-xs" @click="openEdit(p)">编辑</button>
              <button
                type="button"
                class="brut-btn brut-btn-xs"
                :disabled="testingId === p.id"
                @click="onTest(p)"
              >
                {{ testingId === p.id ? '测试中…' : '测试连接' }}
              </button>
              <button type="button" class="brut-btn brut-btn-xs" @click="toggleEnabled(p)">
                {{ p.enabled ? '停用' : '启用' }}
              </button>
              <button
                type="button"
                class="brut-btn brut-btn-xs border-danger-line text-danger-fg"
                @click="pendingProvider = { action: 'delete', provider: p }"
              >
                删除
              </button>
            </div>
          </li>
        </ul>

    <!-- 编辑弹窗。用统一的 AdminModal：Esc 关闭、锁页面滚动、打开后聚焦面板，
         这些行为不该每个弹窗各写一遍。 -->
    <AdminModal
      :open="Boolean(editing)"
      kicker="Edit provider"
      :title="`编辑「${editing?.name ?? ''}」`"
      size="lg"
      @close="cancelEdit"
    >
      <template #subtitle>
        <span v-if="editing" class="mt-1 inline-flex">
          <span class="brut-tag">{{ editing.protocol_label }}</span>
        </span>
      </template>

      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <label class="block">
          <span class="brut-label">配置名（必填）</span>
          <input v-model="editForm.name" type="text" class="brut-field" />
        </label>

        <label class="block sm:col-span-2">
          <span class="brut-label">接口地址</span>
          <input v-model="editForm.base_url" type="text" class="brut-field font-mono" />
        </label>

        <!-- 密钥：留空表示不动；填了才会重加密覆盖。明文只在本次提交里出现。 -->
        <label class="block sm:col-span-2">
          <span class="brut-label">API 密钥（留空=保持不变）</span>
          <input
            v-model="editForm.api_key"
            type="password"
            placeholder="填入新密钥才会覆盖"
            autocomplete="new-password"
            class="brut-field font-mono"
          />
        </label>

        <label class="flex items-center gap-2">
          <input v-model="editForm.clear_key" type="checkbox" class="brut-check" />
          <span class="brut-label">清空密钥</span>
        </label>
        <label class="block">
          <span class="brut-label">默认模型</span>
          <input v-model="editForm.default_model" type="text" class="brut-field font-mono" />
        </label>

        <label class="block">
          <span class="brut-label">输出上限</span>
          <input v-model.number="editForm.max_tokens" type="number" class="brut-field" />
        </label>

        <label class="block">
          <span class="brut-label">上下文窗口（0=用内置默认）</span>
          <input v-model.number="editForm.context_window" type="number" class="brut-field" />
        </label>

        <label class="block">
          <span class="brut-label">预留输出（0=用内置默认）</span>
          <input v-model.number="editForm.max_output" type="number" class="brut-field" />
        </label>

        <label class="block">
          <span class="brut-label">思考模式</span>
          <select v-model="editForm.thinking_enabled" class="brut-field">
            <option :value="false">关闭</option>
            <option :value="true">开启</option>
          </select>
        </label>
      </div>

      <p class="mt-2 text-xs text-subtle">
        换供应商请<b class="text-muted">新建一条配置</b>：协议一变，
        输出上限与思考模式的语义全都不同，就地改会留下半新半旧的字段。
      </p>

      <template #footer>
        <button
          type="button"
          class="brut-btn brut-btn-primary"
          :disabled="savingEdit"
          @click="onSaveEdit"
        >
          {{ savingEdit ? '保存中…' : '保存修改' }}
        </button>
        <button type="button" class="brut-btn" @click="cancelEdit">取消</button>
      </template>
    </AdminModal>

    <!-- 新增配置弹窗 -->
    <AdminModal
      :open="showCreate"
      kicker="New provider"
      title="新增配置"
      size="lg"
      @close="closeCreate"
    >
      <p class="text-xs text-subtle">
        选好<b class="text-muted">供应商</b>，协议与地址就定了，剩下只要写配置名和模型。
        要接自建网关，选「本地 vLLM / Ollama」或「自建网关」再把地址改成你的端点。
      </p>

      <div class="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <label class="block">
          <span class="brut-label">供应商</span>
          <select class="brut-field" :value="form.presetKey" @change="onPickPreset">
            <option value="">请选择供应商…</option>
            <option v-for="preset in presets" :key="preset.key" :value="preset.key">
              {{ preset.name }}
            </option>
          </select>
        </label>

        <label class="block">
          <span class="brut-label">配置名（必填，可重名）</span>
          <input
            v-model="form.name"
            type="text"
            placeholder="如 方舟-生产"
            class="brut-field"
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

        <label class="block sm:col-span-2">
          <span class="brut-label">接口地址（OpenAI 兼容类通常要带 /v1）</span>
          <input
            v-model="form.base_url"
            type="text"
            placeholder="https://api.deepseek.com/v1"
            class="brut-field font-mono"
          />
        </label>

        <!-- 密钥：明文只在提交时出现，后端加密入库。本地 vLLM 可填 not-needed。 -->
        <label class="block sm:col-span-2">
          <span class="brut-label">API 密钥（加密入库；本地服务可填 not-needed）</span>
          <input
            v-model="form.api_key"
            type="password"
            placeholder="留空=稍后再配"
            autocomplete="new-password"
            class="brut-field font-mono"
          />
        </label>

        <label class="block">
          <span class="brut-label">输出上限</span>
          <input v-model.number="form.max_tokens" type="number" class="brut-field" />
        </label>

        <label class="block">
          <span class="brut-label">上下文窗口（0=用内置默认）</span>
          <input v-model.number="form.context_window" type="number" class="brut-field" />
        </label>
      </div>

      <p class="mt-2 text-xs text-subtle">
        自定义模型建议填「上下文窗口」：不填会按 32k 保守估算，长文可能被误判为超出预算。
      </p>

      <template #footer>
        <button
          type="button"
          class="brut-btn brut-btn-primary"
          :disabled="creating"
          @click="onCreate"
        >
          {{ creating ? '添加中…' : '添加配置' }}
        </button>
        <button type="button" class="brut-btn" @click="closeCreate">取消</button>
      </template>
    </AdminModal>
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
              <!-- 说明跟着名字走：左侧是「这是什么」，右侧是「怎么改」。
                   原来这里印的是 item.key（PRD_PIPELINE_CONCURRENCY 这种变量名）——
                   管理员不需要知道配置在代码里叫什么，那一行既占位置又只有噪音。 -->
              <p class="mt-1 text-xs leading-relaxed text-subtle">{{ item.help }}</p>
            </div>

            <div class="min-w-0">
              <!-- 控件靠右：左边是「这是什么」，右边是「改成什么」，
                   两端对齐之后每一行的输入框落在同一条竖线上，扫视时不用来回找。
                   窄屏（单列堆叠）保持左对齐——那时控件在标签正下方，贴右反而脱节。 -->
              <div class="flex flex-wrap items-center gap-2 sm:justify-end">
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

                <!-- 文本：给个上限，否则 width:100% 会撑满整列，
                     右对齐就只剩「左边起点也不齐」这一个结果 -->
                <input
                  v-else
                  type="text"
                  class="brut-field font-mono max-w-[22rem]"
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
          注意：这条配置还没有密钥，切过去后生成会直接报错。
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
