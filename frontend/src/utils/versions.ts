/**
 * 文档版本的展示映射。
 *
 * 版本来源（`source`）是后端给的**机器取值**（`prd` / `api_docs` / `optimize` /
 * `checkpoint` …），把它翻成用户看得懂的说法收在这一个文件里：抽屉与以后任何
 * 展示版本的地方共用同一套说法，两处各写一份必然漂移。
 */

/** 来源取值 → 展示名。认不出的取值**原样返回**，不吞掉信息也不猜一个近似的。 */
const VERSION_SOURCE_LABELS: Record<string, string> = {
  prd: '生成 PRD',
  api_docs: '生成接口文档',
  prompts: '生成提示词',
  optimize: 'AI 优化',
  clarify: '按澄清改写',
  manual: '手动编辑',
  checkpoint: '存为新版本',
  restore: '恢复历史版本',
  migrated: '早期版本导入',
}

/** 把版本来源翻成展示名；未知来源原样显示 */
export function versionSourceLabel(source: string): string {
  return VERSION_SOURCE_LABELS[source] ?? source
}
