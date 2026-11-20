# 交给 hz 服务器 agent：CC Switch 签名下载通道

## 任务和边界

将 `zuijiaosy/cc-switch` 的已验证签名发行包自动同步到 **hz 现有 Docker Caddy**，提供本站推荐构建的下载页、不可变版本直链和 Tauri 更新清单。沿用现有监控、其他站点和 Caddy 配置；不要重新安装一套 Caddy，不要让 Actions 获得 root、Docker 或普通 SSH shell 权限。

用户已授权本仓库公开、配置 GitHub Actions 和自有下载通道。实际下载域名、SSH 连接地址和数据卷须由服务器 agent 结合现状填写；本文 `dl.example.com` / `hz.example.com` 都是占位符。

先读取现有运维仓库的 `AGENTS.md`、`docs/servers/hz.md`、Caddy Compose 和近期磁盘记录。分享计划中提到的 11.2 GiB 空闲、85.07% 使用率是历史观测，部署时必须重新测量。当前旧发行版的 15 个附件合计 475,085,675 字节；新的签名发行版还包含更新附件，使用真实附件总大小做最终预算。

## 接口与目录

| 用途 | hz 路径 | 对外用途 |
| --- | --- | --- |
| SFTP chroot 根 | `/srv/client-downloads/incoming` | 所有祖先均 root 拥有，不可由上传账号写入 |
| 上传暂存 | `incoming/cc-switch/<batch>/` | SFTP 内看到 `/cc-switch/<batch>/`；不挂载到 Caddy |
| 发布回执 | `incoming/receipts/<batch>.json` | SFTP 只读，上传账号不可改 |
| 发布程序工作目录 | `/srv/client-downloads/work/cc-switch` | 仅发布账号可访问 |
| 已发布版本 | `/srv/client-downloads/public/cc-switch/releases/<clean-tag>/` | `https://下载域名/cc-switch/releases/<clean-tag>/<文件名>` |
| 稳定通道 | `public/cc-switch/stable` | 原子符号链接，指向已验证版本的 `channel/` 子目录 |
| 更新清单 | `public/cc-switch/stable/latest.json` | `https://下载域名/cc-switch/stable/latest.json` |
| 下载页 | `public/cc-switch/stable/index.html` | `https://下载域名/cc-switch/stable/` |
| 初始化身份文件 | `public/cc-switch/channel.json` | Actions 构建前核对仓库、域名和公钥 |

目录里的 `<clean-tag>` 使用完整发行标签，例如 `clean-v4.0.36-r3`。不能只用 `4.0.36`，因为同一上游版本的清理修订也可能改变安装包。应用内部版本仍沿用上游。

## 第一步：只准备签名公钥，暂不启用构建

在仓库所有者已经登录 `gh` 的受信任电脑上，使用最新的本仓库代码、Node.js 20+ 和 npm：

```bash
gh auth status
node .clean/scripts/setup-updates.mjs --prepare-only
```

这一步检查并复用原密钥；全新安装时生成本机备份。它不写 GitHub Secrets/Variables，不公开仓库，不触发构建。输出公钥文件为：

```text
~/.config/cc-switch-clean/zuijiaosy-cc-switch/download-channel-public.key
```

只把这份**公钥**送到 hz。签名私钥留在所有者本机备份和后续 GitHub Secret 中，绝不能放在下载服务器、公开目录、Git 或聊天消息里。已有签名版本时必须继续使用原公钥与原私钥，不能重新生成另一套。

## 第二步：准备受限账号和容量

使用两个专用账号：

- `cc-switch-upload`：仅能通过 `internal-sftp` 访问 chroot；只能写自己的暂存区，只读回执。
- `cc-switch-publish`：无登录 shell；systemd 发布程序使用。可读写暂存区、自己的工作目录和 `public/cc-switch`，没有 Caddy/Docker 管理权限。

共享组为 `cc-switch-staging`，两个账号都必须加入这个补充组；分别验证其有效组，不能只依赖 systemd 服务里的 `SupplementaryGroups`。按以下权限建立目录；若同名账号已存在，先核对用途，不要覆盖现有账号：

