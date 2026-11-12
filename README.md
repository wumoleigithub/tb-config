# tv-config

TVBox 订阅地址（推荐使用 jsDelivr，国内访问更稳定）：

```
https://cdn.jsdelivr.net/gh/wumoleigithub/tb-config@main/config.json
```

直播源单独订阅：

```
https://cdn.jsdelivr.net/gh/wumoleigithub/tb-config@main/active_lives.m3u
```

> 源由 GitHub Actions 每天 UTC 02:00 自动发现、检测并更新，也可手动触发。

## 直播地址验证

检测脚本需要 Python 3 和 FFmpeg（macOS：`brew install ffmpeg`；Ubuntu：
`sudo apt-get install ffmpeg`）。每个 HTTP/HTTPS 播放地址必须在 30 秒内成功解码
至少约 5 秒的视频才会保留；不支持的协议、访问失败、解码错误和超时均会剔除。
HTTP 200 或能下载 M3U8 列表不算通过。频道分组、台标和频道级指令会保留，
检测时支持 `#EXTVLCOPT:http-user-agent` 和 `#EXTVLCOPT:http-referrer` 请求头。
相同地址和请求头在一次运行中只检测一次，默认并发检测 8 个地址。

默认完整扫描会按频道补源：先检测现有地址，失效后从 `live_sources.txt`
列出的多个直播列表及 TVBox 配置里的直播列表中寻找同频道替代地址。
不会再按总频道数选择一份列表覆盖全部频道。每个频道保留一个通过验证的地址，
现有可用地址优先；默认最多尝试 8 个不同的替代地址，找到可用的就停止。

`preferred_channels.txt` 保存持续补齐的重点频道，默认包括 CCTV1–17、CCTV5+
和常见卫视。即使频道当前缺失，也会在下次重新寻找；可添加其他希望持续保留的
频道名。现有列表里的其他频道也会尝试修复，但只有目标清单里的名称会在缺失后
持续搜索；自动发现的频道会从在线目录重新获取，不必手工添加。匹配会统一
CCTV 名称和清晰度后缀，并区分 CCTV5/5+、CCTV4/4K
以及 CCTV4 的不同地区版本；不会用其他频道冒充缺失频道。

目标清单也支持 `分组|频道名称`，例如 `台湾中文|台视新闻`、
`免费体育（外语）|beIN SPORTS XTRA`。已加入港澳台、海外中文频道和部分免费
体育、电影频道的目标及候选来源；地区版本和免费/付费版本分别匹配。
国际体育、电影 FAST 频道通常为外语节目，分组会标明。频道的中英文及简繁体
别名在 `live_repair.py` 的 `CHANNEL_NAMES` 中维护。

付费频道需要服务商授权的播放来源；播放检查仅验证技术可用性，不代表订阅授权。
个人订阅地址可能含账号或令牌，不应提交到公开仓库或公开播放列表。

