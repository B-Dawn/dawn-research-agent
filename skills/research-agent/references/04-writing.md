# 论文写作

> 适用对象：AI + 低空经济安全方向硕士生，主线 T1 论文《Explainable Decision-Credibility Monitoring for IDS in UAV Ad Hoc Networks: Graph-Augmented Dual-Layer AI Trust Framework》（N1–N5）。
> 本模块只解决一件事：**把故事讲圆、把 claim 写严谨**。实验怎么跑属于 `03-experiment.md`，创新点怎么论证属于 `02-innovation.md`，本模块不重复其内容，只消费它们的产物（台账、claim-evidence 映射表）。
> 交付形态：**先出 Markdown 阶段稿**，定稿后再转 docx / LaTeX，不在 Markdown 阶段纠结样式。

## 什么时候用

1. 实验已有主结果，要开始写 Experiments / Method 章节正文。
2. 用户说"帮我写 Introduction""这段怎么改更学术""我的 contribution 怎么写"。
3. 需要把 N1–N5 组织成一条连贯的 story，而不是五个并列模块各自介绍。
4. 收到审稿意见（major/minor revision），需要逐条改写正文或写 response letter。
5. 自查发现符号前后不一致、术语混用（UAV / drone / UAV node / agent）、缩写未定义。
6. 要控制篇幅：期刊给的页数或字数上限，需要分配各章节预算并砍内容。
7. 改投其他期刊/会议，需要按新 venue 的调整叙事重心（T-ITS 偏系统与交通场景，DCN 偏通信与网络）。
8. 需要写 abstract、cover letter、response to reviewers 等非正文材料。

**不触发的场景**：查文献走 `01`；设计实验走 `03`；还没有实验结果就想把 Experiments 写满（那时该回去补实验）。

## 输入与输出

**输入（需用户提供）**

| 项 | 说明 | 缺省处理 |
|---|---|---|
| 实验台账与结果 | `03` 的 `ledger/ledger.md`、`results/*.csv`、`figures/*.svg`、图注 | 必填，缺任何结论的支撑就先标 `TODO` 占位，禁止编数字 |
| claim-evidence 映射 | `03` 步骤 8 的 `claim-evidence.md` | 缺则正文每句结论必须现场反查台账 |
| 方法细节 | 架构、模块、公式、符号、损失函数、复杂度 | 必填，缺则先让用户口述，禁止代猜模块结构 |
| 目标 venue | 期刊/会议名、页数或字数上限、模板 | 默认 IEEE T-ITS；未知时按"双栏 10–12 页、约 8000 词"预算 |
| 已有草稿 | 段落、提纲、导师批注、审稿意见原文 | 缺则从零起骨架 |

**输出目录约定**

```
output/draft/
  outline/        提纲与章节字数预算（outline.md、budget.md）
  sections/       分章节 Markdown 阶段稿：01-introduction.md … 06-conclusion.md
  symbols/        符号表 symbol-table.md（唯一真源，正文不得另起炉灶）
  claims/         claim 登记表：每句 claim 的措辞强度、证据编号、是否已被限制语约束
  figures-tables/ 图表标题与正文引用编号表，与 output/experiments/figures 对齐
  related/        Related Work 聚类对比表与"本文 vs 竞品"差异句
  submissions/    abstract、cover letter、response-to-reviewers、highlights
  export/         转出的 docx / LaTeX（由 Markdown 阶段稿生成，不做手工二次编辑）
```

## 工作流

### 步骤 1｜定 story spine（一句话主线）
做什么：把全文压成一句话——"针对 <场景下的具体痛点>，本文提出 <方法的本质做法，而非模块罗列>，其核心洞察是 <为什么这样做能work的一句机理>，在 <数据集/设置> 上相比 <最强竞品> 实现 <可测量提升>，并额外带来 <可解释/可信类性质>"。这句话是 abstract 第一句、Introduction 首段、Conclusion 首段的共同骨架，三处措辞可以不同但语义必须一致。
用什么工具：人工撰写；与 `02-innovation.md` 中 `claims/N1..N5.md` 交叉核对，确保 spine 的"核心洞察"就是主打的那条 claim，不是五个创新点的平铺。
产出：`outline/spine.md`，含一句话 spine、主打 claim 编号、其余四条 claim 在文中的出场位置。
判定：若一句话里塞进三个模块名，说明主线没想清楚，重写到只剩一个洞察为止。

