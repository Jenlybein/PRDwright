/**
 * 文档版本的展示映射。
 *
 * 版本来源（`source`）是后端给的**机器取值**（`prd` / `api_docs` / `optimize` /
 * `checkpoint` …），把它翻成用户看得懂的说法收在这一个文件里：抽屉与以后任何
 * 展示版本的地方共用同一套说法，两处各写一份必然漂移。
 */

/**
 * 来源取值 → 展示名。认不出的取值**原样返回**，不吞掉信息也不猜一个近似的。
 *
 * ⚠️ 这张表要覆盖**后端写得出的每一个来源**：`commit_generated` 的 `source` 直接取
 * `runtime.kind`，于是除了三份产物，还有 `optimize` / `clarify` / `auto_clarify` /
 * `review_fix` 四个动作；`checkpoint` / `restore` / `manual` 由各自的写入口给出。
 * 漏一个的后果不是报错，而是版本历史里出现裸标识符。
 *
 * `review`（重新审核）不在表里也**不需要**在：它只更新结论列，不追加版本，
 * 因此版本行上永远不会出现这个来源。
 */
const VERSION_SOURCE_LABELS: Record<string, string> = {
  prd: '生成 PRD',
  api_docs: '生成接口文档',
  prompts: '生成提示词',
  optimize: 'AI 优化',
  clarify: '按澄清改写',
  auto_clarify: 'AI 自行澄清',
  review_fix: '修复',
  manual: '手动编辑',
  checkpoint: '存为新版本',
  restore: '恢复历史版本',
  migrated: '早期版本导入',
}

/** 把版本来源翻成展示名；未知来源原样显示 */
export function versionSourceLabel(source: string): string {
  return VERSION_SOURCE_LABELS[source] ?? source
}
