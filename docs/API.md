# ETS100 云端协议笔记

> FastETS 逆向整理的接口文档。所有结论都经过对 `api.ets100.com` 的实测验证。

## 一、全局常量

| 常量 | 值 |
|------|-----|
| API 根地址 | `https://api.ets100.com` |
| CDN 根地址 | `https://cdn.subject.ets100.com` |
| `PID` | `grlx` |
| `SECRET_KEY` | `555ffbe95ccf4e9535a110170b445ab8` |
| User-Agent | `libcurl-agent/1.0` |
| Content-Type | `application/x-www-form-urlencoded`（实际发送的是 JSON 字符串） |

## 二、请求封装

业务体不是直接发送 `params`，而是包成数组：

```json
[{"r": "user/login", "params": {"sn": "test"}}]
```

然后：

1. 紧凑 JSON 序列化（`separators=(",", ":")`，`ensure_ascii=False`）
2. UTF-8 编码 → Base64（不换行）
3. `sign = md5(PID + timestamp + body_base64 + SECRET_KEY)`（小写 hex）

最终请求体：

```json
{
  "body": "<base64>",
  "head": {"version": "1.0", "sign": "<md5>", "pid": "grlx", "time": 1700000000}
}
```

响应可能是对象 `{"code":0,"msg":"...","body":{...}}`，也可能是数组 `[{...}]`——后者取第 0 个元素。

### 实测样例

```
POST /user/login  (sn=test, phone=10000000000, password=...)
→ [{"code":20004,"msg":"ets的token过期"}]          # 路由存在
POST /g/zzzdefinitelynotaroute999
→ [{"code":500,"msg":"service not found"}]         # 路由不存在
POST /m/audio/sync-v1
→ [{"code":500,"msg":"Unable to resolve the request: m/audio/sync-v1"}]
```

`g/*` 命名空间是通配处理，任何子路由都先校验 Token（返回 `登录状态失效`），因此**无 Token 无法枚举**；
`m/audio/*` 命名空间会区分不存在的路由，可以据此判断路由是否存在。

### Token 会过期

登录拿到的 token 不是永久有效的（实测同一份 token 存放几十分钟后
`g/homework/list` 就返回 `code=20004 / ets的token过期`）。判定方式：
`code == 20004`，或消息里出现「token过期」「登录状态失效」。FastETS 在遇到这类
错误时会自动用记住的密码重新登录一次，然后重放原请求——所以长时间开着也不会
突然全线失败。`user/login` 与 `user/rebind-code` 不参与自动刷新，避免死循环。

## 三、路由清单

| 路由 | 作用 |
|------|------|
| `user/login` | 手机号 + 密码登录；返回 `code=30014` 表示需要设备绑定 |
| `user/rebind-code` | 设备绑定（同时返回 token）；也用于 token 刷新 |
| `user/login-zt` | 畅言通行证登录（`login`/`user_id` = uid，`suc_user_token` = captchaResult） |
| `m/ecard/list` | 账号（年级/班级）列表，产出 `parent_id` |
| `g/homework/list` | 作业列表；`status`：`1` 未完成 / `2` 进行中 / `3` 已完成 |
| `p1/student/config/config/ossEvaluateUploadStsToken` | 阿里云 OSS 临时凭证 + `mp3Path`/`spxPath`/`xmlPath` |
| `m/audio/sync-v2` | 回传答案（音频 + 分数明细） |
| `m/audio/sync` | 同上，旧版本 |
| `point-service/homework/rank/list` | 排行榜（**普通 form POST**，不走签名封装） |

### 登录参数

```json
{
  "sn": "test", "phone": "...", "password": "...",
  "device_code": "<16hex>|<16hex>", "device_name": "DESKTOP",
  "version": "3", "local_ip": "127.0.0.1", "system": "4",
  "global_client_version": "5.4.5", "sign_response": 1
}
```

### 作业列表参数（关键项）

```json
{
  "sn": "test", "token": "...", "parent_account_id": "...",
  "limit": 0, "status": "1", "offset": 0,
  "show_old_homework": 1, "get_to_do_count": 1, "get_all_count": 1,
  "check_pass": 1, "get_to_overtime_count": 1,
  "version": "3", "system": "4",
  "global_client_version": "5.4.5", "sign_response": 1
}
```

响应 `body` 里含 `base_url`（CDN 根）与 `data[]`，每项的 `struct.contents[]` 是各题型资源包
（`group_name` + `url`）。

## 四、机器码 device_code

