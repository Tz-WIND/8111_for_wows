# 8111 for World of Warships

战舰世界（WG 国际服）的"8111 式"数据导出器，类似战雷官方 `localhost:8111` 的功能：
在游戏内用**官方 ModsAPI** 采集**合法（仅玩家可见）**的战斗数据，再通过本地 **HTTP REST + WebSocket** 暴露给浏览器 / OBS / 你自己的工具。

> 仅采集你在游戏里本来就能看到的数据（已被侦测/已加载的舰船、自身状态、名单元数据等），不解析隐藏敌情、不读取游戏内存、不动网络封包。

---

## 为什么分两个进程？

战舰世界的游戏内 Python 是**沙箱**：它**禁止打开本地 socket / 监听端口**，所以无法像战雷那样直接在游戏进程里开 8111。但沙箱**允许写文件**。因此采用业界通行的"文件桥"方案：

```
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

```
8111_for_wows/
├─ mod/                                  # 游戏内采集器（拷进 res_mods）
│  └─ PnFMods/WowsExtractor/Main.py
├─ tools/probe/                          # 探针 mod（一次性诊断，确认字段名）
│  └─ PnFMods/WowsProbe/Main.py
├─ pyproject.toml                        # uv 项目定义（依赖 aiohttp）
├─ uv.lock                               # uv 锁定文件
├─ server/
│  ├─ server.py                          # 本地 HTTP + WebSocket 服务（aiohttp）
│  ├─ requirements.txt                   # pip 备用安装清单（推荐用 uv）
│  ├─ static/overlay.html                # 演示用小地图 overlay
│  ├─ examples/ws_client.py              # 示例消费端（WS + REST）
│  └─ sample_data/                       # 离线测试用样例 state.json / meta.json
├─ run_server.bat                        # 一键启动服务（先改里面的 GAME_DIR）
└─ README.md
```

---

## 快速开始（不需要游戏，先看效果）

服务端用 [`uv`](https://docs.astral.sh/uv/) 管理环境，依赖 `aiohttp`。在仓库根目录执行：

```bash
uv sync                          # 首次：创建 .venv 并安装 aiohttp
uv run python server/server.py --demo
```

浏览器打开 <http://127.0.0.1:8111/overlay> 就能看到 10 艘船在小地图上跑动（合成数据）。
首页 <http://127.0.0.1:8111/> 列出全部端点。

> 服务端基于 **aiohttp**（异步 HTTP + WebSocket），由 **uv** 管理依赖与虚拟环境；`uv sync` 会按 `pyproject.toml` / `uv.lock` 自动选择合适的 Python（已在 3.13 上测试）并安装 `aiohttp`。
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

1. 把 `mod/PnFMods/WowsExtractor/` 复制到
   `World_of_Warships/bin/<最新build号>/res_mods/PnFMods/WowsExtractor/`
2. 确保 `res_mods/` 下有 `PnFModsLoader.py`
3. 进战斗后，采集器会在自己目录下生成：
   - `meta.json`：开局写一次（名单、舰种/tier、消耗品范围、地图信息）
   - `state.json`：约每 100ms 写一次（可见舰船位置/航向/血量、自身、伤害、弹道）

确认是否生效：看 `python.log` 里的 `[WowsExtractor] writing telemetry to: <绝对路径>`。

### 第 3 步：启动服务

编辑 `run_server.bat` 顶部的 `GAME_DIR` 指向你的安装目录，然后双击它（它会自动 `uv sync` 再启动）。或在仓库根目录手动：

```bash
# 自动在 <game>/bin/<最新build>/res_mods/PnFMods/WowsExtractor/ 下找 state.json
uv run python server/server.py --game-dir "D:\Games\World_of_Warships"
# 或直接指定文件
uv run python server/server.py --state-file "D:\...\res_mods\PnFMods\WowsExtractor\state.json"
```

打开 <http://127.0.0.1:8111/overlay> 即可看到实时小地图。

---

## 命令行参数（`server.py`）

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8111` | 监听端口（和战雷冲突就改掉） |
| `--game-dir` | — | 游戏安装目录，自动定位 `state.json` |
| `--state-file` | — | 直接指定 `state.json` 路径 |
| `--meta-file` | 同 state 目录 | `meta.json` 路径 |
| `--poll-interval` | `0.1` | 文件轮询间隔（秒） |
| `--static-dir` | `server/static` | overlay 等静态文件目录 |
| `--demo` | 关 | 用合成数据跑，不需要游戏 |

