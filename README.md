# OLS (Overleaf Sync Lite)

本地文件夹 ↔ Overleaf 的保守双向同步工具。**使用免费的网页下载/上传接口，不使用付费 Git bridge，也不需要 Overleaf Git token。**

一次配置一个项目，日常只需 `ols sync`。无需 git commit、分支或版本管理。Linux、macOS 和 Windows WSL；Python 3.10+，Chrome/Chromium。

通用命令为 `ols`；旧命令 `olsync` 保留为兼容入口。已有项目配置和登录状态可直接复用。

## 安装与登录

```bash
git clone https://github.com/yuelinxin/overleaf-sync-lite.git
cd overleaf-sync-lite
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
ols login
```

`login` 打开**独立的浏览器配置目录**。在窗口里正常登录 Overleaf，完成验证码/SSO；看到项目列表后，回终端按 Enter。登录状态由浏览器保存在 `~/.config/overleaf-sync/<host>-browser/`，下次复用。程序不会读取日常 Chrome 的用户目录、密码或 cookie，也不会把认证信息写入论文项目或 GitHub。

默认使用系统 Chrome。没有 Chrome 时：

```bash
python -m playwright install chromium
export OLSYNC_BROWSER_CHANNEL=chromium
ols login
```

浏览器验证需要人工操作时：`OLSYNC_VISIBLE=1 ols status`，或重新运行 `ols login`。不绕过验证码。登录过期会停下并提示重新登录。

## 连接一个项目

```bash
ols init --path ~/papers/my-paper \
  --project https://www.overleaf.com/project/YOUR_24_CHARACTER_PROJECT_ID

ols status --path ~/papers/my-paper   # 只预览
ols sync --path ~/papers/my-paper     # 双向同步
```

也接受 24 位项目 ID。项目文件夹可为空：第一次同步会下载线上文件。已存在的文件夹也可以连接；初次同步同名文件内容不同时会报冲突，不按时间戳猜测哪一边更新。

在论文目录中可省略 `--path`：

```bash
cd ~/papers/my-paper
ols status
ols sync
```

每个文件夹独立存放 `.overleaf-sync/` 配置和同步基准，可以连接任意多个项目。不同账户可以用 `login --profile /absolute/profile/path` 和 `init --profile /absolute/profile/path` 配对；自托管站点可用 `login --site https://latex.example.org` 及该站点的 `/project/<id>` 地址。

## 冲突规则

比较 **上次成功同步的内容摘要、本地内容、线上内容**：

| 情况 | 行为 |
|---|---|
| 仅本地修改/新增/删除 | 上传或删除对应线上文件 |
| 仅线上修改/新增/删除 | 下载或删除对应本地文件 |
| 不同文件分别在两边修改 | 同时完成双向同步 |
| 同一文件两边最终内容相同 | 直接接受，无需传输 |
| 同一文件两边修改且内容不同 | **停止整个计划**，不应用其他文件 |
| 一边删除、另一边修改 | 冲突，停止 |
| 二进制文件双边修改 | 冲突，显示大小和 SHA-256 |

这是**文件级保守策略**：即使两人改的是同一文件不同段落，也请人工合并；不会自行拼接研究文稿。文本冲突显示路径、`@@ -本地行号 +线上行号 @@` 和有限行数的差异，长行只显示前 240 字符。完整两侧文件保持原位。

例如：

```text
STOP: 1 conflict(s). No project files changed by this run.

CONFLICT: main.tex
--- local/main.tex
+++ overleaf/main.tex
@@ -10,3 +10,3 @@
-local paragraph
+remote paragraph
```

检查后可以明确选择一个文件的整份内容（包括该侧的删除状态）：

```bash
ols sync --take-local main.tex
ols sync --take-remote references.bib
# 先预览多个明确选择
ols sync --dry-run --take-local main.tex --take-remote references.bib
```

也可以人工合并两侧，让同一文件内容一致后再运行 `sync`。不要删除 `.overleaf-sync/` 来“修复”冲突，这会丢失识别删除操作所需的同步基准。

## 时间戳

本地纳秒级修改时间、成功同步时间和线上 ZIP 导出时间会记录在状态里；下载文件应用 ZIP 中的时间戳。

**内容哈希决定变化，时间戳仅供参考。** `touch` 不会产生上传；内容变了但时间没变仍会检测出来。Overleaf ZIP 的时间可能是导出时间，并且没有可靠时区，所以不能承诺准确的线上逐文件修改时间，更不会按“较晚的一边获胜”。

UTF-8 文本（`.tex/.bib/.sty/.cls/.bst/.txt/.md/.csv/.tsv/.svg`）按 LF 换行比较，以兼容 Overleaf 上传时的换行转换。本地仅有 CRLF/LF 差别不会被重写；从线上更新文本后使用 LF。其他文件逐字节比较。非 UTF-8 的上述文本先转换编码再同步。

## 默认排除与文件范围

默认跳过：隐藏文件/目录、`.git/`、`.overleaf-sync/`、`backups/`、`output/`、`build/`、`tmp/`、虚拟环境和 LaTeX 编译产物（`.aux/.bbl/.log/.synctex.gz` 等），以及根目录常见的 `main.pdf`、`output.pdf`。`figures/diagram.pdf` 等 PDF 资源仍同步。名为 `main.pdf`/`output.pdf` 的文件在任意目录都会被排除。

