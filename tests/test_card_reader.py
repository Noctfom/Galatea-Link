# 卡片数据库测试，验证后台决策线程可以安全查询卡片信息

import asyncio
import unittest

from utils.card_reader import DEFAULT_CARD_DB_PATH, CardReader


class CardReaderTests(unittest.IsolatedAsyncioTestCase):
    # 验证默认数据库路径不依赖外部进程工作目录
    async def test_default_database_path_uses_link_project_root(self):
        reader = CardReader()
        try:
            self.assertEqual(reader.db_path, str(DEFAULT_CARD_DB_PATH))
            self.assertIsNotNone(reader.conn)
        finally:
            if reader.conn is not None:
                reader.conn.close()

    # 验证跨线程查询不会触发 SQLite 线程限制
    async def test_reads_card_name_from_worker_thread(self):
        reader = CardReader("cards.cdb")
        try:
            name = await asyncio.to_thread(reader.get_card_name, 89631139)
            text = await asyncio.to_thread(reader.get_card_text, 89631139)
            legacy_effect = await asyncio.to_thread(
                reader.get_effect_description,
                (39015 << 4) | 1,
                39015,
            )
            modern_effect = await asyncio.to_thread(
                reader.get_effect_description,
                ((39015 << 20) | 0) & 0xFFFFFFFF,
                39015,
            )
        finally:
            if reader.conn is not None:
                reader.conn.close()

        self.assertNotEqual(name, "Code 89631139")
        self.assertTrue(text)
        self.assertEqual(legacy_effect, "改变种族·属性")
        self.assertEqual(modern_effect, "特殊召唤")


if __name__ == "__main__":
    unittest.main()