### 步骤 2｜建骨架与字数预算
做什么：按下方模板 B 分配各章节字数占比，先写预算再写正文。骨架细化到三级标题，每个三级标题配一句"这一段要回答什么问题"。
用什么工具：文本编辑；预算表 `outline/budget.md`。
产出：`outline/outline.md`（含每节的问题清单）+ `outline/budget.md`。
判定：任何一节的实际字数超过预算 20%，先删内容而不是调预算。

### 步骤 3｜先定符号表，再写 Method
做什么：Method 之前先把符号表写完并冻结：斜体/粗体、上下标、集合与标量、图的节点与边、时间步、信任值、阈值 τ、损失项权重等。全文符号只能来自这张表，禁止同符号两义、异符号同义。术语同步冻结：UAV / UAV node / drone 选一个，IDS / intrusion detection 全文统一，缩写首次出现给全称。
用什么工具：`symbols/symbol-table.md`；分节写作时逐公式回填符号表行号。
产出：`symbols/symbol-table.md`（符号/含义/形状/首次出现章节/量纲）+ `sections/03-method.md`。
判定：随机抽 10 个符号，在 Method 与 Experiments 中逐一比对是否一致；出现一个不一致就整体过一遍。

### 步骤 4｜写 Method：先动机，后结构，再算法
做什么：三段式——(a) 问题形式化与动机（为什么现有建模不够，用一段推导或反例说明）；(b) 模块逐个展开，每个模块开头必有一句"它解决上面动机里的哪一个问题"，模块顺序按 story spine 的因果链而非实现顺序；(c) 伪代码 + 复杂度分析。N1–N5 每个创新点在 Method 里有唯一的锚点小节，命名与 `02` 的 claim 名一致，方便审稿人对照 contribution list 找位置。
用什么工具：Markdown 写作；公式用 LaTeX 行内/行间写法，保持与最终 LaTeX 模板兼容。
产出：`sections/03-method.md`，含复杂度一节（时间 + 空间，按输入规模与图规模给渐近式）。
判定：每个模块都能回答"去掉它会怎样"，答不出来的模块写得不完整，或本身就是冗余模块。

### 步骤 5｜写 Experiments：先结论句，后挂证据
做什么：每小节先写一句结论（来自台账 `结论` 列），再挂表/图与数字。结构固定为：设置（数据集/指标/基线/实现细节/超参选择依据）→ 主结果 → 消融（对齐 N1–N5）→ 敏感性/鲁棒性 → 开销与可扩展性 → 可解释性/可信度可视化 → 讨论。数字一律引用 `03` 的 `results/*.csv`，不手抄。
用什么工具：`output/experiments/` 下的 CSV 与 SVG；`figures-tables/` 维护"图表编号 ↔ 文件名 ↔ 支撑结论"三列表。
产出：`sections/04-experiments.md` + `figures-tables/index.md`。
判定：正文每个数字都能在 `claim-evidence.md` 反查到实验 ID；正文中出现"显著提升"必须有 `03` 步骤 7 的显著性记录支撑，否则降级为"数值上更高"。

### 步骤 6｜写 Introduction：倒三角 + 逐条 contribution
做什么：四段式——(1) 场景重要性与规模（低空经济/UAV 自组网的安全需求，给可核查数据）；(2) 现有做法与三个具体 gap（每个 gap 一句，且与 Related Work 的分类一致）；(3) 本文做法与核心洞察（story spine 展开）；(4) contributions 逐条列表，每条一行，格式 `（<编号>）<做法/发现>：<可测量证据>`，最后一句指向全文结构。contributions 数量与 N1–N5 对齐，条目顺序按重要性而非方法流程。
用什么工具：与 `02` 的 claims 卡逐条核对；与 `03` 的 claim-evidence 核对每条 contribution 是否有实验编号。
产出：`sections/01-introduction.md`。
判定：删掉 contribution list 中的任一条，若 Method 里找不到对应小节、Experiments 里找不到对应表/图，则该条不合格。

