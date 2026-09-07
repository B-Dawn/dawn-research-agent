# 投稿匹配与选刊

面向「一篇已成稿/接近成稿的论文，投到哪里、怎么投」。
本模块只解决**已有稿件的落点问题**，不解决"下一批做什么方向"（见 `06-direction.md`）。

## 什么时候用

1. 稿件主体（摘要 + 实验 + 结论）已成型，需要决定投稿期刊，用户问："这稿子能投哪？""T-ITS 有戏吗？"
2. 需要在两个及以上候选期刊之间做取舍，例如 IEEE T-ITS（IF 8.4）vs Digital Communications and Networks（IF 7.5）。
3. 被拒后需要换刊重投，要快速给出"降级/平行/冲刺"三档落点清单。
4. 需要判断某期刊是否是 predatory journal，或核实其 scope、APC、审稿周期是否真实。
5. 投稿前需要 Cover Letter、highlights、graphical abstract、cover letter 中的 scope 契合表述。
6. 用户说"帮我看看这本期刊靠不靠谱""IF 有没有掉""中科院分区是几区"。

## 输入与输出

输入：

| 输入项 | 位置 | 说明 |
| --- | --- | --- |
| 稿件全文 | `<manuscript_dir>/manuscript.pdf` 或 `.docx` | 至少含 title / abstract / keywords |
| 论文元信息 | `<manuscript_dir>/meta.yaml` | 题目、五个创新点（N1–N5）、关键词、目标分区 |
| 期刊库 | `assets/journals.json` | 期刊事实表：IF、JCR 分区、中科院分区、scope 关键词、APC、审稿周期 |
| 用户硬约束 | `profile/researcher-profile.md` | 毕业截止时间、IF≥7 要求、是否接受 OA 费用 |
| 近期同主题论文 | 检索结果（见工作流第 3 步） | 用于验证"该刊确实发过这类题" |

输出（统一写入 `workspace/submission/<target_journal>/`）：

- `journal-shortlist.md` —— 选刊决策表 + 三档推荐（冲刺 / 主投 / 保底）
- `scope-fit.md` —— 每本候选刊的 scope 契合论证（引用该刊 Aims & Scope 原文片段）
- `cover-letter.md` —— Cover Letter 正文
- `submission-checklist.md` —— 该刊格式/伦理/数据可用性逐条核销表
- `resubmit-plan.md` —— 仅拒稿场景：修回要点 + 次选刊

目录约定：`workspace/submission/` 下按目标期刊建子目录，一次投稿一个目录，文件名固定，便于 `scripts/ra_journal.py` 反复覆写。

## 工作流

### 第 1 步：抽取稿件指纹

做什么：从稿件中抽出可用于匹配的**结构化特征**，而不是靠题目关键词硬匹配。
用什么工具：Read 读稿件，`scripts/ra_journal.py` 的 `extract` 子命令。

```
python scripts/ra_journal.py extract --manuscript <manuscript_dir>/manuscript.pdf --out <manuscript_dir>/fingerprint.json
```

产出：`fingerprint.json`，字段包括：

- `topic_keywords`：题目+摘要抽取的主题词（如 `UAV ad hoc network`、`intrusion detection`、`explainable AI`、`graph neural network`、`trust`）
- `method_family`：方法族（GNN / 双层框架 / 可解释性模块 / 在线监控）
- `venue_affinity`：参考文献中高频出现的期刊名（这是最强的落点信号）
- `novelty_list`：N1–N5 五条创新点的一句话概述
- `constraint`：IF≥7、Q2 及以上、是否 OA

判断标准：**参考文献里出现次数最多的那本刊，通常是第一落点**。这个方法比关键词匹配准得多，不要跳过。

### 第 2 步：期刊库匹配打分

做什么：把 fingerprint 与期刊库做匹配。
用什么工具：`python scripts/ra_journal.py`（读取 `assets/journals.json`）。

```
python scripts/ra_journal.py match --fingerprint <manuscript_dir>/fingerprint.json --min-if <7.0> --tiers <Q1,Q2> --out workspace/submission/journal-shortlist.md
```

`assets/journals.json` 中每条期刊记录包含的字段：

