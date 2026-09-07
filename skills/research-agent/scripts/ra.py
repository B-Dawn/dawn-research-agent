#!/usr/bin/env python3
"""科研智能体统一命令行入口。

  python ra.py doctor                    环境自检（Python 版本 + 四个数据源连通性）
  python ra.py search "UAV IDS" --limit 20
  python ra.py matrix --input out.json --out matrix.md
  python ra.py plot --csv results.csv --x epochs --y acc --type line
  python ra.py journal --abstract abstract.txt --target-if 7
  python ra.py track --queries queries.txt --days 7
  python ra.py profile --show
  python ra.py profile --recommend

所有子命令零第三方依赖，只用 Python 标准库。
"""

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from lib import net  # noqa: E402

SUBCOMMANDS = {
    "search": "ra_search.py",
    "matrix": "ra_matrix.py",
    "plot": "ra_plot.py",
    "journal": "ra_journal.py",
    "track": "ra_track.py",
    "profile": "ra_profile.py",
}


def _run(script, rest):
    path = os.path.join(HERE, script)
    if not os.path.isfile(path):
        print("[错误] 缺少脚本: %s" % path)
        return 2
    return subprocess.call([sys.executable, path] + list(rest))


def doctor():
    net._ensure_utf8()
    print("科研智能体 · 环境自检")
    print("=" * 46)
    print("Python      : %s" % sys.version.split()[0])
    print("解释器路径  : %s" % sys.executable)
    print("Skill 根目录: %s" % SKILL_ROOT)
    ok = True
    if sys.version_info < (3, 7):
        print("[警告] Python 版本偏低，建议 3.8+（当前 %s）" % sys.version.split()[0])
        ok = False

    from lib import sources
    probes = [
        ("arXiv", sources.ARXIV_API, {"search_query": "all:test", "max_results": 1}),
        ("Semantic Scholar", sources.S2_API,
         {"query": "test", "limit": 1, "fields": "title"}),
        ("OpenAlex", sources.OPENALEX_API, {"search": "test", "per-page": 1}),
        ("Crossref", sources.CROSSREF_API, {"query": "test", "rows": 1}),
    ]
    print("")
    print("数据源连通性（任一不可用会自动跳过，不影响其它源）：")
    for name, url, params in probes:
        raw = net.http_get(url, params, timeout=15, retries=1)
        if raw:
            status = "可用"
        else:
            status = "不可达 / 被限流（自动跳过）"
        print("  %-18s %s" % (name, status))
    print("")
    print("说明：arXiv 与 Crossref 免密钥长期稳定；Semantic Scholar 无密钥时会 429 限流，")
    print("      属正常现象，隔几分钟重试或配置 api_key 环境变量即可；OpenAlex 建议填 mailto 提升配额。")

    print("")
    profile = os.path.join(SKILL_ROOT, "..", "..", "profile", "researcher-profile.md")
    profile = os.path.abspath(profile)
    print("科研画像    : %s (%s)" % (profile, "存在" if os.path.isfile(profile) else "缺失"))
    db = os.path.join(SKILL_ROOT, "assets", "journals.json")
    print("期刊库      : %s (%s)" % (db, "存在" if os.path.isfile(db) else "缺失"))

    print("")
    print("自检%s。%s" % ("通过" if ok else "发现问题",
                         "" if ok else "上方标为不可达的源会被自动跳过，其余功能仍可用。"))
    return 0 if ok else 1


def main(argv=None):
    net._ensure_utf8()
    if not argv:
        argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if argv[0] == "doctor":
        return doctor()
    script = SUBCOMMANDS.get(argv[0])
    if not script:
        print("[错误] 未知子命令: %s" % argv[0])
        print("可用: %s | doctor" % ", ".join(sorted(SUBCOMMANDS)))
        return 2
    return _run(script, argv[1:])


if __name__ == "__main__":
    sys.exit(main())