格式固定为 `数据 MD5 中段 | MAC MD5 中段`，总长 33：

```
md5(data).upper()[8:24] + "|" + md5(mac).upper()[8:24]
```

- Windows：`ProductId` + `MachineGuid` + MAC
- macOS：`IOPlatformUUID` + MAC
- Linux：`/etc/machine-id` + MAC

**必须长期稳定**，否则服务端会反复要求设备绑定。

## 五、资源包解密

每个题型资源包是一个带密码的 ZIP，尾部固定 336 字节：

```
footer[0:8]    == b"MSTCHINA"   或   footer[144:149] == b"EPLAT"
seed           = footer[16:144]                      # 128 字节
password       = MD5(seed).upper() + MD5(上述大写 hex 的 ASCII).upper()
```

解压后递归找 `content.json`。

## 六、content.json 结构

核心字段 `structure_type`：

| structure_type | 题型 | 答案位置 |
|----------------|------|----------|
| `collector.role` | 听选信息 / 逐句跟读 / 回答问题 | `info.question[].std[].value` |
| `collector.3q5a` | 广东高中 3 问 5 答 | `info.question[].std[]` |
| `collector.choose` | 听后选择 | `info.xtlist[].answer`，选项在 `info.xtlist[].xxlist` |
| `collector.picture` | 信息转述 | `info.std[].value` |
| `collector.fill` | 听后记录 | `info.std[].value`，题号 `info.std[].xth` |
| `collector.dialogue` | 回答问题 | `info.question[].std[].value` |
| `collector.read` | 模仿朗读 / 短文朗读 | 无标准答案，正文在 `info.value` |

文本清洗：`ets_th{数字}` 前缀、HTML 标签、零宽字符 `\u200B`、`</p><p>` → 换行。

## 七、上传与回传

1. 取 OSS 临时凭证：
   `p1/student/config/config/ossEvaluateUploadStsToken` → `upload.{accessKeyId, accessKeySecret, securityToken, bucket, endpoint, cname}`，
   以及 `mp3Path` / `spxPath` / `xmlPath`。
2. PUT 三个对象到 OSS（`x-oss-object-acl: public-read`）：

```
key      = <prefix>public_<32位小写hex>_<毫秒时间戳><ext>
PUT      https://<bucket>.<endpoint>/<key>
Auth     OSS <accessKeyId>:<HMAC-SHA1-base64(
            "PUT\n" + Content-MD5 + "\n" + content-type + "\n" + Date + "\n" +
            "x-oss-object-acl:public-read\nx-oss-security-token:<token>\n" +
            "/<bucket>/<key>")>
```

3. 回传：`m/audio/sync-v2?sn=<sn>`

```json
{
  "homework_id": "...", "use_time": 3, "resource_id": "240",
  "serial_id": "0", "set_id": "...", "entity_id": "...", "order": "1",
  "score": "5.0", "correct": 1, "auto_submit": 1,
  "client_time": "2026-01-01 00:00:00", "version": 2, "graduation": 0,
  "upload_file_id": "", "upload_file_url": "<mp3 url>",
  "detail_file_id": "", "detail_file_url": "<xml url>",
  "score_detail": "<json>", "mark_service": 1,
  "spx_file_url": "<speex url>",
  "mark_trace_id": "{\"engine\":\"\"}",
  "system": "2", "global_client_version": "7.0.71.2569(2569)",
  "sn": "...", "sign_response": 2, "token": "...", "ets_app": 0, "is_zk_pad": 0
}
```

### resource_id —— 最容易踩的坑

`resource_id` **不是**固定的 55，而是**这份作业自己的**资源 id，来自
`g/homework/list` 返回的作业行顶层字段 `resource_id`（实测 `(新教材)8年级(上)U3A`
是 `240`，`set_id` 是 `614336`）。

内容包里的 `content.json` **没有** `resource_id` 字段（只有 `stid`），所以只能从
作业列表拿。发错 id 的后果是：附件都上传成功、`sync-v2` 也发出去了，但服务端按
resource_id 去找评测服务配置时找不到，返回 **`远程获取ets服务失败`**，整份作业
每一题都「失败」，而作业在官方 App 里始终显示未完成。

> 公开实现 `HULU53/EPlus_Windows` 也在这里写死了 `ETS_EVALUATE_RESOURCE_ID`，
> 属于同一个坑。FastETS 从作业行透传真实 id，缺失时才回落到默认值。

### use_time

