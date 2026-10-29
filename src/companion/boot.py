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


def _force_utf8() -> None:
    """把 stdout/stderr 钉成 UTF-8。

    Windows 上 Python 默认用 locale 编码（en-US 的机器是 cp1252），
    只要输出里有一个中文就 ``UnicodeEncodeError`` —— 而这条 CLI 的
    输出全是中文。CI 的 windows-latest 就是 en-US，一跑就崩。

    ``errors="replace"`` 是兜底：终端真显示不了某个字符时，宁可显示成
    问号，也不要因为一行提示让整条命令失败。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            # 被重定向到不支持 reconfigure 的对象（某些嵌入式宿主）时跳过
            pass


from .cli import main as _main  # noqa: E402  （必须在关掉字节码之后导入）


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    return _main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
