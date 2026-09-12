# Link 卡组仓库测试，验证 AstrBot 会话隔离、来源标注和 YDK 内容读取

import tempfile
import json
import unittest
from pathlib import Path

from service.deck_repository import LinkDeckRepository
from utils.deck_utils import load_deck


YDK_TEXT = """#main
89631139
89631139
#extra
23995346
!side
46986414
"""


class LinkDeckRepositoryTests(unittest.TestCase):
    # 验证 Link 本地卡组可以安全导入覆盖和删除
    def test_local_deck_import_overwrite_and_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = LinkDeckRepository(root)

            imported = repository.import_local_ydk("临时测试.ydk", YDK_TEXT)
            self.assertEqual(imported["deck_ref"], "临时测试")
            self.assertEqual(imported["counts"], {"main": 2, "extra": 1, "side": 1})
            self.assertTrue((root / "decks/临时测试.ydk").is_file())

            with self.assertRaises(FileExistsError):
                repository.import_local_ydk("临时测试.ydk", YDK_TEXT)
            overwritten = repository.import_local_ydk(
                "临时测试.ydk",
                YDK_TEXT.replace("46986414", "89631139"),
                overwrite=True,
            )
            self.assertEqual(overwritten["counts"]["side"], 1)

            deleted = repository.delete_local_deck("临时测试")
            self.assertEqual(deleted["deck_ref"], "临时测试")
            self.assertFalse((root / "decks/临时测试.ydk").exists())

            with self.assertRaises(ValueError):
                repository.import_local_ydk("../越界.ydk", YDK_TEXT)

    # 验证 Link 本地卡组支持单卡增删和跨区域移动
    def test_local_deck_card_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = LinkDeckRepository(root)
            repository.import_local_ydk("需要调整.ydk", YDK_TEXT)

            updated = repository.edit_local_deck(
                "需要调整",
                [
                    {"operation": "remove", "code": 89631139, "section": "main"},
                    {"operation": "add", "code": 46986414, "section": "main"},
                    {
                        "operation": "move",
                        "code": 23995346,
                        "section": "extra",
                        "to_section": "side",
                    },
                ],
            )

            self.assertEqual(updated["counts"], {"main": 2, "extra": 0, "side": 2})
            self.assertTrue(updated["source"]["content_modified"])
            deck = load_deck(root / "decks", "需要调整")
            self.assertEqual(deck.main, [89631139, 46986414])
            self.assertEqual(deck.extra, [])
            self.assertEqual(deck.side, [46986414, 23995346])

    # 验证本地卡组复制后成为当前会话独享且可编辑的临时副本
    def test_local_deck_copy_is_session_scoped_and_editable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deck_root = root / "decks"
            deck_root.mkdir()
            (deck_root / "本地卡组.ydk").write_text(YDK_TEXT, encoding="utf-8")
            repository = LinkDeckRepository(root)

            record = repository.copy_local_to_astrbot(
                "group_100",
                "本地卡组",
                "AstrBot A",
            )
            visible_other = repository.discover(scope_id="group_200")

            self.assertTrue(record["deck_ref"].startswith("astrbot:"))
            self.assertEqual(record["source"]["kind"], "link_local_copy")
            self.assertEqual(record["source"]["origin_display_name"], "本地卡组")
            self.assertNotIn(
                record["deck_ref"],
                {item["deck_ref"] for item in visible_other},
            )
            edited = repository.edit_astrbot_deck(
                "group_100",
                record["deck_ref"],
                [{"operation": "add", "code": 46986414, "section": "main"}],
            )
            self.assertEqual(edited["counts"]["main"], 3)

    # 验证不同 AstrBot 会话只能发现自己的导入卡组
    def test_astrbot_decks_are_isolated_by_session_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deck_root = root / "decks"
            deck_root.mkdir()
            (deck_root / "本地卡组.ydk").write_text(YDK_TEXT, encoding="utf-8")
            repository = LinkDeckRepository(root)
            first = repository.import_astrbot_ydk(
                {
                    "scope_id": "qq:group:100",
                    "display_name": "群内解析卡组",
                    "instance_name": "AstrBot A",
                    "original_filename": "deck_group_100.ydk",
                    "ydk_text": YDK_TEXT,
                }
            )
            second = repository.import_astrbot_ydk(
                {
                    "scope_id": "qq:group:200",
                    "display_name": "另一个群卡组",
                    "instance_name": "AstrBot A",
                    "ydk_text": YDK_TEXT,
                }
            )
            refreshed = repository.import_astrbot_ydk(
                {
                    "scope_id": "qq:group:100",
                    "display_name": "当前会话工具箱缓存",
                    "instance_name": "AstrBot A",
                    "ydk_text": YDK_TEXT,
                }
            )

            public_refs = {item["deck_ref"] for item in repository.discover()}
            first_records = repository.discover(scope_id="qq:group:100")
            first_refs = {item["deck_ref"] for item in first_records}

            self.assertEqual(public_refs, {"本地卡组"})
            self.assertIn(first["deck_ref"], first_refs)
            self.assertNotIn(second["deck_ref"], first_refs)
            self.assertEqual(refreshed["deck_ref"], first["deck_ref"])
            self.assertEqual(refreshed["display_name"], "群内解析卡组")
            self.assertTrue(repository.contains(first["deck_ref"], "qq:group:100"))
            self.assertFalse(repository.contains(first["deck_ref"], "qq:group:200"))
            self.assertNotIn("session_id", first["source"])
            self.assertEqual(first["source"]["scope"], "current_session")

    # 验证导入卡组可以读取主卡组、额外卡组和副卡组内容
    def test_imported_deck_ref_loads_all_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = LinkDeckRepository(root)
            record = repository.import_astrbot_ydk(
                {
                    "scope_id": "qq:user:42",
                    "display_name": "我的缓存卡组",
                    "instance_name": "AstrBot",
                    "ydk_text": YDK_TEXT,
                }
            )

            deck = load_deck(root / "decks", record["deck_ref"])

            self.assertEqual(deck.name, "我的缓存卡组")
            self.assertEqual(len(deck.main), 2)
            self.assertEqual(len(deck.extra), 1)
            self.assertEqual(len(deck.side), 1)
            self.assertEqual(record["counts"], {"main": 2, "extra": 1, "side": 1})
            self.assertEqual(record["cards"]["main"][0]["count"], 2)

    # 验证过期清理只访问当前会话目录
    def test_expired_cleanup_stays_within_current_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = LinkDeckRepository(root)
            first = repository.import_astrbot_ydk(
                {
                    "scope_id": "group_100",
                    "display_name": "即将过期",
                    "instance_name": "AstrBot",
                    "ydk_text": YDK_TEXT,
                }
            )
            second = repository.import_astrbot_ydk(
                {
                    "scope_id": "group_200",
                    "display_name": "其他会话",
                    "instance_name": "AstrBot",
                    "ydk_text": YDK_TEXT,
                }
            )
            _, first_scope, first_id = first["deck_ref"].split(":")
            _, second_scope, second_id = second["deck_ref"].split(":")
            first_meta = root / "decks/astrbot_imports" / first_scope / f"{first_id}.meta.json"
            metadata = json.loads(first_meta.read_text(encoding="utf-8"))
            metadata["source"]["expires_at"] = 1
            first_meta.write_text(json.dumps(metadata), encoding="utf-8")

            visible = repository.discover(scope_id="group_100")

            self.assertNotIn(first["deck_ref"], {item["deck_ref"] for item in visible})
            self.assertFalse(first_meta.exists())
            self.assertTrue(
                (root / "decks/astrbot_imports" / second_scope / f"{second_id}.ydk").exists()
            )

    # 验证当前会话临时卡组支持增删和跨区域移动
    def test_edit_astrbot_deck_updates_only_matching_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = LinkDeckRepository(root)
            record = repository.import_astrbot_ydk(
                {
                    "scope_id": "group_100",
                    "display_name": "待调整卡组",
                    "instance_name": "AstrBot",
                    "ydk_text": YDK_TEXT,
                }
            )

            with self.assertRaises(PermissionError):
                repository.edit_astrbot_deck(
                    "group_200",
                    record["deck_ref"],
                    [{"operation": "add", "code": 46986414, "section": "main"}],
                )

            updated = repository.edit_astrbot_deck(
                "group_100",
                record["deck_ref"],
                [
                    {"operation": "remove", "code": 89631139, "section": "main"},
                    {"operation": "add", "code": 46986414, "section": "main"},
                    {
                        "operation": "move",
                        "code": 23995346,
                        "section": "extra",
                        "to_section": "side",
                    },
                ],
            )

            self.assertEqual(updated["counts"], {"main": 2, "extra": 0, "side": 2})
            self.assertEqual(updated["source"]["modified_by"], "AstrBot 主智能体")
            deck = load_deck(root / "decks", record["deck_ref"])
            self.assertEqual(deck.main, [89631139, 46986414])
            self.assertEqual(deck.extra, [])
            self.assertEqual(deck.side, [46986414, 23995346])

            refreshed = repository.import_astrbot_ydk(
                {
                    "scope_id": "group_100",
                    "display_name": "当前会话工具箱缓存",
                    "instance_name": "AstrBot",
                    "ydk_text": YDK_TEXT,
                }
            )
            refreshed_deck = load_deck(root / "decks", refreshed["deck_ref"])
            self.assertEqual(refreshed_deck.main, [89631139, 46986414])
            self.assertEqual(refreshed_deck.extra, [])
            self.assertTrue(refreshed["source"]["content_modified"])


if __name__ == "__main__":
    unittest.main()
