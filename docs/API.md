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
| `collector.word` | 单词朗读（Vocabulary） | 无标准答案，要读的词在 `info.value` |
| `collector.repeat_essay` | 短文跟读 / 短文复述 | 正文在 `info.value`，分句在 `info.sublist[]` |

文本清洗：`ets_th{数字}` 前缀、HTML 标签、零宽字符 `\u200B`、`</p><p>` → 换行。

### collector.word —— 一个包一个词

资源包里只有**一个单词**，还有 `info.translate`（释义）、`info.xh`（包内题号，实测恒为 1）、
`info.audio`。所以它就是**一个 item（order=1）、一条录音**，读的是 `info.value` 里的那个词。
实测一份作业的 Vocabulary 大题有 43 个这样的包（871333…871375），每包一词。

### collector.repeat_essay —— 一道题、每段一条录音

`info.value` 是**整篇短文**，`info.sublist` 是它的分句表，每句带 `seq`、`text`、
`translate`，以及该句在原音频里的 `begintime` / `endtime`（实测一篇 10 句、约 50 秒，
时间轴从 0 连续到 49.9）。

**这个包是「一个 item + 每段一条录音」**。这一点由资源包里的两份官方文件确定：

```
paper.res                                   # 官方卷面 XML
<section caption="短文跟读" category="read_sentence" order="0">
  <item caption="subitem" itemcount="1" MaxScore="1"><id>592132_1</id></item>
  ...  共 10 条，每条 1 分
</section>

592132_1.ctrl                               # 官方控制文件（每段一个）
{"id":"592132","comment":"跟读题型","proto":"item.passage","set":[
  {"type":"action.record","filename":"592132_1","playtime":3.66,"playhint":"录音中"}, ...]}
```

- 整篇短文在服务端只有**一个 item**（`stid=592132`），也就是 `order=1`；十句话是它的
  十段（`paper.res` 的 item id 就是 `592132_1` … `592132_10`），官方客户端对每一段
  单独录一条音（`action.record` 的 filename 就是段号）。
- 早先两个方向都是错的：**v1.0.5** 把分句表拆成 10 个 item（order 1…10），服务端只认
  order=1 那一条，其余九条成了废记录；**v1.0.8** 把整篇录成一条，item 号对了但十段里
  只答了一段。两次都停在 12 分。
- 正确的键是 **`(entity_id, order=1, serial_id=段序号)`**：`serial_id` 从 0 递增到
  N-1，`order` 始终是 1。
- 每条录音的评测 XML **只描述它自己那一段**（`read_chapter` 类别，句表只有一段），
  不把整篇塞进同一份 XML。
- 分值直接取官方卷面：每段 `MaxScore=1` 分，整道大题 8 分或 10 分（取决于包里的段数）。
  没有 `paper.res` 时按 10 分兜底。

`paper.res` 是每个资源包里都有的官方卷面 XML（`fastets/core/paper.py` 的
`parse_paper_res()` 会解析它），它同时给出大题的分值、分类（`read_sentence`）和
每道 item 的满分。

### 未知 structure_type 的通用拆分

上面 9 种里，7 种是公开实现（`laststudio/Fuck_ets100` 的 API 文档、
`banglee13/Fuck_ets100_win`、`HULU53/EPlus_Windows`）能对上的类型；另外两种
（`collector.word` / `collector.repeat_essay`）是 2026-10-30 从用户作业的真实资源包里
拿到样本后补上的（公开仓库里搜不到这两个名字）。

**再往下的类型名仍然没有可核实的出处**，所以 FastETS 不去猜题型名，而是按**数据结构**
兜底：

只要 `info` 里带一份条目列表（`question` / `sublist` / `exam_info` / `items` /
`list` / `word` / `words`），就拆成「一个条目一道题」；条目里的答案列表
（`std` / `answer`）当作**备选答案**，只读其中一份；题号优先取 `seq` / `xh` /
`xth` / `num` / `no` / `index`，取不到就用顺序。`std` / `xtlist` 这两个键只有
在条目自带题号时才会被当成列表——在 `collector.picture` 里它们是同一道题的
多份备选答案，不能拆。

这样做解决的是「整包只记一条」：一包 N 道题只落库 1 条，完成度必然上不去。
拆出来的题强制「一道题一条录音」，避免一句答案被切成多句后落成多条记录。

遇到没见过的题型时，程序会在日志里打一行
`未识别的题型结构 <type>（<大题名>），已按通用规则拆成 N 道题`，
把类型名和包名记下来——拿到真实样本后就能据此写精确解析。**这条日志是 v1.0.5
上线后最有价值的东西**：2026-10-30 的新作业里就靠它直接报出了
`collector.repeat_essay（短文跟读）` 和 `collector.word（Vocabulary）`。

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

### order —— 完成度卡在 75% 的真正根因

服务端把一次作答记成 **资源包(entity) + 包内题号(order)** 一条记录。`order` 是
**这道题在它自己那个资源包里是第几题**（1 开始），**每个资源包都要从 1 重新开始**。

FastETS 早期把 `order` 发成了「整份卷子里的第几题」，而卷子序号是按**大题名**累加的：
实测作业 `(新教材)8年级(上)U3A` 有 8 个资源包，其中「朗读词组」占 3 个包、
「朗读句子」占 2 个包，于是第 2、3 个资源包的题号被顶成了 2、3：

| 资源包 (entity) | 大题 | 题数 | 旧实现发的 order | 服务端期望 | 命中 |
|------|------|------|------|------|------|
| 665159 | 朗读词组 | 1 | 1 | 1 | ✓ |
| 665160 | 朗读词组 | 1 | **2** | 1 | ✗ |
| 665161 | 朗读词组 | 1 | **3** | 1 | ✗ |
| 665162 | 朗读句子 | 1 | 1 | 1 | ✓ |
| 665163 | 朗读句子 | 1 | **2** | 1 | ✗ |
| 665164 | 交际应答 | 5 | 1–5 | 1–5 | ✓✓✓✓✓ |
| 665165 | 复述 | 1 | 1 | 1 | ✓ |
| 665166 | 表达 | 1 | 1 | 1 | ✓ |

命中 9/12 = **75%**，与三次真实提交（2026-10-25 09:08 / 19:42 / 20:06）测到的完成度
完全一致——同一个作业不管重做多少次、记录攒了多少条，那三个包的答案永远对不上，
官方 App 里也就永远是「未完成」。

修法：`order` 改用**包内题号**（资源包里 `xh` / `xth` 等自带题号，没有就按顺序
1、2、3…），显示用的卷子序号与它分开（`Question.order` 只用于界面和报告，
`Question.pack_order` 才是发出去的值）。改完后同样这份作业的 12 条记录
`(entity, order)` 两两不同，命中 12/12。

### serial_id

`serial_id` 是**这道题里的第几段录音**（0 开始）：逐句跟读一题要分句提交，
每句一条记录，`serial_id` 依次是 0、1、2…；`order` 保持不变。

> v1.0.4 曾把 `serial_id` 当成「包内第几条记录」并按包递增，那次改动对完成度
> 没有影响（实测 v1.0.4 仍是 75%），已在 v1.0.6 改回「题内第几段」。
>
> v1.0.9 在短文跟读上**证实**了这个模型：官方卷面 XML（`paper.res`）里整篇短文只有
> 一个 item（`order=1`），十句话是它的十段，官方控制文件对每段各录一条音——所以键是
> `(entity_id, order=1, serial_id=0…N-1)`。

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

作答记录以 `homework_id` + `entity_id` + `order`（+ `serial_id`）为键，
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