| 路径 | 所有者 / 组 | 权限 |
| --- | --- | --- |
| `/srv/client-downloads`、`incoming` | `root:root` | `0755` |
| `incoming/cc-switch` | `cc-switch-upload:cc-switch-staging` | `2770` |
| `incoming/receipts` | `cc-switch-publish:cc-switch-staging` | `2750`；回执 `0640` |
| `work/cc-switch` | `cc-switch-publish:cc-switch-publish` | `0700` |
| `public` | `root:root` | `0755` |
| `public/cc-switch` | `cc-switch-publish:cc-switch-publish` | 目录 `0755`；公开文件 `0644` |

上传使用 umask `0007`，客户端在上传完成后显式设置共享组权限，让发布账号能够读取和清理暂存文件。`work/cc-switch` 与 `public/cc-switch/releases` 必须在同一文件系统，才能原子发布；`incoming` 可以使用单独的限额文件系统。Caddy 只读挂载 `public`，不挂载 `incoming`、工作区、SSH 目录或 `/etc`。

**启用上传前必须落实硬容量限制。** 为上传账号配置文件系统用户/项目配额，或把暂存放在独立、有硬容量上限的文件系统；建议暂存总上限 2 GiB。发布程序的空闲空间检查不能限制一个正在进行的 SFTP 上传，不能把它当作配额替代品。不要为此格式化已有数据盘或贸然修改共享根分区；选择现有合适的数据卷并记录实际实现。

默认单批最大 2 GiB，发布后至少保留 4 GiB 空闲。至少预算：已有保留版本总量 + 一批上传 + 一批私有校验副本 + 4 GiB 余量。确认实际出口带宽、流量套餐和监控中心的共享负载。空间不满足时先扩容或使用合适的数据卷，再开放通道。程序不自动删除已发布旧版本，因此旧直链和回退来源继续保留；磁盘容量需要纳入现有监控。

为 Actions 生成一把**独立 Ed25519 上传密钥**，私钥保存在所有者受信任电脑，公钥放到 root 管理的 `/etc/ssh/authorized_keys/cc-switch-upload`。不要使用服务器管理员密钥。

按现有 SSH Include 规则加入专用用户配置，合并而非覆盖全局配置：

```text
Match User cc-switch-upload
    AuthenticationMethods publickey
    PubkeyAuthentication yes
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AuthorizedKeysFile /etc/ssh/authorized_keys/%u
    ChrootDirectory /srv/client-downloads/incoming
    ForceCommand internal-sftp -u 0007 -d /cc-switch
    DisableForwarding yes
    PermitTTY no
    X11Forwarding no
    PermitTunnel no
    PermitUserRC no
Match all
```

同时核对全局有效配置 `PermitUserEnvironment no`；该指令不能放在 `Match` 块内。通常保留 OpenSSH 默认值即可，若现有全局配置开启了它，先评估现有用户再在任何 `Match` 之前调整，不能直接覆盖共享 SSH 配置。

执行 `sshd -t` 和该用户的 `sshd -T -C ...` 检查后再 reload SSH。保留现有管理会话，验证该密钥能 SFTP 到 `/cc-switch`、能读 `/receipts`，不能写回执、不能访问公开目录、不能执行 shell。账号的 PAM/锁定状态也要实际验证，不能仅依据配置文本判定可登录。

从 hz 本机读取 SSH 主机公钥及指纹，经过已有受信任连接核实后，制作所有者电脑上的专用 `known_hosts` 文件。22 端口的条目是 `主机名或IP ssh-ed25519 公钥`；其他端口是 `[主机名或IP]:端口 ssh-ed25519 公钥`。不要把未经核实的 `ssh-keyscan` 输出直接当信任依据。

## 第三步：安装发布程序和初始化身份

hz 需要 Python 3.10+、OpenSSH 和 `minisign`。把本仓库 `.clean/server/` 的程序安装到 `/opt/cc-switch-clean/server/`，由 root 拥有且上传账号不可写。使用对应的 systemd service/timer 模板；每 30 秒扫描一次已完成批次。

保留 service 中单一的 `ReadWritePaths=/srv/client-downloads`，由前述目录所有权限制发布账号的实际写入范围。不要拆成工作区、公开目录各一条：systemd 会建立分开的 bind mount，使跨目录原子重命名即使在同一磁盘上也可能报 `EXDEV`。

若系统源没有 `minisign`（例如 Ubuntu 22.04），可使用仓库附带的官方固定版本安装器。它下载 minisign 0.12 的 Linux x86_64/aarch64 静态程序，并在解包前核对固定 SHA-256：

```bash
sudo bash .clean/scripts/install-minisign.sh /usr/local/bin
/usr/local/bin/minisign -v
```

