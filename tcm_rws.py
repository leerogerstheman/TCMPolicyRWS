#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""中医药政策与真实世界研究工具 —— 根入口。

用法
----
    python tcm_rws.py crawl      # 采集 gov.cn 中医药政策文件
    python tcm_rws.py search 中医药
    python tcm_rws.py analyze    # 政策变迁分析 + 出图 + 报告
    python tcm_rws.py rws --adr examples/sample_adr_synthetic.csv
    python tcm_rws.py demo       # 一键跑通全流程
    python tcm_rws.py info

完整参数见 ``python tcm_rws.py --help``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 保证从任意工作目录调用本脚本时都能 import 到 src 包
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Windows 控制台常为 GBK,中文输出会抛 UnicodeEncodeError。
# 在导入业务模块前先把标准流切到 UTF-8;若环境不支持则忽略,
# 由 cli._safe_print 的逐条降级兜底。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError, OSError):
        pass

# 影响子进程(PowerShell 中显式设置更可靠,这里作为兜底)
os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def main() -> int:
    from src.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
