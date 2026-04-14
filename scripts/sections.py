"""
Section type definitions for TOC-driven extraction.

Unlike themes.py (keyword scoring), these configs drive TOC-based page range lookups.
No keyword scoring needed: the TOC tells us exactly which pages belong to each section.
"""

from __future__ import annotations
from typing import Any

SECTION_TYPES: dict[str, dict[str, Any]] = {

    # ------------------------------------------------------------------
    # 管理层经营分析 / 讨论与分析 / 业绩综述
    # 各公司叫法不同，toc_patterns 按优先级排列，取第一个命中的 TOC 条目
    # ------------------------------------------------------------------
    "management-discussion": {
        "description": "管理层经营分析（管理层讨论与分析/业绩综述/经营情况讨论）",
        "toc_patterns": [
            r"管理层讨论与分析",       # 新华保险、中国人寿、中国人保
            r"经营业绩回顾与分析",     # 中国太保
            r"经营情况讨论",           # 中国平安（经营情况讨论及分析 = 大类标题，取整段）
            r"业绩综述",               # 中国平安（若大类未命中则取业绩综述单节）
            r"管理层分析",
            r"经营业绩",               # 中国太保（较宽的匹配）
            r"经营分析",
        ],
        # 取最宽范围：若多个 pattern 命中了同一 TOC 大类下的连续条目，合并范围
        "merge_consecutive": True,
        # body_patterns: 正文中识别该节起始页的 pattern（TOC 命中时不使用）
        "body_patterns": [
            r"管理层讨论与分析",
            r"经营业绩回顾与分析",     # 中国太保
            r"业绩综述",
            r"经营情况讨论",
        ],
        # stop_body_patterns: 遇到这些 pattern 时本节结束（用于 fallback 模式）
        "stop_body_patterns": [
            r"内含价值",
            r"公司治理",
            r"董事会报告",
        ],
    },

    # ------------------------------------------------------------------
    # 合并资产负债表（资产 + 负债 + 股东权益在同一张表）
    # ------------------------------------------------------------------
    "balance-sheet": {
        "description": "合并资产负债表",
        "toc_patterns": [
            r"合并资产负债表",
        ],
        "body_patterns": [
            r"合并资产负债表",
            r"合并及公司资产负债表",    # 新华/人保格式（有标题时）
            # 新华格式：无独立标题，直接以表格开始，表头含"资产 … 附注 … 合并"
            r"资产.{1,40}附注.{1,40}合并",
            # 中国人寿格式：合并报表第一页直接从表格开始，表头为
            # "资产  附注十  2025年12月31日  2024年12月31日"
            r"资产.{1,40}附注[一二三四五六七八九十]+.{1,80}20\d{2}年12月31日",
        ],
        # subtoc_names: 用于在内嵌财务子目录（如人保 page 119）中精确定位页码
        "subtoc_names": ["合并及公司资产负债表", "合并资产负债表"],
        "stop_body_patterns": [
            r"合并.*利润表",            # 含"合并利润表"及"合并及公司利润表"
            r"合并损益表",
            r"合并利润及其他综合收益表",
        ],
    },

    # ------------------------------------------------------------------
    # 合并利润表
    # 新华/人保格式叫"合并及公司利润表"，且可能跨2页（第二页无标题）
    # ------------------------------------------------------------------
    "income-statement": {
        "description": "合并利润表",
        "toc_patterns": [
            r"合并利润表",
            r"合并损益表",
            r"合并利润及其他综合收益表",
            r"Consolidated.*Income",
            r"Consolidated.*Profit",
        ],
        "body_patterns": [
            r"合并.*利润表",            # 匹配"合并利润表"及"合并及公司利润表"
            r"合并损益表",
            r"合并利润及其他综合收益表",
        ],
        "subtoc_names": ["合并及公司利润表", "合并利润表"],
        "stop_body_patterns": [
            r"合并股东权益变动表",
            r"合并.*现金流量表",        # 含"合并及公司现金流量表"
            r"合并综合收益表",
        ],
    },

    # ------------------------------------------------------------------
    # 财务报表附注（整段，通常几百页）
    # 与 balance-sheet / income-statement 配合 locate + build-records 使用
    # ------------------------------------------------------------------
    "financial-notes": {
        "description": "财务报表附注",
        "toc_patterns": [
            r"财务报表附注",
            r"合并财务报表项目附注",
            r"Notes to.*Financial",
        ],
        "body_patterns": [
            r"财务报表附注",
            r"合并财务报表项目附注",
        ],
        "subtoc_names": ["财务报表附注"],
        "stop_body_patterns": [
            r"附录.*财务报表补充资料",
            r"财务报表补充资料",
        ],
    },
}
