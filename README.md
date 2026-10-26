# FastETS

> 该项目将在2027/10/31左右停止维护
> **易听说（讯飞 E听说 / ets100）作业自动化助手 · 桌面版**
> Python + pywebview · 支持 macOS 与 Windows

FastETS 复刻了讯飞 E听说客户端与云端之间的通信协议，把「登录 → 拉取作业 → 下载试卷 → 合成朗读音频 → 回传成绩」这条链路自动化。它可以识别作业里所有题型（跟读、朗读、逐句跟读、听选信息、回答问题、信息转述、模拟考试），用**你自己的克隆音色**或**服务商的默认音色**生成朗读音频，再逐题提交。

---

## ⚠️ 免责声明 / Disclaimer（请先读这一段）

本软件仅供学习、研究与技术原理探究使用。

1. **仅用于学习原理**
   FastETS 是一个技术研究项目，用于学习 E听说（讯飞）客户端与云端接口的通信原理、语音合成流程与自动化编排方法。任何使用者仅可将本软件用于学习原理，并须在使用结束后 **24 小时内删除**本软件及其全部副本（包括源码、编译产物、缓存、录制的音频与克隆音色数据）。

2. **滥用与我无关**
   使用本软件造成的任何后果，包括但不限于账号被封禁、成绩异常、学校处罚、学术诚信问题或第三方索赔，均由使用者自行承担，与本软件的作者、贡献者、维护者及发布平台完全无关。本软件作者不对任何形式的滥用行为负责，也不为任何违规使用提供便利或技术支持。

3. **必须保证正常练习**
   使用者必须保证自己仍然进行正常、真实的英语练习。本软件不能、也不应替代你真实的听说训练。请在完成作业后自行对照原文复述与订正，把工具当作「参考答案生成器」与「朗读练习器」，而不是「刷分器」。

4. **账号与数据**
   你必须拥有科大讯飞 E听说平台的合法、正版使用权限。本软件不会收集、上传或共享你的账号数据到任何第三方服务器；账号口令、Token 与录制的音频仅保存在你本机的应用数据目录中。请自行承担使用本软件所带来的账号安全风险（包括但不限于与官方客户端异地登录导致的顶号）。

5. **声音与授权**
   你只克隆你自己或已获得明确授权的声音。声音克隆得到的音色数据仅用于你本人的练习，请勿用于冒充他人。

点击「我已阅读并同意」即表示你已完整阅读并接受以上全部条款。如果你不同意，请立即删除本软件。

---

## 功能

- **登录与作业拉取**：手机号密码登录（自动处理设备绑定）、畅言网页登录、导入 Token 三种方式；自动拉取所有未完成作业，含**作业集**内的子作业。
- **题型全覆盖**：`collector.role`（听选信息 / 逐句跟读 / 回答问题）、`collector.3q5a`、`collector.choose`（听后选择）、`collector.picture`（信息转述）、`collector.fill`（听后记录）、`collector.dialogue`、`collector.read`（短文朗读 / 模仿朗读），以及整卷的**模拟考试**。
- **逐句跟读自动分段**：跟读与朗读类题目按句切分，每句单独合成、单独上传、单独回传，与官方客户端一致。
- **两种声音来源**：
  - **直接使用默认声音**（默认推荐）：不需要克隆，填一个音色 ID 或从列表里挑一个系统音色就能开工。MiniMax 提供 300+ 个系统音色。
  - **克隆我的声音**：自行录一段中文 + 一段英文样本并**导入音频文件**（程序不提供麦克风录制），克隆出你的音色；中文作为主样本，英文作为 `prompt_audio`，跨语言音色更自然。