```json
{
  "name": "IEEE Transactions on Intelligent Transportation Systems",
  "abbr": "T-ITS",
  "publisher": "IEEE",
  "if_latest": 8.4,
  "if_year": "<最新JCR年份>",
  "jcr_quartile": "Q1",
  "cas_quartile": "中科院1区/2区",
  "scope_keywords": ["intelligent transportation", "vehicular network", "UAV", "network security"],
  "apc_usd": 2045,
  "oa": "hybrid",
  "first_decision_days_avg": 60,
  "submission_to_accept_days_avg": 200,
  "predatory": false
}
```

打分公式（脚本内实现，需在报告中显式展开每一维的得分）：

```
score = 0.35 * scope_fit
      + 0.25 * recent_topic_hits      # 近3年同主题论文数（需联网核实后回填）
      + 0.20 * if_constraint_satisfy  # 满足用户硬约束则满分，否则按偏离度衰减
      + 0.10 * cycle_fit              # 一审周期与用户毕业时间表的匹配度
      + 0.10 * reference_affinity     # 参考文献中出现频率
```

产出：`workspace/submission/journal-shortlist.md`，按 score 降序，且强制分组为**冲刺（Q1）/ 主投（Q2 达标）/ 保底（Q2 边缘或 IF 略低）**三档。

脚本只能给出**基于本地事实表的初筛**。任何进入最终 shortlist 的期刊，其 IF、分区、周期必须经过第 3 步的人工核实，脚本分值不作为对外结论。

### 第 3 步：事实核实（不可跳过）

做什么：对进入 shortlist 的每本刊，核实四项硬事实。
用什么工具：WebSearch / WebFetch 访问期刊官网 Aims & Scope 页、最新 JCR/SCImago 数据、出版社 APC 页。

核实清单：

1. **scope 契合**：抄下官网 Aims & Scope 中与本文直接相关的**原句**，逐句说明本文哪部分落在句内。若找不到任何一句能覆盖本文主题，直接淘汰，不看 IF。
2. **近 3 年同主题论文数**：在该刊站内检索 `topic_keywords` 组合，统计 2024/2025/2026 年命中数。少于 2 篇视为 scope 不符信号。
3. **IF 与分区**：以**最新一期 JCR** 为准，同时记录中科院分区（二者经常不一致，分别标注）。
4. **APC 与周期**：是否 OA、APC 金额、是否对学生减免、官网公布的一审周期与近期实际周期（可用自引数据或学术社区反馈交叉验证）。

产出：`workspace/submission/<journal>/scope-fit.md`，每条结论后附来源 URL 与核实日期。

### 第 4 步：命中概率与风险评估

做什么：给每本候选刊估一个**诚实**的命中概率区间，并列出风险。
用什么工具：Read 读 `fingerprint.json` + `scope-fit.md`，结合用户自述的写作完成度。

评估维度：

- 本文相对该刊近 3 年同主题论文的**增量**（N1–N5 中哪几条是该刊尚未覆盖的）
- 实验规模是否达到该刊惯例（UAV-IDS 类论文常见要求：真实/半真实数据集 + 至少两类对比基线 +  ablation）
- 写作语言与篇幅是否达标
- 该刊对"AI + 交通/网络"类稿件的拒稿率（学术社区口碑）

产出：写入 shortlist 表的"命中概率"与"风险"两列。命中概率用区间（如 30%–45%），禁止给单点值。

### 第 5 步：锁定主投并生成 Cover Letter

做什么：从三档中定一本主投、一本次选，然后写 Cover Letter。
用什么工具：Write 生成文件。

```
python scripts/ra_journal.py recommend --shortlist workspace/submission/journal-shortlist.md --pick <primary_journal> --out workspace/submission/<primary_journal>/cover-letter.md
```

Cover Letter 必须包含的五件事（缺一即退修）：

1. 明确声明**未一稿多投**、未在其他期刊审稿中。
2. 用一句话说清本文解决什么**该刊读者关心**的问题（不是复述摘要）。
3. 点名 N1–N5 中**最该被强调的两条**，并说明为什么匹配该刊 scope。
4. 说明数据与代码可用性（Data/Code Availability Statement 的链接或声明）。
5. 推荐/回避审稿人（如该刊要求），回避名单要有理由。

产出：`workspace/submission/<primary_journal>/cover-letter.md`。

### 第 6 步：格式核销

做什么：按目标刊的 Guide for Authors 逐条核销。
用什么工具：WebFetch 拉该刊作者指南，Read 逐条比对稿件。