---

## HTTP API

所有 JSON 端点都带 `Access-Control-Allow-Origin: *`，可被任意网页直接 `fetch`。

| 端点 | 说明 |
| --- | --- |
| `GET /` | 首页，列出所有端点 |
| `GET /healthz` | 服务状态：`battleActive`、最后更新 `ageSeconds`、`wsClients` 等 |
| `GET /all` | **全量合并快照**（meta + state + 归一化对象列表）。WebSocket 推送的也是它 |
| `GET /map_obj.json` | 所有可见对象数组（归一化 `nx/ny` + 世界 `x/z`、`relation`、`type`、`name`、`hpRatio`、`yaw`…） |
| `GET /map_info.json` | 地图名、世界边界 `bounds`、`battleType`、`boundsKnown` |
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
  "x": 1500.0, "z": -3200.0,           // 世界坐标（BigWorld）
  "nx": 0.51, "ny": 0.62,              // 归一化小地图坐标 [0,1]，仅当地图边界已知时出现
  "yaw": 1.2,                          // 航向（弧度）
  "health": 81000, "maxHealth": 97700, "hpRatio": 0.829
}
```

### 坐标系说明

- 采集器始终输出**世界坐标** `x/z`（BigWorld 单位）。
- 若 `meta.json` 提供了地图边界（`minX/maxX/minZ/maxZ` 或 `width/height`），服务端会顺带给出归一化 `nx/ny ∈ [0,1]`（原点左上、北朝上）。
- 若边界未知（探针没找到对应 API），`map_info.json` 的 `boundsKnown=false`，对象里就没有 `nx/ny`——overlay 会**自动按当前所有舰船位置缩放**（client-side auto-fit），照样能用；等你用探针确认了边界 API 再补到采集器里即可获得精确归一化。

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
uv run python server/examples/ws_client.py --port 8111 --messages 10   # 流式打印
uv run python server/examples/ws_client.py --port 8111 --rest          # 单次 REST
```

---

## 离线自测（不开游戏）

```bash
uv run python server/server.py --state-file server/sample_data/state.json --port 8124
curl http://127.0.0.1:8124/all
curl http://127.0.0.1:8124/map_obj.json
```

`sample_data/` 里的 `state.json` / `meta.json` 就是采集器输出格式的样例，可拿来对照 schema。

---

## 故障排查

- **没有 `state.json`**：确认 mod 在 `res_mods/PnFMods/WowsExtractor/`（不是 `mod/` 这层），且 `res_mods/` 下有 `PnFModsLoader.py`；看 `python.log` 有没有 `[WowsExtractor] loaded`。
- **服务端找不到文件**：用 `--state-file` 直接指定 `python.log` 里打印的那个绝对路径。
- **overlay 上船都挤在一起 / 位置不准**：多半是地图边界未知走了 auto-fit。跑探针确认边界 API，把 `minX/maxX/minZ/maxZ` 填进采集器的 `_build_map_info()`。
- **某些字段为 null**：该客户端版本的属性名不同。看 `probe_dump.txt` 对照修正属性名（缺字段不会崩，只是为空）。
- **端口被占用**：`--port` 换一个。

---

## 说明 / 免责

- 本工具只用官方 ModsAPI 读取**你本就可见**的数据，等价于把游戏 UI 已显示的信息换种方式输出，不获取隐藏信息。
- mod 与第三方工具的使用请遵守对应服务器的相关规定，风险自负。