把第一步的公钥放到 `/etc/cc-switch-downloads/updater.pub`。公钥可以公开，但这个受信任副本仅 root 能修改。创建 `/etc/cc-switch-downloads/config.json`，将示例域名替换为实际值：

```json
{
  "root": "/srv/client-downloads",
  "repository": "zuijiaosy/cc-switch",
  "base_url": "https://dl.example.com/cc-switch",
  "public_key_file": "/etc/cc-switch-downloads/updater.pub",
  "max_batch_bytes": 2147483648,
  "min_free_bytes": 4294967296
}
```

公钥和配置由 `cc-switch-publish` 只读。首次以发布账号运行：

```bash
python3 /opt/cc-switch-clean/server/publisher.py --config /etc/cc-switch-downloads/config.json --write-channel
python3 /opt/cc-switch-clean/server/publisher.py --config /etc/cc-switch-downloads/config.json --once
```

第一条生成公开身份文件与入口，第二条验证本地配置并扫描已完成批次。随后启用仓库附带的 timer。程序先将上传内容复制进私有工作区，对校验清单验签，再核对文件哈希、完整平台集合、构建来源和版本；全部成功才发布版本目录并原子切换 `stable`。上传中断、篡改、空间不足、同标签不同内容或旧工作流迟到均不得改变当前稳定通道。

不要手工移动未验签目录到 `public`，不要通过更换公钥或关闭校验来处理发布失败。

## 第四步：把公开目录挂载给现有 Caddy

在现有 Caddy Compose 的 volumes 中合并一项：

```yaml
volumes:
  - /srv/client-downloads/public:/srv/downloads:ro
```

下载域名 DNS 指向 hz，检查实际 A/AAAA 记录及 80/443 连通性。Caddyfile 加入对应站点（保留已有站点）：

```caddyfile
dl.example.com {
    root * /srv/downloads

    @mutable path /cc-switch /cc-switch/ /cc-switch/channel.json /cc-switch/stable/*
    header @mutable Cache-Control "no-cache, max-age=0, must-revalidate"

    @immutable path /cc-switch/releases/*
    header @immutable Cache-Control "public, max-age=31536000, immutable"

    header X-Content-Type-Options nosniff
    header Referrer-Policy no-referrer
    file_server
}
```

不要打开 `browse`；暂存和密钥不在挂载范围内。不要缓存 `stable/latest.json` 为长期不可变文件，不要给安装包套登录页、挑战页或重写为 HTML 的 CDN 规则。保留正常 `HEAD`、`GET`、`Range` 和 `Content-Length` 行为。

先验证合并后的 Compose 和 Caddy 配置。首次增加 volume 必须重建**对应 Caddy 服务**才会生效，可能短暂影响共享站点；按现有运维流程备份并仅操作目标服务。日常发版只变更文件，不重启 Caddy。

确认公网可以直接读取：

```text
https://实际下载域名/cc-switch/channel.json
```

字段必须为 `schema: 1`、`application: cc-switch`、`repository: zuijiaosy/cc-switch`，`base_url` 和公钥必须与实际配置一致。首次尚无签名版本时，没有 `stable/latest.json` 是预期状态；不要伪造一个更新清单来让检查通过。

## 第五步：写入 GitHub 通道配置并启用

在所有者已登录 `gh` 的电脑上，把 `.clean/downloads/setup.example.json` 复制到仓库外的本机文件，填入实际域名、SSH 地址、端口，以及本机上传私钥、公钥和核实过的 known_hosts 文件路径。示例只包含路径，不包含密钥内容。

```bash
node .clean/scripts/setup-downloads.mjs --config /本机绝对路径/download-setup.json
node .clean/scripts/setup-updates.mjs
```

第一个脚本会验证公开下载站身份、公钥和受限 SFTP 连接，然后写入：

| GitHub 配置类型 | 名称 |
| --- | --- |
| Variable | `DOWNLOAD_BASE_URL` |
| Variable | `DOWNLOAD_SSH_HOST` |
| Variable | `DOWNLOAD_SSH_PORT` |
| Variable | `DOWNLOAD_SSH_USER` |
| Secret | `DOWNLOAD_SSH_PRIVATE_KEY` |
| Secret | `DOWNLOAD_SSH_KNOWN_HOSTS` |

