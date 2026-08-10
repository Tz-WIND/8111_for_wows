# 8111 for World of Warships

战舰世界（WG 国际服）的"8111 式"数据导出器，类似战雷官方 `localhost:8111` 的功能：
在游戏内用**官方 ModsAPI**采集**合法**的战斗数据，再通过本地 **HTTP REST + WebSocket** 暴露给浏览器 / OBS / 你自己的工具。

> 仅采集你在游戏里本来就能看到的数据（已被侦测/已加载的舰船、自身状态、名单元数据等），不解析隐藏敌情、不读取游戏内存、不动网络封包。

---

## 为什么分两个进程？

战舰世界的游戏内 Python 是**沙箱**：它**禁止打开本地 socket / 监听端口**，所以无法像战雷那样直接在游戏进程里开 8111。但沙箱**允许写文件**。因此采用业界通行的"文件桥"方案：

```text
游戏进程 (Python 2.7 沙箱)                     独立进程 (你的 Python 3)
┌─────────────────────────────┐               ┌──────────────────────────────┐
│ PnFMods/WowsExtractor        │   写 JSON     │ server.py                    │
│  事件钩子 + 每~100ms 采集     │ ───────────▶ │  监听文件变化                  │
│  battle / dataHub / events   │  state.json   │  REST: /state /map_obj.json …│
│                              │  meta.json    │  WebSocket: /ws (~10Hz 推送)  │
└─────────────────────────────┘               └───────────────┬──────────────┘
                                                               │ HTTP / WS
                                                       浏览器 / OBS / 工具 / overlay
```

---

## 目录结构

```text
8111_for_wows/
├─ mod/                                  # 游戏内采集器（拷进 res_mods）
│  └─ PnFMods/WowsExtractor/
│     ├─ Main.py
│     └─ config.example.ini              # 采集器配置模板（复制为 config.ini）
├─ tools/probe/                          # 探针 mod（一次性诊断，确认字段名）
│  └─ PnFMods/WowsProbe/Main.py
├─ config.example.ini                    # 服务端配置模板（复制为 config.ini）
├─ pyproject.toml                        # uv 项目定义（依赖 aiohttp）
├─ uv.lock                               # uv 锁定文件
├─ server/
│  ├─ server.py                          # 本地 HTTP + WebSocket 服务（aiohttp）
│  ├─ requirements.txt                   # pip 备用安装清单（推荐用 uv）
│  ├─ static/overlay.html                # 演示用小地图 overlay
│  ├─ examples/ws_client.py              # 示例消费端（WS + REST）
│  └─ sample_data/                       # 离线测试用样例 state.json / meta.json
├─ run_server.bat                        # 一键启动服务（设置都在 config.ini，见下方）
└─ README.md
```

---

## 快速开始（不需要游戏，先看效果）

`--demo` 模式不需要 `config.ini`。若要跑真实数据，先复制配置模板：

```bash
copy config.example.ini config.ini
```

