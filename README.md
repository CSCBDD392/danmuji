# 弹幕点歌机 · 弹幕面板

PC 端两只小工具，配套使用：

1. **点歌机** —— 监听 B 站直播间弹幕，观众发「点歌 晴天」，自动去**酷狗音乐网页版**搜索并播放。
2. **弹幕面板** —— 从 B 站抓弹幕，实时显示在一个**正方形面板**里，面板可以贴在任意窗口的左/右边缘，也可自由拖动、投到别的显示器。

面板里每条消息带：**头像 · 昵称（B 站原色）· 大航海/粉丝牌/房管头衔 · 等级 · 时间**，
礼物、上舰、进场等事件用一行高亮显示。

```
┌───────────────────────┐
│ ● 弹幕     拖动/右键关 │  ← 26px 标题条
├───────────────────────┤
│ [头] 昵称 提督 粉丝牌0 20:36:06 │
│      666666                    │
│ [头] 昵称 舰长 粉丝团14 20:36:08 │
│      点歌 晴天                  │
│ 🎁 谁 投喂 小心心×3             │
└───────────────────────┘
```

## 快速开始

需要 Windows + Python 3.8+，浏览器装有 Edge 或 Chrome。

```bat
pip install -r requirements.txt
set live.room=              :: 在 config.json 里填你的直播间号
python main.py --gui
```

`--gui` 会弹出小控制窗：填房间号、选面板贴哪边、点「开始」即可。

其它入口：

| 命令 | 作用 |
| --- | --- |
| `python main.py` | 正常模式：连 B 站弹幕 + 自动拉起弹幕面板 |
| `python main.py --gui` | 控制窗（推荐日常用） |
| `python main.py --panel` | 只开弹幕面板，不管点歌 |
| `python main.py --once 晴天` | 只测一首歌能不能在酷狗里播出来 |
| `python main.py --offline` | 不连 B 站，用键盘输入模拟弹幕（调试） |
| `python danmu.py --test` | 面板演示模式（假弹幕，看效果用） |
| `python test_all.py` | 离线自测，应输出 `FAILED: 0` |

## 弹幕面板的用法

```bat
python danmu.py --host "哔哩哔哩"        :: 贴到标题含「哔哩哔哩」的窗口上
python danmu.py --host "" --screen 1    :: 不跟窗口，投到第 2 块屏
python danmu.py --host "PotPlayer" --app PotPlayerMini64.exe --side left
python danmu.py --size 620              :: 初始边长 620
python danmu.py --size-mode free        :: 拉伸时宽高各自变（默认 square 保持正方形）
```

### 鼠标操作

| 操作 | 效果 |
| --- | --- |
| 拖标题条 / 任意空白处 | 移动面板（拖到任意屏幕都行） |
| 鼠标移到**上/下/左/右边框**，出现双向箭头后拖动 | 拉高 / 拉矮 / 往左拉宽 / 往右拉宽 |
| 拖**四个角** | 同时改宽高 |
| 右键 / 双击 / `Esc` | 关闭面板 |
| `Ctrl+Alt+D` / `Ctrl+Alt+Q` | 显示隐藏 / 关闭 |

- **宽高可自由调整**：`size_mode` 为 `square` 时，从任意一条边拉伸都保持正方形；
  设成 `free` 后宽高各自独立拉伸（可以拉成细长条）。
- **往左延伸**：抓住左边框往左拖，右侧位置不动，宽度变大；往右延伸同理抓右边框。
- **吸附**：默认正方形边长 = 目标窗口高度，压在目标窗口右边缘内侧，`--side left` 换左边。
  目标窗口移动/缩放时面板自动跟随，直到你亲手拖动过它为止。
- 最小尺寸 `min_width` / `min_height`（默认 260 / 160），`edge_margin` 可让面板与窗口边缘留点缝。
- `config.json → danmu` 可调：`side`、`size_mode`、`size`、`min_width`、`min_height`、
  `edge_margin`、`screen`、`alpha` 透明度、`bg` 底色、`font_size` 字号、
  `max_msgs` 保留多少条、`toggle_hotkey`、`quit_hotkey`。
