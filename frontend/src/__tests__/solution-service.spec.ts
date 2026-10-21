import { AxiosError, AxiosHeaders } from 'axios'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import http from '../services/api'
import {
  createSolution,
  deleteSolution,
  fetchSolution,
  fetchSolutions,
  SolutionNotFoundError,
  updateSolution,
} from '../services/solutionService'
import type { SolutionSavePayload } from '../types'
import { toState } from './support/solutionState'

/**
 * 方案接口测试。
 *
 * 这一层值得单独测的不是「发没发请求」，而是几处**约定**：
 * 1. 404 的语义分叉——`fetchSolution` 抛 `SolutionNotFoundError`、
 *    `updateSolution` 返回 `null`、`deleteSolution` 直接当成功。
 *    三者处理方式不同是有意的，改错了不会报错，只会让自动保存对着不存在的 id 空转；
 * 2. 后端错误文案要透传（`detail` 字段），不能吞成一句「请求失败」；
 * 3. 响应字段缺失时要收敛成安全默认值而不是让界面炸掉。
 */

vi.mock('../services/api', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))

/** 造一个带后端错误响应体的 AxiosError（`isAxiosError` 为真） */
function axiosError(status: number, detail?: unknown): AxiosError {
  const error = new AxiosError('请求失败', 'ERR_BAD_RESPONSE')
  error.response = {
    status,
    statusText: '',
    data: detail === undefined ? {} : { detail },
    headers: {},
    config: { headers: new AxiosHeaders() },
  }
  return error
}

const PAYLOAD: SolutionSavePayload = {
  title: '订单系统',
  mode: 'structured',
  stage: 'form',
  state: toState({ viewState: 'form' }),
  content_changed: true,
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('fetchSolutions', () => {
  it('把后端响应收敛成摘要列表', async () => {
    vi.mocked(http.get).mockResolvedValue({
      data: {
        solutions: [
          {
            id: 3,
            title: '订单系统',
            mode: 'structured',
            stage: 'review-prd',
            created_at: '2026-10-13T01:00:00',
            updated_at: '2026-10-13T02:00:00',
          },
        ],
      },
    })

    const items = await fetchSolutions()

    expect(http.get).toHaveBeenCalledWith('/api/solutions')
    expect(items).toHaveLength(1)
    expect(items[0]?.title).toBe('订单系统')
  })

  it('响应里 solutions 缺失时返回空数组而不是抛错', async () => {
    vi.mocked(http.get).mockResolvedValue({ data: {} })

    await expect(fetchSolutions()).resolves.toEqual([])
  })

  it('透传后端的 detail 文案', async () => {
    vi.mocked(http.get).mockRejectedValue(axiosError(500, '数据库连接失败'))

    await expect(fetchSolutions()).rejects.toThrow('数据库连接失败')
  })

  it('取不到 detail 时用兜底文案', async () => {
    vi.mocked(http.get).mockRejectedValue(new Error('网络断了'))

    await expect(fetchSolutions()).rejects.toThrow('无法获取方案列表')
  })
})

describe('fetchSolution', () => {
  it('返回摘要 + 快照正文', async () => {
    vi.mocked(http.get).mockResolvedValue({
      data: {
        id: 7,
        title: '订单系统',
        mode: 'structured',
        stage: 'form',
        created_at: '',
        updated_at: '',
        state: toState({ viewState: 'form' }),
      },
    })

    const detail = await fetchSolution(7)

    expect(http.get).toHaveBeenCalledWith('/api/solutions/7')
    expect(detail.state.view_state).toBe('form')
  })

  it('404 抛 SolutionNotFoundError（与网络异常区分开）', async () => {
    vi.mocked(http.get).mockRejectedValue(axiosError(404, '方案不存在'))

    await expect(fetchSolution(7)).rejects.toBeInstanceOf(SolutionNotFoundError)
  })

  it('其他错误抛普通 Error，保留后端文案', async () => {
    vi.mocked(http.get).mockRejectedValue(axiosError(503, '服务暂不可用'))

    const error = await fetchSolution(7).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(Error)
    expect(error).not.toBeInstanceOf(SolutionNotFoundError)
    expect((error as Error).message).toBe('服务暂不可用')
  })
})

describe('createSolution', () => {
  it('POST 整个载荷并返回新摘要', async () => {
    vi.mocked(http.post).mockResolvedValue({
      data: {
        id: 9,
        title: '订单系统',
        mode: 'structured',
        stage: 'form',
        created_at: '',
        updated_at: '',
      },
    })

    const created = await createSolution(PAYLOAD)

    expect(http.post).toHaveBeenCalledWith('/api/solutions', PAYLOAD)
    expect(created.id).toBe(9)
  })

  it('校验失败（422 且 detail 是数组）时给可读文案', async () => {
    vi.mocked(http.post).mockRejectedValue(axiosError(422, [{ msg: 'bad' }]))

    await expect(createSolution(PAYLOAD)).rejects.toThrow('提交的内容不符合要求，请检查后重试')
  })
})

describe('updateSolution', () => {
  it('PUT 整份覆盖并返回详情', async () => {
    vi.mocked(http.put).mockResolvedValue({
      data: {
        id: 5,
        title: '订单系统',
        mode: 'structured',
        stage: 'chatting',
        created_at: '',
        updated_at: '',
        state: { view_state: 'chatting' },
      },
    })

    const updated = await updateSolution(5, PAYLOAD)

    expect(http.put).toHaveBeenCalledWith('/api/solutions/5', PAYLOAD)
    expect(updated?.stage).toBe('chatting')
  })

  it('404 返回 null——调用方据此停止自动保存，而不是无限重试', async () => {
    vi.mocked(http.put).mockRejectedValue(axiosError(404, '方案不存在'))

    await expect(updateSolution(5, PAYLOAD)).resolves.toBeNull()
  })

  it('其他错误照常抛出', async () => {
    vi.mocked(http.put).mockRejectedValue(axiosError(500, '写入失败'))

    await expect(updateSolution(5, PAYLOAD)).rejects.toThrow('写入失败')
  })
})

describe('deleteSolution', () => {
  it('正常删除', async () => {
    vi.mocked(http.delete).mockResolvedValue({ data: { ok: true } })

    await expect(deleteSolution(4)).resolves.toBeUndefined()
    expect(http.delete).toHaveBeenCalledWith('/api/solutions/4')
  })

  it('404 视为删除成功——用户的意图是「让它消失」，而它确实不在了', async () => {
    vi.mocked(http.delete).mockRejectedValue(axiosError(404, '方案不存在'))

    await expect(deleteSolution(4)).resolves.toBeUndefined()
  })

  it('其他错误照常抛出', async () => {
    vi.mocked(http.delete).mockRejectedValue(axiosError(500, '删不掉'))

    await expect(deleteSolution(4)).rejects.toThrow('删不掉')
  })
})
