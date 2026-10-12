/**
 * 登录态 store。
 *
 * 关键约束：**前端不知道自己是否登录**。会话是后端 HttpOnly Cookie，
 * JS 读不到，所以唯一的真相来源是 `GET /api/auth/me`。
 * 这里缓存的是「上次问到的结果」，刷新页面后必须重新问一次。
 *
 * 弹窗状态也放在这里，而不是组件里：触发登录窗的来源有三处
 * （顶栏主动点击、选择入口时被拦、任何接口返回 401），
 * 分散在各自组件里就会出现「两处都想弹窗」的互相打架。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  fetchCurrentUser,
  login as loginRequest,
  logout as logoutRequest,
  register as registerRequest,
} from '@/services/authService'
import type { AuthUser, LoginPayload, RegisterPayload } from '@/types'

/** 弹窗的两个页签 */
export type AuthDialogMode = 'login' | 'register'

export const useAuthStore = defineStore('auth', () => {
  // ------------------------------------------------------------------
  // 状态
  // ------------------------------------------------------------------

  /** 当前登录用户；`null` 表示未登录（或还没问过后端） */
  const user = ref<AuthUser | null>(null)

  /** 是否已完成首次登录态探测。未探测完时 `user` 为空不代表未登录 */
  const ready = ref(false)

  /** 登录 / 注册请求进行中，用于禁用按钮与显示「处理中」 */
  const submitting = ref(false)

  /** 提交过程中的错误提示，直接展示在弹窗里 */
  const errorMessage = ref('')

  /** 登录 / 注册弹窗是否展开 */
  const dialogOpen = ref(false)

  /** 弹窗当前显示的页签 */
  const dialogMode = ref<AuthDialogMode>('login')

  /**
   * 弹窗是**因为什么**被弹出的。
   * 为空表示用户主动点顶栏；非空时会在弹窗顶部说明原因（例如「创建任务需要先登录」）。
   */
  const promptReason = ref('')

  const isLoggedIn = computed(() => user.value !== null)

  /** 顶栏展示的文字：未登录是「登录」，已登录是昵称 */
  const displayName = computed(() => user.value?.username ?? '')

  // ------------------------------------------------------------------
  // 动作
  // ------------------------------------------------------------------

  /** 清空错误提示 */
  function clearError(): void {
    errorMessage.value = ''
  }

  /** 打开弹窗。`reason` 为空表示用户主动打开 */
  function openDialog(mode: AuthDialogMode = 'login', reason = ''): void {
    dialogMode.value = mode
    promptReason.value = reason
    errorMessage.value = ''
    dialogOpen.value = true
  }

  /** 关闭弹窗并清掉临时提示 */
  function closeDialog(): void {
    dialogOpen.value = false
    promptReason.value = ''
    errorMessage.value = ''
  }

  /**
   * 因为某个操作需要登录而弹出登录窗。
   *
   * 已经登录时不重复弹；请求进行中也不弹，避免覆盖用户正在填的表单。
   */
  function requireLogin(reason: string): void {
    if (isLoggedIn.value || dialogOpen.value) return
    openDialog('login', reason)
  }

  /**
   * 探测登录态。整个过程只跑一次（`ready` 之后直接返回）。
   *
   * 失败时按「未登录」处理而不是抛异常——后端没起、Cookie 过期、服务 500
   * 对用户的实际后果都是一样的：看到「登录」按钮，点了再试。
   */
  async function load(): Promise<void> {
    if (ready.value) return
    ready.value = true
    try {
      user.value = await fetchCurrentUser()
    } catch {
      user.value = null
    }
  }

  /** 登录。成功后关闭弹窗。 */
  async function login(payload: LoginPayload): Promise<boolean> {
    submitting.value = true
    errorMessage.value = ''
    try {
      user.value = await loginRequest(payload)
      closeDialog()
      return true
    } catch (error) {
      errorMessage.value = error instanceof Error ? error.message : '登录失败'
      return false
    } finally {
      submitting.value = false
    }
  }

  /** 注册。成功后后端直接签发会话，等同于登录，因此同样关闭弹窗。 */
  async function register(payload: RegisterPayload): Promise<boolean> {
    submitting.value = true
    errorMessage.value = ''
    try {
      user.value = await registerRequest(payload)
      closeDialog()
      return true
    } catch (error) {
      errorMessage.value = error instanceof Error ? error.message : '注册失败'
      return false
    } finally {
      submitting.value = false
    }
  }

  /**
   * 登出。
   *
   * 失败也要把本地状态清掉：后端吊销没成功，但 Cookie 已经删了，
   * 界面上继续显示「已登录」会让用户以为自己还登着。
   */
  async function logout(): Promise<void> {
    try {
      await logoutRequest()
    } catch {
      // 网络问题时忽略：本地状态无论如何都要清干净
    }
    user.value = null
    closeDialog()
  }

  return {
    // 状态
    user,
    ready,
    submitting,
    errorMessage,
    dialogOpen,
    dialogMode,
    promptReason,
    // 派生
    isLoggedIn,
    displayName,
    // 动作
    clearError,
    openDialog,
    closeDialog,
    requireLogin,
    load,
    login,
    register,
    logout,
  }
})
