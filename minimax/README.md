# 借阅清单的逾期边界

业务日期为 2026-04-10。未归还的 B 在 4 月 9 日到期，A 当天到期，C 次日到期；D 已归还。原实现把 B、A 都列为逾期。任务要求只返回 B，同时默认清单仍按登记顺序返回 B、A、C，空清单仍返回空列表。完整初始数据在 `sample/loans/test_catalog.py`。

## 从干净环境复现

在本目录运行命令。实测 CPython 3.13.15、seekdb 1.4.0.0、PowerContext 1.5.2 和 PyMySQL 1.2.0。需要一个可连接的 seekdb MySQL 端口、一个已存在的测试库，以及对该库创建表、查询和写入的权限。可在已有实例上新建空库，例如 `CREATE DATABASE sdd_demo;`。程序只管理该库中的专用表 `sdd_sources`。请为本题预留此表名。

如果从零启动数据库，先安装 [seekdb](https://www.oceanbase.ai/download)。以下是 Windows PowerShell 示例，在独立终端中运行，先将已安装的 seekdb 加入 PATH，留出至少 4 GiB 数据文件空间及日志余量：

```powershell
mkdir .run/db
seekdb.exe --nodaemon --port 12886 --base-dir "$((Resolve-Path .run/db).Path)" --parameter datafile_size=2G --parameter log_disk_size=2G --parameter memory_limit=2G
```

另开终端，进入本目录。新初始化实例可使用自带的 `test` 库。自备实例通过 `SDD_HOST`、`SDD_PORT`、`SDD_DATABASE`、`SDD_USER`、`SDD_PASSWORD` 设置连接；默认分别为 127.0.0.1、2881、test、root、空密码。若出现连接错误，先确认实例已就绪和端口匹配。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:SDD_PORT = '12886'
python verify.py all .run/review
```

Linux/macOS 客户端可使用 `python3.13 -m venv .venv`、`source .venv/bin/activate` 和 `export SDD_PORT=12886` 连接已就绪实例。复验读取随仓交付的 MiniMax 返回值和修改后快照，原始模型调用可单独核对。无需模型凭据即可运行全部复验断言。以普通 Python 模式运行，保留验证断言。

命令成功时输出 `all: PASS`。四份测试日志均记录命令、退出码、源文件和测试文件的 SHA-256（文本换行统一为 LF）。目标测试预期先因 `['B', 'A'] != ['B']` 失败、退出 1，修复后通过、退出 0。两个回归用例前后均通过。每次重置只需改用新的输出目录，例如 `.run/review2`；程序复制初始快照、建立新的数据库索引命名空间和空的 PowerContext 状态，不覆盖已存在的运行目录。

## 验证上下文与恢复

`context.py` 是 CLI 入口，输入任务 JSON（`id`、`question`）、项目根目录、状态目录，输出 JSON。`import` 导入或刷新索引并检索，`context` 读取索引与进度，`save` 接受含 `done`、`todo`、`checks` 三个列表的 JSON 文件，返回 PowerContext 写入后的进度。下面三条命令可单独观察复验后的状态。

```text
python context.py context --state .run/review/state
python context.py import --project .run/review/project --state .run/review/state
python context.py context --state .run/review/state
```

任务要求以下三条来源，路径相对当次导入的项目根目录。评审可以编辑运行副本的规格，再执行上面后两条命令检查正文及 SHA-256 的变化。

| 必要来源 | 核对内容 |
| --- | --- |
| loans/spec.md | LOAN-1 规定原有过滤与顺序，LOAN-2 规定到期当天仍在期限内 |
| loans/catalog.py | open_loans，业务修改所在函数 |
| loans/test_catalog.py | Target 的日期边界及 Regression 的默认清单、空输入测试 |

`holds/spec.md` 随初始项目一并导入，但本次借阅任务的查询结果只含 loans 的三条记录。`verify.py` 会在独立子进程中依次导入、保存、读取，再给规格追加一句说明并检查重新导入后的新正文。运行目录中的 `saved-1.json`、`saved-2.json` 是写入回执；`context.json`、`final.json` 是后续进程读回的上下文；`reimport.json` 包含更新后的规格。SQLite 状态文件由 PowerContext 管理。

## 核对实际上交的修改

- 修改前后快照为 `sample/loans/catalog.py` 和 `solution/catalog.py`。同一组测试运行两次，逐行差异见 `evidence/run/change.patch`。
- `evidence/minimax/messages.json` 是提供给模型的实际消息，包含数据库命中行及此前读回的测试进度。`reply.json` 保存返回标识、模型、用量和公开的回答正文。复验会把该正文与修改后的源文件对照。
- `evidence/run/` 保存实际开发运行的基线、检索、重启、规格更新和测试记录。日志中的本机路径只标识原始执行位置。
- 如要重新调用模型，先安装并登录官方 CLI。准备新的运行目录并请求候选文件：

```text
npm install -g mmx-cli@1.0.29
mmx auth login
python verify.py prepare .run/new-task
python agent.py .run/new-task/context.json .run/new-proposal
```

新回答和候选代码写入 `.run/new-proposal/`，供检查差异。历史提交使用 MiniMax-M3.1-Flash。模型重生成会消耗额度，结果可能变化。

模块分工、选择规则与范围见 [一页技术说明](DESIGN.md)。