- 面板是独立进程，通过 `127.0.0.1:9701` 收推送，点歌机重开不影响它：
  ```bat
  python danmu.py --emit "测试一条"
  ```

## 点歌

观众弹幕支持这些写法（大小写/全半角无所谓）：

```
点歌 晴天
点歌：晴天 - 周杰伦
【点歌】起风了 - 买辣椒也用券
来一首《稻香》
!点歌 孤勇者
```

`切歌` / `下一首` / `跳过` / `skip` 直接切歌。

规则（`config.json → live`）：

| 键 | 说明 |
| --- | --- |
| `room` | 直播间房间号（不是主播 UID） |
| `sessdata` | 可选，登录 Cookie，能收到更完整的弹幕 |
| `enabled` | 点歌总开关（关掉就只显示弹幕） |
| `user_cooldown_sec` | 同一人点歌冷却，默认 20 秒 |
| `max_order_len` | 歌名最长字数 |
| `admin` | 管理员昵称/UID 列表，不受冷却限制 |

`config.json → queue`：`gap_sec` 每首间隔、`wait_song_end` 等这首放完再放下一首、
`max_pending` 最多排队几首。

### 酷狗播放是怎么实现的

不逆向、不盗链，就是**真的开一个酷狗网页版页面去播放**：

1. 用 CDP 启动一个独立 profile 的 Edge/Chrome（端口 `9333`，profile 在 `~/.danmuji/kugou-profile`），
   可正常登录酷狗，登录态会保留。
2. 先用酷狗公开搜索接口选最匹配的一首（标题+歌手打分，避开 Live/伴奏/翻唱）。
3. 打开 `https://www.kugou.com/mixsong/<id>.html` 播放页，直接调页面自己的播放接口；
   浏览器策略拦自动播放时，会补一次模拟点击；再不行就把页面切到前台，点一下就能放。

> 付费/无版权歌曲酷狗本来就不给放，面板里会提示「暂时放不了」。
> 第一次用建议先手动在面板里登录酷狗一次（`python main.py --once 晴天` 会打开浏览器）。

## 分发给别人 / 杀软误报

- 全部是**纯 Python 源码**，`pip install` 后直接跑，不打包 exe —— 这是最不容易被误报的形态。
- 不写注册表、不装服务、不加自启动；后台只有本机的 `127.0.0.1:9701` 和一个 `9333` 调试端口。
- 外连域名只有：`api.bilibili.com`、`live.bilibili.com`、`songsearch.kugou.com`、`www.kugou.com`。
- 如果一定要打包成 exe：用 PyInstaller **不开 UPX、不混淆**，体积大一点的「一体式」包反而更少被报毒；
  发出去之前先传到 VirusTotal 看看，别用在线「加壳」服务。
- 别把带你自己 Cookie 的 `config.json`、`.github-token` 发给别人。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `main.py` | 主程序：弹幕→点歌分发、播放队列、拉起面板、GUI 入口 |
| `danmu.py` | 正方形弹幕面板（tkinter 无边框窗口 + Win32 窗口吸附） |
| `gui.py` | 小控制窗 |
| `bili.py` | B 站弹幕 WebSocket（zlib/brotli 解包、心跳、重连、wbi 签名） |
| `kugou.py` | 酷狗搜索 + CDP 打开播放页并播放 |
| `cdp.py` | 极简 Chrome DevTools Protocol 客户端（只用标准库 websocket） |
| `page.js` / `_gen_page.py` | 页面内执行的小脚本及其生成器 |
| `publish_github.py` | 一键发布到 GitHub（只用 REST API，不需要 git） |

## 常见问题

- **面板没出现**：目标窗口标题没匹配上（`--host` 是子串匹配），或窗口被最小化；
  不跟窗口时用 `--screen 0` 固定到屏幕。
- **有弹幕但没声**：酷狗需要登录或该曲付费；面板会提示，点「下一首」继续。
- **端口被占用**：面板默认 9701，酷狗调试口 9333，可在 `config.json` 里改。