`use_time` 是这道题的**实际耗时（秒）**。官方客户端一定会带上，不能是 0——
否则服务端可能把这次作答当成「没有练习过」。注意随包附带的 ffmpeg **没有**
ffprobe，用 `ffprobe` 探测时长会静默返回 0，必须用 `ffmpeg -i` 的输出解析。

### serial_id —— 完成度卡在 75% 的根因

`serial_id` 是**这道录音在资源包（entity）里的第几条**，不是全局题号，也不是
「这道题的第几句」。

服务端把一次作答存成 **entity + serial_id** 一条记录。于是：

- 一个资源包里有多道题时，每道题必须用**不同的** `serial_id`；
- 同一个资源包里的编号从 0 重新开始。

实测 `(新教材)8年级(上)U3A`：8 个资源包共 12 道题，其中「交际应答」这一个包
（entity 665164）里塞了 5 道题。早期实现把 `serial_id` 写成「段序号」（每道题
都从 0/1 开始），服务端只认下 2 条记录 → 12 题只落库 9 条 → 作业完成度正好
卡在 **75%**，官方 App 里始终显示「未完成」，`complete_count=0`。

FastETS 现在为每个资源包维护一个游标，包内每提交一条录音就 +1，
包切换时归零：

| 资源包 (entity) | 题目数 | 提交的 serial_id |
|------|------|------|
| 665159 / 665160 / 665161 | 各 1 | 0 |
| 665162 / 665163 | 各 1 | 0 |
| 665164（交际应答） | 5 | 0, 1, 2, 3, 4 |
| 665165（复述）/ 665166（表达） | 各 1 | 0 |

选择题用同一个路由，但 `upload_file_url` / `spx_file_url` / `detail_file_url` 留空，
`upload_file_id` 固定为 `"0"`，并在 `score_detail.choose` 里带上所选项。

### 附件格式

| 附件 | 生成方式 |
|------|----------|
| mp3 | ffmpeg `-ac 1 -ar 16000 -codec:a libmp3lame -q:a 4` |
| speex | 裸 Speex 帧（没有容器）。ffmpeg ≤5.x 可用 `-codec:a libspeex -f spx` 再从 Ogg 容器里抽出裸帧；**ffmpeg ≥6.0 已删除 `libspeex` 编码器**，FastETS 改为调用自带的 `speexenc_raw`（静态链接 libspeex 1.2.1），其输出与 ffmpeg 5.x 的 `libspeex` 逐字节相同（macOS arm64 实测 sha256 一致；x86_64 因 FMA 差异个别帧的量化结果可能不同，但仍是等价的合法 Speex 流） |
| xml | 讯飞评测结果 XML（见下） |

## 八、评测分数 XML

三种文档形态：

- `simple_expression` — 跟读 / 回答问题
- `topic` — 信息转述（含 `keypoints`）
- `read_chapter` — 短文朗读 / 模仿朗读（含逐句 `sentence` → `word` → `syll` → `phone`）

`score_detail` 是对应字段的 JSON：

```json
{"total_score":"5.0","choose":"","fluency_score":"5.0","accuracy_score":"5.0",
 "integrity_score":"5.0","standard_score":"5.0","category":"simple_expression",
 "repeat_mode":"0","version":"7.0.71.2569(2569)","rate_scale":0}
```

## 九、关于「重做」

服务端**没有**清除历史作答的专用路由——已确认 `m/audio/reset`、`m/audio/delete`、
`m/audio/remove`、`g/answer/delete` 等均返回 `Unable to resolve the request`，
而 `g/*` 命名空间在无 Token 时一律先返回 `登录状态失效`。

作答记录以 `homework_id` + `entity_id` + `serial_id` 为键（`order` 只是附加信息），
`m/audio/sync-v2` 会**覆盖**同键记录。
因此 FastETS 的「重做」实现为：重新走一遍完整流程并再次回传，从而替换上一次的结果。
若后续确认了官方清除接口，可在「设置 → 重做时的重置路由」填入路由名，程序会先调用它再重做。

## 十、参考实现

本项目逆向过程中参考了若干公开实现：

- `laststudio/Fuck_ets100` — 云端 API 文档（签名、资源包解密、答案解析）
- `banglee13/Fuck_ets100_win` — Python 客户端实现
- `HULU53/EPlus_Windows` — TypeScript 客户端实现（OSS 上传与 `m/audio/sync-v2` 回传）
- `HULU53/Ets100_Plus` — Android 客户端（本地自动化与畅言网页登录）
- `laoshuikaixue/ECRACK` — 家长端 API 解析