服务端用 [`uv`](https://docs.astral.sh/uv/) 管理环境，依赖 `aiohttp`。在仓库根目录执行：

```bash
uv sync --no-dev                 # 首次：创建 .venv，仅安装运行依赖
uv run --no-dev python server/server.py --demo
```

浏览器打开 <http://127.0.0.1:8111/overlay> 就能看到 10 艘船在小地图上跑动（合成数据）。
首页 <http://127.0.0.1:8111/> 列出全部端点。

> 服务端基于 **aiohttp**（异步 HTTP + WebSocket），由 **uv** 管理依赖与虚拟环境；`uv sync --no-dev` 会按 `pyproject.toml` / `uv.lock` 自动选择合适的 Python（已在 3.13 上测试）并只安装运行依赖。
> 没装 uv 时也可用 pip：`pip install -r server/requirements.txt` 后 `python server/server.py --demo`。

---

## 安装到游戏（WG `World_of_Warships`）

### 第 0 步：拿到 `PnFModsLoader.py`

ModsAPI 的 Python mod 由 `PnFModsLoader.py` 加载。如果你的 `res_mods/` 里还没有它，从任意现成的 Unbound2 mod 包里复制一个即可（例如 TTaroTeamPanel / StreamerMode 的发布 zip 里都带，或 Wargaming 的 ModsSDK）。它是通用文件，各 mod 通用同一个。

### 第 1 步（推荐）：先跑探针，确认字段名

不同客户端版本的 API/字段名会有细微差别。先用探针 mod 把**你这台机器实际可用**的 API dump 出来：

1. 把 `tools/probe/PnFMods/WowsProbe/` 复制到
   `World_of_Warships/bin/<最新build号>/res_mods/PnFMods/WowsProbe/`
2. 确保同级 `res_mods/` 下有 `PnFModsLoader.py`
3. 进任意一场战斗（训练房即可）
4. 查看产出：
   - 日志：`World_of_Warships/python.log`（搜索 `[WowsProbe]`）
   - 文件：`.../res_mods/PnFMods/WowsProbe/probe_dump.txt`

`probe_dump.txt` 会列出 `dir(battle)` / `dir(ui)`、`dataHub` 实体与组件、`getAllShips()` 第一艘船的全部属性、自身字段、地图/小地图候选 API。
如果某字段名和采集器里用的不一致，按它修正 `mod/PnFMods/WowsExtractor/Main.py` 里对应的属性名即可

> 探针还顺便验证了"游戏内能写文件"，并在日志里打印实际写入路径。

### 第 2 步：装采集器

1. 把整个 `mod/PnFMods/WowsExtractor/`（含 `Main.py` 和 `config.example.ini`）复制到
   `World_of_Warships/bin/<最新build号>/res_mods/PnFMods/WowsExtractor/`
2. 确保 `res_mods/` 下有 `PnFModsLoader.py`
3. （可选）在同目录执行 `copy config.example.ini config.ini`，按需修改 `config.ini`（例如把采集频率 `state_interval` 调大一点更省帧）；缺 `config.ini` 就用内置默认（10Hz）
4. 进战斗后，采集器会在自己目录下生成：
   - `meta.json`：开局写一次（名单、舰种/tier、消耗品范围、地图信息）
   - `state.json`：默认约每 100ms 写一次（可见舰船位置/航向/血量、自身、伤害、弹道）

确认是否生效：看 `python.log` 里的 `[WowsExtractor] writing telemetry to: <绝对路径>`。

> **性能**：采集器把 JSON 编码与写盘放到后台线程（沙箱不允许线程时自动退回同步写），所以即便 10Hz 也几乎不占游戏帧数。想再省一点就把 `config.ini` 里的 `state_interval` 调到 `0.15` 或 `0.2`。

### 第 3 步：启动服务

在仓库根目录复制并编辑配置，把 `game_dir` 指向你的安装目录（端口等也在里面）：

```bash
copy config.example.ini config.ini
# 编辑 config.ini 里的 game_dir
```

然后双击 `run_server.bat`（它会自动 `uv sync --no-dev` 再启动）。或在仓库根目录手动：

```bash
# 默认读取 config.ini（game_dir / host / port / poll_interval）
uv run --no-dev python server/server.py
# 命令行参数会覆盖 config.ini，例如临时换端口或指定文件：
uv run --no-dev python server/server.py --port 8125
uv run --no-dev python server/server.py --state-file "D:\...\res_mods\PnFMods\WowsExtractor\state.json"
```

打开 <http://127.0.0.1:8111/overlay> 即可看到实时小地图。

---

## 配置文件

| 位置 | 初始化 |
| --- | --- |
| 仓库根（服务端） | `copy config.example.ini config.ini` |
| `mod/PnFMods/WowsExtractor/`（采集器） | `copy config.example.ini config.ini`（拷进游戏目录后执行） |

**① 仓库根 `config.ini` — 服务端读**

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `game_dir` | — | 游戏安装目录，自动定位 `state.json`（原来写在 `run_server.bat` 里的路径搬到这里了） |
| `host` | `127.0.0.1` | 监听地址 |
| `port` | `8111` | 监听端口（和战雷冲突就改掉） |
| `allow_remote` | `false` | 是否允许监听非本机地址（如 `0.0.0.0` 或局域网 IP）；默认拒绝，避免把遥测暴露到局域网 |
| `allowed_origins` | 本机常见 Origin | 浏览器 Origin 白名单，逗号分隔，同时用于 HTTP CORS 与 WebSocket 握手。留空时仅允许 `127.0.0.1` / `localhost` / `[::1]` 对应端口；设为 `*` 才恢复任意网页可跨域读取 |
| `poll_interval` | `0.1` | 文件轮询间隔（秒），建议 ≤ 采集器的 `state_interval` |
| `state_file` / `meta_file` | — | 可选：跳过自动查找，直接指定文件路径 |

**② `mod/PnFMods/WowsExtractor/config.ini` — 游戏内采集器读**

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `state_interval` | `0.1` | 写 `state.json` 的间隔（秒）。`0.1`=10Hz；调成 `0.15`/`0.2` 更省帧（代码里最小限制 `0.02`） |
| `last_seen_ttl` | `60.0` | 敌舰消失后小地图保留"残影"标记多久（秒） |

> 命令行参数 **>** `config.ini` **>** 内置默认。即用 `run_server.bat --port 8125` 之类的 flag 仍可临时覆盖配置。

---

## 命令行参数（`server.py`）

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--config` | 仓库根 `config.ini` | 指定其它配置文件 |
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8111` | 监听端口（和战雷冲突就改掉） |
| `--game-dir` | — | 游戏安装目录，自动定位 `state.json` |
| `--state-file` | — | 直接指定 `state.json` 路径 |
| `--meta-file` | 同 state 目录 | `meta.json` 路径 |
| `--poll-interval` | `0.1` | 文件轮询间隔（秒） |
| `--static-dir` | `server/static` | overlay 等静态文件目录 |
| `--demo` | 关 | 用合成数据跑，不需要游戏 |
| `--allow-remote` | 关 | 允许监听非本机地址；不加时 `--host 0.0.0.0` 会被拒绝 |
| `--allowed-origin` | 本机常见 Origin | 添加一个允许的浏览器 Origin，用于 HTTP CORS 与 WebSocket 握手；可重复传。传 `--allowed-origin "*"` 会恢复通配行为 |

> 表中"默认"列指**既没传命令行、`config.ini` 里也没设**时的内置值。

---

## v1 数据契约

- 服务身份固定为 `serviceId="8111_for_wows"`、`apiVersion="1.0"`。消费者应拒绝未知的服务 ID 或 API major；同一 major 内只做向后兼容的增量扩展。
- `(instanceId, seq)` 是快照游标：服务进程重启会更换 `instanceId`；数据内容或 `source.status` 变化才推进 `seq`，重复读取不会推进。相同游标的 `/all` 与 `/ws` 字节一致。
- `source.status` 只有 `waiting`（尚无有效 state）、`live`（战斗中且新鲜）、`stale`（战斗数据停止更新）和 `ended`（采集器明确给出 inactive）。断流不会伪造成 `ended`。
- `source.updatedAt` 是最后一次有效 state 的 Unix 秒时间；文件模式使用 state 文件修改时间并钳制未来时钟。meta 更新和 stale 状态翻转不会改写它。
- `availability` 只有 `available`、`unknown`、`stale`。字段存在且类型正确时，即使数组或对象为空也可为 `available`；ballistics 仅在其 `available` 字段严格为布尔 `true` 时可用。
- 地图 `bounds` 顺序固定为 `[minX, maxX, minZ, maxZ]`。
- 扩展 ID 必须使用带点号的命名空间（如 `vendor.feature`），并包含字符串 `schema` 与 `data`。服务会原样保留其它扩展元数据，将 schema 加入 `capabilities`，并用严格布尔 `available` 生成动态 availability。
- 后台采集任务异常退出时 `/healthz` 返回 HTTP `503`，同时仍返回服务身份、游标和有界诊断。

---

## HTTP API

HTTP JSON 端点默认只对本机常见 Origin 返回 CORS 许可；WebSocket 握手也会校验浏览器传来的 `Origin`。同源访问（例如内置 `/overlay`）不受影响；非浏览器客户端没有 `Origin` 时仍可连接。若确实要让其它网页跨域读取，可在 `config.ini` 里设置 `allowed_origins`，或用 `--allowed-origin` 传入明确的 Origin。

| 端点 | 说明 |
| --- | --- |
| `GET /` | 首页，列出所有端点 |
| `GET /healthz` | 服务状态：`battleActive`、最后更新 `ageSeconds`、`wsClients` 等 |
| `GET /all` | **全量合并快照**（meta + state + 归一化对象列表）。WebSocket 推送的也是它 |
| `GET /map_obj.json` | 所有可见对象数组（归一化 `nx/ny` + 世界 `x/z`、`relation`、`type`、`name`、`hpRatio`、`yaw`…） |
| `GET /map_info.json` | 地图识别结果：`mapId`（内部空间名）、`mapName`（友好名）、世界边界 `bounds`、`boundsKnown`、`boundsSource`、`battleType` |
| `GET /state` | 自身舰船状态 |
| `GET /indicators` | 自身航向/航速/血量/坐标 |
| `GET /roster` | 完整名单 + 舰种/tier + 消耗品范围 |
| `GET /damage` | 造成 / 承受 / 全队总伤 |
| `GET /ballistics` | 当前弹种穿深 / 跳弹角 / 引信 等 |
| `GET /ws` | WebSocket：每次数据更新（约 10Hz）推送一份 `/all` |
| `GET /overlay` | 演示小地图页面 |

### `/map_obj.json` 单个对象字段

```jsonc
{
  "uiId": 1, "vehicleId": 1, "playerId": 537,
  "teamId": 0, "relation": 1,          // relation: 1=友方, 2=敌方
  "type": "Battleship", "name": "PJSB018_Yamato_1944", "playerName": "You", "tier": 10,
  "alive": true, "visible": true,
  "x": 200.0, "z": -420.0,             // 世界坐标（BigWorld，原点居中，约 ±600~±1000）
  "nx": 0.625, "ny": 0.7625,          // 归一化小地图坐标 [0,1]，地图被识别/边界已知时出现
  "yaw": 1.2,                          // 航向（弧度）
  "health": 81000, "maxHealth": 97700, "hpRatio": 0.829
}
```

### 坐标系与地图识别

- 采集器始终输出**世界坐标** `x/z`（BigWorld 单位，所有对战地图都以世界原点 `(0,0)` 居中）。
- 服务端按下面的优先级给出地图边界，并据此输出归一化 `nx/ny ∈ [0,1]`（原点左上、北朝上）。`map_info.json` 的 `boundsSource` 会告诉你用的是哪一种：
  1. **`runtime`** —— 采集器在 `meta.map` 里直接给了数值边界（`minX/maxX/minZ/maxZ` 或 `width/height`），优先采用。
  2. **`table`** —— 采集器给出了游戏内空间名（如 `spaces/13_OC_new_dawn`），服务端用内置的 15.5 地图表（`server/maps.py`）识别为友好名（如 `New Dawn`）并填入该图**精确的、以原点居中的世界边界**。
  3. 都没有 —— `boundsKnown=false`，对象里没有 `nx/ny`，overlay 退化为**按当前舰船位置自动缩放**（client-side auto-fit），照样能用。
- `server/maps.py` 由 `tools/gen_maps.py` 从客户端 `space.settings` 自动生成（边界 = `chunk 网格 × <chunkSize>`，默认 100m，已对 15.5 全部对战地图核对、零对称性误差）。换游戏版本后重新解包并重跑生成器即可，详见下文“更新地图表”。

---

## WebSocket 用法

```js
const ws = new WebSocket(`ws://${location.host}/ws`);
ws.onmessage = (ev) => {
  const snap = JSON.parse(ev.data);   // 等同 GET /all
  for (const o of snap.objects) {
    // o.nx / o.ny 或 o.x / o.z, o.relation, o.name, o.hpRatio, ...
  }
};
```

命令行示例（客户端只用 Python 标准库，无需额外依赖）：

```bash
uv run --no-dev python server/examples/ws_client.py --port 8111 --messages 10   # 流式打印
uv run --no-dev python server/examples/ws_client.py --port 8111 --rest          # 单次 REST
```

---

## 离线自测（不开游戏）

```bash
uv run --no-dev python server/server.py --state-file server/sample_data/state.json --port 8124
curl http://127.0.0.1:8124/all
curl http://127.0.0.1:8124/map_obj.json
```

`sample_data/` 里的 `state.json` / `meta.json` 就是采集器输出格式的样例，可拿来对照 schema。

---

## 故障排查

- **没有 `state.json`**：确认 mod 在 `res_mods/PnFMods/WowsExtractor/`（不是 `mod/` 这层），且 `res_mods/` 下有 `PnFModsLoader.py`；看 `python.log` 有没有 `[WowsExtractor] loaded`。
- **服务端找不到文件**：确认根目录已有 `config.ini`（从 `config.example.ini` 复制）且 `game_dir` 正确；或用 `--state-file` 直接指定 `python.log` 里打印的那个绝对路径。
- **overlay 上船都挤在一起 / 位置不准**：看 `map_info.json` 的 `boundsKnown` 与 overlay HUD 的 Bounds 行。若 `boundsKnown=false`（overlay 显示 `auto-fit`），说明这张图没被识别——多半是采集器没拿到游戏内空间名。确认该客户端版本下 `_build_map_info()` 能取到形如 `13_OC_new_dawn` 的空间名（可用探针对照），或确认它在 `server/maps.py` 表里（新图需按下文重跑生成器）。
- **某些字段为 null**：该客户端版本的属性名不同。看 `probe_dump.txt` 对照修正属性名（缺字段不会崩，只是为空）。
- **端口被占用**：`--port` 换一个。

---

## 更新地图表（换游戏版本时）

`server/maps.py` 是由 `tools/gen_maps.py` 从客户端 `space.settings` **自动生成**的（当前对应 15.5，含 50 张对战图）。游戏更新、出新图后按三步重建即可：

```bat
:: 1) 用官方解包器导出所有 space.settings（用你安装目录里的 build 号）
wowsunpack.exe "E:\World_of_Warships\bin\<build>\idx" ^
  -p "E:\World_of_Warships\res_packages" ^
  -I "*space.settings" -x -o "<解包输出目录>"

:: 2) 把 tools/gen_maps.py 里的 SPACES_DIR 指向上一步产生的 spaces\ 目录
:: 3) 重新生成 server/maps.py
uv run python tools/gen_maps.py
```

原理：`space.settings` 的 `<bounds>` 是**区块网格**坐标，每格大小取 `<chunkSize>`（默认 100m，个别图 300m）；所有对战图都满足 `minX+maxX=-1`（即以世界原点居中），于是世界边界 = `网格 × chunkSize`，可精确换算、无需经验估计。友好显示名优先用内置精选表，缺失的按内部名自动清洗（去掉 `13_`/区域码、下划线转空格）。

---

## 说明 / 免责

- 本工具只用官方 ModsAPI 读取**你本就可见**的数据，等价于把游戏 UI 已显示的信息换种方式输出，不获取隐藏信息。
- mod 与第三方工具的使用请遵守对应服务器的相关规定，风险自负。

---

## 开源协议

本项目采用 [MIT License](LICENSE) 开源。
