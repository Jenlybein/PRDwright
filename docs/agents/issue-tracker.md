# Issue tracker：GitHub

本仓库的 issue 与规格住在 GitHub Issues：**[Jenlybein/PRDwright](https://github.com/Jenlybein/PRDwright)**（public，Issues 已开启）。一切读写走 `gh` CLI，不要在本仓库里另开 `.scratch/` 目录存工单。

`gh` 在 clone 目录内会自动推断仓库；首次 `gh issue create` 前先确认识别对的是自己的仓库：

```bash
gh repo view --json nameWithOwner   # 期望输出 Jenlybein/PRDwright
```

## 常用操作

- **新建**：`gh issue create --title "..." --body "..."`。多行正文用 heredoc。
- **读单条**：`gh issue view <n> --json number,title,body,labels,comments`。
- **列表**：`gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'`，配合 `--label` / `--state` 过滤。
- **设为子 issue**：`gh issue create --parent <parent> ...`，或事后 `gh issue edit <parent> --add-sub-issue <child>`（`gh` 2.94+）。旧版走 `gh api --method POST repos/<owner>/<repo>/issues/<parent>/sub_issues -F sub_issue_id=<child-db-id>`（用 **database id**）。子 issue 不可用时，在子 issue 正文首行写 `Part of #<parent>`。
- **阻塞关系**：用 GitHub 原生 issue dependency —— `gh issue create --blocked-by <n>,<n> ...` 或 `gh issue edit <child> --add-blocked-by <blocker>`（`gh` 2.94+）。旧版：`gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`，其中 `<blocker-db-id>` 是阻塞者的数字 **database id**（`gh api repos/<owner>/<repo>/issues/<n> --jq .id`，**不是** `#number`，也不是 `node_id`）。不可用时退化为正文首行 `Blocked by: #<n>, #<n>`。所有阻塞者都关闭时才算解除。
- **数还开着的阻塞者**：`gh api repos/<owner>/<repo>/issues/<n> --jq .issue_dependencies_summary.blocked_by`（只数开着的，0 表示可以开工）。注意 `gh issue view <n> --json blockedBy` **也会列出已关闭的**，需过滤：`--jq '[.blockedBy.nodes[] | select(.state == "OPEN")] | length'`。
- **评论**：`gh issue comment <n> --body "..."`。
- **加／去标签**：`gh issue edit <n> --add-label "..."` / `--remove-label "..."`。
- **关闭**：先用 `gh issue comment` 留下理由，再 `gh issue close <n>`。注意对已关闭的 issue 用 `gh issue close --comment` **会吞掉评论但仍返回 0**。

## 本仓库附加约定

- 标签词表见 [`triage-labels.md`](./triage-labels.md)，五个角色一一对应，不要临时造第六个。
- issue 里提到架构决策时**直接写清结论与所在层**（如「v8 的方言收敛收进 `core/dialect.py`」），不再有全局编号；决策正文在各层 `ARCHITECTURE.md` 的「取舍」节与 issue 历史里。
- 术语用 [`GLOSSARY.md`](../../GLOSSARY.md) 的写法：说「方案」不说「项目」，说「被取代」不说「中止」。
- CI 只在 **push 之后**才真跑（`.github/workflows/ci.yml`），本地绿灯不等于远端绿灯。
- 本仓库铁律：**提交与推送前必须经用户确认**（先 `git add -An .` 预演）。agent 不要自作主张 commit。

## PR 是否算一种需求入口

**否。** （置为 `yes` 才表示本仓库把外部 PR 当成功能请求；`/triage` 会读这一行。）

置为 `yes` 后，PR 与 issue 走同一套标签与状态，命令换成 `gh pr` 系列：

- 读：`gh pr view <n>`，评论 `gh pr view <n> --comments`（非 TTY 下只打印评论、不含正文），差异用 `gh pr diff <n>`。
- 列出待分诊的外部 PR：`gh api --paginate 'repos/{owner}/{repo}/pulls?state=open' --jq '.[] | select(.author_association | IN("OWNER","MEMBER","COLLABORATOR") | not) | {number, title, author: .user.login, author_association, labels: [.labels[].name]}'`。
- 评论／贴标签／关闭：`gh pr comment`、`gh pr edit --add-label`／`--remove-label`、`gh pr close`。

GitHub 的 issue 与 PR **共用一套编号**，所以裸写的 `#42` 可能是两者之一：先 `gh pr view 42`，失败再 `gh issue view 42`。

## 当某个 skill 说「发到 issue tracker」

→ 建一个 GitHub issue。

## 当某个 skill 说「取回对应工单」

→ 按上面「读单条」取回。

## Wayfinding（`/wayfinder`）

**地图**是一张 issue，**票**是它的子 issue。

- **地图**：单张 issue，贴 `wayfinder:map` 标签，正文装 Notes / 已定决策 / 迷雾。`gh issue create --label wayfinder:map`。
- **子票**：作为子 issue 挂到地图下（见「设为子 issue」）；子 issue 功能不可用时，把子票加进地图正文的任务清单，并在子票首行写 `Part of #<地图>`。标签 `wayfinder:<type>`（`research`／`prototype`／`grilling`／`task`）。认领后 assign 给动手的人。
- **阻塞**：见上文「阻塞关系」。
- **找前沿**：列出地图下开着的子票，剔掉有开着阻塞者的（用「数还开着的阻塞者」）与已有 assignee 的，按地图顺序取第一个。
- **认领**：`gh issue edit <n> --add-assignee @me`，这是本次会话的第一笔写操作。
- **结案**：`gh issue comment <n> --body "<答案>"` → `gh issue close <n>` → 把结论摘要（gist + 链接）追加到地图的「已定决策」。
