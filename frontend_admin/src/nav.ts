/**
 * 后台导航的**单一真源**。
 *
 * 侧栏、窄屏横排导航、顶栏标题、配置页的分区，全部从这一份数据里长出来。
 * 加一页只改这里，不用去三四个地方各改一遍——漏改一处的症状是
 * 「窄屏能进、侧栏没有」，而这类不一致只有用窄屏的人会撞上。
 *
 * 两个结构上的取舍
 * ----------------
 * 1. **子菜单只在确有分层时出现**（目前只有「配置」）。一列十几个平级项的问题
 *    不是「排不下」，而是看不出哪几项是一伙的：配置下面有模型、生成、额度、
 *    安全四摊，摊平之后它们和「系统日志」看起来是同级的东西，其实不是。
 * 2. **子项落在 URL 上（`?sec=`），不是组件内部状态**。同一个页面切成几屏时，
 *    如果「我在哪一屏」只活在内存里，刷新、分享链接、浏览器后退就全都会
 *    把人送回第一屏。放到 query 上，侧栏高亮与页面内容读的是同一个值。
 *
 * ⚠️ `CONFIG_SECTIONS` 里的 `groups` 必须与后端 `admin_config_service._GROUPS`
 * 的分组键对得上。对不上不会报错，只会让那一组配置**只出现在「全部」里**，
 * 从子菜单进去就找不着——`config.spec.ts` 里有一条用例专门盯这件事。
 */

export interface NavChild {
  /** 路由名。子项与父项共用同一个路由名，靠 `sec` 区分 */
  name: string
  label: string
  /** 右侧那列等宽小字（OVERVIEW / JOBS …），纯装饰，不参与逻辑 */
  code: string
  /** 落在 URL 上的分区（`?sec=`）。没有就是「这一整页」 */
  sec?: string
}

export interface NavItem extends NavChild {
  children?: NavChild[]
}

export interface NavGroup {
  key: string
  label: string
  code: string
  items: NavItem[]
}

/** 配置页的一个分区 */
export interface ConfigSection {
  /** URL 上的取值（`?sec=`） */
  key: string
  label: string
  code: string
  /** 展示哪些配置分组（`admin_config_service._GROUPS` 的键） */
  groups: string[]
  /** 是否展示模型配置列表（供应商那一段） */
  providers: boolean
  /** 分区说明，进页头 */
  help: string
}

/**
 * 配置页的分区。
 *
 * ⚠️ 没有「全部」这一项：四个分区加起来就是全部配置，
 * 再摆一个「全部」等于给了两条路看同一堆东西——而多出来的那条
 * 恰好是最难扫的一条（一页里四摊参数连着排，找不到「并发」在哪一摊）。
 *
 * 第一项是**兜底**：`?sec=` 缺失或取值不认识时都落在这里，
 * 于是 `/config` 这个裸链接永远有内容，不会因为改了个拼写就白屏。
 */
export const CONFIG_SECTIONS: ConfigSection[] = [
  {
    key: 'model',
    label: '模型配置',
    code: 'MODEL',
    groups: [],
    providers: true,
    help: '一条配置 = 一个接口 + 一套参数。密钥由后端加密入库，列表只显示「已配置 / 未配置」。',
  },
  {
    key: 'generation',
    label: '模型生成配置',
    code: 'GEN',
    groups: ['prd', 'review'],
    providers: false,
    help: '控制 PRD 逐章撰写的节奏与审核强度。改完对下一次生成生效，正在跑的任务不受影响。',
  },
  {
    key: 'budget',
    label: '预算与额度',
    code: 'QUOTA',
    groups: ['llm', 'quota'],
    providers: false,
    help: '单次调用的超时与重试、发出请求前的 token 预算检查，以及每个用户每天能生成多少。',
  },
  {
    key: 'security',
    label: '安全与登录',
    code: 'AUTH',
    groups: ['auth'],
    providers: false,
    help: '登录失败限流与注册开关改完立刻生效；会话有效期只影响新登录——想立刻踢人用用户页的「强制下线」。',
  },
]

/** 兜底分区：`?sec=` 缺失或不认识时的落点 */
export const DEFAULT_SECTION: ConfigSection = CONFIG_SECTIONS[0]!

/** 兜底分区在 URL 上的取值。路由用它把裸 `/config` 补成 `/config?sec=…` */
export const DEFAULT_SEC: string = DEFAULT_SECTION.key

/** 按 `?sec=` 取分区；取不到就回落兜底，绝不返回 undefined */
export function sectionOf(sec: string | null | undefined): ConfigSection {
  return CONFIG_SECTIONS.find((s) => s.key === String(sec ?? '')) ?? DEFAULT_SECTION
}

export const NAV: NavGroup[] = [
  {
    key: 'ops',
    label: '运营',
    code: 'OPS',
    items: [
      { name: 'dashboard', label: '概览', code: 'OVERVIEW' },
      { name: 'jobs', label: '任务监控', code: 'JOBS' },
      { name: 'users', label: '用户', code: 'USERS' },
      { name: 'invites', label: '邀请码', code: 'INVITES' },
    ],
  },
  {
    key: 'sys',
    label: '系统',
    code: 'SYS',
    items: [
      { name: 'logs', label: '系统日志', code: 'LOGS' },
      {
        // ⚠️ 这一项**没有去处**：点它只是展开/收起子菜单。
        // 配页被切成了四个分区，「配置」本身不再是一屏——
        // 给它一个去处就等于造出第五个（和四个加起来一样多的）入口。
        name: 'config',
        label: '配置',
        code: 'CONFIG',
        // 子项与「配置页的分区」是同一份数据，不再手抄一遍标签——
        // 抄一遍的结果必然是某天改了一边，两边叫法不一致。
        children: CONFIG_SECTIONS.map((s) => ({
          name: 'config',
          label: s.label,
          code: s.code,
          sec: s.key,
        })),
      },
      { name: 'audit', label: '操作记录', code: 'AUDIT' },
    ],
  },
]

/**
 * 扁平化一份，供窄屏横排导航与「当前页是什么」共用。
 *
 * 有子项的项**只摊子项**：父项没有去处，摊进来就是一个点了不跳的按钮。
 */
export const flatNav: (NavItem | NavChild)[] = NAV.flatMap((group) =>
  group.items.flatMap((item) => item.children ?? [item]),
)
