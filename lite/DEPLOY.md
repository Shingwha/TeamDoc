# TeamDoc Lite 部署与运维

> 面向把这套系统放到内网服务器上长期运行的人。开发相关约定见 `HANDOFF.md`。
> 内网离线环境无需联网安装:vendor 目录随仓库提交,服务端零外网调用。

---

## 1. 首次部署

### 1.1 准备

| 项 | 要求 |
|---|---|
| Python | 3.12 或更高(开发环境用 3.13) |
| 依赖管理器 | [uv](https://docs.astral.sh/uv/)(推荐);无 uv 时可用 pip + venv |
| 磁盘 | 按实际用量 + **至少 1GB 余量**(服务端会保留 `STORAGE_RESERVE_MB`,默认 1024,低于它拒绝上传) |

```bash
cd lite/server
uv sync                     # 有网环境:装依赖;离线环境见 §1.4
```

### 1.2 启动

```bash
uv run uvicorn main:app --host 0.0.0.0 --port 8000
# 或
uv run python main.py       # 读 PORT 环境变量
```

**必须单 worker。** SQLite 是单写者模型,多进程会出现推送丢失、写锁冲突等难以排查的问题。不要加 `--workers N`。

首次启动只建表、不预置账号。浏览器访问走初始化向导,创建第一个管理员。

> ⚠ **初始化窗口**:从服务启动到第一个管理员建好之间,**任何能访问该端口的人都能抢先完成初始化**并成为管理员。所以启动服务后请立刻自己打开页面把管理员建掉;若暂时不部署,别挂着服务让人扫到。

**必须放行防火墙端口**(不做这步的典型症状:部署机上访问 `127.0.0.1:8000` 正常,别的电脑连不上):

```bat
:: Windows(默认拦截入站)
netsh advfirewall firewall add rule name="TeamDoc" dir=in action=allow protocol=TCP localport=8000
```
```bash
# Linux
sudo firewall-cmd --permanent --add-port=8000/tcp && sudo firewall-cmd --reload   # firewalld
sudo ufw allow 8000/tcp                                                          # ufw
```

> **数据目录必须放在本地磁盘。** SQLite 的 WAL 模式依赖文件锁,跑在 SMB/NFS 网络盘上会锁失效直至库文件损坏 —— `TEAMDOC_DATA_DIR` 不要指到网络共享。(**备份**目录放 NAS 是可以的,那里只是普通文件读写。)

> **数据库结构由 `models.py` 单一定义**,启动时自动建缺失的表并做一致性自检。全新部署不需要任何额外步骤。
> 若启动时报「数据库结构与 models.py 不一致」,说明库里有残留列或缺列 —— 见 §6 的处理方式。

### 1.3 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `8000` | 监听端口(仅 `python main.py` 时生效) |
| `TEAMDOC_DATA_DIR` | `server/data` | 数据目录(库 + 物理文件)。**生产环境建议挪到独立数据盘** |
| `MAX_UPLOAD_MB` | `20480` | 单文件上传上限(20GB)。**改了必须同步改反代**,见 §1.5 ① |
| `STORAGE_RESERVE_MB` | `1024` | 要求保留的最小磁盘余量,低于它拒绝上传。这才是防写满磁盘的兜底 |
| `SESSION_TTL_DAYS` | `7` | 会话有效期(剩余寿命不足一半时自动续期) |
| `REMEMBER_TTL_DAYS` | `30` | 登录勾选"记住我"时的会话有效期。不勾选仍按 `SESSION_TTL_DAYS` |
| `LOGIN_MAX_FAILS` | `5` | 同一账号在窗口内的失败上限,达到即进入登录冷却(见 §4「账号安全」) |
| `LOGIN_FAIL_WINDOW` | `900` | 失败计数窗口(秒),账号与来源两个维度共用 |
| `LOGIN_LOCKOUT` | `60` | 账号首次冷却时长(秒);重复触发按 2 的幂翻倍 |
| `LOGIN_LOCKOUT_MAX` | `3600` | 账号冷却上限(秒)。1 小时封顶,不会永久锁号 |
| `LOGIN_IP_MAX_FAILS` | `20` | 同一来源 IP 在窗口内的失败上限(防密码喷洒)。**`0` = 关闭来源维度**(反代不给真实 IP 时必须关,见 §1.5 ④) |
| `LOGIN_IP_LOCKOUT` | `300` | 来源 IP 超限后的冷却时长(秒),不倍增 |
| `LOGIN_EVENT_KEEP_DAYS` | `30` | 登录审计保留天数(超期分批删除)。`0` = 不清理 |
| `BACKUP_DIRS` | 空 | 备份目标目录,逗号分隔(可多个盘/网络共享)。为空则不启用自动备份,见 §3.1 |
| `BACKUP_INTERVAL_HOURS` | `24` | 自动备份间隔(小时)。`0` = 关闭定时,只保留手动备份 |
| `BACKUP_KEEP` | `7` | 每个目标目录保留最近几份备份,更旧的自动删除。`0` = 不限 |
| `VERSION_MERGE_MINUTES` | `5` | 同一人在窗口内的连续保存合并为一个版本还原点 |

`MAX_UPLOAD_MB` 默认给到 20GB 是有意的:内网常见设计源文件与素材压缩包动辄十几 GB,
默认值定小了就变成"每次遇到更大的包都来改一次配置"。**上限调大不会让磁盘失去保护** ——
真正兜底的是 `STORAGE_RESERVE_MB` 那道磁盘守卫(写盘前与写盘过程中各查一次),
所以"文件太大"和"空间不足"会以各自贴切的原因分别被拦下。
当前生效值可在「管理后台 → 存储」只读查看,不必翻文档。

### 1.4 离线环境安装依赖

在有网机器上导出依赖,把 wheel 一起带进内网:

```bash
# 有网机器
uv export --no-hashes > requirements.txt
pip download -r requirements.txt -d wheels/

# 内网机器
python -m venv .venv
.venv/Scripts/pip install --no-index --find-links=wheels/ -r requirements.txt
```

前端不需要任何构建步骤:vendor 目录已随仓库提交,`index.html` 直接引用相对路径。

### 1.5 反代(可选但推荐)

内网若要 80/443 或域名访问,用 Nginx / Caddy 反代。**三处必须注意**:

> **① 与 `MAX_UPLOAD_MB` 是一条铁律:反代上限必须 ≥ 应用上限。**
> 下面示例写 `20g`,正好等于应用默认值。两个值必须**一起改** ——
> 若只调大应用而上限仍卡在反代,大包会在到达应用前就被 nginx 拦成 413,
> 用户看到的是 nginx 那句英文错误,而不是应用明确的「文件过大(上限 20GB)」,
> 排查时极易误判成应用侧问题。

```nginx
server {
    listen 80;
    server_name teamdoc.intranet;
    client_max_body_size 20g;       # ① 默认 1m;须 ≥ MAX_UPLOAD_MB(§1.3)
    proxy_read_timeout 3600s;       # ② 大文件下载/打包耗时长,默认 60s 会掐断
    proxy_send_timeout 3600s;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /ws/ {                 # ③ WebSocket 必须显式升级,否则协同失效
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
    }
}
```

Caddy 下同理:WebSocket 会自动升级,但要注意 `request_body { max_size }` 与超时,
且 `max_size` 同样须 ≥ `MAX_UPLOAD_MB`。

> **若启用了 HTTPS**:会话 Cookie 未设 `Secure` 标志,因为它同时要服务于"内网直连 HTTP:8000"
> 这种部署。如果反代对**外**提供 HTTPS、而 8000 端口在网络上也可达,那么会话 Cookie 可能
> 通过明文 HTTP 回传。两种收敛方式:① 防火墙只放行反代端口、关掉对外的 8000;
> ② 若确定只用 HTTPS,给 `_set_session_cookie` 加上 `secure=True`(见 `server/auth.py`)。
> 另注意反代若**不在同一台机器**上,uvicorn 需要 `--proxy-headers --forwarded-allow-ips=<反代IP>`
> 才会采信 `X-Forwarded-Proto`。

> **④ 反代必须给应用真实客户端 IP,否则关掉登录节流的来源维度。**
> 登录失败会按**来源 IP**计数(挡"一个来源轮着试很多账号"的密码喷洒)。应用从
> `request.client.host` 取地址,由 uvicorn 的 proxy-headers 中间件从 `X-Forwarded-For`
> 还原;该中间件**默认只信任 127.0.0.1 发来的头**。
> - 反代与应用**同机**(上面的示例就是):什么都不用做,拿到的是真实客户端地址。
> - 反代在**另一台机器**:启动时必须加 `--proxy-headers --forwarded-allow-ips=<反代IP>`,
>   否则所有请求都来自反代地址 —— 那时光源维度会退化成"全站共用一个桶",
>   一个人连错就会把所有人一起关在门外。**这种情况请设 `LOGIN_IP_MAX_FAILS=0` 关掉该维度**
>   (账号维度仍然生效,撞单账号依旧会被冷却)。
> 是否退化很好判断:管理后台 → 用户 → 「登录详情」里的来源 IP。若所有人都显示成反代地址,
> 就是没传真实 IP。

---

## 2. 日常运行

### 2.1 Windows(内网服务器常见)

用 [NSSM](https://nssm.cc/) 注册为服务,开机自启、崩溃自动重启:

```bat
:: 日志目录必须先建好:NSSM/Windows 不会自动创建它,目录不存在日志会静默丢失
mkdir D:\teamdoc-data\logs

nssm install TeamDoc "C:\path\to\lite\server\.venv\Scripts\python.exe" "main.py"
nssm set TeamDoc AppDirectory "C:\path\to\lite\server"
nssm set TeamDoc AppEnvironmentExtra PORT=8000 TEAMDOC_DATA_DIR=D:\teamdoc-data BACKUP_DIRS=D:\teamdoc-backup
nssm set TeamDoc AppStdout "D:\teamdoc-data\logs\out.log"
nssm set TeamDoc AppStderr "D:\teamdoc-data\logs\err.log"
nssm set TeamDoc AppRotateFiles 1
nssm set TeamDoc AppRotateBytes 10485760
nssm start TeamDoc
```

**环境变量要在这里一并写全**(尤其是 `BACKUP_DIRS`)。只在命令行里 export 过、没写进服务配置的话,服务方式启动时读不到,表现为"配了自动备份却一直不备份"。`TEAMDOC_DATA_DIR` 已由 models.py 自动创建,但 `logs\` 目录不会 —— 见上面第一行。

**日志一定要落盘**:uvicorn 默认只写 stdout,不重定向的话重启即丢,出问题无从排查。上面前两行 `AppStdout/AppStderr` 就是做这件事,配合 `AppRotateFiles` 自动轮转(10MB 一份)。

### 2.2 Linux

```ini
# /etc/systemd/system/teamdoc.service
[Unit]
Description=TeamDoc Lite
After=network.target

[Service]
Type=simple
User=teamdoc
WorkingDirectory=/opt/teamdoc/lite/server
# StateDirectory 让 systemd 建好 /var/lib/teamdoc 并授予 User 属主。
# 不写它的话,目录不存在或属主不对,首次启动会在 models.py 的 mkdir 处因权限失败。
StateDirectory=teamdoc
Environment=PORT=8000
Environment=TEAMDOC_DATA_DIR=/var/lib/teamdoc
Environment=BACKUP_DIRS=/mnt/backup/teamdoc
ExecStart=/opt/teamdoc/lite/server/.venv/bin/python main.py
Restart=always
RestartSec=3
# 日志走 journald:journalctl -u teamdoc -f
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
# 备份目标目录要预先建好并授权(程序不会自动创建,以免写到未挂载的假路径)
sudo install -d -o teamdoc -g teamdoc /mnt/backup/teamdoc
```

```bash
systemctl daemon-reload && systemctl enable --now teamdoc
```

### 2.3 健康检查

```bash
curl -s http://127.0.0.1:8000/api/auth/status
# {"bootstrapped":true,"dbReady":true}
```

`dbReady:false` 表示数据库打不开(权限/磁盘满/文件损坏)。

### 2.4 上线前检查清单

逐项过一遍再交给同事用(每条都对应过一次真实故障):

- [ ] **防火墙已放行**端口,从**另一台电脑**访问成功(不是只在部署机上试 127.0.0.1)
- [ ] 第一个管理员已建好(`/api/auth/status` 返回 `bootstrapped:true`)——别让服务裸奔着等初始化
- [ ] `TEAMDOC_DATA_DIR` 指向**本地**独立数据盘,且已确认该目录有写入权限
- [ ] `BACKUP_DIRS` 已写进**服务配置**(NSSM `AppEnvironmentExtra` / systemd `Environment`),目标目录**已存在**
- [ ] 管理后台「备份与恢复」显示每个目标为"正常",不是"目录不存在"
- [ ] **做过一次真实的恢复演练**:造点数据 → 立即备份 → 改数据 → 上传备份 → 重启 → 确认回到备份时点
      (一条命令:`cd lite/server && uv run pytest ../tests/test_backup_restore.py -v`,见 §3.5)
- [ ] 若挂了反代:大文件上传、大文件下载、**协同编辑(WebSocket)**三项都实测通过(§1.5 三处坑)
- [ ] 日志确实落盘(重启服务后 `logs/out.log` 有内容)
- [ ] 已知浏览器版本能正常打开界面(旧内核兼容性见 §7)

---

## 3. 备份与恢复

管理后台「备份与恢复」区负责全部备份操作。备份有**三种触发方式**,产物是同一个:
定时自动、点「立即备份」、点「下载备份」。

### 3.1 配置备份位置(多个目标)

用环境变量 `BACKUP_DIRS` 指定备份目录,**逗号分隔**,可填多个:

```bash
# 本机另一块盘 + 网络共享 + 移动硬盘,一次备份同时写三处
BACKUP_DIRS=D:\teamdoc-backup,\\NAS\share\teamdoc,E:\td-backup
```

| 变量 | 默认 | 说明 |
|---|---|---|
| `BACKUP_DIRS` | 空 | 备份目标目录,逗号分隔。**为空则不启用自动备份**(手动下载仍可用) |
| `BACKUP_INTERVAL_HOURS` | `24` | 自动备份间隔(小时)。`0` = 关闭定时,只保留手动 |
| `BACKUP_KEEP` | `7` | 每个目标目录保留最近几份,更旧的自动删除。`0` = 不限 |

**一次备份会写入每个配置的目录**,所以单块盘故障不会同时带走数据和备份 —— 这正是
`DEPLOY.md` 一贯建议的"备份放到另一台机器"。多个盘、网络共享、移动硬盘都只是**路径**,
不需要任何额外协议或客户端。

> **目标目录必须已存在,程序不会自动创建。** 这是有意的:移动盘没插、共享没挂载时,
> 若自动 `mkdir` 会在本机建出同名目录,备份写到假路径上而你毫不知情 —— 比直接失败
> 危险得多。目录不存在会在管理后台标红并记为失败。

**保留策略只删自己生成的备份**(文件名形如 `teamdoc-backup-<时间戳>.zip`)。你手工放进
那个目录的其它文件、旧备份副本,一律不动。

### 3.2 备份产物与校验

每次备份产出 `teamdoc-backup-YYYYMMDD-HHMMSS.zip`,内含:

```
teamdoc.db    数据库一致性快照(VACUUM INTO 生成)
files/        云空间全部物理文件
RESTORE.txt   恢复步骤(压缩包内自带)
```

**文件名带时间戳**,多次备份不会互相覆盖,事后能分清哪份是新的。

每次写完后会**回读校验**:zip 可解、库能打开、表齐全。校验不通过就删掉该文件并记为
失败 —— 不留一个"看起来成功、恢复那天才发现坏掉"的包。管理后台会显示每个目标目录的
上次结果(成功 / 失败原因 / 跳过旧份数)。

**为什么必须用这套机制而不是直接复制 data 目录**:数据库以 WAL 模式运行,未 checkpoint
的写入还在 `teamdoc.db-wal` 里,单独复制 `.db` 会拿到**旧快照**(实测中复制出的库一张表
都没有)。`VACUUM INTO` 生成的是单文件完整快照,且**不需要停机**。

### 3.3 从备份恢复

**推荐在管理后台操作**(「从备份恢复」卡):

1. 点「选择文件」上传备份 zip。上传即校验(结构、库可读、版本一致),不通过直接拒绝并
   说明原因。
2. 确认页面显示的备份信息(上传时间、内含几个用户/项目/文档/文件)无误后,点
   **「重启后生效」**。
3. **重启 TeamDoc 服务**。重启时将:
   - 把当前 `teamdoc.db` 与 `files/` 整体挪到 `data.pre-restore-<时间戳>/`(**恢复错了能回退**)
   - 解包备份内容
   - 清理暂存区

**为什么必须重启**:替换正在运行的 SQLite 库(及其 `-wal`/`-shm`)是危险动作 ——
连接池里还有句柄、并发请求可能正在写。重启瞬间没有任何连接,替换才是安全的。
所以"上传"与"生效"是两步,页面上会明确提示这一点。

**只能恢复到相同版本。** 本项目**没有数据库迁移机制**:启动时只建缺失的表、已有表不加列,
结构与 `models.py` 不一致会**直接启动失败**。所以上传时会预先比对表结构,来自不同版本的
备份会被当场拒绝,而不是等重启后起不来。跨版本迁移请用同版本代码恢复后再升级。

> 具体判据:备份里的库若**缺少**当前版本的表,会被拒绝。若备份来自**更新**的版本
> (多出一些表),当前校验不会拦 —— 那种情况请直接用对应版本的代码。

**恢复后文件路径是可移植的**:`files.storage_path` 只存文件名,物理位置由
`TEAMDOC_DATA_DIR` 决定。因此把备份恢复到**另一台机器或另一个数据目录**后,文件仍能正常
下载(历史库里的绝对路径会在启动时自动归一为文件名)。这也意味着**换机恢复是受支持的**,
不必把数据装回原来的绝对路径。

### 3.5 恢复演练(上线前必做一次)

备份最怕的不是没配,而是"配了、看着成功、真要恢复时不可用"。上线前请完整跑一遍:

```bash
cd lite/server
uv run pytest ../tests/test_backup_restore.py -v
```

测试自建隔离实例(临时数据目录 + 随机端口),自动完成:造数据 → 备份 → 改数据 →
上传备份 → 置为待生效 → **重启服务(测试自动完成,无需手工)** → 断言数据回到备份
时点、恢复前的数据被留了一份。全绿即恢复链路可用。

无法进入管理后台时的手工恢复步骤见压缩包内的 `RESTORE.txt`(停服 → 改名数据目录 →
解包 → 启动)。

### 3.4 磁盘与清理

管理后台「存储」区能看到:磁盘总量/已用/剩余、各项目占用、回收站占用、**无主文件**
(磁盘上有、数据库里没有引用)。

无主文件来自上传中断或历史遗留,占着空间但界面上永远看不见。点「清理」即可回收,
流程是**先预览再确认**:先告诉你将删除几个文件、释放多少字节,确认后才执行。

> **危险保护:熔断。** 当超过 2/3 的磁盘文件都被判为无主时,清理会被**拒绝**。这不是
> 垃圾变多了,而更像"数据库为空 / 指向了错的数据目录 / 恢复了一份旧备份"——
> 这种情况下清理会把磁盘上的文件全部删光。界面上会明确阻止并提示先核对数据目录。
> 确属垃圾要强清,可调接口并显式带 `force=1`。

**磁盘占用怎么降**:
- 清空回收站(回收站里的文件仍占物理磁盘,只有彻底删除才释放)
- 历史版本:文档版本按年龄分层保留(稳态约 124 条/文档),频繁编辑且粘过大表格的文档版本占用可观,可在管理后台看到总量
- 清理无主文件

---

## 4. 日常维护要点

| 事项 | 做法 |
|---|---|
| 用户忘记密码 | 管理后台 → 用户行「重置密码」 |
| 用户离职 | 管理后台 → 用户行「禁用」(立即失去访问,数据保留) |
| 邮箱填错了 | 管理后台 → 用户行「编辑」,**邮箱可改**(会查重)。改完用新邮箱登录 |
| 建错了账号(如笔误邮箱) | 若该账号**从未产生数据**,可「删除」;一旦有文档/文件/项目就只剩「禁用」——见下方说明 |
| 脚本/自动化访问 | 个人设置 → 访问令牌(PAT,`tdp_` 前缀)。只读令牌无法执行写操作 |
| 放开某个项目给全团队看 | 项目设置 → 可见性 → 公开(任何登录用户都能在「发现」里看到并**自助加入**,加入前看不到项目内容;加入后的角色在同一张卡上选) |
| 只共享一个文件 | 云空间 → 文件行「公开」图标(任何登录用户可下载该文件,不必公开整个项目) |
| 忘记管理员密码且无人可重置 | 见 §5 |

### 关于账号安全(没有两步验证)

本项目**不做两步验证(TOTP)**——曾经的实现已整体移除。理由:内网自部署、30 人、无公网暴露,TOTP 防的"密码泄露后的二次验证"价值低(内部人都有自己的账号),而账号管控已由这三条覆盖:

- **禁用用户**:管理后台一行操作,立即失去访问,数据保留。**离职当天就该做这一步**
- **删除用户**:只对**从未产生数据**的账号开放(管理后台只在可删的行显示删除按钮)。
  典型用途是清理建号时填错邮箱的账号 —— 否则那个邮箱会一直占着唯一约束,既登不进去
  也无法重名重建。**一旦该账号有文档/文件/项目,删除会被拒绝**并提示改用禁用:
  文档与文件的 `created_by` 是软引用,删掉用户只会留下指向不存在用户的悬空记录,
  "他建的内容归谁"是个必须回答却答不好的问题。这也是协作类系统的通行做法
- **PAT 可只读、可吊销**:脚本化访问用令牌,不给密码;令牌泄露直接吊销
- **个人空间私有 + 项目可见性默认私有**:内容不会因为某个账号被盗而外泄到组织之外

要知道的代价:内网若有人拿到同事密码(比如共享密码、离职账号未禁用),没有第二道防线。所以**禁用离职账号**在这里比任何二次验证都重要;也建议提醒团队不要把密码写在共享文档里。**猜密码/撞库**这类"口令被反复试"的攻击则另有专门防线,见下一节。

### 登录节流与登录可见性

没有第二道口令防线的前提下,防"猜密码"与"撞库"靠两件事:**挡住** + **看清**。

**挡住(节流)**:同一账号在窗口内失败到 `LOGIN_MAX_FAILS`(默认 5)次即进入冷却 —— 首次 60 秒,重复触发翻倍,1 小时封顶;同一**来源 IP** 失败到 `LOGIN_IP_MAX_FAILS`(默认 20)次也会冷却,挡的是"一个来源轮着试很多账号"(密码喷洒)。三条要点:

- 计数**落库**(`throttle_state` 表),重启服务不会清零 —— 重启不能当绕过手段。
- 冷却期内**即使密码正确也会被拒**(`429` + `Retry-After`)。所以告诉用户"稍等几十秒再试"不是系统故障。
- 账号**不存在**时同样计数、同样冷却,且响应耗时与真实账号一致 —— 从外面看不出某个邮箱是否注册过。

**看清(登录状态)**:管理后台 → 用户 → 行内「登录详情」,分三块:

| 区块 | 内容 | 能做什么 |
|---|---|---|
| 登录限制 | 是否在冷却、第几次触发、窗口内已失败几次 | **解除锁定**(不必等冷却过期) |
| 活跃会话 | 每台设备的 设备/系统、来源 IP、登录时间、最近活跃、是否在线 | 单条**强制下线**、**全部下线** |
| 登录记录 | 最近 50 次尝试(成功/密码错误/已禁用账号/触发锁定)含来源与设备 | 判断是本人还是他人在试 |

页面底部的「**登录动态**」是全站最近 100 条,点「仅失败」立刻看有没有人在撞库 —— **账号不存在的失败也在里面**(那是密码喷洒最典型的特征)。用户列表本身也带「最近登录(时间 + IP + 在线)」与「锁定至 …」徽标,不用逐个点开。

要留意并处理的信号:

- 某来源 IP 对**一批不同账号**连续失败 → 密码喷洒:来源维度会自动冷却;必要时在网关/防火墙层封该 IP。
- 某个账号被**长期**大量尝试 → 目标性攻击:确认该账号口令强度,必要时先禁用。
- 已禁用/离职账号出现「已禁用账号」记录 → 有人还在试旧账号,确认设备是否已交回。

按处置动作选入口:被锁住 → 「解除锁定」;怀疑会话被窃取 → 「强制下线/全部下线」(不动口令);怀疑口令泄露 → 「重置密码」(会话与访问令牌一并失效);人离职 → 「禁用」。

### 关于公开的边界

- 公开 = **本实例所有登录用户可发现(「发现」广场)+ 可自助加入**;**加入前看不到项目里的任何内容**,加入后按项目设置的角色(只读成员 / 编辑者,默认只读)获得权限。关闭公开后立即失效:从广场消失、不能再被加入(**已在册成员不受影响**)。
- 与私有项目的区别只有两条:能出现在广场、能被自助加入。**没有审批环节** —— 想让某个人进来但不想对外开放的项目,保持私有、由管理员在成员页添加。
- **个人空间永不可公开**,服务端硬拒;它恒为私有,因此也永远无法被加入。个人空间是私有草稿区,设计上不承担共享职能。
- 成员列表只有真成员与全局管理员能打开(非成员没有任何角色),因此照常包含邮箱。
- 没有"匿名链接"(发给没有账号的人)。内网外部访问不到本服务,该功能收益低;要分享单个文件给项目外的同事请用**单文件公开**(云空间文件行,与项目可见性无关)。

---

## 5. 故障处理

| 现象 | 排查 |
|---|---|
| 大文件上传 413 | 反代 `client_max_body_size` 太小(§1.5 ①) |
| 大文件下载中途断/进度卡住 | 反代 `proxy_read_timeout` 太小(§1.5 ②) |
| 协同编辑不生效(看不到他人光标) | 反代 WebSocket 未升级(§1.5 ③);或误开了多 worker |
| 上传报"服务器存储空间不足" | 磁盘余量低于 `STORAGE_RESERVE_MB`;去管理后台清理或扩容 |
| 界面里文件总数远小于磁盘占用 | 回收站里还有内容,或无主文件待清理(§3.4) |
| 备份目标目录标红"目录不存在" | 移动盘没插/共享没挂载。程序不会自动建目录(§3.1),插好或检查路径即可 |
| 备份显示"部分成功" | 有目标没写成(见该行失败原因);通常是目标盘满或路径不可达 |
| 清理无主文件被拒(提示超过 2/3) | 熔断保护生效(§3.4)。先确认数据目录与数据库是否匹配,别直接强清 |
| 上传备份被拒"结构与当前版本不一致" | 备份来自不同版本。需用同版本代码恢复,跨版本请先恢复再升级(§3.3) |
| 恢复后重启,数据没变 | 确认点过「重启后生效」(上传后需再点一次),且**确实重启了服务** |
| 页面改版后仍是旧样式 | 静态资源已设 `no-cache`,浏览器会携 ETag 重验证;若仍异常,确认反代没有额外加长缓存 |
| 管理员账号被锁(忘记密码 + 无人能重置) | 用另一个管理员账号重置;若**所有**管理员都被锁,只能手改数据库 —— 停服后改 `users.password_hash`,值由 `auth.hash_password("新密码")` 生成 |
| 登录提示「失败次数过多,请 N 秒后重试」 | 登录节流生效(§4)。等 N 秒即可;或管理员到 用户 →「登录详情」→ 解除锁定。**全员同时出现**说明来源 IP 维度退化成了一个桶:反代没传真实客户端 IP,按 §1.5 ④ 配置或设 `LOGIN_IP_MAX_FAILS=0` |
| 用户"被登出"但没改过密码 | 有人点了「强制下线/全部下线」,或该账号被重置过密码(会吊销全部会话与令牌)。到「登录详情」看会话与登录记录 |
| 服务起不来 | 看日志(§2.1);`/api/auth/status` 的 `dbReady` 判断是不是数据库问题 |
| **进程还活着,但所有客户端都连不上/转圈** | 见下面「全站无响应怎么查」 |

### 全站无响应怎么查

日志落在 `lite/server/logs/`(控制台直跑时也一样,重启不丢):

1. `teamdoc.log` —— 应用与访问日志。看最后几行是什么时候、哪个请求开始不对。
2. `stall-*.txt` —— 事件循环卡住超过 10 秒时 watchdog 自动转储的**全线程 Python 栈**,
   直接指出卡在哪个调用(SQLite I/O、锁等待等)。
3. 服务还能应答时,管理员调 `GET /api/admin/diagnostics`:进程运行时长、连接池状态、
   库/-wal 文件大小、线程名、WS 连接数。库文件异常大或 WS 数暴涨都能一眼看出。
4. 服务完全不响应、也要拿到现场:用外部工具抓栈(它不依赖目标进程配合):
   ```bash
   uv tool install py-spy        # 或 pipx install py-spy
   py-spy dump --pid <uvicorn 的 python 进程 pid>
   ```
5. 记录下当时的**操作时间点**(谁在传大文件、谁开着编辑器),再对照日志定位。

> Windows 控制台直跑时注意:鼠标选中控制台窗口的文字会暂停程序输出(快速编辑模式),
> 看起来就像"卡死"。按 Esc 或回车即可恢复;正式部署请用 §2.1 的服务方式落盘日志。

### 手工重置密码(最后手段)

```bash
cd lite/server
.venv/Scripts/python.exe -c "
import models, auth
from models import SessionLocal, User
db = SessionLocal()
u = db.query(User).filter_by(email='admin@teamdoc.local').first()
u.password_hash = auth.hash_password('新密码至少8位')
db.commit()
print('已重置', u.email)
"
```

停服执行,改完再启动。

---

## 6. 升级

1. **先备份**(§3.2,建议先点「立即备份」确认每个目标都写成了)
2. 拉取新代码,`uv sync` 更新依赖
3. 重启服务
4. 跑一遍页面资产校验,确认前端资源完整(零外链 + no-cache,KaTeX/Prism 全量可达;
   内网部署前后都该跑):

```bash
cd lite/server
uv run pytest ../tests/test_page_assets.py -v
```

5. 浏览器强制刷新一次(静态资源已设 no-cache,通常不需要)

### 启动报「结构与 models.py 不一致」

这是**结构漂移**,不是数据问题。两种情形:

- **库里有、模型没有的列**(残留列):若它 NOT NULL 且无默认值,任何插入都会失败。删除它:
  ```bash
  # 停服后执行(SQLite 3.35+;本项目实测 3.47)
  cd lite/server
  .venv/Scripts/python.exe -c "
  import sqlite3
  c = sqlite3.connect('data/teamdoc.db')
  c.execute('ALTER TABLE projects DROP COLUMN color')   # 表名/列名按报错信息替换
  c.commit()"
  ```
- **模型有、库里缺的列**:`create_all` 不会给已有表加列。加回来:
  ```bash
  .venv/Scripts/python.exe -c "
  import sqlite3
  c = sqlite3.connect('data/teamdoc.db')
  c.execute(\"ALTER TABLE projects ADD COLUMN visibility VARCHAR(10) DEFAULT 'private'\")
  c.commit()"
  ```

不确定时就先备份(§3.1)再动手。开发期最省事的做法是删掉数据目录重建。

### 升级到「登录节流 + 登录状态」版本(2026-09)

本次新增两张表、给会话表加了来源信息。**只需要一条命令**,其余全自动:

```bash
# 停服后执行(保留现有会话;旧会话的 ip/UA 为空,界面显示"未知"或"—")
cd lite/server
.venv/Scripts/python.exe -c "
import sqlite3
c = sqlite3.connect('data/teamdoc.db')
for col, typ in (('ip', 'VARCHAR(45)'), ('user_agent', 'VARCHAR(300)'), ('last_seen_at', 'DATETIME')):
    try: c.execute(f'ALTER TABLE sessions ADD COLUMN {col} {typ}')
    except sqlite3.OperationalError as e: print('跳过(可能已存在):', e)
c.commit()"
```

- **两张新表**(`throttle_state` 登录节流状态、`login_events` 登录审计)由启动时的 `create_all` 自动创建,**不需要**任何手工步骤。
- 也可以最省事地 `DROP TABLE sessions;` 让程序重建 —— 代价是所有人重新登录一次(会话不承载业务数据)。
- 不执行则服务启动时会按 §2.1 的自检报「结构与 models.py 不一致」并停住,不会带病运行。
- **升级前的备份仍然可以恢复**:这两张表标了"可丢弃状态"(见 `models.py` 的 `info.disposable`),恢复校验不要求备份里存在它们,启动时会自动补建。正因为它们可丢弃,恢复一份旧备份不会带来任何结构问题。
- 本次新增的环境变量都有默认值(§1.3),不配也能跑;`LOGIN_IP_MAX_FAILS=0` 可关掉来源维度。

### 升级前的兼容性说明

- **不支持回退到旧版本代码**:新版本可能已写入旧代码不认识的字段。回退需同时恢复对应时点的备份。
- API 契约变更见 `HANDOFF.md` §5。若有外部脚本调用 API,升级前先看该节。

---

## 7. 规模与性能

### 7.1 浏览器兼容性

服务端只依赖标准库,但**前端有最低浏览器要求**:需要 **Chrome/Edge 111+(2023 年 3 月)**。

- 低于此版本(部分国产内核停留在 Chromium 86–110)不会白屏,界面可用,但 `color-mix()`
  相关的部分样式会退化:登录页渐变背景、搜索命中高亮、选中态的淡色底、悬浮态。
  关键几处(搜索高亮、遮罩层定位)已加静态回退,不至于不可用。
- 已知**会白屏**的只有旧于 Chrome 77 的内核,该问题已修(`matchMedia` 兼容分支)。
- 建议内网统一用较新的 Chrome/Edge;若必须支持老内核,请上线前用目标浏览器实际打开一遍,
  重点看:登录页、搜索、云空间、文档预览。

### 7.2 容量与性能

当前设计面向 **30 人小团队**:

- 云空间列表已分页(100/页 + 加载更多),服务端排序;文件夹上限 2000/目录。
- 单次打包上限 1000 个文件 / 4GB(可用 `ZIP_MAX_BYTES_MB` 调整)。
- 搜索与回收站仍是单次 500 条截断(超出时搜索会少结果,回收站会少列)。
- `referenced`(被引用徽标)每次列目录会扫描该项目全部文档正文——文档量上千后应改为引用索引表。
- 上传走 raw body 流式写盘,大文件不双写;单 worker 下并发上传会排队。
- 文档版本按年龄分层保留(稳态约 124 条/文档),不需要手工清理。

超过这个规模(比如用户上百、单目录上万文件、文档上万篇)需要先做的改动见 `HANDOFF.md` §7。
