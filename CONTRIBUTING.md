# 参与开发

这个项目的核心约束很少，但每一条都有测试守着。先看完这一页再动手，能省掉一轮返工。

## 起环境

```bash
git clone https://github.com/Yullieuli/companion-kit
cd companion-kit
python -m venv .venv
.venv/bin/pip install -e .        # Windows: .venv\Scripts\pip install -e .
.venv/bin/companion --home /tmp/ck init
```

不需要安装任何运行时依赖：`dependencies = []` 是刻意的，core 只用标准库。
外部能力（TTS、OCR、ADB、CDP）都是可选工具，缺了会降级并说明，不会崩。

## 跑测试

```bash
python -m unittest discover -s tests          # 全量
python -m unittest tests.test_connect -v      # 单个模块
```

提交前请确保全量绿。测试里有几组是「守卫」性质的，改代码时容易被它们拦下：

| 测试 | 守什么 |
|---|---|
| `test_doc_truth.py` | 文档里写的工具数必须等于 `mcp.TOOLS` 的真实数量 |
| `test_packaging.py` | `pyproject` / `install.sh` / `install.ps1` / `package.json` 版本必须一致；`.sh` 必须是 LF |
| `test_contract.py` | CLI、MCP、direct 三条路径产出的 context 必须逐字节一致 |
| `test_coverage_gate.py` | core 覆盖率 ≥ 90%（平台适配层不计入） |

## 加一个新宿主

宿主接入全在 `src/companion/connect.py`，一个宿主一个类，四段式：

```python
class FooHost(Host):
    name = "foo"
    label = "foo（MCP stdio）"

    def detect(self) -> bool:      # 探测：找不到就设 self.reason 并返回 False
    def plan(self) -> list[Step]:  # 计划：返回要做的步骤，什么都不用做就返回 []
    def verify(self) -> tuple[bool, str]:  # 校验：写完之后检查真的生效了
```

四条不能破的规矩：

1. **先备份再改**。`Step(kind="write"|"edit", path=...)` 会自动备份；
   校验失败时 `Host.rollback()` 会还原（改过的还原、本次新建的删掉）。
2. **幂等**。`plan()` 里先判断「已经配好了吗」，配好了返回 `[]`，
   让上层报「没有需要改的东西」。
3. **不猜格式**。能用宿主自己的 CLI 就用 CLI；没有 CLI 才按本机实证格式写，
   并且写完必须能解析（JSON / JSON5 / TOML 至少要过一种）。
4. **不覆盖用户手写的段**。宁可失败并说明，也不做合并猜测。

新宿主加完，在 `tests/test_connect.py` 里补三件事：探测、写入后能解析、跑第二次是 `latest`。
测试只碰 `tempfile` 目录，**不要**写真实的 `~/.codex` 之类。

## 加一个 MCP 工具

1. 在 `src/companion/mcp.py` 的 `TOOLS` 里加定义，**必须带注解**（`readOnlyHint` 等），
   客户端靠它决定要不要为每次调用弹批准。
2. 跑 `test_doc_truth.py`，它会告诉你哪些文档的数字对不上了。

## 提交前

```bash
python -m unittest discover -s tests
bash -n install.sh                                  # 改了 shell 的话
```

如果你改了安装脚本或版本号，记得四处一起改：`pyproject.toml`、`install.sh`、`install.ps1`、`package.json`。
`test_packaging.py` 会拦。

## 不要提交的东西

- 任何凭据：`.credentials`、cookie、`devices.json` 里的 token。
- `~/.companion` 里的 store 内容（人格、记忆、人物卡属于个人数据）。
- `dist/`、`build/`、`__pycache__`、`.coverage`。

## 关于安全边界

`device` 那组会用 ctypes 截屏、模拟输入、连本地 CDP 端口，所以部分杀软会报
`Trojan/Python.ShellLoader`。这是启发式误报，说明见 [docs/AV.md](docs/AV.md)。
**不要**为了绕过误报去掉那五道闸（白名单、危险动作黑名单、主人限定、留痕、幂等+超时）。

安全问题请走 [SECURITY.md](SECURITY.md)，不要开公开 issue。
