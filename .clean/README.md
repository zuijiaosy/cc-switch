# CC Switch 自有供应商版：自动构建与签名更新

本目录维护对上游的有限清理补丁及自动发布流程。应用代码来自 `farion1231/cc-switch` 的最新正式 Release；每次运行先锁定该版本的完整提交 SHA，再应用补丁、验证并构建。应用中仅提供 **CodexZH 与 CcodeZH** 两家自有供应商的配置预设，移除其他内置供应商推荐及推广展示，其余功能沿用上游。

**本轮代码提交时的状态（2026-11-19）：`zuijiaosy/cc-switch` 仍为 Private，尚未完成更新签名密钥与公开更新源的启用。** 当前 `r2` 配置已经要求独立签名更新；必要配置未完成时，工作流会停止发布。下文提供在仓库所有者本机运行的一次性启用脚本。执行后的实际状态以仓库设置和 Actions 结果为准。

## 应用改动范围

- 新增供应商界面只提供 CodexZH、CcodeZH 有公开文档依据的预设入口；移除其他供应商目录和广告。
- 首次启动不自动生成内置供应商卡片，包括这两家自有供应商；已有真实本地配置仍按上游逻辑导入。选择预设、填写自己的 API Key 并保存后才新增配置。
- 保留用户自行添加或导入的供应商、已有本地配置、网站协议导入、账户认证、OAuth、云服务参数及其他上游功能；不清空已有供应商数据。
- 保留内部用于模型能力识别、账户恢复和旧配置兼容的必要元数据，避免因清理展示入口而破坏功能。
- 应用更新、安装包下载及版本发布入口指向本仓库，更新配置使用本仓库唯一端点与自己的公钥。

补丁修改范围由 `scripts/verify-clean.py` 和审计清单限制。上游结构不再符合已审计条件、补丁无法应用、回归测试失败或任一平台构建失败时，本次发布停止，已发布版本保持不变。未来上游新增供应商入口或重构相关模块时，可能需要维护补丁。

## 两家自有供应商的预设范围

CodexZH 覆盖 **7 个客户端**，CcodeZH 覆盖 **5 个客户端**。下表中的地址按客户端实际协议分别配置，不能统一给所有客户端添加或删除 `/v1`。

| 客户端 | CodexZH | CcodeZH |
| --- | --- | --- |
| Claude Code | `https://api.codexzh.com`，Anthropic Messages | `https://api.ccodezh.com`，Anthropic Messages |
| Claude Desktop | `https://api.codexzh.com`，Anthropic Messages | `https://api.ccodezh.com`，Anthropic Messages |
| Codex | `https://api.codexzh.com/v1`，OpenAI Responses | `https://api.ccodezh.com/v1`，OpenAI Responses |
| OpenCode | `https://api.codexzh.com/v1`，`@ai-sdk/openai` | `https://api.ccodezh.com/v1`，`@ai-sdk/anthropic` |
| OpenClaw | `https://api.codexzh.com/v1`，`openai-responses` | `https://api.ccodezh.com`，`anthropic-messages` |
| Hermes | `https://api.codexzh.com/v1`，`codex_responses` | 无预设 |
| DeepSeek Harness（DSH） | `https://api.codexzh.com/v1`，OpenAI Responses | 无预设 |

CodexZH 的 Claude 入口使用 `cc-` 前缀模型，Responses 入口使用不带 `cc-` 前缀的模型。CcodeZH 的 Codex 入口要求 `cx` 分组 Key；OpenCode、OpenClaw 要求 `cc-3` 或 `cc-n` 分组 Key；Claude Desktop 的 `cc` 分组仅支持 Code 模式，`cc-n` 可用于其他模式。两站账号、Key 与额度互不通用。预设中没有嵌入真实 API Key，使用者需要自行填写，并根据自己的账户选择可用模型。

