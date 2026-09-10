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

> **数据库迁移是自动的**:每次启动执行未应用的迁移(`migrations.py`)。升级代码后直接重启即可,不需要手动改库;也不需要停机——但建议在升级前先备份(§3)。

### 1.3 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `8000` | 监听端口(仅 `python main.py` 时生效) |
| `TEAMDOC_DATA_DIR` | `server/data` | 数据目录(库 + 物理文件)。**生产环境建议挪到独立数据盘** |
| `MAX_UPLOAD_MB` | `2048` | 单文件上传上限 |
| `STORAGE_RESERVE_MB` | `1024` | 要求保留的最小磁盘余量,低于它拒绝上传 |
| `SESSION_TTL_DAYS` | `7` | 会话有效期(剩余寿命不足一半时自动续期) |
| `VERSION_MERGE_MINUTES` | `5` | 同一人在窗口内的连续保存合并为一个版本还原点 |

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

```nginx
server {
    listen 80;
    server_name teamdoc.intranet;
    client_max_body_size 2g;        # ① 默认 1m,不改则大文件上传直接 413
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

Caddy 下同理:WebSocket 会自动升级,但要注意 `request_body { max_size }` 与超时。

---

## 2. 日常运行

### 2.1 Windows(内网服务器常见)

用 [NSSM](https://nssm.cc/) 注册为服务,开机自启、崩溃自动重启:

```bat
nssm install TeamDoc "C:\path\to\lite\server\.venv\Scripts\python.exe" "main.py"
nssm set TeamDoc AppDirectory "C:\path\to\lite\server"
nssm set TeamDoc AppEnvironmentExtra PORT=8000 TEAMDOC_DATA_DIR=D:\teamdoc-data
nssm set TeamDoc AppStdout "D:\teamdoc-data\logs\out.log"
nssm set TeamDoc AppStderr "D:\teamdoc-data\logs\err.log"
nssm set TeamDoc AppRotateFiles 1
nssm set TeamDoc AppRotateBytes 10485760
nssm start TeamDoc
```

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
Environment=PORT=8000
Environment=TEAMDOC_DATA_DIR=/var/lib/teamdoc
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
systemctl daemon-reload && systemctl enable --now teamdoc
```

### 2.3 健康检查

```bash
curl -s http://127.0.0.1:8000/api/auth/status
# {"bootstrapped":true,"dbReady":true}
```

`dbReady:false` 表示数据库打不开(权限/磁盘满/文件损坏)。

---

## 3. 备份与恢复

### 3.1 备份

管理后台「存储」卡片有**下载备份**按钮(`GET /api/admin/backup`,仅管理员),导出一个 zip:

```
teamdoc.db    数据库一致性快照
files/        云空间全部物理文件
RESTORE.txt   恢复步骤
```

**为什么必须用它而不是直接复制 data 目录**:数据库以 WAL 模式运行,未 checkpoint 的写入还在 `teamdoc.db-wal` 里,单独复制 `.db` 会拿到**旧快照**(实测中复制出的库一张表都没有)。备份端点内部用 `VACUUM INTO` 生成完整快照,且**不需要停机**。

> 自动备份:内网机器上用计划任务定时 `curl` 这个端点(需带管理员会话)或直接 `VACUUM INTO` + 打包 `files/`。备份文件建议放到另一台机器——同一块盘上的备份在磁盘故障时一起没了。

### 3.2 恢复

1. **停服**(运行中覆盖库文件会写坏数据)
2. 原数据目录改名保留:`mv data data.old`
3. 新建数据目录,解包使结构为 `data/teamdoc.db` + `data/files/...`
4. 启动服务,迁移会自动把结构补齐到当前版本
5. 确认无误后再删 `data.old`

### 3.3 磁盘与清理

管理后台「存储」区能看到:磁盘总量/已用/剩余、各项目占用、回收站占用、**无主文件**(磁盘上有、数据库里没有引用)。

无主文件来自上传中断或历史遗留,占着空间但界面上永远看不见。点「清理」即可回收。

**磁盘占用怎么降**:
- 清空回收站(回收站里的文件仍占物理磁盘,只有彻底删除才释放)
- 历史版本:文档版本按年龄分层保留(稳态约 124 条/文档),频繁编辑且粘过大表格的文档版本占用可观,可在管理后台看到总量
- 清理无主文件

---

## 4. 日常维护要点

