"""store 锁的回归：重入、异常释放、残留锁文件、多进程不丢更新、跨进程超时。

对应 WORK_PLAN P0 第 3 条。多进程用例起真实子进程（sys.executable），
不复用父进程里的锁状态，验证的是文件锁本身。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion.store import Home  # noqa: E402

SRC = str(Path(__file__).resolve().parents[1] / "src")

# 子进程：在锁保护下对 counter.json 做 读→+1→写
_INCR_SCRIPT = """
import json, sys
from pathlib import Path
from companion.store import Home

home = Home(Path(sys.argv[1]))
for _ in range(int(sys.argv[2])):
    with home.lock("store"):
        p = home.root / "counter.json"
        try:
            v = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            v = {"n": 0}
        v["n"] = int(v.get("n", 0)) + 1
        home.save_json("counter.json", v)
print("done", flush=True)
"""

# 子进程：拿住锁睡 N 秒（验证别的进程会超时）
_HOLD_SCRIPT = """
import sys, time
from pathlib import Path
from companion.store import Home

home = Home(Path(sys.argv[1]))
with home.lock("store"):
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
"""


def run_py(code: str, args: list[str], **kw) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": SRC}
    return subprocess.Popen([sys.executable, "-c", code, *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env, **kw)


class LockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        # Windows 上子进程的句柄释放有延迟，立刻删目录可能撞上
        # "另一个程序正在使用此文件"。重试几次，别让偶发变成 flaky。
        import time
        for attempt in range(10):
            try:
                self.tmp.cleanup()
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.2)

    def test_reentrant_in_same_thread(self):
        """同线程嵌套拿锁不死锁，退出后状态清干净。"""
        with self.home.lock("store"):
            with self.home.lock("store"):
                with self.home.lock("store"):
                    self.home.save_json("counter.json", {"n": 1})
        # 全部退出后再拿一次应该立即成功
        with self.home.lock("store", timeout=1.0):
            self.assertEqual(json.loads(
                (self.home.root / "counter.json").read_text(encoding="utf-8"))["n"], 1)

    def test_released_after_exception(self):
        """锁内抛异常必须释放，不能把后续写操作全卡死。"""
        with self.assertRaises(ValueError):
            with self.home.lock("store"):
                raise ValueError("炸了")
        with self.home.lock("store", timeout=1.0):
            self.home.save_json("counter.json", {"n": 2})

    def test_stale_lock_file_does_not_block(self):
        """崩溃进程留下的锁文件不挡路：OS 文件锁随进程消亡。"""
        p = self.home.root / "logs" / ".locks" / "store.lock"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\0")  # 模拟上次崩溃时留下的文件
        with self.home.lock("store", timeout=1.0):
            self.home.save_json("counter.json", {"n": 3})

    def test_multiprocess_no_lost_update(self):
        """6 个真实进程各加 5 次：一个都不能丢。"""
        procs = [run_py(_INCR_SCRIPT, [self.tmp.name, "5"]) for _ in range(6)]
        for p in procs:
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err)
            self.assertIn("done", out)
            # 显式关掉管道：Windows 上不关的话句柄会一直占着，
            # 后面 tearDown 删目录就会撞 PermissionError。
            for stream in (p.stdout, p.stderr):
                try:
                    stream.close()
                except Exception:
                    pass
        v = json.loads((self.home.root / "counter.json").read_text(encoding="utf-8"))
        self.assertEqual(v["n"], 30)
        # 不留临时文件
        leftovers = list(Path(self.tmp.name).glob("*.tmp")) + \
            list(Path(self.tmp.name).glob(".*.tmp"))
        self.assertEqual(leftovers, [])

    def test_open_while_locked_does_not_crash(self):
        """Windows 上：别的进程持锁时 open() 本身会 PermissionError。

        第二个进程必须**等**，不能直接崩——这是真踩过的坑：
        6 个宿主同时起，一个抢到锁，其余 5 个直接 PermissionError 挂掉。
        """
        p = run_py(_HOLD_SCRIPT, [self.tmp.name, "3"])
        try:
            line = p.stdout.readline()
            self.assertEqual(line.strip(), "held")
            other = Home.at(self.tmp.name)
            # 不该抛 PermissionError；最多等 3 秒拿到（对方 3 秒后放）
            with other.lock("store", timeout=10.0):
                pass
        finally:
            p.kill()
            p.wait(10)
            for stream in (p.stdout, p.stderr):
                try:
                    stream.close()
                except Exception:
                    pass

    def test_timeout_other_process(self):
        """别的进程拿住锁：短超时必须 TimeoutError，而不是永远阻塞。"""
        p = run_py(_HOLD_SCRIPT, [self.tmp.name, "10"])
        try:
            line = p.stdout.readline()
            self.assertEqual(line.strip(), "held")
            other = Home.at(self.tmp.name)
            with self.assertRaises(TimeoutError):
                with other.lock("store", timeout=0.5):
                    self.fail("不该拿到锁")
        finally:
            p.kill()
            p.wait(10)
            for stream in (p.stdout, p.stderr):
                try:
                    stream.close()
                except Exception:
                    pass
        # 持锁进程死后，锁必须立即可用
        with Home.at(self.tmp.name).lock("store", timeout=2.0):
            pass

    def test_threads_in_one_process_are_serialized(self):
        """同进程多线程并发读改写也不丢更新。"""
        def worker():
            for _ in range(25):
                with self.home.lock("store"):
                    p = self.home.root / "counter.json"
                    try:
                        v = json.loads(p.read_text(encoding="utf-8"))
                    except Exception:
                        v = {"n": 0}
                    v["n"] = int(v.get("n", 0)) + 1
                    self.home.save_json("counter.json", v)

        ts = [threading.Thread(target=worker) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        v = json.loads((self.home.root / "counter.json").read_text(encoding="utf-8"))
        self.assertEqual(v["n"], 100)


if __name__ == "__main__":
    unittest.main()
