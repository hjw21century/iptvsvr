# 📡 IPTV-Hub —— 自建直播源采集与分发服务

对标并重写自 [zilong7728/Collect-IPTV](https://github.com/zilong7728/Collect-IPTV)（GitHub Actions + 静态页面方案）。
本项目是一个**可自托管的常驻服务**：采集公开源 → **真正播一遍**做深度探测 → 跨轮次稳定性评分 →
输出可按条件动态订阅的播放列表，自带 Web 控制台与 JSON API。

纯 Python 3 标准库实现，**零第三方依赖**，Python ≥ 3.7 直接跑。

---

## 一、对参照项目的分析

参照项目的流程是：GitHub Actions 每 4 小时跑一次 `iptv.py` → 抓 14 个公开清单 → `aiohttp` 并发
请求每条链接 → HTTP 200 即判定可用 → 按频道名归类（央视/卫视/省份/主题）→ 同频道保留延迟最低的
一条 → 写出 `best_sorted.m3u` / `.m3u8` → 另一个 workflow 把页面发布到 GitHub Pages，
页面再回头解析 README 里的 raw 链接来渲染表格。

思路清晰，工程量集中在中文频道名的归类规则上（繁简映射、省市地名、智能分类关键字），这部分做得相当细，
本项目直接继承了这套思路。但从"做成一个服务"的角度看，它有几处结构性短板：

| # | 问题 | 后果 |
|---|------|------|
| 1 | **可用性判据太弱**：`session.get(url)` 拿到 `status == 200` 就算通过，一个字节码流都没读 | 返回 200 的错误页、空的 m3u8、拉不到分片的源，全部混进播放列表 |
| 2 | **没有画质/码率信息**，只比较请求耗时 | "延迟最低"可能是一条 240p 或卡顿的源；master playlist 里的 RESOLUTION/BANDWIDTH 被浪费 |
| 3 | **没有跨轮次历史**，每次从零判断 | 无法区分"长期稳定"和"这次碰巧通了"；也无法对连续失败的源做冷却，每轮都白测一遍 |
| 4 | **先测后去重**：`for file_url in file_urls` 串行处理每个源，去重只在单个源内部做 | 同一条 URL 被 N 个上游收录就测 N 次；14 个源串行，整轮耗时被拉长 |
| 5 | **每个频道只留一条 URL** | 主源一挂，该频道就没了，没有备播 |
| 6 | **TXT 解析丢分组**：只认 `名称,URL`，忽略 `分类名,#genre#` 与 `url1#url2` 多源写法 | 上游已有的分组信息被丢弃，多源行只取到一条 |
| 7 | **产物是静态文件**，前端还要去解析 README 里的 raw 链接 | 无法按分组/画质/网络类型订阅；GitHub raw 在国内访问不稳定；页面依赖 CDN 与第三方统计 |
| 8 | **源清单写死在脚本里**，含已失效域名与 `rtp://` 组播源 | 改配置得改代码；组播源在公网完全不可用（实测该清单中 2 个源 100% 是 `rtp://`） |

## 二、我们的做法

| 维度 | Collect-IPTV | IPTV-Hub（本项目） |
|------|--------------|--------------------|
| 可用性判定 | HTTP 200 | 解析 manifest → 选 variant → **下载真实分片测速**；TS 流校验 `0x47` 同步字节 |
| 质量指标 | 无 | 分辨率（manifest 没有就**从码流里解 H.264 SPS**）、声明带宽、**实测吞吐 kbps**、首包延迟、分片数、是否加密 |
| 历史 | 无 | SQLite 记录每轮结果，偏差修正 EWMA 可用率 + 连续失败计数 + 失效冷却 |
| 排序 | 延迟最低 | 稳定性 45% + 吞吐 25% + 画质 20% + 延迟 10%，另有 HTTPS/IPv4/多源收录加成 |
| 每频道 | 1 条 | 主源 + 最多 N 条备用，**强制跨主机分散** |
| 去重 | 测完再去重 | 采集阶段按 URL 合并（本次实测 4.4k 条目 → 3.5k 唯一 URL，省掉约 25% 探测量） |
| 分组 | 央视/卫视/省份/主题 | 同上 + 港澳台 + 海外，规则全部外置到 `config/groups.json` |
| 源清单 | 硬编码在脚本 | `config/sources.json`，带 enabled/weight/note |
| 产物 | 2 个静态文件 | M3U / M3U8 / TXT / JSON + **带参数的动态订阅**（分组、关键词、画质、IPv4/6、评分、是否含备用源）|
| 部署 | GitHub Actions + Pages | systemd 常驻服务（内置定时更新）+ nginx；也保留 Actions/静态导出方案 |
| 容错 | 无 | 主机级熔断（整台机器连不上时快速失败）、非 ASCII URL 自动编码、失效冷却 |
| 依赖 | aiohttp | 无（标准库线程池 + urllib + sqlite3） |
| 测试 | 无 | `scripts/selftest.py`，24 个离线用例 |

## 二之二、本机实测（2026-09-20，一台海外 4 核小主机）

15 个上游 → 解析出 4.4k 条目 → 按 URL 合并为 **3501 条唯一链接** → 深度探测：

| 指标 | 结果 |
|------|------|
| 一轮耗时 | **9 分 52 秒**（未加主机熔断前是 16 分 29 秒） |
| 判定可用 | 1187 / 3501（33.9%） |
| 输出频道 | 829 个，其中 159 个带备用源 |
| **分辨率识别率** | **641 / 829 = 77%**（仅靠 manifest 的 RESOLUTION 字段只能拿到 7%） |
| 连续 3 轮全通的源 | 1129 条 |
| 失败原因 top5 | `host_unreachable` 529（熔断快速失败）、`http_403` 475、`timeout` 395、`unreachable` 217、`http_404` 194 |

顺带暴露了两个只有真跑才会发现的问题，均已修复：上游约 1/6 的链接路径含中文，
`http.client` 直接抛 `UnicodeEncodeError` 被误判为不可用；某台挂掉的服务器上挂着 68 条链接，
没有熔断时光等它就多花 5 分钟。

---

## 三、目录结构

```
iptv/
├── iptvhub/              服务本体
│   ├── config.py         配置加载（config/*.json）
│   ├── util.py           繁简/全角归一化、自然排序
│   ├── netclient.py      HTTP 客户端、按主机限流、IP 版本识别
│   ├── parser.py         M3U / TXT 解析（含 #genre#、多源行、噪声过滤）
│   ├── classify.py       频道身份归一 + 分组归类
│   ├── probe.py          ★ 流媒体深度探测（HLS / TS / FLV）
│   ├── videoinfo.py      ★ 从 MPEG-TS/fMP4 码流解析 H.264 SPS，拿真实分辨率
│   ├── store.py          SQLite：候选源、探测历史、运行记录
│   ├── rank.py           评分与选优（含备用源跨主机分散）
│   ├── export.py         M3U / M3U8 / TXT / JSON 导出与过滤
│   ├── pipeline.py       采集 → 探测 → 评分 → 导出 全流程
│   ├── server.py         HTTP 服务：网页 + API + 动态播放列表 + 定时更新
│   ├── admin.py          管理后台 API（令牌鉴权、源/参数读写、实时探测）
│   ├── proxy.py          ★ 网页播放中转：manifest 改写 + HMAC 签名 + 防开放代理
│   ├── feedback.py       观众反馈：校验、限流、IP 加盐哈希
│   ├── notices.py        站内公告按日期区间自动生效
│   ├── analytics.py      访客与流量统计（按天聚合，不存请求流水）
│   ├── auth.py           账号 / 密码哈希 / 会话 / 订阅密钥
│   ├── runtime.py        进程内运行状态：更新进度 + 日志环形缓冲
│   └── cli.py            命令行入口
├── config/
│   ├── config.json       运行参数（并发、超时、权重、服务端口…）
│   ├── notices.json      节日公告 / 欢迎界面（按日期自动上下线）
│   ├── sources.json      上游源清单
│   └── groups.json       分组规则（省市地名、主题关键字、噪声词、别名）
├── web/                  前台 index.html + 后台 admin.html + 登录页 login.html
├── data/                 运行产物：iptv.db + playlist.* + channels.json
├── deploy/               systemd unit / timer、nginx 站点、install.sh 一键部署
├── scripts/selftest.py   离线自测
└── deploy/github-actions/ 可选：Actions 更新 + Pages 发布（启用时复制到 .github/workflows/）
```

---

## 四、快速开始

```bash
cd /data/code/iptv

# 1) 看看上游源当前是否可用
python3 -m iptvhub sources

# 2) 跑一轮完整更新（首轮约 5~10 分钟，取决于网络）
python3 -m iptvhub -v run
#    调试时可以只测前 N 条： python3 -m iptvhub -v run --limit 300

# 3) 启动服务（网页 + API + 播放列表 + 内置定时更新）
python3 -m iptvhub serve            # 默认 0.0.0.0:8088

# 其它
python3 -m iptvhub stats            # 查看库内统计与分组分布
python3 -m iptvhub token            # 查看管理后台地址与令牌
python3 -m iptvhub probe <url>      # 调试单条链接，输出探测细节
python3 -m iptvhub export           # 不重测，仅用库中数据重新评分并导出
python3 -m iptvhub static --out public   # 导出纯静态站点
python3 scripts/selftest.py         # 离线自测（装了 esprima 时会额外做 JS 语法检查）
```

> 无自有服务器时也可以用 GitHub Actions 跑：把 `deploy/github-actions/update.yml`
> 复制到 `.github/workflows/`（推送该路径需要 PAT 带 `workflow` 权限），
> 它会每 4 小时更新一次并发布到 Pages。

### 绑定域名 iptv.tomeleaf.com

后端只监听 `127.0.0.1:8088`（`config/config.json` 的 `server.host`），公网入口交给 nginx。
一键安装（幂等，可重复执行）：

```bash
sudo bash deploy/install.sh
```

脚本依次做五件事：装并启动 `iptv-hub.service` → 装 HTTP 站点 `deploy/nginx/iptv.http.conf`
→ `certbot certonly --nginx -d iptv.tomeleaf.com` 签发证书 → 换成 `deploy/nginx/iptv.tls.conf`
（HTTP 跳 HTTPS、HSTS、noindex、gzip、限流，`/api/update` 只放行本机）→ 自检。

手动等价操作：

```bash
sudo install -m 644 deploy/iptv-hub.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now iptv-hub
sudo install -m 644 deploy/nginx/iptv.http.conf /etc/nginx/conf.d/iptv.conf
sudo nginx -t && sudo systemctl reload nginx
sudo certbot certonly --nginx -d iptv.tomeleaf.com -n --agree-tos --keep-until-expiring
sudo install -m 644 deploy/nginx/iptv.tls.conf /etc/nginx/conf.d/iptv.conf
sudo nginx -t && sudo systemctl reload nginx
```

更新方式二选一：`config.json` 的 `server.auto_update`（默认开，服务内每 4 小时一次），
或关掉它改用 systemd timer：

```bash
sudo cp deploy/iptv-hub-update.{service,timer} /etc/systemd/system/
sudo systemctl enable --now iptv-hub-update.timer
```

---

## 五、订阅地址与 API

**播放列表**（都支持下面的查询参数）：

```
https://iptv.tomeleaf.com/playlist.m3u      https://iptv.tomeleaf.com/playlist.m3u8
https://iptv.tomeleaf.com/playlist.txt      https://iptv.tomeleaf.com/playlist.json
```

| 参数 | 说明 | 示例 |
|------|------|------|
| `group` | 只要某个分组 | `?group=央视频道` |
| `q` | 频道名关键词 | `?q=CCTV` |
| `ipv` | 只要 IPv4 或 IPv6 的源 | `?ipv=4` |
| `min_height` | 最低分辨率高度 | `?min_height=720` |
| `min_score` | 最低综合评分 | `?min_score=0.6` |
| `limit` | 最多返回多少个频道 | `?limit=200` |
| `backups` | 是否附带备用源 | `?backups=1` |

例：只要 1080p 以上、IPv4、带备用源的央视频道 —
`https://iptv.tomeleaf.com/playlist.m3u?group=央视频道&min_height=1080&ipv=4&backups=1`

播放列表响应带 `Cache-Control: public, max-age=120`，订阅器频繁拉取时不会每次都回源重算。

### 网页播放：能直连就直连，剩下的才中转

浏览器放不出来、VLC 却正常，是浏览器独有的两条限制：页面是 `https://` 而很多
直播源是 `http://`（**混合内容**被拦），且不少上游不返回 `Access-Control-Allow-Origin`
（**跨域**被拦）。表现就是 hls.js 报 `manifestLoadError`。VLC 两条都不受限。

所以播放分两条路，探测时就把每条源归好类：

**① 直连（不花本站一分流量）** —— 探测时带上 `Origin` 走一遍完整链路
（manifest → variant → 分片），**每一跳都是 https 且都返回可用的 CORS 头**，
才标记为 `direct`。这类源浏览器自己去拉，本站只提供一个地址。
实测库里 **46% 的可用源是 https**，其中大部分带 `Access-Control-Allow-Origin: *`。
评分里有 `direct_bonus`，同等条件下优先把这类源选为主源——对用户一样好，对服务器省钱。

**② 中转 `/proxy?u=<地址>`** —— 其余的（http 源、不给 CORS 的源）才经本站 HTTPS 转一道：

* 拉到 manifest 后**改写其中的分片/子清单/密钥地址**，让整条链路都经本站；
* 改写出来的地址带 HMAC 签名，首个 manifest 则必须是**库中已收录**的地址——
  两道一起挡住"被人当开放代理用"；
* 响应补上 CORS 头，分片按上游的 `Content-Length` 透传，没有长度就用 chunked。

前端先按 `direct` 标记直连，**失败自动退回中转**再试一次，用户无感。
源列表上每条都标了「直连 / 中转」。

**③ 完全不经浏览器（推荐）** —— 首页顶部常驻一条引导：复制订阅地址 / 下载 m3u /
「怎么用？」弹出分平台使用指南（Windows·macOS 的 VLC 打开网络串流、iOS 的 VLC for Mobile、
Android 与电视盒子的 VLC/Kodi/TiviMate，含订阅地址一键复制与筛选参数示例）。
播放面板里另有 `下载本频道 m3u`（`/channel.m3u?key=…`，约 1 KB）；复制按钮给的一直是
**原始地址**。第一次点播放会提示一次（之后不再打扰），试看到点暂停时也会再提醒一句。
这条路零本站流量，画质与稳定性也更好。

#### 流量闸门

中转是流量放大器：一个人在网页看 1 小时 3.5 Mbps 的流，出站就是 **1.5 GB 左右**
（入站同样一份）。作为参照，本机 9 周累计出站才 11.8 GB——也就是说
**一个人看一小时，抵得上这台机器过去 9 天的出站量**。所以对"中转"这条路设三层闸门：

| 配置项 | 默认 | 作用 |
|--------|------|------|
| `proxy_daily_gb` | 10 | 全站每日中转上限，超了所有人都改走 VLC |
| `proxy_per_ip_daily_mb` | 1200 | 单访客每日上限，约 1 小时高清 |
| `proxy_per_ip_concurrent` | 2 | 单访客同时预览路数 |
| `proxy_max_concurrent` | 6 | 全站同时中转的请求数 |
| `proxy_max_request_mb` / `_seconds` | 200 / 300 | 单次请求上限，挡住无限长的裸 TS 流 |

（任意一项填 0 表示不限；`proxy_enabled: false` 可整体关掉中转，页面只保留目录、
复制地址与 m3u 下载。）触发上限时返回 429，页面提示"请复制地址用 VLC 播放"。
网页端另有 **10 分钟预览上限**，到点自动暂停，避免后台标签页整天挂着耗流量。

用量按天累计并持久化（进程收到 SIGTERM 会先落盘再退出，`systemctl restart` 不丢账），
后台概览有「今日中转流量」卡片，配额也可以在后台「参数」页直接改。

### 观众反馈

机器只能测出"连得上、有码流"，画面卡不卡、有没有声音、是不是放广告，只有真人看得出来。
所以每条播放源下面都有留言反馈，**点一下选项就直接提交**，不用填任何身份信息，
想多说两句再写「补充说明」。

* `GET /api/feedback?url=…` 或 `?channel=<频道键>` 读取，`POST /api/feedback` 提交；
* **身份由服务端自动从来源 IP 提取，提交者改不了**——请求体里传 `nickname` 一律忽略；
  公开列表只显示打码 IP（`1.2.3.*`），完整 IP 与设备（如 `iPhone · Safari`）只在后台可见；
* 只接受**库中存在**的播放源地址，类型白名单校验，留言 300 字截断，控制字符过滤；
* 限流：同一访客 10 分钟最多 10 条，相同内容不重复入库（限流键是 IP+UA 的加盐哈希）；
* 后台「观众反馈」页可筛选、隐藏、删除，并给出把反馈折算成排序系数的参考值
  （当前只展示，不自动改排序，避免被刷）。

### 节日公告 / 欢迎界面

`config/notices.json` 里按日期区间配置，**到期自动上线、过期自动消失**，
不需要谁记得回来撤横幅：

```json
{"id": "guoqing-2026", "enabled": true,
 "start": "2026-09-29", "end": "2026-10-08", "priority": 10,
 "style": "festive", "emoji": "🇨🇳",
 "title": "普天同庆，祝伟大祖国生日快乐",
 "subtitle": "国庆快乐 · 阖家团圆",
 "lines": ["假期期间直播源照常每 4 小时自动检测更新", "…"],
 "button": "进入观看"}
```

* 页面顶部一条横幅（`festive` 为红金渐变，另有 `info` / `warn`），
  首次访问再弹一次欢迎卡片，**同一条公告每人只弹一次**（记在 localStorage），
  之后可以点横幅上的「查看详情」再看；
* 同时命中多条时按 `priority` 取最大的；`enabled: false` 可临时停用；
* 接口 `GET /api/notice`，后台概览也会显示当前生效的公告。

### 访客与流量统计

`analytics.py` 按天聚合，**不保留原始请求流水**——一台小机器没必要为了看趋势扛一张
越滚越大的日志表。三张表：每日指标、每日访客（去重后每人一行）、每日频道播放数。

* 记什么：页面打开、订阅/单频道 m3u 下载、公开接口调用、中转请求与字节数、
  点击播放（前端 `sendBeacon` 上报频道）、反馈提交；
* **后台自身的页面与轮询接口不计入访客**，否则每 1.5 秒一次的进度轮询会淹没统计；
* 访客按 IP+UA 的加盐哈希去重，落库保存完整 IP、设备、请求数、流量、播放数与活跃时间；
* 写入是攒批的（25 条或 30 秒落一次盘，SIGTERM 时强制落盘），读取接口
  `GET /api/admin/analytics?days=14`；`prune(keep_days)` 可清理历史。

图表遵循项目的可视化规范：不同量纲**不共轴**，四个指标各一张单序列柱图
（单序列不需要图例，标题即名称）；柱宽封顶 24px、顶部 4px 圆角、底边贴基线、
柱间 2px 空隙；只对峰值做直接标注；按容器**实测像素宽度**绘制（viewBox 拉伸会把圆角压成椭圆）；
悬停有独立浮层；并提供「查看数据表」作为等价的表格视图。配额仪表用固定的状态色，
且始终带图标与文字（充足/接近上限/即将用尽），不靠颜色单独表意。

### 登录认证与账号

两种角色：**admin** 可进 `/admin`（管理源、参数、反馈、统计、账号），**user** 只能用前台。

```bash
python3 -m iptvhub user add <用户名> --role admin   # 不加 --password 则交互式输入
python3 -m iptvhub user list                        # 看角色、订阅密钥、最后登录
python3 -m iptvhub user passwd <用户名>             # 改密码（该账号所有登录立即失效）
python3 -m iptvhub user role|disable|enable|key|delete <用户名>
```

* 密码用 **PBKDF2-HMAC-SHA256 加盐** 存储（20 万次迭代），任何地方都不保存明文；
* 会话是服务端签发的随机令牌，**存库可吊销**，Cookie 带 `HttpOnly` + `SameSite=Lax`
  （经 HTTPS 访问时自动加 `Secure`），默认 14 天，`session_days` 可调；
* 登录失败按 IP 限流：10 分钟内 8 次后暂时拒绝；
* 访问分三层（见下表），由 `require_login`（默认 `false`）与
  `proxy_require_login`（默认 `true`）控制；
* **播放器没法登录**，所以每个账号有一个**订阅密钥**，订阅地址形如
  `/playlist.m3u?key=<密钥>`——登录后首页的「复制订阅地址 / 下载 m3u」会自动带上。
  密钥可在后台一键换发，旧地址立即失效；
* 后台的 `X-Admin-Token` 保留给脚本与自动化，网页端直接用登录态即可。

后台「账号」页签可以新增账号、改密码、切换角色、换发订阅密钥、停用/启用、删除，
并列出当前所有活动会话（账号、登录时间、到期、IP、设备）。最后一个可用管理员不允许删除。

#### 三层访问权限

| | 游客（免登录） | 普通用户 | 管理员 |
|---|---|---|---|
| 浏览频道、搜索、筛选 | ✅ | ✅ | ✅ |
| 复制地址 / 下载 m3u / 订阅 | ✅ | ✅ | ✅ |
| 网页播放**直连**频道 | ✅ | ✅ | ✅ |
| 网页播放**中转**频道 | ❌ 提示登录 | ✅ | ✅ |
| 提交反馈 | ✅ | ✅ | ✅ |
| 后台 `/admin` | ❌ | ❌ | ✅ |

这条分界线跟成本对齐：**直连源由浏览器直接从源站拉流，不花本站一分带宽**，所以对所有人开放；
**中转要消耗本站流量**（一个人看一小时约 1.5 GB 出站），所以留给登录用户，并继续受
每日配额与 10 分钟试看上限约束。当前库里 **309/799（39%）的频道有直连源**，游客可直接观看；
前台有「只看直连（免登录可播）」筛选。游客点中转频道时会自动改选该频道的直连备用源，
确实没有才弹窗引导登录。

### 管理后台 `/admin`

浏览器打开 `https://iptv.tomeleaf.com/admin`，首次进入需要输入管理令牌：

```bash
python3 -m iptvhub token           # 查看令牌与后台地址
python3 -m iptvhub token --reset   # 换一个（重启服务生效）
```

令牌优先取 `config.json` 的 `server.admin_token`；没设置时自动生成并保存在
`data/admin_token`（0600）。令牌校验用 `hmac.compare_digest`，不会写进日志。

四个页签：

| 页签 | 能做什么 |
|------|----------|
| **概览与运行** | 统计卡片；一键触发「开始更新 / 全量重测 / 仅采集 / 抽样 200 条」；**实时进度条 + 滚动日志**（增量拉取）；「重新评分并导出」「清理失效源」；最近运行记录与失败原因分布 |
| **上游源** | 表格式增删改：启用开关、名称、地址、类型、权重；显示每个源贡献了多少候选/多少可用；**单源测试**（抓取 + 解析 + 归类预览 + 有多少是新地址），保存即写回 `config/sources.json` |
| **源明细排查** | 按关键词/分组/状态/错误类型/排序检索到单条 URL；查看某条源的**历次探测记录**；**实时重测**单条链接（返回类型、分辨率、吞吐、错误）；删除脏数据 |
| **流量与访客** | KPI 行（今日访客/页面打开/播放/中转流量/订阅下载/反馈）、中转配额仪表（状态色 + 文字标签）、最近 7/14/30 天的访客数·播放次数·中转流量·订阅下载四张柱图（可切数据表）、今日访客榜（IP·设备·请求·播放·流量）与热门频道榜 |
| **账号** | 新增/改密/切角色/换订阅密钥/停用/删除，查看所有活动会话 |
| **观众反馈** | 查看全部留言（可按类型筛选、含已隐藏），隐藏或删除违规内容 |
| **参数** | 表单调整探测/评分/清理/选优参数与四项权重，带取值范围校验；越界或不在白名单的键会被拒绝并回显原因。数值项下一轮更新即生效（每轮重新读盘），`server.*` 需重启 |

前端资源由 `scripts/selftest.py` 做静态把关：JS 语法（需 `pip install esprima`，未安装则跳过）、
JS 引用的元素 id 必须在 HTML 里存在、页面不得写死绝对 `/static/` 路径。

后台接口都在 `/api/admin/*`，需要 `X-Admin-Token` 头（或 `?token=`）。
写操作做了白名单与范围校验，配置/源文件都是**先写临时文件再原子替换**。

**API**：

| 路径 | 说明 |
|------|------|
| `GET /api/stats` | 总体统计、分组分布、上次运行信息 |
| `GET /api/groups` | 分组及频道数 |
| `GET /api/channels` | 频道列表（支持上面全部过滤参数 + `offset`） |
| `GET /api/history?url=` | 某条源的历次探测记录 |
| `GET /api/runs` | 最近若干次运行记录 |
| `GET /api/sources` | 当前启用的上游源 |
| `POST /api/update` | 手动触发一次更新（nginx 只放行本机；另可在 `config.json` 设 `admin_token`） |
| `GET /healthz` | 健康检查 |

---

## 六、探测与评分

**探测（`probe.py`）** 对每条链接走一遍真实播放路径：

1. 拉首包 8KB，嗅探类型：`#EXTM3U` → HLS；首字节 `0x47` 且第 188 字节也是 `0x47` → MPEG-TS；`FLV` → FLV；HTML/JSON → 判定 `not_a_stream`。
2. HLS 若是 master playlist，取带宽最高的 variant（记录 `RESOLUTION` / `BANDWIDTH`），递归一层。
3. media playlist 取**最新分片**（失败回退到第一个分片），真实下载最多 256KB / 4 秒，算出实测吞吐。
   manifest 里没有 `RESOLUTION` 时（国内源绝大多数如此），直接解复用 MPEG-TS、
   找到 H.264 SPS 并解析出真实分辨率（`videoinfo.py`，纯 Python，无需 ffprobe）。
4. 字节数不足且未读完 → `insufficient_data`；空列表 → `empty_playlist`；其余异常归类为
   `timeout` / `dns_error` / `conn_refused` / `tls_error` / `http_4xx` 等，写进库便于排查。

单条链接受 `probe_timeout` 硬约束，并按主机做并发限流（默认每主机 4）。
此外有**主机级熔断**：某主机连续 8 次连接级失败（超时/拒绝/DNS）后，该主机剩余链接直接快速失败——
实测一台挂掉的服务器上挂着 68 条链接时，没有熔断会让整轮多花 5 分钟空等（HTTP 404/500 不计入熔断，因为那说明主机是活的）。

**评分（`rank.py`）**：

```
score = 0.45·稳定性 + 0.25·吞吐 + 0.20·画质 + 0.10·延迟
        (+ HTTPS 0.03, IPv4 0.02) × 多源收录系数 × 上游权重 × 连续失败衰减
稳定性 = 0.6 · 偏差修正EWMA + 0.4 · 历史成功率
```

偏差修正 EWMA 解决冷启动问题：第一次就成功的新源不会因为 `ewma = alpha` 而被低估。
选优时每频道取评分最高的源作主源，再按"同一主机最多 1 条"的规则补足备用源，避免主备同时失效。

所有权重都在 `config/config.json` 里，改完执行 `python3 -m iptvhub export` 即可重算，无需重测。

---

## 七、配置要点

`config/config.json` 常调的几项：

| 键 | 默认 | 说明 |
|----|------|------|
| `concurrency` | 48 | 全局并发探测数；带宽富裕可以调到 96~128 |
| `per_host_concurrency` | 4 | 单主机并发上限，别把上游压挂 |
| `probe_timeout` | 14 | 单条链接总耗时上限（秒） |
| `probe_bytes` / `probe_seconds` | 256KB / 4s | 测速读取量，调小可加快整轮速度 |
| `host_failure_limit` | 8 | 单主机连续连接失败多少次后熔断（0 = 关闭） |
| `recheck_dead_after_hours` | 12 | 连续失败的源多久后才再测一次 |
| `prune_after_days` / `prune_fail_streak` | 7 / 8 | 清理长期失效源的阈值 |
| `max_backups_per_channel` | 3 | 每频道备用源数量 |
| `min_score` | 0.15 | 低于此分不进播放列表 |
| `server.host` / `server.port` | 127.0.0.1 / 8088 | 监听地址；公网由 nginx 反代 |
| `proxy_enabled` | true | 网页播放中转总开关（关掉后只剩直连源与 m3u 下载） |
| `server.update_interval_hours` | 4 | 内置定时更新间隔 |
| `site_url` | `https://iptv.tomeleaf.com` | 写进 M3U 头部的 `# Source:`，标明播放列表来源 |

`config/sources.json` 增删上游源；`config/groups.json` 调整分组规则，**改规则不用改代码**：
省市地名（含拼音别名，能认出 `Anhui TV` 这类英文命名）、主题关键字、港澳台/海外关键词、
直播平台前缀（`「B站」`/`「斗鱼」` 开头的游戏间整段归入"游戏电竞"）、噪声词、频道别名。

---

## 八、注意事项

* 本服务从**部署它的机器**发起探测，"可用"是相对该机器的网络位置而言的。
  放在海外 VPS 上测国内组播回源，和放在家宽上测，结果会明显不同——这一点参照项目（跑在 GitHub 服务器上）同样存在。
* 上游清单里的 `rtp://` 组播源只在对应运营商内网可用，本项目会直接跳过。
* 数据库 `data/iptv.db` 保存了探测历史，**不要随意删除**，否则稳定性评分要重新积累。

详见 [DISCLAIMER.md](./DISCLAIMER.md)：本项目不生产、不存储、不篡改任何媒体内容，仅整理公开链接，
请仅用于个人学习测试，并遵守当地法律法规。