| 事项 | 做法 |
|---|---|
| 用户忘记密码 | 管理后台 → 用户行「重置密码」 |
| 用户丢了两步验证设备 | 管理后台 → 用户行「重置两步验证」(**没有这条出路账号会永久锁死**) |
| 用户离职 | 管理后台 → 用户行「禁用」(立即失去访问,数据保留) |
| 脚本/自动化访问 | 个人设置 → 访问令牌(PAT,`tdp_` 前缀)。只读令牌无法执行写操作 |
| 放开某个项目给全团队看 | 项目设置 → 可见性 → 公开(本项目所有登录用户可只读浏览,可在「发现」里看到) |
| 只共享一个文件 | 云空间 → 文件行「公开」图标(任何登录用户可下载该文件,不必公开整个项目) |
| 忘记管理员密码且无人可重置 | 见 §5 |

### 关于公开的边界

- 公开 = **本实例所有登录用户可以只读浏览**,不产生成员关系;关闭后立即失效。
- **个人空间永不可公开**,服务端硬拒。个人空间是私有草稿区,设计上不承担共享职能。
- 公开项目的成员列表对所有登录用户可见(含邮箱)。这与同事目录的可见面一致,30 人内网可接受;若要求更严,需要改成员列表的可见性(见 HANDOFF §7)。
- 没有"匿名链接"(发给没有账号的人)。内网外部访问不到本服务,该功能收益低;要分享给同事请用项目/文件公开。

---

## 5. 故障处理

| 现象 | 排查 |
|---|---|
| 大文件上传 413 | 反代 `client_max_body_size` 太小(§1.5 ①) |
| 大文件下载中途断/进度卡住 | 反代 `proxy_read_timeout` 太小(§1.5 ②) |
| 协同编辑不生效(看不到他人光标) | 反代 WebSocket 未升级(§1.5 ③);或误开了多 worker |
| 上传报"服务器存储空间不足" | 磁盘余量低于 `STORAGE_RESERVE_MB`;去管理后台清理或扩容 |
| 界面里文件总数远小于磁盘占用 | 回收站里还有内容,或无主文件待清理(§3.3) |
| 页面改版后仍是旧样式 | 静态资源已设 `no-cache`,浏览器会携 ETag 重验证;若仍异常,确认反代没有额外加长缓存 |
| 管理员账号被锁(忘记密码 + 无人能重置) | 用另一个管理员账号重置;若**所有**管理员都被锁,只能手改数据库 —— 停服后改 `users.password_hash`,值由 `auth.hash_password("新密码")` 生成 |
| 服务起不来 | 看日志(§2.1);`/api/auth/status` 的 `dbReady` 判断是不是数据库问题 |

### 手工重置密码(最后手段)

```bash
cd lite/server
.venv/Scripts/python.exe -c "
import models, auth
from models import SessionLocal, User
db = SessionLocal()
u = db.query(User).filter_by(email='admin@teamdoc.local').first()
u.password_hash = auth.hash_password('新密码至少8位')
u.totp_secret = None
u.totp_enabled = False
db.commit()
print('已重置', u.email)
"
```

停服执行,改完再启动。

---

## 6. 升级

1. **先备份**(§3.1)
2. 拉取新代码,`uv sync` 更新依赖
3. 重启服务(迁移自动执行)
4. 跑一遍 `lite/tests/verify_page_assets.py` 确认前端资源完整(它会校验零外链,内网部署前后都该跑)

```bash
TD_BASE=http://127.0.0.1:8000 python lite/tests/verify_page_assets.py
```

5. 浏览器强制刷新一次(静态资源已设 no-cache,通常不需要)

### 升级前的兼容性说明

- **数据库结构变更全部走迁移**(`migrations.py`),向前兼容:旧库启动时自动补列。
- **不支持回退到旧版本代码**:新版本可能已写入旧代码不认识的字段。回退需同时恢复对应时点的备份。
- API 契约变更见 `HANDOFF.md` §5。若有外部脚本调用 API,升级前先看该节。

---

## 7. 规模与性能

当前设计面向 **30 人小团队**:

- 云空间列表已分页(100/页 + 加载更多),服务端排序;文件夹上限 2000/目录。
- 搜索与回收站仍是单次 500 条截断(超出时搜索会少结果,回收站会少列)。
- `referenced`(被引用徽标)每次列目录会扫描该项目全部文档正文——文档量上千后应改为引用索引表。
- 上传走 raw body 流式写盘,大文件不双写;单 worker 下并发上传会排队。
- 文档版本按年龄分层保留(稳态约 124 条/文档),不需要手工清理。

超过这个规模(比如用户上百、单目录上万文件、文档上万篇)需要先做的改动见 `HANDOFF.md` §7。
