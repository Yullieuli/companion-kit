"""connect：把 companion 接到宿主，且不能把宿主的配置弄坏。

这组测试守的不是"功能有没有"，是"会不会把用户的 agent 搞挂"：

  · JSON5 配置里插一段，原有键、注释、单引号不能丢
  · 已有 mcp 段时不覆盖（宁可失败也不猜）
  · Windows 路径写进 YAML 不能因为反斜杠被当转义而解析失败
  · 跑第二次必须报"已是最新"，不重写、不堆备份
  · 失败要能回滚成原样（改过的还原，新建的删掉）

为什么不用真实 HOME：这些用例会写 config.toml / openclaw.json。
一个测试写坏开发机的 codex 配置，代价远大于测试本身的价值。
所有用例只碰 tempfile 目录，并且显式传 *-home。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from companion import connect  # noqa: E402
from companion.store import Home  # noqa: E402


class _Tmp(unittest.TestCase):
    """每个用例一个临时根目录，退出即删。"""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.base = Path(self._td.name)
        self.home = Home.at(str(self.base / "store"))
        self.home.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._td.cleanup()

    def write(self, rel: str, text: str) -> Path:
        p = self.base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="")
        return p

    def opts(self) -> dict:
        return {
            "hermes_home": str(self.base / "hermes"),
            "dsh_home": str(self.base / "dsh"),
            "openclaw_home": str(self.base / "openclaw"),
            "codex_home": str(self.base / "codex"),
            # 测试不联网：pnpm install 拉公网既慢又不稳定
            "no_install": True,
        }


# ── JSON5 ────────────────────────────────────────────────────


class Json5Test(unittest.TestCase):
    def test_bare_keys(self):
        self.assertEqual(connect.json_parse_loose('{a: 1, b: {c: 2}}'), {"a": 1, "b": {"c": 2}})

    def test_trailing_comma(self):
        self.assertEqual(connect.json_parse_loose('{"a": 1,}'), {"a": 1})

    def test_single_quotes(self):
        self.assertEqual(connect.json_parse_loose("{'a': 'b'}"), {"a": "b"})

    def test_comments(self):
        text = '{\n  // 注释\n  "a": 1, /* 块注释 */\n}'
        self.assertEqual(connect.json_parse_loose(text), {"a": 1})

    def test_double_slash_inside_string_is_not_comment(self):
        self.assertEqual(
            connect.json_parse_loose('{"url": "http://a//b"}'),
            {"url": "http://a//b"},
        )

    def test_insert_keeps_original(self):
        """插入不能重排原文：用户的注释与缩进要留着。"""
        orig = '{\n  // 我的注释\n  models: {\n    "mode": "merge",\n  },\n}\n'
        new = connect._json_insert_top(orig, "mcp", {"servers": {}})
        self.assertIn("// 我的注释", new)
        self.assertIn("models", new)
        data = connect.json_parse_loose(new)
        self.assertIn("mcp", data)
        self.assertIn("models", data)


# ── TOML ─────────────────────────────────────────────────────


class TomlTest(_Tmp):
    def test_append_and_parse(self):
        cfg = self.write("config.toml", 'model = "x"\n\n[mcp_servers]\n')
        changed, _ = connect.toml_append_server(
            cfg, "mcp_servers.companion", r"C:\py\python.exe", ["-m", "companion", "mcp"],
            {"COMPANION_HOME": r"C:\Users\x\.companion"},
        )
        self.assertTrue(changed)
        ok, note = connect.toml_verify(cfg, "mcp_servers.companion")
        self.assertTrue(ok, note)
        text = cfg.read_text(encoding="utf-8")
        self.assertIn("[mcp_servers.companion.env]", text)

    def test_no_overwrite(self):
        cfg = self.write("config.toml", '[mcp_servers.companion]\ncommand = "keep-me"\n')
        changed, note = connect.toml_append_server(cfg, "mcp_servers.companion", "x", [], {})
        self.assertFalse(changed)
        self.assertIn("不动", note)
        self.assertIn("keep-me", cfg.read_text(encoding="utf-8"))

    def test_windows_path_is_literal(self):
        """单引号字面量：反斜杠不能被当成转义。"""
        cfg = self.write("config.toml", "")
        connect.toml_append_server(cfg, "mcp_servers.c", r"C:\Users\Anran\python.exe", [], {})
        ok, note = connect.toml_verify(cfg, "mcp_servers.c")
        self.assertTrue(ok, note)
        self.assertIn(r"C:\Users\Anran\python.exe", cfg.read_text(encoding="utf-8"))


# ── YAML ─────────────────────────────────────────────────────


class YamlTest(unittest.TestCase):
    BASE = (
        "model: deepseek\n"
        "hooks:\n"
        "  pre_llm_call:\n"
        '    - command: "/usr/bin/python3 said.py hook-pre"\n'
        "      timeout: 10\n"
    )

    def test_inject_into_existing_section(self):
        new, added = connect.yaml_inject_hooks(
            self.BASE, [("pre_llm_call", r"C:\x\hook.cmd"), ("post_llm_call", r"C:\x\post.cmd")]
        )
        self.assertEqual(added, ["pre_llm_call", "post_llm_call"])
        self.assertIn(r"C:\x\hook.cmd", new)
        self.assertIn(r"C:\x\post.cmd", new)
        # 原有的钩子还在
        self.assertIn("said.py hook-pre", new)
        # 事件段归属正确：post 不能落到 pre 段里
        pre_idx = new.index("pre_llm_call:")
        post_idx = new.index("post_llm_call:")
        self.assertLess(pre_idx, post_idx)
        self.assertLess(new.index(r"C:\x\hook.cmd"), post_idx)
        self.assertGreater(new.index(r"C:\x\post.cmd"), post_idx)

    def test_windows_path_uses_single_quote(self):
        """双引号里的 \\U 会被当转义，safe_load 直接抛错 —— 必须用单引号。"""
        new, _ = connect.yaml_inject_hooks(self.BASE, [("pre_llm_call", r"C:\Users\Anran\hook.cmd")])
        line = [ln for ln in new.splitlines() if "Anran" in ln][0]
        self.assertTrue(line.strip().startswith("- command: '"), line)
        self.assertNotIn('"C:\\Users', new)

    def test_no_hooks_section_creates_it(self):
        new, added = connect.yaml_inject_hooks("model: x\n", [("pre_llm_call", "/a/b.sh")])
        self.assertEqual(added, ["pre_llm_call"])
        self.assertIn("hooks:", new)
        self.assertIn("/a/b.sh", new)

    def test_idempotent(self):
        new, _ = connect.yaml_inject_hooks(self.BASE, [("pre_llm_call", "/a/b.sh")])
        again, added = connect.yaml_inject_hooks(new, [("pre_llm_call", "/a/b.sh")])
        self.assertEqual(added, [])
        self.assertEqual(again, new)


# ── 四个宿主 ─────────────────────────────────────────────────


class HostDetectTest(_Tmp):
    def test_nothing_installed(self):
        results = connect.run(self.home, None, opts=self.opts())
        self.assertTrue(all(r.status == "skipped" for r in results), [r.status for r in results])

    def test_codex_connect(self):
        self.write("codex/config.toml", 'model = "x"\n\n[mcp_servers]\n')
        results = connect.run(self.home, ["codex"], opts=self.opts())
        self.assertEqual(results[0].status, "connected", results[0].detail)
        ok, note = connect.toml_verify(self.base / "codex/config.toml", "mcp_servers.companion")
        self.assertTrue(ok, note)

    def test_openclaw_connect_json5(self):
        self.write("openclaw/openclaw.json", '{\n  models: {\n    "mode": "merge",\n  },\n}\n')
        results = connect.run(self.home, ["openclaw"], opts=self.opts())
        self.assertEqual(results[0].status, "connected", results[0].detail)
        data = connect.json_parse_loose((self.base / "openclaw/openclaw.json").read_text(encoding="utf-8"))
        self.assertIn("companion", data["mcp"]["servers"])
        self.assertIn("models", data)  # 原有内容没丢

    def test_openclaw_refuses_to_overwrite_mcp(self):
        """已有 mcp 段但没有 companion：不猜，直接报错让人手改。"""
        self.write("openclaw/openclaw.json", '{\n  mcp: { servers: { other: {} } },\n}\n')
        results = connect.run(self.home, ["openclaw"], opts=self.opts())
        self.assertEqual(results[0].status, "failed")
        self.assertIn("不会覆盖", results[0].detail)

    def test_dsh_connect(self):
        self.write(
            "dsh/profiles/desktop/package.json",
            json.dumps({"name": "p", "dependencies": {}, "dsh": {"profile": {"bundles": []}}}, indent=2),
        )
        results = connect.run(self.home, ["dsh"], opts={"dsh_home": str(self.base / "dsh"), "dsh_spec": "0.3.0", "no_install": True})
        self.assertEqual(results[0].status, "connected", results[0].detail)
        data = json.loads((self.base / "dsh/profiles/desktop/package.json").read_text(encoding="utf-8"))
        self.assertEqual(data["dependencies"]["dsh-plugin-companion"], "0.3.0")
        self.assertIn("dsh-plugin-companion", data["dsh"]["profile"]["bundles"])

    def test_dsh_strips_redundant_prefix(self):
        """传 dsh-plugin-companion@0.3.0 时不能拼成 name@name@ver。"""
        self.write(
            "dsh/profiles/desktop/package.json",
            json.dumps({"name": "p", "dependencies": {}, "dsh": {"profile": {"bundles": []}}}),
        )
        connect.run(self.home, ["dsh"], opts={"dsh_home": str(self.base / "dsh"), "dsh_spec": "dsh-plugin-companion@0.3.0", "no_install": True})
        data = json.loads((self.base / "dsh/profiles/desktop/package.json").read_text(encoding="utf-8"))
        self.assertEqual(data["dependencies"]["dsh-plugin-companion"], "0.3.0")


# ── 幂等 / 回滚 ──────────────────────────────────────────────


class IdempotentTest(_Tmp):
    def test_second_run_is_latest(self):
        self.write("codex/config.toml", 'model = "x"\n')
        self.write("openclaw/openclaw.json", '{\n  models: {},\n}\n')
        first = connect.run(self.home, ["codex", "openclaw"], opts=self.opts())
        self.assertTrue(all(r.status == "connected" for r in first), [r.status for r in first])
        second = connect.run(self.home, ["codex", "openclaw"], opts=self.opts())
        self.assertTrue(all(r.status == "latest" for r in second), [r.status for r in second])

    def test_no_backup_pile(self):
        cfg = self.write("codex/config.toml", 'model = "x"\n')
        connect.run(self.home, ["codex"], opts=self.opts())
        connect.run(self.home, ["codex"], opts=self.opts())
        baks = list(cfg.parent.glob("config.toml" + connect.BAK + "*"))
        self.assertEqual(len(baks), 1, [b.name for b in baks])


class RollbackTest(_Tmp):
    def test_rollback_restores_modified(self):
        cfg = self.write("codex/config.toml", 'model = "x"\n')
        before = cfg.read_text(encoding="utf-8")
        connect.run(self.home, ["codex"], opts=self.opts())
        self.assertNotEqual(cfg.read_text(encoding="utf-8"), before)
        connect.rollback(self.home)
        self.assertEqual(cfg.read_text(encoding="utf-8"), before)

    def test_rollback_deletes_created(self):
        self.write("dsh/profiles/desktop/package.json",
                   json.dumps({"name": "p", "dependencies": {}, "dsh": {"profile": {"bundles": []}}}))
        connect.run(self.home, ["dsh"], opts={"dsh_home": str(self.base / "dsh"), "dsh_spec": "1", "no_install": True})
        connect.rollback(self.home)
        # package.json 是原有文件 → 还原而不是删
        self.assertTrue((self.base / "dsh/profiles/desktop/package.json").exists())


class HealthTest(_Tmp):
    def test_dry_run_writes_nothing(self):
        cfg = self.write("codex/config.toml", 'model = "x"\n')
        before = cfg.read_text(encoding="utf-8")
        results = connect.run(self.home, ["codex"], dry_run=True, opts=self.opts())
        self.assertEqual(results[0].status, "planned")
        self.assertEqual(cfg.read_text(encoding="utf-8"), before)

    def test_status_writes_nothing(self):
        cfg = self.write("codex/config.toml", 'model = "x"\n')
        before = cfg.read_text(encoding="utf-8")
        results = connect.run(self.home, ["codex"], status_only=True, opts=self.opts())
        self.assertEqual(results[0].status, "detected")
        self.assertEqual(cfg.read_text(encoding="utf-8"), before)

    def test_mcp_spec_uses_interpreter_and_home(self):
        exe, args, env = connect.mcp_spec(self.home)
        self.assertTrue(exe)
        self.assertEqual(args, ["-m", "companion", "mcp"])
        self.assertEqual(env["COMPANION_HOME"], str(self.home.root))


if __name__ == "__main__":
    unittest.main()