高频必核项：篇幅（T-ITS 类常限 14 页双栏/正文字数）、摘要结构（是否为结构化摘要）、关键词数量与是否来自受控词表、图表分辨率与矢量格式、参考文献格式与数量上限、作者贡献声明 CRediT、利益冲突声明、伦理声明（涉及人体/动物则必需，纯网络仿真一般不需要）、Data Availability Statement。

产出：`workspace/submission/<primary_journal>/submission-checklist.md`，每行三态：`[x] 已核 / [ ] 待办 / [-] 不适用`。

### 第 7 步：拒稿后的换刊决策（条件触发）

做什么：收到 reject 或 major revision 后重新定位。
用什么工具：Read 审稿意见，`python scripts/ra_journal.py match` 带约束重跑。

```
python scripts/ra_journal.py match --fingerprint <manuscript_dir>/fingerprint.json --min-if <7.0> --tiers <Q1,Q2> --exclude <rejected_journal> --out workspace/submission/journal-shortlist.md
```

先判**拒稿性质**：scope 不符 → 换刊，意见几乎不用改；创新不足 → 换刊也会再被拒，先补实验；写作/格式问题 → 同档换刊即可。

产出：`workspace/submission/resubmit-plan.md`，含"意见 → 修改动作 → 修改位置"三列表。

### 第 8 步：回写画像

做什么：把本次投稿决策与最终结果写回 `profile/researcher-profile.md` 的"在投工作"一节。
用什么工具：Edit 追加条目（期刊、投稿日期、状态、命中概率预估）。

作用：让 `06-direction.md` 的方向推荐能避开"已投过且被拒"的方向，也避免同时投两篇高度重叠的稿件造成自我重复。

## 输出模板

### 选刊决策表

| 期刊 | IF | 分区 | scope 契合度 | 近3年相关论文数 | 平均一审周期 | 命中概率 | 风险 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| IEEE T-ITS | 8.4（<JCR年份>） | JCR Q1 / 中科院<x>区 | 高：Aims&Scope 含 vehicular/UAV network security 原句 | <n> 篇（2024–2026） | <x> 天 | <%>–<%> | <风险> |
| Digital Communications and Networks | 7.5 | JCR Q1 / 中科院<x>区 | 中高：偏通信网络，需强调 IDS 的网络侧贡献 | <n> 篇 | <x> 天 | <%>–<%> | <风险> |
| <保底刊> | <IF> | <分区> | <等级+依据> | <n> | <x> 天 | <%>–<%> | <风险> |

三档结论：

- **冲刺**：<期刊> —— 理由 <一句话>
- **主投**：<期刊> —— 理由 <一句话>
- **保底**：<期刊> —— 理由 <一句话>

scope 契合度分级标准（写表时必须用这四个词，不写"还行"）：

- **高**：官网 Aims & Scope 有原句直接覆盖本文核心主题
- **中高**：需把本文某一部分（如双层框架的网络侧）前置才能落进 scope
- **中**：仅边缘相关，靠"方法通用性"勉强解释
- **低**：无可引用的 scope 原句 → 淘汰

### Cover Letter 骨架

```
Dear Editor-in-Chief / <Editor Name>,

[第一段] We submit the manuscript entitled "<Title>" for consideration as a
<Article Type> in <Journal Full Name>. This work is original, has not been
published previously, and is not under consideration elsewhere.

[第二段 | scope 契合] <期刊名> has a standing interest in <引用 Aims & Scope 中
的原句或关键词>. This manuscript addresses <一句话问题>，which sits directly
within that scope: <本文与该刊读者群的关联，一句话>.

[第三段 | 创新点] The contributions are threefold.
 (1) <N_x：一句话，聚焦最匹配该刊 scope 的那条>
 (2) <N_y：一句话>
 (3) <N_z：一句话，若为可解释性/可信度类贡献，点明对该刊读者的价值>
Compared with <该刊近 3 年最相关的一篇论文，给出年份>，our work differs in
<差异，一句话>.

[第四段 | 数据与伦理] All datasets used are <公开数据集名 / 自采并附可用性声明>.
Code is available at <url or statement>. No human or animal subjects were involved;
therefore ethical approval is not applicable.

[第五段 | 审稿人] Suggested reviewers: <name, affiliation, email, 推荐理由>.
Excluded reviewers: <name or 机构，回避理由>.

[第六段] We confirm that all authors have read and approved the manuscript and
agree to its submission.

Sincerely,
<Name>, <Affiliation>, <email>
<Corresponding author>
```