依据：[CodexZH 文档](https://docs.codexzh.com/codex)、[Claude 模型与客户端](https://docs.codexzh.com/claude-code/codexzh-models)、[CcodeZH 客户端文档](https://docs.codexzh.com/claude-code/clients)、[OpenCode 配置](https://docs.codexzh.com/codex/opencode)、[Hermes 配置](https://docs.codexzh.com/codex/hermes-config)、[DSH 配置](https://docs.codexzh.com/codex/deepseek-harness)。OpenCode 预设使用 `setCacheKey`，大小写以 [OpenCode 官方配置源码](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/v1/config/provider.ts)为准。

其他客户端继续保留上游功能，但没有添加未经文档证实的两家预设，也不宣称两站支持全部客户端或 Gemini 原生协议。上述验证覆盖配置生成与保存流程，不代表已使用真实账户逐一调用所有模型。

## 在 Mac 上一次性启用

先安装 GitHub CLI（`gh`）、Node.js 20 或更新版本与 npm，并在本机用有仓库管理权限的 GitHub 账号登录 `gh`。以下检查均应成功：

```bash
gh auth status
node --version
npm --version
```

如果尚未克隆仓库：

```bash
gh repo clone zuijiaosy/cc-switch
cd cc-switch
```

在包含本目录的仓库根目录运行：

```bash
node .clean/scripts/setup-updates.mjs
```

脚本使用 Node.js 内置库和已安装的 `gh`，并通过固定版本的官方 `@tauri-apps/cli@2.12.4` 生成 Tauri 更新签名密钥。它会完成以下设置：

1. 检查 GitHub 登录、仓库权限及已有签名配置；已有完整公私钥配置时保留原配置，不自动轮换密钥。
2. 首次配置时在本机生成密钥，保存在 `~/.config/cc-switch-clean/zuijiaosy-cc-switch/`，将公钥写入仓库 Variable，将私钥写入 GitHub Secret。
3. 将整个 `zuijiaosy/cc-switch` 仓库设为 **Public**，使应用可以匿名读取 Release 与更新附件；公开范围包含仓库代码和 Git 历史。
4. 等待 GitHub 的仓库信息及上一份清理版的构建来源附件可以匿名读取，再启用并触发 **Clean upstream release** 工作流，开始构建与发布。公开下载尚未生效时会等待并重试；最终失败会保留已保存的密钥，稍后可再次运行。

脚本不在聊天中传递私钥，也不将私钥提交到 Git。请在自己管理的安全位置另存一份本机密钥目录备份；GitHub Secrets 不能反向下载出原始私钥。供应商 API Key 与 Tauri 更新签名私钥是两类不同凭据，前者不需要写入此构建流程。

脚本完成后，在 [Actions](https://github.com/zuijiaosy/cc-switch/actions/workflows/clean-release.yml) 查看运行结果，在 [Releases](https://github.com/zuijiaosy/cc-switch/releases) 下载首个成功发布的安装包。工作流通过签名和公开下载验证后，才能认定应用更新链路已启用；仅完成 Variable/Secret 设置还不足以说明安装包已经发布。

## 独立签名与更新连续性

`config.json` 当前设置为 `release_revision: 2`、`require_signed_updates: true`。此版本必须具备自己的有效公私钥和公开可下载的更新附件；缺少任一条件会停止构建或发布，**不会降级为无签名发行**。

本仓库更新源为：

```text
https://github.com/zuijiaosy/cc-switch/releases/latest/download/latest.json
```

配置会替换完整的更新端点列表和公钥，不保留官方更新源作为备用。工作流只接受当前仓库自己的 GitHub Release 地址；不能仅填写另一个 URL 而省略对应发布流程。

一次性脚本使用的 Actions 配置如下；需要人工维护时也使用这些名称：

| 类型 | 名称 | 内容 |
| --- | --- | --- |
| Variable | `TAURI_PUBLIC_KEY` | 自己的 Tauri/minisign 公钥 |
| Secret | `TAURI_SIGNING_PRIVATE_KEY` | 对应的更新签名私钥 |
| Secret，可选 | `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` | 私钥密码；未加密时留空 |
| Variable，可选 | `CLEAN_UPDATE_ENDPOINT` | 只能指向当前仓库的 `releases/latest/download/latest.json`；默认按当前仓库生成 |

工作流在原生构建前匿名检查仓库可读性；发布前用公钥验证每份更新产物的真实 minisign 签名，并核对上一份正式清理版的构建信息。已发布过独立签名版本后，后续版本必须继续使用**同一把签名密钥**，也不能把更新流倒退到更旧版本。删除公私钥配置、误换公钥或使用不匹配的私钥都会使发布失败。

不要为解决一次构建失败而重新生成密钥：已经安装的应用内置了旧公钥，无法信任随意更换的密钥。私钥丢失后需要设计专门的迁移方式或让用户重新手动安装，不能通过修改一个 Variable 使旧客户端自动信任新钥匙。

发布完成后，工作流会匿名读取固定版本及 `latest` 的更新清单，核对平台、签名字段和附件下载。如果发布后的 GitHub/CDN 读取验证失败，会明确报告失败并保留已发布版本；后续例行检查重试验证，不覆盖该版本或重复构建。

首次从官方版或未配置更新的旧版切换到本构建，需要手动安装一次。后续官方版本号升高时，沿用上游客户端的更新检查和安装行为，更新包由本仓库提供并验证签名；不会绕过用户对安装的确认。

## 如何看 Actions 的检查状态

自动检查区分首次配置等待与真正的运行故障：

| 状态 | 含义 | 行为 |
| --- | --- | --- |
| `setup_required` | 公钥、私钥和更新端点均未配置，且本仓库没有过清理版签名更新附件 | 定时或代码推送检查正常结束，在运行 Summary 中说明待初始化；不拉取上游、不构建、不发布，也不触发失败通知 |
| `up_to_date` | 对应版本已经发布；签名更新版本还会检查公开下载通道 | 不重复构建或覆盖安装包 |
| `build_required` | 配置完整，并发现需要构建的正式版本 | 继续补丁、测试、原生构建与签名发布 |
| 运行失败 | 配置部分缺失、已上线后签名配置丢失、补丁不适用、测试/构建/验签/下载验证失败等 | 明确失败并保留通知，阻止发布不完整或不可信版本 |

`setup_required` 只用于尚未初始化的自动检查，不表示客户端自动更新已经启用。主动点击 **Run workflow** 时仍要求配置完整；缺少配置会直接指出初始化步骤。若历史清理版发布或草稿中已经存在 `latest.json` / `.sig`，即使后来全部删除签名配置，也不会被误判成首次等待。

运行摘要会写明当前状态。单元测试使用的模拟发布消息和模拟警告不会输出为 Actions 的真实发布日志或警告注解。月度活动记录在 `setup_required` 时跳过；配置启用后恢复正常规则。

## 自动跟随上游的流程

工作流：`.github/workflows/clean-release.yml`。

1. 每隔 6 小时读取上游最新正式 Release，排除草稿和预发布版本。
2. 解析 Release 对应的 Git 提交，不直接从不断变化的 `main` 构建。
3. 检查本仓库是否已发布对应清理版本；已发布则检查公开更新源后结束，不重复构建。
4. 应用有限补丁和独立更新配置，运行源码审计、TypeScript 检查、相关前端回归测试和前端生产构建。
5. 将经过验证的应用源码打成带 SHA-256 校验的统一快照。所有原生平台从同一快照构建；Linux x64 还运行启动、官方账户按需恢复及本地配置导入回归测试。
6. 构建 Windows x64 / ARM64、Linux x64 / ARM64、macOS Universal 安装包及签名更新附件。
7. 校验各平台产物齐全、签名有效、公钥连续，上传到草稿 Release；所有附件上传完成后，发布为最新正式版，再验证匿名更新访问。

北京时间计划检查时间为 **02:17、08:17、14:17、20:17**。GitHub 定时任务可能延迟；这不是实时上游推送。也可以在 Actions 的 **Clean upstream release → Run workflow** 手动运行。修改 `.clean/` 或本工作流并推送至 `main` 时也会检查一次。

GitHub 会在公开仓库连续 60 天没有仓库活动时自动停用定时工作流（[官方说明](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows)）。为保持这套检查持续运行，定时任务会至多每 30 天向默认分支提交一次 `.clean/check-state.json`，记录检查时间、工作流链接与检查结果。该任务不改应用源码或工作流，不必等待上游有新版本才产生记录。仓库规则需允许 `GITHUB_TOKEN` 更新此状态文件；如果 Actions 已被停用，应先重新启用。

## macOS 安装

本方案不要求 Apple 开发者账号或公证。macOS Universal 包默认使用 ad-hoc 签名，Tauri 更新附件另由自己的更新私钥签名；这两种签名用途不同。

下载本仓库发布的 DMG / ZIP，将应用放入 `/Applications/CC Switch.app`。首次启动若被系统的隔离标记阻止，确认应用来自本仓库后，在终端运行仅针对该应用的命令，再重新打开：

```bash
xattr -dr com.apple.quarantine "/Applications/CC Switch.app"
```

Windows / Linux 选择与系统及 CPU 架构对应的安装包即可；`.sig`、`latest.json` 和 macOS `.tar.gz` 是更新用附件，手动安装不需要下载。

## 版本、源码与可追溯性

当前发布标签类似 `clean-v4.0.35-r2`，避免与仓库原有的 `v*` 发布流程冲突。内部应用版本及安装包基础名称仍使用官方 `4.0.35` / `v4.0.35`。

只有修改清理补丁、发布方式或构建配置并希望为同一个官方版本重新发布时，才递增 `config.json` 中的 `release_revision`。后续官方版本会自动使用当前清理修订号，无需每次修改配置。

由于应用内部版本保持上游值，`clean-v4.0.35-r1` 与 `clean-v4.0.35-r2` 的内部版本都是 `4.0.35`。这种同版本清理修订不会触发 SemVer 自动升级，必须手动覆盖安装；官方从 `4.0.35` 升到 `4.0.36` 时可正常识别更新。

本仓库现有 `main` 应用代码和历史不被自动覆盖。发布标签标记自动化配置提交，所以 GitHub 自动提供的 **Source code (zip/tar.gz)** 是自动化仓库状态，不能当成本次应用构建源码。

每个 Release 单独附带：

- `CC-Switch-v<版本>-Clean-Source.tar.gz`：与所有平台安装包对应的完整、已应用补丁的应用源码。
- `clean-build-provenance.json`：上游版本与 SHA、自动化提交、清理修订、更新配置、构建源码校验及产物校验。
- `SHA256SUMS.txt`：附件的 SHA-256 校验值。

所有平台的产物清单必须引用同一个源码快照 SHA。已发布的 Release 不被覆盖；同一标签的草稿只在来源提交一致时续传。这是个人维护的衍生构建；原项目 MIT 许可证和版权声明保留。

## 维护与故障处理

- **Actions 没有开始：** 检查工作流是否启用、账户 Actions 状态和仓库策略；定时工作流只从默认分支运行。检查月度状态记录是否持续更新。
- **尚未公开或缺少签名配置：** 完成一次性脚本，或恢复原来的公私钥配置。`r2` 不支持退回无签名发布。
- **补丁或审计失败：** 先检查上游相关变更，更新有限补丁及审计规则，保留回归测试，再递增清理修订号。
- **测试、构建或签名失败：** 查看失败平台日志，修复后重新运行；构建不完整或签名验证失败的版本不会成为最新 Release。
- **已发布版本的公开访问验证失败：** 检查仓库是否仍公开及 GitHub 附件/CDN 是否可访问；重新运行检查，无需覆盖已发安装包。
- **需要改动已发布的同一上游版本：** 递增 `release_revision` 后重新发布，并手动安装该修订。

常规自动构建和发布使用仓库的 `GITHUB_TOKEN`，不需要额外提供个人访问令牌。一次性脚本使用本机已有的 `gh` 登录来完成仓库设置；启用后不依赖这台 Mac 持续开机。整个构建、签名和发布在同一次工作流中完成，不依赖机器人推送标签触发另一个工作流。

仓库根目录保留的旧应用源码有独立的 `CI` 工作流。仅修改 `.clean/`、清理发布工作流或历史一次性恢复工作流时，不运行这套旧应用检查；应用源码或 `ci.yml` 本身发生变化时仍运行。它的 Rust 工具链固定为已确认的版本，并继续执行格式检查、Clippy 的 `-D warnings` 和测试，避免随浮动 `stable` 自动引入新的检查规则。Cargo 使用锁文件中的依赖版本；前端 Vitest 保留默认测试匹配规则，但排除 `.clean/`，其中的初始化测试由清理发布工作流使用 Node 原生测试运行器独立执行。
