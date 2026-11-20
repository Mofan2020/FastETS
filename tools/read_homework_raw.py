#!/usr/bin/env python3
"""把服务端那边一份作业的原始结构打出来（只读）。

调 ``g/homework/list`` 拿到作业的 ``raw``，里面的 ``contents`` 带着每个
大题的信息（group_name / entity / 题数）。这是判断「服务端把这份作业
切成几道题」的地方。

用法::

    python3 tools/read_homework_raw.py <homework_id>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastets.core.session import Session  # noqa: E402


def main(argv: list[str]) -> int:
    session = Session()
    token = session.token or session.ensure_fresh_token()
    parent_id = session.parent_account_id

    wanted = set(argv)
    for status in ("1", "2", "3"):
        try:
            result = session.client.homework_list(token, parent_id, status=status)
        except Exception as exc:  # noqa: BLE001
            print(f"status={status} 拉取失败: {exc}")
            continue
        for homework in result.get("homeworks") or []:
            hid = str(homework.get("id") or "")
            if wanted and hid not in wanted:
                continue
            print("=" * 70)
            print(f"作业 {hid} 「{homework.get('name')}」 status={status}")
            raw = homework.get("raw") or {}
            print("  raw 顶层键:", sorted(raw.keys()))
            for key in ("contents", "sections", "groups", "list", "items"):
                value = raw.get(key)
                if isinstance(value, list) and value:
                    print(f"  raw[{key}] 共 {len(value)} 項:")
                    for entry in value:
                        print("   ", json.dumps(entry, ensure_ascii=False)[:400])
                    break
            else:
                print("  raw 全文:", json.dumps(raw, ensure_ascii=False)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
