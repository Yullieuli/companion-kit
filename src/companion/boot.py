"""启动入口：先把字节码写入关掉，再进 CLI。

为什么：`device` 那组模块会被部分杀软判成加载器（误报，见 docs/AV.md），
而它们盯上的正是编译后的 `.pyc`。不写字节码，就没有东西可删——
Python 会每次在内存里即时编译，慢几十毫秒，换来的是不再被拦。

只影响本进程（`sys.dont_write_bytecode`），不改系统环境变量，
所以别的 Python 程序不受影响。
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

from .cli import main as _main  # noqa: E402  （必须在关掉字节码之后导入）


def main(argv: list[str] | None = None) -> int:
    return _main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