highlights（该刊要求时附）：

```
• <N_x 的 85 字符以内一句话，动词开头>
• <N_y 的 85 字符以内一句话>
• <N_z 的 85 字符以内一句话>
• <最关键的一个量化结果，含数字与单位>
• <对工程实践/标准制定的一句话意义>
```

## 质量清单

1. 至少核实目标期刊近 3 年确实发表过同主题论文，且给出具体篇数与检索日期；不足 2 篇的，报告中必须显式标注 scope 风险。
2. IF 以最新 JCR 为准，并同时标注年份与中科院分区；报告中出现的所有 IF 都必须有来源 URL。
3. shortlist 中每本刊都附了官网 Aims & Scope 的**原句引用**，不是凭印象概括。
4. 冲刺 / 主投 / 保底三档齐全，每档至少 1 本；三档之间没有重复期刊。
5. 命中概率以区间给出，且不同档位之间的区间不重叠（重叠说明分档没有区分度）。
6. APC 金额、是否 OA、学生减免政策均已核实并记录；不接受"大概两三千美元"这类模糊表述。
7. 每本候选刊的"风险"列不是空的，至少写明一条具体风险（周期过长 / scope 边缘 / 该刊近期 IF 下滑 / 同类稿件扎堆）。
8. Cover Letter 中包含未一稿多投声明、scope 契合论证、数据与代码可用性声明三项必备内容，缺一即视为不合格。
9. Cover Letter 中引用的"该刊近 3 年最相关论文"确实存在，且年份、主题核对无误。
10. 格式核销表中每一行都有状态标记，不存在空白行；"不适用"项需写明不适用理由。
11. 用户硬约束（IF≥7、Q2 及以上、毕业截止时间）在 shortlist 中被逐条检验，不满足的期刊被显式标注而非静默剔除。
12. 决策结论已回写 `profile/researcher-profile.md`，含投稿日期与预估命中概率，便于后续换刊时对比。

## 常见坑

1. **只看 IF 不看 scope**。IF 8.4 的刊如果你的题不在它 scope 里，编辑会在 3 天内 desk reject，浪费的是你最长的时间成本。先做 scope 契合论证，再看 IF。
2. **被 predatory journal 骗**。特征：名字与世界名刊高度相似、承诺"两周内接受"、编委查无此人、APC 要求在录用后极短时间内支付、不在 JCR/SCImago 收录内。核实方式：只认 JCR 或 SCImago 收录 + 出版社为 IEEE/Elsevier/Springer/Wiley/KeAi 等已知机构。用户时间紧张，尤其容易被"快速录用"话术吸引，本模块遇到"承诺周期 < 1 个月"的刊一律标注警示。
3. **忽略 APC 费用**。Hybrid OA 走 OA 通道常需 2000–4000 美元；学生无经费时务必走非 OA 通道（绿色 OA 自存档需遵守 embargo 规则）。APC 未在决策表中列出 = 决策表不完整。
4. **中科院分区与 JCR 分区混淆**。同一本刊可能 JCR Q1 但中科院 2 区，或反之。国内学位/评奖通常认中科院分区，国际投稿认 JCR。两个都要写，不能只写一个。
5. **用旧的 IF 数据**。IF 每年 6 月更新，且部分刊波动很大（一年内 ±2 分常见）。写了旧数据会让整个短名单失效。核实日期必须写在报告里。
6. **Cover Letter 复述摘要**。编辑读 Cover Letter 是为了判断"为什么要发在这本刊"，不是为了再看一遍摘要。scope 契合那段是全篇最关键的，不能省。
7. **不核"近 3 年同主题论文"。** 某刊 8 年前发过一批 UAV IDS 论文，不代表现在还收；也可能该刊刚发过 5 篇同类，你的增量被稀释。必须按年份统计。
8. **一次只盯一本刊。** 用户三线并行（科研 / 教材 / 小说），被拒后再决策会白白损失 1–2 个月。必须同时备好三档，且保底档的"最低可接受 IF"事先敲定，被拒当天就能投出。
9. **同一批创新点拆给两篇稿件却投同一本刊。** 会造成自我重复（self-plagiarism）指控。回写画像就是为了防止这件事，不要跳过第 8 步。
