#!/usr/bin/env python3
"""读回服务端对一份作业记下的逐题记录（只读）。

只调用 ``g/homework/score-detail`` —— 读路由，不写任何东西。
目的是看服务端到底按什么键记账：``(entity, order)`` 还是
``(entity, order, serial)``，以及它认的 order 序列长什么样。

用法::

    python3 tools/read_score_detail.py <homework_id> [homework_id ...]
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
    parent_id = session.parent_account_id
    resource_id = "240"
    print(f"parent_account_id={parent_id!r} resource_id={resource_id!r}")

    for homework_id in argv:
        print("=" * 70)
        print(f"作业 {homework_id}")
        try:
            info = session.client.homework_info(token, homework_id, resource_id, parent_id)
        except Exception as exc:  # noqa: BLE001
            print(f"  homework/info 失败: {exc}")
            info = {}
        print(f"  point={info.get('point')} total_point={info.get('total_point')} "
              f"use_time={info.get('use_time')}")

        try:
            detail = session.client.score_detail(token, homework_id, resource_id, parent_id)
        except Exception as exc:  # noqa: BLE001
            print(f"  score-detail 失败: {exc}")
            continue

        print(f"  score-detail 顶层键: {sorted(detail.keys()) if isinstance(detail, dict) else type(detail)}")
        rows = None
        if isinstance(detail, dict):
            for key in ("data", "score", "list", "records", "items"):
                value = detail.get(key)
                if isinstance(value, list):
                    rows = value
                    print(f"  用 {key!r} 里的列表：{len(value)} 条")
                    break
        if rows is None:
            print("  没有列表字段，整个返回:")
            print(json.dumps(detail, ensure_ascii=False, indent=2)[:3000])
            continue

        for row in rows:
            if not isinstance(row, dict):
                print("   ", row)
                continue
            picked = {
                k: v
                for k, v in row.items()
                if k in (
                    "entity_id", "order", "serial_id", "score", "point", "item_order",
                    "total_complete", "complete_count", "homework_id", "set_id",
                    "resource_id", "item_id", "record_id", "user_id", "create_time",
                    "submit_time", "status", "type", "category",
                )
            }
            print("   ", json.dumps(picked, ensure_ascii=False))
            if not picked:
                print("     全部字段:", json.dumps(row, ensure_ascii=False)[:600])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