### 步骤 7｜写 Related Work：按"路线"分，不按"论文"堆
做什么：先按解法路线分 3–4 簇（如：传统/统计类 IDS、深度学习类 IDS、图神经网络类 IDS、可信与可解释 AI），每簇一段，段内按时间或演进逻辑串，段末一句"该路线的共同局限是 X，本文从 Y 角度切入"。然后单列一小段"与本文最接近的工作"，逐篇点名差异化（对应 `02` 步骤 3 的三问法答案）。禁止出现"A 提出了…B 提出了…C 提出了…"的流水账。
用什么工具：`01-literature.md` 的矩阵与 `02` 的 `evidence/N<x>-competitors.md`。
产出：`sections/02-related-work.md` + `related/cluster-table.md`（簇/代表工作/共同局限/本文差异）。
判定：任一段若把某篇论文的名字去掉后逻辑不变，说明该段是流水账，重写。

### 步骤 8｜写 Conclusion、Abstract 与投稿材料，再导出
做什么：Conclusion 不重复摘要：写"本文解决了什么、证据是什么、局限是什么、下一步是什么"，局限要具体（如"仅在仿真拓扑与公开数据集上验证，未含真实信道与硬件在环"），不写"未来将进一步提升性能"这类空话。Abstract 按"背景→gap→方法→结果数值→意义"五句骨架，含 2–3 个关键数值。最后生成 cover letter 与 highlights。
用什么工具：Markdown 阶段稿定稿后统一转 docx / LaTeX；导出后在 `export/` 只做模板级调整，正文修改一律回到 `sections/` 再重新导出，避免两份真源。
产出：`sections/05-06-*.md`、`submissions/{abstract,cover-letter,highlights,response-to-reviewers}.md`、`export/paper.{docx,tex}`。
判定：导出稿与 Markdown 阶段稿做一次逐段比对，确认无改写丢失；公式、图表编号、交叉引用在导出后重新检查一遍。

## 输出模板

### 模板 A｜各章节写作要点表

| 章节 | 字数占比 | 必须回答的问题 | 常见失分点 |
|---|---|---|---|
| Abstract | ~3%（150–200 词） | 场景痛点是什么？方法本质是什么？关键数值提升多少？ | 只写模块名不写洞察；无数值；与方法章节术语不一致 |
| Introduction | 12–15% | 为什么重要？现有方法差在哪（≥3 个具体 gap）？本文凭什么能解决？贡献有几条？ | 背景铺太长、gap 空泛（"仍未充分研究"）；contribution 写成"本文提出了一种框架"这类不可证伪句；无 roadmap 段 |
| Related Work | 10–12% | 该领域有哪几条解法路线？每条路线的共同局限是什么？与本文最接近的 2–3 篇差在哪？ | 流水账罗列；只述不评；不点名最强竞品；漏 2023 年后工作；与 Introduction 的 gap 分类对不上 |
| Method | 25–30% | 问题如何形式化？每个模块解决哪个具体子问题？符号是否自洽？复杂度多少？ | 符号前后不一致；模块与 motivation 脱节；伪代码与正文描述不符；缺复杂度分析；可复现细节（超参选择依据、初始化）缺失 |
| Experiments | 25–30% | 设置是否可复现？主结果是否真的优于最强竞品？每个创新点贡献多少？超参敏感性？开销与可扩展性？ | 只报最好一次；指标口径与基线不一致；消融不干净；无显著性判断；图表数字与正文不符；超参在 test set 上选 |
| Conclusion | 4–6% | 解决了什么？证据是什么？局限是什么（≥2 条具体）？下一步做什么？ | 复制摘要；局限写成"数据量不足"这类无效自曝；未来工作空泛；过度 claim（"彻底解决"） |

