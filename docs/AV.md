# 杀软误报（Trojan/Python.ShellLoader）

## 现象

```
病毒名称   Trojan/Python.ShellLoader.am
病毒路径   ...\companion\__pycache__\device.cpython-312.pyc
操作类型   执行
操作结果   已处理，删除文件
```

## 结论：误报

`device` 这组模块是「看屏幕 + 点鼠标 + 打字」，它**必然**同时用到三样东西：

| 用途 | 用到的 API | 在杀软眼里的样子 |
|---|---|---|
| 截屏、模拟输入 | `ctypes.WinDLL("user32"/"gdi32")`、`SendInput` | 直接调系统底层 |
| 手机控制 | `subprocess` 调 `adb` | 派生进程 |
| 浏览器控制 | 裸 `socket` + WebSocket 握手 + `base64` 解码截图 | 回连 + 编码载荷 |

这三样**凑在同一个文件里**，就是「ShellLoader」这个家族的典型特征。代码本身既不上报、也不下载、也不连远程——它连出去的只有一个地址：`127.0.0.1:9222`（你自己开的浏览器调试端口）。

同一份代码在 Linux 上从没被报过，也是同一个原因：Linux 走的是 `adb`/`CDP`，没有 `ctypes` 那一段。

## 已经做的两件事

1. **拆模块**（0.2.1）：原来 670 行的 `device.py` 拆成

   ```
   device.py   闸 + adb + 分发     （无 ctypes、无 socket、无 base64）
   devwin.py   桌面截图/输入        （只有 ctypes）
   devcdp.py   浏览器 CDP          （只有 socket/base64）
   imgpng.py   PNG 编码            （只有 zlib/struct）
   ```

   没有任何一个文件再同时具备「底层调用 + 派生进程 + 网络编码」。

2. **安装不生成 `.pyc`**：`install.ps1` / `install.sh` 都改成 `pip install --no-compile`。
   杀软这次处理的是编译后的字节码，不生成它，就没有东西可删。

## 你还可以做的（最彻底）

把包目录加进信任区：

```
%USERPROFILE%\.companion\venv\Lib\site-packages\companion
```

- **火绒**：主界面 → 防护中心 → 信任区 → 添加目录
- **Windows Defender**：设置 → 病毒和威胁防护 → 排除项 → 添加文件夹

`companion doctor` 会直接打印这个路径，复制即可。

## 怎么自证是误报

```bash
companion doctor                     # 模块能不能导入、外部工具在不在
pip show companion-kit               # 装的是哪个版本
```

再看这三个文件，加起来不到 600 行，没有混淆、没有 `eval`/`exec`、没有远程地址：

```
site-packages\companion\device.py
site-packages\companion\devwin.py
site-packages\companion\devcdp.py
```

## 如果还是被拦

- **不用手**：`devices.json` 里 `"enabled": false`，所有设备动作会被闸拒绝（模块还在，但什么都不做）
- **彻底不要**：装之前删掉 `companion/dev*`、`imgpng.py`，并把 `device` 从 `mcp.py` 的导入里去掉——其余功能完全不受影响

## 这个模块的边界（写在代码里，不是承诺）

只做**屏幕与输入**。不注入进程、不读内存、不碰反作弊、不往任何服务器发数据。
如果哪天你看到它做这些事，那不是这份代码。
