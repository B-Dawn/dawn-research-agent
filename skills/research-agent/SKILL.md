---
name: research-agent
description: 科研智能体。覆盖文献综述与追踪、创新点挖掘与论证、实验辅助与出图、论文写作、投稿选刊、方向推荐六个模块。内置零第三方依赖的多源检索（arXiv/Semantic Scholar/OpenAlex/Crossref）、文献对比矩阵、SVG 出图、期刊匹配、画像驱动的方向推荐。触发词：文献调研、综述、追踪新论文、创新点、gap、实验记录、出图、论文写作、选刊、投稿、影响因子、研究方向推荐、科研。
agent_created: true
---

# 科研智能体

面向研究生与科研人员的全流程助手。核心设计原则：**不编造引用、不编造数据、不替用户做决策**。
脚本负责检索、去重、结构化与出图；判断与写作由人和模型共同完成。

## 六个模块与路由

用户说到下面任何一类意图时，**先读对应的 reference 文件再动手**，不要凭通用知识硬答。

| 用户意图 | 加载文件 | 主用脚本 |
| --- | --- | --- |
| 查文献、做综述、追踪新论文、找基线 | `references/01-literature.md` | `ra_search.py` `ra_matrix.py` `ra_track.py` |
| 我这创新点新在哪、怎么论证、找竞品 | `references/02-innovation.md` | `ra_search.py` `ra_matrix.py` |
| 跑实验、记台账、出结果图 | `references/03-experiment.md` | `ra_plot.py` |
| 写/改论文章节、Abstract、Rebuttal | `references/04-writing.md` | —（写作任务） |
| 投哪个期刊、Cover Letter、命中率 | `references/05-submission.md` | `ra_journal.py` |
| 下一批做什么方向 | `references/06-direction.md` | `ra_profile.py --recommend` |

## 目录约定

所有产物落在 skill 根目录下，便于统一归档：

```
output/
  literature/    检索结果 JSON、文献对比矩阵
  weekly/        周报
  figures/       CSV 原始数据与 SVG 图
  experiments/   实验台账
  draft/         论文阶段稿
  directions.md  方向推荐报告
  .state/        追踪快照（勿手动改）
profile/         科研画像（长期记忆核心，建议每季度更新）
assets/          期刊库
```

## 脚本速查

工作目录统一切到 `scripts/` 下执行。Windows 下若 `python` 不在 PATH，用完整解释器路径。

```bash
python ra.py doctor                                   # 环境自检，新装后先跑这个
python ra.py search "UAV intrusion detection" --limit 20 --from 2023
python ra.py matrix --input output/literature/x.json --out matrix.md
python ra.py plot --csv results.csv --x epochs --y acc --type line --out fig.svg
python ra.py journal --abstract abstract.txt --target-if 7 --out journal.md
python ra.py track --queries queries.txt --days 7
python ra.py profile --show
python ra.py profile --recommend --max-queries 8
```

任意脚本加 `--help` 看完整参数。想先看 CSV 有哪些列，用 `ra.py plot --csv x.csv --list`。

## 数据源说明

| 源 | 特点 | 免密钥稳定性 |
| --- | --- | --- |
| arXiv | 预印本全、更新快，覆盖 CS/AI 最新工作 | 高 |
| Semantic Scholar | 被引数、venue 质量好，摘要较全 | 无密钥会 429 限流，隔几分钟重试 |
| OpenAlex | 覆盖广，含中文刊与非英文文献 | 高，建议配 mailto |
| Crossref | DOI 权威，正式发表版本准 | 高 |

任一源失败自动跳过，不影响其它源。检索失败时**降低 limit 或换源重试**，不要改检索式去将就。

## 硬性纪律（必须遵守）

1. **不编造文献**。没检索到的论文、DOI、作者、年份一律不许写。引用必须来自脚本真实返回。
2. **不编造实验数据**。没有跑过的实验，指标一律留空或标注「待补」。
3. **IF 与分区必须核实**。`assets/journals.json` 里的数字是参考值，逐年变动，选刊前以最新 JCR / 中科院分区表为准，并在报告里显式提示用户。
4. **命中概率是启发式估计**，用于排序，不得表述为承诺。
5. **推荐方向不超过 5 个，A 级不超过 2 个**，每个方向必须附真实检索证据。
6. **区分事实与推测**。脚本算出的是证据，模型推断的是假设，输出时要分开标注。
7. 用户没给的个人信息（单位、经费、截止时间）主动问，不要假设。

## 典型组合流程

`文献综述` → `ra_search.py` 检索 → `ra_matrix.py` 生成矩阵 → 人工补方法与局限列 → 按主题成稿

`创新点论证` → 从矩阵里挑最强竞品 → `02-innovation.md` 的三问法 → 补消融实验 → 回填 claim-evidence 表

`论文收尾投稿` → `ra_journal.py` 初筛 → 核实 scope 与近 3 年同主题论文 → `05-submission.md` 出 Cover Letter

`下一批方向` → 更新 `profile/researcher-profile.md` → `ra_profile.py --recommend` → 按 A 级方向做两周验证
