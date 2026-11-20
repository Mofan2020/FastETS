# 工具

排查服务端行为用的脚本。全部是**只读**的：它们调 `g/homework/list`、
`g/homework/info`、`g/homework/score-detail` 这些读路由，不会改服务端任何东西。

- `read_homework_raw.py <homework_id>` —— 打印一份作业在服务端那边的原始结构
  （大题、entity、题数）。
- `read_score_detail.py <homework_id> ...` —— 打印服务端记下的逐题记录。
- `read_checkstart.py <homework_id>` —— 打印作业的开始信息。

跑法（venv 里）：

```bash
python3 tools/read_homework_raw.py 112856962
```

写接口的探针不在仓库里 —— 它会真的往作业里写记录。
