#!/usr/bin/env python3
"""读一份作业在服务端那边的题目结构（只读）。

调 ``g/homework/checkstart`` —— 官方客户端开始一份作业时调的就是这个，
返回体里带着这个大题在服务端那边是怎么切分的（item 数量、每 item 的
编号）。这是判断「order 该发成什么」的唯一权威来源。

用法::

    python3 tools/read_checkstart.py <homework_id>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastets.core.session import Session  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    session = Session()
    token = session.token or session.ensure_fresh_token()

    for homework_id in argv:
        print("=" * 70)
        print(f"作业 {homework_id}")
        try:
            body = session.client.checkstart(token, homework_id)
        except Exception as exc:  # noqa: BLE001
            print(f"  checkstart 失败: {exc}")
            continue
        print(json.dumps(body, ensure_ascii=False, indent=2)[:6000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