- **复述 / 表达可挑示例答案**：这类题要从作业资源里解析出全部示例答案（一份作业实测复述 6 份、表达 10 份），朗读时按设置挑一份——**第一个** / **随机** / **手动**（手动会弹出答案列表让你点）。
- **交际应答按「一份备选回答」朗读**：`std` 里放的是若干份备选回答（实测 36～45 份），不是一段要照读的长文本。它们同样进入候选池，朗读时只读其中一份，每题只录一条音；「解析」预览逐条列出所有备选答案。
- **每道题在服务端都有独立记录**：服务端按「资源包 + 包内第几题」存作答记录，`order` 必须是**这道题在它自己那个资源包里的序号**（每个包从 1 重新开始）。发成整卷的连续题号会让服务端认不出那几条记录，作业完成度就永远卡在 75%。FastETS 现在把「包内题号」和「界面上的卷子序号」分开算。
- **没见过的题型也不会「整包只记一条」**：除了已知的 7 种题型，其余资源包只要带条目列表（`sublist` / `question` / `items` / `word` 等）就会自动拆成「一个条目一道题」，避免只落库一条记录。遇到新题型时日志会记下类型名和大题名。
- **提交用的是这份作业自己的 `resource_id`**：服务端按 `resource_id` 找评测服务，填错会让整份作业每一题都返回「远程获取ets服务失败」。FastETS 从作业列表直接取真实值（实测 `240`），不再用通用默认值。
- **账号密码进系统凭据库**：macOS 存进 FastETS 专属钥匙串，访问控制只信任 FastETS 自己的可执行文件，别的程序读不到；Windows 用 DPAPI 加密。旧版本留在配置文件里的明文密码会自动迁移走。
- **评分服务异常不再算整题失败**：万一真的遇到「远程获取ets服务失败」，程序会自动重试；仍失败则记为「已提交但 ETS 评分未确认」，不计入失败，并在报告里单独提示你去官方 App 核对分数。
- **「停止」按钮真的停得下来**：取消标志与等待逻辑打通，等待中的节流睡眠会被立刻唤醒，不会出现「界面说停了、后台还在跑」。
- **Token 过期自动续期**：token 用久了会过期，程序会自动重新登录并重放请求。
- **自带 Speex 编码器**：ffmpeg 6.0 起删掉了 `libspeex` 编码器，当代 ffmpeg 根本没法产出 ETS100 需要的 `spx` 附件。FastETS 自带一个静态链接 libspeex 1.2.1 的小工具，产出的帧与 ffmpeg 5.x 的 `libspeex` **逐字节相同**（同一份 libspeex 源码、同样的 `quality=8` / CBR / `complexity=3` / 320 采样一帧；编译时关闭自动向量化以对齐 ffmpeg 的浮点结果）；万一它缺失，提交也**不会中断**（会省略 spx，mp3 与评测 XML 照常上传，分数不受影响）。
- **拟人节奏 / 防限流**：按朗读文本的词数推算等待时间，随机抖动请求间隔，大题之间额外停顿，所有等待可随时中断。
- **重做**：不达标、进行中、已达标（已完成）的作业都可以重做。
- **演练模式**：默认开启，只生成并上传音频、不提交成绩，先验证链路再真实提交。

## 支持的语音服务

| 服务 | 声音克隆 | TTS | 说明 |
|------|:---:|:---:|------|
| **MiniMax** | ✅ | ✅ | `files/upload` → `voice_clone` → `t2a_v2`，原生支持中英双语。默认走**中国区** `https://api.minimaxi.com`，可在设置里一键切到国际区 `https://api.minimax.io` |
| **OpenAI 兼容接口** | 视服务端 | ✅ | 任何暴露 `/audio/speech` 的服务：OpenAI、Azure、Groq、以及 GPT-SoVITS / CosyVoice / IndexTTS 等本地克隆服务器 |
| **Microsoft Edge TTS** | ❌ | ✅ | 免密钥兜底方案 |
| **macOS 系统朗读** | ❌ | ✅ | 仅 macOS：直接调用系统 `/usr/bin/say`，免密钥，可指定系统音色与语速 |

> MiniMax 中国区账号在 `platform.minimaxi.com` 申请 Key，国际区账号在 `platform.minimax.io` 申请，两边的 Key 不通用。FastETS 默认用中国区端点。

## 安装

### 直接下载