（合计 100%，Related Work 与 Method 的占比按 venue 微调：偏系统的 T-ITS 可给 Method 更高占比，偏通信的 DCN 可给 Experiments 的可扩展性部分更多篇幅。）

### 模板 B｜story spine 卡

```
一句话 spine：<针对 <痛点>，本文提出 <本质做法>，核心洞察是 <机理>，在 <设置> 上相对 <最强竞品> 取得 <可测量提升>，并具备 <性质>。>
主打 claim：<N?，一句话>
其余 claim 出场位置：N? → Method §<x.y> / Experiments §<x.y>；N? → …
不写进 spine 的内容（避免稀释主线）：<…>
```

### 模板 C｜contribution list（Introduction 末段）

```
（1）<模块/机制名>：为应对 <gap 1>，提出 <做法>，实现 <效果>。（支撑：Method §<x>，Table <n> / Fig. <n>，实验 <exp_id>）
（2）…
（3）…
（4）…
（5）<可解释/可信类发现>：首次给出 <性质的形式化/实证>，表明 <结论>。（支撑：§<x>, Fig. <n>）
```

每条贡献的硬性格式：做法 + 效果 + 证据位置；不得出现"novel / efficient / robust"等无证据形容词单独立条。

### 模板 D｜符号表（`symbols/symbol-table.md`）

| 符号 | 含义 | 字体/形状 | 量纲 | 首次出现 |
|---|---|---|---|---|
| $\mathcal{G}=(\mathcal{V},\mathcal{E})$ | UAV 自组网拓扑图 | calligraphic 大写 | — | §3.1 |
| $\mathbf{h}_v^{(l)}$ | 节点 $v$ 第 $l$ 层隐表示 | 粗体小写 | $\mathbb{R}^{d}$ | §3.2 |
| $\tau$ | 决策可信度告警阈值 | 希腊字母 | $[0,1]$ | §3.4 |
| $|\mathcal{V}|, |\mathcal{E}|$ | 节点数/边数 | 绝对值 | 个 | §3.5 |

规则：标量小写、向量粗体小写、矩阵粗体大写、集合 calligraphic；下标不参与含义区分（同一量纲的两个量不得只靠下标大小写区分）。

### 模板 E｜claim 登记表（`claims/claims.md`）

| 编号 | 位置 | claim 措辞 | 强度等级 | 限制语 | 证据编号 | 状态 |
|---|---|---|---|---|---|---|
| C-07 | §4.3 ¶2 | "N3 模块使 F1 提升 3.2 个点" | 定量-对照 | "在 <数据集> 上，3 次运行均值" | E07 / Table 4 | ok |
| C-12 | §1 末 | "首次实现 …" | 强 claim | 需限定"据我们所知，在 <场景> 下" | 检索证据 + E03 | 待补限定 |

强度等级分四档（选档后不得跳档）：
- **存在性**：我们观察到 X。→ 需一处证据。
- **定量**：X 使 Y 提升 n 个点。→ 需对照实验 + 口径说明。
- **因果**：因为 X 所以 Y。→ 需消融 + 反例排除其他解释。
- **唯一/首次**：首次实现 X。→ 需检索证据 + "to the best of our knowledge" + 场景限定。

### 模板 F｜段落模板：gap 句与差异化句

```
gap 句：<现有路线> 在 <具体条件> 下会 <具体失败模式>，其根源在于 <机理>，因此 <后果>。
差异化句：与 <竞品> 相比，本文不 <其做法>，而是 <本文做法>，这使得 <可测量后果>；
         两者在 <输入/结构/目标/部署> 层面实质不同。
限制句：需要指出的是，该结论成立依赖于 <条件>，在 <条件> 下 <范围声明>。
```

### 模板 G｜response to reviewers 单条结构

```
意见 <编号>：<原文引用>
作者理解：<审稿人真正担心的是什么>
修改：<改了哪一段，写出新句子>
位置：§<x.y> 第 <n> 段 / 新增 Table <n> / 新增实验 <exp_id>
补充证据：<补跑的实验 ID 与结果>
```