`DOWNLOAD_BASE_URL` 最后写入，作为已配置通道的标志。第二个脚本验证服务器准备好后，完成原有 `TAURI_PUBLIC_KEY`、`TAURI_SIGNING_PRIVATE_KEY`、可选签名密码、`CLEAN_UPDATE_ENDPOINT`，按既有授权公开仓库并触发正式构建。客户端主更新地址为自有域名，备用为 **zuijiaosy/cc-switch 自己的 GitHub 更新源**，二者使用同一公钥，不使用上游官方更新源。

Tauri 只有在主清单请求返回非 2XX 时才继续下一个清单端点；不能让 CDN 用 HTTP 200 返回错误页面，也不能指望主站 200 旧清单会自动改用 GitHub。已发布客户端使用的主域名必须保持服务；更换域名需专门迁移，不能只改 Variable。

构建工作不持有 hz 上传凭据；只有最后的下载同步任务读取 SFTP Secrets。GitHub Release 发布成功后直接运行同步任务，不依赖由 `GITHUB_TOKEN` 发布 Release 再触发另一个工作流。

## 第六步：真实验收与交接回报

等 **Clean upstream release** 的原生构建、签名发布和 **Publish and verify hz download channel** 全部通过。下载同步任务会验证签名校验清单、上传完整批次，最后先写 `READY.json.part` 再原子重命名为 `READY.json`，等待受限 SFTP 回执，然后从公开 HTTPS 通道验证稳定清单、附件完整 SHA-256、更新签名、大小和 Range 下载。后续检查若已验证同一镜像完整健康，则跳过重复上传。

对首次正式版本做以下人工/agent 验收：

1. 所有平台的 GitHub Release 附件齐全，`SHA256SUMS.txt.sig` 能由固定公钥验证。
2. hz timer/service 状态正常，回执的 tag 和校验清单指纹与该 Release 一致，暂存清理正常。
3. `stable/latest.json` 每个平台指向自有域名的同一个完整 clean-tag，签名与 GitHub 原清单一致。
4. 公网下载页能按平台下载，下载的安装包哈希与签名清单一致；验证 `Range: bytes=0-1023` 返回正确 `206` 和长度。
5. 定时检查再次运行时不重新编译、不覆盖已发布版本；镜像同步可重试/幂等。
6. 在独立测试批次验证缺文件、错误哈希、错误签名、低空间和旧版本不能推进稳定通道。不要用生产更新私钥制造伪造发布；失败测试使用测试目录与测试密钥。
7. 手动安装首个新的签名客户端，实际检查一次更新源；下一个更高上游版本发布后，再验证客户端真实下载安装。相同内部版本的 r2/r3 修订不触发 SemVer 自动更新，不能拿它冒充真实升级测试。

向本对话回报以下内容即可，**不要发任何私钥、Token 或供应商 API Key**：

```text
下载域名 / DOWNLOAD_BASE_URL：
SSH host、port、user：
核实过的 SSH 主机公钥及指纹：
实际数据卷、硬配额、可用空间和保留余量：
公开 channel.json 地址：
公钥身份（或公钥文件 SHA-256）：
systemd timer/service 验证结果：
GitHub Actions 运行链接：
已发布的 clean-tag / stable/latest.json 地址：
下载页和 Range/哈希验证结果：
```

这份回报后，再从本对话复核实际 Actions 日志和公开下载结果；仅提交代码或配置 Secrets 不等于整个通道已经上线。

## 故障恢复

- GitHub 已发布但 hz 同步失败：保留旧稳定通道；下次定时检查或手动运行同一工作流重试同步，不重打包、不覆盖 Release。
- 上传中断：没有完成标记的批次不会发布；清理只作用于本应用暂存/工作区，已发布版本不受影响。
- 同 tag 内容变化或签名不一致：拒绝，调查来源；合法重新构建必须增加 `release_revision`。
- 空间不足：发布拒绝且旧版可用；按记录扩容/迁移专用数据卷后重试，不删除共享监控数据。
- 需要撤回有问题的更新：先按运维流程停止发布 timer，并处理 GitHub 备用更新源，避免下一次自动同步再次推进；客户端不会自动降级到更低 SemVer。不能只改一个 symlink 就宣称已回退安装在用户电脑里的程序。

参考：[Caddy file_server](https://caddyserver.com/docs/caddyfile/directives/file_server)、[OpenSSH sshd_config](https://man.openbsd.org/sshd_config)、[Tauri updater](https://v2.tauri.app/plugin/updater/)。