从 [Releases](https://github.com/Mofan2020/FastETS/releases) 下载对应系统的压缩包：

- `FastETS-macOS-arm64.zip` — macOS Apple Silicon，解压后得到 `FastETS.app`
- `FastETS-macOS-x64.zip` — macOS Intel
- `FastETS-Windows-x64.zip` — Windows 64 位，解压后直接运行 `FastETS.exe`

ffmpeg 与 Speex 编码器已随包附带，无需另外安装。

### 从源码运行

```bash
git clone https://github.com/Mofan2020/FastETS.git
cd FastETS
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

macOS 若提示缺少 WebView 后端，可额外安装：`pip install "pywebview[qt]"`。

## 使用流程

1. **首次启动**会弹出免责声明，必须阅读并同意后才能使用。
2. 在「**账号**」页填写手机号与密码登录；若账号下有多个年级/班级，选择要使用的那一个。
3. 在「**声音**」页选择声音来源：
   - 选「直接使用默认声音」→ 填一个音色 ID（或点「获取音色列表」从 300+ 个系统音色里挑），**这一步就能开工**；
   - 选「克隆我的声音」→ 自行录好中文与英文文本的音频并「导入音频文件」，再点「克隆声音」。
4. 在「**设置**」页填写语音服务配置（MiniMax API Key 或 OpenAI 兼容端点；不想用密钥可选 `edge` 或 macOS 的 `macos`），并确认：
   - 「演练模式」建议**先保持勾选**，确认流程无误后再取消。
   - 「节奏与防限流」保持开启，避免被服务端限流。
   - 「复述/表达答案」选择 `第一个` / `随机` / `手动`（交际应答的备选回答也用这个设置）。
5. 在「**作业**」页点「拉取作业」，勾选要处理的作业，可先「解析选中作业」查看每道题要朗读的内容。
6. 点「开始完成」；需要重做（无论是否已达标）时点「重做」或「重做选中」。

## 数据存放位置

| 平台 | 路径 |
|------|------|
| macOS | `~/Library/Application Support/FastETS` |
| Windows | `%APPDATA%\FastETS` |
| Linux | `~/.local/share/fastets` |

目录内含 `state.json`（登录态）、`settings.json`（配置）、`voice/`（录音样本）、`cache/`（试卷与音频缓存）、`logs/`（日志）。**全部只保存在本机**，可用 `FASTETS_HOME` 环境变量改变位置。

**账号与密码不在这些文件里**：`state.json` 只留 token 和设备码，手机号与密码存在系统凭据库里——macOS 是 FastETS 专属钥匙串（`fastets.keychain-db`，条目只信任 FastETS 可执行文件，其它程序读会被拒），Windows 是 DPAPI 加密的 `credentials.enc`。旧版本留下的明文密码在首次启动时自动迁移并从文件里删除。

## 项目结构

```
fastets/
├── config.py              # 路径、默认设置、免责声明文本
├── core/
│   ├── crypto.py          # 签名封装、机器码
│   ├── ets_api.py         # ETS100 云端 API 客户端
│   ├── session.py         # 登录、Token、账号选择
│   ├── zipcrypto.py       # 资源包解密与解压
│   ├── paper.py           # content.json 解析、题型判定、分句
│   ├── score.py           # 评测分数 XML 生成
│   ├── oss.py             # 阿里云 OSS 上传
│   ├── audio.py           # ffmpeg 转码（mp3 / speex）与音频探测
│   ├── speex.py           # 内置 Speex 编码器（speexenc_raw）的调用封装
│   ├── pacing.py          # 拟人节奏与防限流
│   ├── secure_store.py    # 系统凭据库（macOS 钥匙串 / Windows DPAPI）
│   └── completer.py       # 端到端作业编排
├── voice/
│   ├── base.py            # Provider 接口
│   ├── minimax.py         # MiniMax 克隆 + T2A
│   ├── openai_compat.py   # OpenAI 兼容端点
│   ├── edge.py            # Edge TTS 兜底
│   └── mac_say.py         # macOS 系统朗读（say）兜底
├── jobs/runner.py         # 后台任务与事件流
└── ui/
    ├── bridge.py          # JS ↔ Python 桥
    ├── app.py             # pywebview 入口
    └── web/               # Liquid Glass 前端
```

协议细节见 [`docs/API.md`](docs/API.md)。

## 开发

```bash
python tests/selftest.py                  # 自检：签名、解析、评分、节奏、音频链路、提交载荷、真实接口连通性
python -m compileall -q fastets
./packaging/native/build_speex.sh         # 构建内置 Speex 编码器（macOS / Linux）
./packaging/icon/make_icon.py             # 重新生成应用图标（需要 Pillow）
```

CI 在每次 push / PR 上跑 `ubuntu / macos / windows` 三套自检；打 `v*` tag 会触发 Release 工作流，测试通过后构建 macOS 与 Windows 安装包并发布为 **pre-release**，验证通过后再手动标记为正式版。

## 许可

[MIT](LICENSE) © FastETS contributors