## 质量清单

1. Introduction 最后一段逐条列出 contribution，每条含做法 + 效果 + 证据位置（§/Table/Fig/实验 ID）。
2. contribution 的条数与 N1–N5 一致，且每条在 Method 有唯一锚点小节、在 Experiments 有对应表或图。
3. 每个 claim 都能在 `claims/claims.md` 查到强度等级与限制语，强 claim（首次/最优/彻底解决）均已加限定。
4. 每个可测量 claim 都有引用或实验编号支撑；正文中不存在无出处的数字。
5. Related Work 按解法路线分簇，每簇段末有"共同局限"句；与本文最接近的工作被点名差异化。
6. 全文符号与 `symbols/symbol-table.md` 完全一致，抽样 10 个符号跨章节比对通过。
7. 术语统一：UAV/节点/agent、IDS/入侵检测、缩写首次出现均给全称；无一处同义词混用。
8. Method 每个模块开头都有"解决哪个问题"的动机句，模块顺序按因果链而非实现顺序。
9. 伪代码与正文描述、符号表三者一致；复杂度分析（时间+空间）存在且与符号体系自洽。
10. Experiments 正文数字与 `output/experiments/results/*.csv` 当前值一致；"显著提升"均有显著性记录支撑。
11. 图表编号与 `figures-tables/index.md` 一致，正文引用无悬空引用、无编号错乱。
12. Conclusion 含 ≥2 条具体 limitation，且每条对应 Experiments 中可指出的设置约束，非空话。
13. Abstract 含 2–3 个关键数值，五句骨架完整，无"novel framework"式空洞自评。
14. 导出稿与 Markdown 阶段稿逐段比对一致；正文修改已回写 `sections/` 而非只在 docx 里改。

## 常见坑

1. **Related Work 写成流水账**："A 提出了…B 提出了…C 提出了…"，最后一句"然而仍有不足"。审稿人直接判"缺乏洞察"。必须按路线分簇，每簇给出共同局限并指向本文 gap。
2. **Method 符号前后不一致**：同一个符号在 §3.2 是节点特征、§3.4 变成边权重；或大小写/粗体混用。先冻结符号表再动笔，导出前再全局搜一遍。
3. **过度 claim 被抓**："彻底解决""首次实现""显著优于所有方法"没有限定语和证据。按模板 E 降档 + 加"据我们所知/在该场景下/在所测数据集上"。
4. **Introduction 与 Related Work 的 gap 对不上**：前者列三个 gap 是 A/B/C，后者分类成 X/Y/Z，审稿人找不到对应关系。两处的 gap 必须用同一套命名与顺序。
5. **contribution 写成"提出了一种…框架"**：不可证伪，也体现不出增量。必须写成"做法 + 可测量效果 + 证据位置"。
6. **Method 只描述结构不讲动机**：五个模块依次罗列，读者不知道为什么需要第五个。每个模块开头一句动机，末尾一句"去掉它会怎样"（并在消融里有对应数据）。
7. **Conclusion 复制 Abstract**：审稿人会对比两处。Conclusion 应写给已经读完正文的人：结论的证据、具体局限、下一步的实质方向。
8. **limitation 写成无效自曝**："数据集规模有限""算力不足"这类写法既暴露弱点又不提供信息。写具体约束（仿真拓扑 vs 真实信道、单跳假设、离线训练 vs 在线更新）并给出对应的下一步。
9. **图表编号与正文脱节**：改稿时插入一张新图，后面编号全乱，交叉引用指到错图。维护 `figures-tables/index.md`，编号只在导出前统一一次。
10. **在 docx 里改正文、Markdown 阶段稿没同步**：形成两份真源，下一次导出把修改冲掉。正文修改一律回到 `sections/`，docx 只做模板级排版调整。
11. **术语迎合式堆砌**：为了显得相关而硬贴"低空经济"等词，但正文的实验设置与场景假设与 UAV 自组网无关。场景描述必须与数据集、拓扑、威胁模型实际一致。
