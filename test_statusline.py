# test_statusline.py — 実行: python3 -m unittest test_statusline
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import statusline

RESET_AT = 1_800_000_000


class TestParsePlan(unittest.TestCase):
    def _parse(self, acct):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"oauthAccount": acct}, f)
        self.addCleanup(os.unlink, f.name)
        return statusline.parse_plan(f.name)

    def test_planDisplayNameを優先する(self):
        self.assertEqual(self._parse({"planDisplayName": "Ultra",
                                      "organizationRateLimitTier": "default_claude_max_20x"}), "Ultra")

    def test_planDisplayNameが無ければ枠区分から組み立てる(self):
        self.assertEqual(self._parse({"organizationRateLimitTier": "default_claude_max_20x"}), "Max 20x")

    def test_planDisplayNameが空や非文字列なら枠区分に落ちる(self):
        self.assertEqual(self._parse({"planDisplayName": "  ",
                                      "organizationRateLimitTier": "default_claude_max_5x"}), "Max 5x")
        self.assertEqual(self._parse({"planDisplayName": 1,
                                      "organizationRateLimitTier": "default_claude_pro"}), "Pro")

    def test_組織種別にも落ちる(self):
        self.assertEqual(self._parse({"organizationType": "claude_team"}), "Team")


class TestPlanCache(unittest.TestCase):
    def test_旧版のキャッシュは使わない(self):
        with tempfile.TemporaryDirectory() as home:
            src = os.path.join(home, ".claude.json")
            cache = os.path.join(home, "plan.cache")
            with open(src, "w") as f:
                json.dump({"oauthAccount": {"planDisplayName": "Ultra"}}, f)
            with open(cache, "w") as f:  # 版の無い旧形式のキャッシュ
                f.write(f"{int(os.stat(src).st_mtime)}\tMax 20x")
            with mock.patch.dict(os.environ, {"HOME": home}), \
                 mock.patch.object(statusline, "PLAN_CACHE_PATH", cache):
                self.assertEqual(statusline.plan_name(), "Ultra")


class TestRateLimitSections(unittest.TestCase):
    def setUp(self):
        # 実環境の codex セッション・設定・キャッシュを読み書きさせない
        for name, value in (("codex_limits", None), ("format_codex", None),
                            ("format_meta", None), ("format_dir", None)):
            p = mock.patch.object(statusline, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def _render(self, rate_limits):
        data = {} if rate_limits is None else {"rate_limits": rate_limits}
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(data))):
            return statusline.render()

    def test_週枠だけ届いていれば5h枠を出さない(self):
        out = self._render({"seven_day": {"used_percentage": 14, "resets_at": RESET_AT}})
        self.assertNotIn("5h枠", out)
        self.assertIn("週枠", out)

    def test_5h枠がnullや空でも週枠があれば出さない(self):
        for v in (None, {}, "x"):
            out = self._render({"five_hour": v, "seven_day": {"used_percentage": 14}})
            self.assertNotIn("5h枠", out, v)

    def test_両方届いていれば両方出す(self):
        out = self._render({"five_hour": {"used_percentage": 3, "resets_at": RESET_AT},
                            "seven_day": {"used_percentage": 2, "resets_at": RESET_AT}})
        self.assertIn("5h枠", out)
        self.assertIn("週枠", out)

    def test_両方無ければ両方NAで残す(self):
        out = self._render(None)
        self.assertIn("5h枠 N/A", out)
        self.assertIn("週枠 N/A", out)

    def test_5h枠だけなら週枠はNA(self):
        out = self._render({"five_hour": {"used_percentage": 3}})
        self.assertIn("5h枠", out)
        self.assertIn("週枠 N/A", out)



def _assistant(mid, ctx, out):
    return json.dumps({"type": "assistant", "message": {"id": mid, "usage": {
        "input_tokens": ctx, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        "output_tokens": out}}})


class TestSessionReadPerOutput(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tp = os.path.join(self.tmp.name, "session.jsonl")
        p = mock.patch.object(statusline, "EFF_CACHE_DIR", os.path.join(self.tmp.name, "eff"))
        p.start()
        self.addCleanup(p.stop)

    def _write(self, lines, mode="w"):
        with open(self.tp, mode, encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in lines))

    def _ratio(self):
        return statusline.session_read_per_output({"transcript_path": self.tp})

    def test_出力が少ないうちは出さない(self):
        self._write([_assistant("a", 100_000, 500)])
        self.assertIsNone(self._ratio())

    def test_入力側の合計を出力で割る(self):
        self._write([_assistant("a", 300_000, 1000), _assistant("b", 500_000, 1000)])
        self.assertEqual(self._ratio(), 400)

    def test_同じmessage_idは1回だけ数える(self):
        self._write([_assistant("a", 400_000, 2000), _assistant("a", 400_000, 2000)])
        self.assertEqual(self._ratio(), 200)

    def test_増えた分だけ読み足す(self):
        self._write([_assistant("a", 400_000, 2000)])
        self.assertEqual(self._ratio(), 200)
        self._write([_assistant("b", 800_000, 2000)], mode="a")
        self.assertEqual(self._ratio(), 300)

    def test_書きかけの最終行は次回に回す(self):
        self._write([_assistant("a", 400_000, 2000)])
        with open(self.tp, "a", encoding="utf-8") as f:
            f.write(_assistant("b", 800_000, 2000)[:40])
        self.assertEqual(self._ratio(), 200)

    def test_ファイルが無ければNone(self):
        self.assertIsNone(statusline.session_read_per_output({"transcript_path": self.tp + ".none"}))
        self.assertIsNone(statusline.session_read_per_output({}))

    def test_壊れたキャッシュは捨てて数え直す(self):
        self._write([_assistant("a", 400_000, 2000)])
        os.makedirs(statusline.EFF_CACHE_DIR)
        with open(os.path.join(statusline.EFF_CACHE_DIR, "sessionjsonl.json"), "w") as f:
            f.write('{"offset": null, "ctx": "x", "out": 1, "ids": 3}')
        self.assertEqual(self._ratio(), 200)

    def test_辞書でない行は読み飛ばす(self):
        self._write(['["assistant","usage"]', '"assistant usage"', _assistant("a", 400_000, 2000)])
        self.assertEqual(self._ratio(), 200)

    def test_しきい値の環境変数が不正なら既定値(self):
        for bad in ("", "abc", "nan", "inf", "0", "-5"):
            with mock.patch.dict(os.environ, {"STATUSLINE_EFF_WARN": bad}):
                self.assertEqual(statusline._env_threshold("STATUSLINE_EFF_WARN", 300.0), 300.0)
        with mock.patch.dict(os.environ, {"STATUSLINE_EFF_WARN": "250"}):
            self.assertEqual(statusline._env_threshold("STATUSLINE_EFF_WARN", 300.0), 250.0)

    def test_閾値を超えたら次の一手を添える(self):
        self._write([_assistant("a", 640_000, 2000)])
        with mock.patch.object(statusline, "plan_name", return_value=None):
            meta = statusline.format_meta({"transcript_path": self.tp})
        self.assertIn("320 区切りどき", meta)

if __name__ == "__main__":
    unittest.main()