初始化时增加排除规则：

```bash
ols init --path ~/papers/my-paper --project PROJECT_ID \
  --exclude 'notes/**' --exclude '*.private.tex'
```

规则保存在 `.overleaf-sync/config.json` 的 `exclude` 列表，可修改；新排除的路径不参与任何同步，包括删除。匹配整个相对路径或任意路径组成部分。`.git` 和工具状态目录永久排除。

删除会传播，请先用 `status` 检查。重命名作为旧路径删除、新路径新增处理；空文件夹不同步，也不自动清理旧空目录。符号链接、路径穿越和文件/目录冲突会停止同步。每文件 50 MiB、项目 250 MiB 的保守大小上限。

## 中断与并发

冲突预检失败时，本地和线上项目文件均不改变，仅记录诊断信息。同步中断会留下 `.overleaf-sync/pending.json` 以及相关内容对象：

```bash
ols recover
```

`ols status` 可以预览中断后的恢复计划，`ols sync` 也会自动进入恢复流程，无需手动清理记录。`ols recover --dry-run` 只预览。

恢复会核对每个文件仍是“操作前”或“计划结果”，补齐未完成部分。如果上次只是下载，而且本地还保持操作前的内容，会基于当前线上快照和上次成功同步的基准重新计算；真正的双边改动仍会报冲突。此前通过 `--take-remote` 等选项做的选择，不会自动套用到后续产生的新内容。

中断后出现的新改动若无法安全判断，会显示文件、行号和简略内容差异。检查后可明确选择该文件当前的整份内容：

```bash
ols recover --dry-run --take-remote main.tex  # 预览采用当前线上版本
ols recover --take-remote main.tex           # 确认采用当前线上版本
# 或者采用当前本地版本（不要两条都执行）
ols recover --take-local main.tex
```

这些选项同样支持删除状态，也可重复指定多个文件；存在任何未解决的冲突时，整次操作停止。替换恢复计划前，旧计划、旧内容对象及当前两边的文件都会备份到 `.overleaf-sync/recovery-backups/`，完成时显示路径；备份里的 `local/` 和 `overleaf/` 可直接查看。预览不会替换恢复计划或创建恢复备份。

纯下载会保存已经成功取得的线上快照。如果线上随后继续编辑，这些改动留到下一次同步处理，不再因为下载后的重复检查而陷入恢复循环。同步仍会检查本地文件，避免覆盖下载期间新产生的本地修改。

不要直接删除恢复文件。`.overleaf-sync/objects/` 是单次恢复数据，不是完整版本历史；`recovery-backups/` 只保留重新规划恢复时的副本，不会自动清理，可在确认恢复成功且无需旧内容后自行删除。

**网页接口没有多文件事务或原子 compare-and-swap。同步期间应暂停本地编辑；涉及上传或线上删除时也应暂停 Overleaf 上的编辑。** 程序会完整预检、每次线上写入前重新校验、全部线上写入后重新下载核对，但仍存在“检查后、写入前”的极短竞态窗口。如果上传了一部分后网络中断或发现并发改动，已经完成的线上文件可能保留；程序会停止，保留 journal，不谎称整次回滚。

为遵守网页版 ZIP 下载频率限制，两次下载之间至少间隔 6.5 秒。单文件/少数文件的日常同步通常需要数秒到数十秒；初次同步大量文件可能更慢。无后台监控、无自动循环，始终由你明确调用。

## 调试与机器调用

```bash
ols status --json
ols sync --dry-run --json
python -m unittest discover -s tests -v
```

退出码：`0` 成功，`1` 认证/网络/文件/中断恢复等错误，`2` 内容冲突，`130` 用户中断。冲突摘要另存 `.overleaf-sync/conflicts.json`。请不要把状态目录或独立浏览器配置目录提交到公共仓库。

可用两个普通文件夹测试整个同步流程，不需要登录或网络：

```bash
mkdir -p /tmp/ols-local /tmp/ols-remote
ols init --path /tmp/ols-local --project /tmp/ols-remote
echo 'hello' > /tmp/ols-local/main.tex
ols sync --path /tmp/ols-local
```

## 更新

```bash
cd /path/to/overleaf-sync-lite
git pull --ff-only
.venv/bin/python -m pip install -e .
```

这是基于 Overleaf 开源实现的**非官方兼容客户端**。网页版可以不依赖 Git 付费功能；认证、导出、上传和项目元数据结构发生变化时可能需要更新适配器，程序会遇错停止。项目本身必须对登录账户有相应读写权限。

实现参照 Overleaf 自身的 [上传处理](https://github.com/overleaf/overleaf/blob/main/services/web/app/src/Features/Uploads/ProjectUploadController.mjs)、[文件导入](https://github.com/overleaf/overleaf/blob/main/services/web/app/src/Features/Uploads/FileSystemImportManager.mjs)、[文件操作](https://github.com/overleaf/overleaf/blob/main/services/web/app/src/Features/Editor/EditorHttpController.mjs) 与 [路由](https://github.com/overleaf/overleaf/blob/main/services/web/app/src/router.mjs)。代码独立实现，未复制上游源码。

当前验证覆盖同步引擎与网页协议的模拟测试；不把这些测试当作已在每个 Overleaf 部署完成真实账户端到端验证。首次使用请先 `status`，并在一个副本项目中验证上传/下载后再用于协作文稿。