内置候选来自 [iptv-org](https://github.com/iptv-org/iptv)、
[Guovin](https://github.com/Guovin/iptv-api) 和
[fanmingming](https://github.com/fanmingming/live)，只是候选，不保证可播放。
IPv6 地址仍需检测及观看网络都支持 IPv6。

```bash
# 推荐：检测、替换失效地址并补齐缺失频道，不修改 config.json
python3 check_sources.py --repair-live

# 预览补源结果，不写文件
python3 check_sources.py --repair-live --dry-run

# 仅清理现有直播列表，不修改 config.json 或扫描候选源
python3 check_sources.py --live-only

# 预览检测结果，不写文件
python3 check_sources.py --live-only --dry-run

# 完整扫描（每天自动执行）：扩展点播资源池、更新配置，再发现及修复直播
python3 check_sources.py

# 自定义并发、单地址超时和解码时长
python3 check_sources.py --live-only --workers 8 --timeout 45 --sample-seconds 10

# 每个缺失频道最多尝试 12 个替代地址
python3 check_sources.py --repair-live --max-alternatives 12

# 扩大每次自动发现的目标数（默认 40）
python3 check_sources.py --repair-live --discovery-limit 80

# 临时关闭新频道发现，只修复当前列表和重点频道
python3 check_sources.py --repair-live --no-discovery

# 单个候选源调试（从 1 开始，不写文件）
python3 check_sources.py 3
```

补源后仍找不到可用地址的频道不会写入播放列表；`--live-only` 仅过滤，不补源。
全部失败时输出仅含
`#EXTM3U` 的空列表。FFmpeg 未安装或无法启动时会中止，不覆盖列表。
检测结果仅代表执行检测的网络和时间：地区限制或暂时网络故障也会导致剔除，
GitHub Actions 和本地检测结果可能不同。短时解码通过不保证长期稳定或直播实时性。

测试：`python3 -m unittest discover -s tests -v`。

## 自动发现新频道

按个人观看偏好，`channel_policy.py` 屏蔽新唐人（NTD）及指定的法轮功相关品牌。
规则检查频道名称、EPG 标识、来源域名，以及在线目录的别名、运营方等元数据，
同时应用于现有列表清理、补源和自动发现，避免后续更新重新加入。
这属于基于已知标识的过滤，无法识别完全匿名或伪装的来源。

`--repair-live` 和完整扫描默认都会运行 `live_discovery.py`：

1. 在线读取 [Free-TV 免费频道目录](https://github.com/Free-TV/IPTV)。
2. 从 [iptv-org API](https://github.com/iptv-org/api) 获取频道、语言、分类和最新播放地址。
3. 在免费目录收录的频道中，发现非大陆的中文主语言频道，以及中文/英语体育、电影频道；
   跳过已停播、成人、语言不明的频道。中文别名由在线元数据自动获取。
4. 按精确频道 ID 和播出版本查找替代地址，按分类轮流选择，默认每次最多验证
   40 个自动发现目标。数量可通过 `--discovery-limit` 调整。
5. 与已有频道、重点频道合并，实际解码通过后写入 `active_lives.m3u`。

因此，在线目录新增频道或更换地址后，后续运行会自动获取，不需要先修改
`preferred_channels.txt` 或 `live_sources.txt`。后两者保留为重点频道和额外来源的可选配置。
这里的搜索范围是上述在线目录，不是任意网页的全网爬取。
目录不可用时继续检查现有来源；地区限制、网页播放器、DRM 或需登录的地址不会因
被目录收录就自动变成可播放地址。免费属性依据上游目录的收录规则。

GitHub Actions 每天执行完整流程，结果有变化时自动提交播放列表和配置。
本地修改需提交并推送后，新规则才会在 GitHub 定时任务及订阅地址上生效。

## 点播资源池自动扩展与检查

完整扫描现在也会在线搜索 GitHub 的 TVBox 仓库，读取默认分支、嵌套配置文件和
README 中的配置链接；已有资源池每次都会重新检查。默认每次最多检查 40 个
在线发现的候选配置。GitHub 搜索不可用或限流时，继续检查已有来源，原因记录在
`vod_report.json`。定时任务使用仓库自带的 `GH_TOKEN`。

检查分层进行：

- 配置：识别 JSON/JSONC、BOM、注释及尾逗号，拒绝 HTML 错误页面和畸形站点；
  将相对 JAR、站点参数和解析地址转换为绝对地址。
- Java 插件站点：下载但不执行 JAR，检查 ZIP/DEX 内容、配置声明的 MD5，以及
  对应的 `csp_` 类是否存在。站点自己的 `jar` 优先于全局 JAR。
- 标准 type 0/1 采集接口：请求目录接口，检查是否返回影视条目。
- 超时、证书错误、403、429 等记为“未知”，不冒充有效；404/410 连续两次出现才
  将资源池地址标记为 `#dead`，以后仍会复查，恢复后自动启用。手动注释不自动启用。

**这些检查不等于电影播放验证。** Android Java 插件需要在 TVBox 中运行才能确认
搜索、选集、解析和播放是否成功；这里不执行下载的第三方插件，也不保证电影数量、
更新时效、地区可用性或付费内容授权。JS/Python 等未验证接口不加入新生成的配置。
为避免慢源拖住更新，每份配置最多检查 8 个独立 JAR、12 个标准采集接口；
累计检查超过 60 秒后，剩余站点标记为未知（正在进行的请求仍受网络超时约束）。

配置更新以完整来源为单位：刷新已有站点参数、JAR、解析规则等，而不是只追加名字。
不会因 JAR 域名相同而混合不同配置。当前来源可验证时优先刷新它；没有通过检查的
当前站点时，从候选里选择检查通过比例和站点覆盖较好的整套配置；没有候选则保留当前
配置。直播入口沿用本地配置。其他通过检查的来源进入资源池，供后续故障切换。

`vod_policy.json` 的 `excluded_site_keys` 保存此前从配置删除的站点，避免自动更新
重新加入；既有频道品牌屏蔽规则也用于点播站点名称和接口地址。
更新前保存 `config.previous.json`；逐站点检查结果、发现失败原因及选择结果写入
`vod_report.json`，连续失败记录写入 `vod_health.json`。这些文件由定时任务一起保存。

```bash
# 只更新点播配置和资源池，不检查直播，也不要求安装 FFmpeg
python3 check_sources.py --vod-only

# 预览全部检查结果，不修改任何文件
python3 check_sources.py --vod-only --dry-run

# 仅检查已有点播资源池，不在线寻找新源
python3 check_sources.py --vod-only --no-discovery

# 增加在线发现候选数量
python3 check_sources.py --vod-only --vod-discovery-limit 80

# 只发现、验证及维护资源池，不切换当前配置
python3 discover_sources.py
```

这些修改提交并推送后，GitHub 的每日任务才会采用新流程；本地执行会立即更新本地文件。
