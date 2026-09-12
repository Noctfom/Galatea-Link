# Link 运行资产管理测试，验证资产路径约束和 142 宣言兜底池编辑

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from core.gamestate import DuelState
from model_protocols.v3.code_semantic_embedder import CodeSemanticEmbedder
from model_protocols.v3.lua_semantic_parser import YGOProLuaSemanticParser
from service.asset_manager import LinkAssetManager


class LinkAssetManagerTests(unittest.TestCase):
    # 创建包含一张卡片的最小 YGOPro CDB
    def _write_card_database(self, path: Path) -> None:
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE datas (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE texts (id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute("INSERT INTO datas (id) VALUES (14558127)")
        connection.execute("INSERT INTO texts (id, name) VALUES (14558127, '灰流丽')")
        connection.commit()
        connection.close()

    # 验证当前模型资产目录独立保存并返回具名 142 兜底卡
    def test_updates_model_scoped_meta_staples(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset_root = root / "model_assets" / "v3" / "model-id"
            asset_root.mkdir(parents=True)
            self._write_card_database(asset_root / "cards.cdb")
            manager = LinkAssetManager(
                root,
                lambda: {"assets_path": "./model_assets/v3/model-id"},
            )

            result = manager.update_meta_staples("replace", [14558127])
            status = manager.get_status()

            self.assertEqual(result, [{"code": 14558127, "name": "灰流丽"}])
            self.assertEqual(status["meta_staples"], result)
            self.assertTrue((asset_root / "meta_staples.json").is_file())

    # 验证模型配置不能把资产更新引向项目目录之外
    def test_rejects_asset_path_outside_model_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = LinkAssetManager(
                root,
                lambda: {"assets_path": "../outside"},
            )

            with self.assertRaisesRegex(ValueError, "model_assets/v3"):
                manager.get_active_asset_root()

    # 验证 DuelState 从选中模型资产读取 142 宣言兜底池
    def test_duel_state_uses_model_scoped_meta_staples(self):
        with tempfile.TemporaryDirectory() as directory:
            asset_root = Path(directory)
            (asset_root / "meta_staples.json").write_text(
                "[14558127, 23434538]",
                encoding="utf-8",
            )

            state = DuelState(asset_dir=asset_root)

            self.assertEqual(state.meta_staples, [14558127, 23434538])

    # 验证本机 Lua 解析器生成模型协议 V3 效果槽和绑定
    def test_local_lua_semantic_parser_builds_incremental_knowledge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script_root = root / "script"
            script_root.mkdir()
            (script_root / "c12345678.lua").write_text(
                "function c12345678.initial_effect(c)\n"
                " local e1=Effect.CreateEffect(c)\n"
                " e1:SetDescription(aux.Stringid(12345678,0))\n"
                " e1:SetType(EFFECT_TYPE_IGNITION)\n"
                " e1:SetCategory(CATEGORY_DRAW)\n"
                " e1:SetOperation(c12345678.op)\n"
                " c:RegisterEffect(e1)\n"
                "end\n"
                "function c12345678.op(e,tp,eg,ep,ev,re,r,rp)\n"
                " Duel.Draw(tp,1,REASON_EFFECT)\n"
                "end\n",
                encoding="utf-8",
            )
            parser = YGOProLuaSemanticParser(script_root)

            result = parser.run_batch(root / "knowledge_base.json")

            self.assertEqual(result["added_card_count"], 1)
            knowledge = json.loads(
                (root / "knowledge_base.json").read_text(encoding="utf-8")
            )
            effect = knowledge["12345678"]["effects"][0]
            self.assertEqual(effect["categories"], ["CATEGORY_DRAW"])
            self.assertEqual(effect["runtime_desc_ids"], [197530848])

    # 验证代码语义嵌入器可在假模型下生成完整可校验资产
    def test_code_semantic_embedder_writes_complete_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "knowledge_base.json").write_text(
                '{"1":{"effects":[{"slot":1,"raw_code":"draw"}]}}',
                encoding="utf-8",
            )

            class FakeModel:
                # 返回固定维度测试向量
                def encode(self, values, **kwargs):
                    return np.ones((len(values), 384), dtype=np.float32)

            embedder = CodeSemanticEmbedder()
            with patch.object(embedder, "_get_model", return_value=FakeModel()):
                result = embedder.generate_embeddings(
                    root / "knowledge_base.json",
                    root / "code_embeddings.npy",
                )

            self.assertEqual(result["effect_slot_count"], 1)
            self.assertEqual(result["embedding_shape"], [1, 384])

    # 验证资产管理器在校验完成后安装本机构建语义资产
    def test_asset_manager_rebuilds_semantics_into_active_model_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script_root = root / "script"
            script_root.mkdir()
            (script_root / "c1.lua").write_text(
                "function c1.initial_effect(c)\n"
                " local e1=Effect.CreateEffect(c)\n"
                " e1:SetType(EFFECT_TYPE_IGNITION)\n"
                " e1:SetCategory(CATEGORY_DRAW)\n"
                " c:RegisterEffect(e1)\n"
                "end\n",
                encoding="utf-8",
            )
            manager = LinkAssetManager(
                root,
                lambda: {"assets_path": "./model_assets/v3/model-id"},
            )

            class FakeModel:
                # 返回固定维度测试向量
                def encode(self, values, **kwargs):
                    return np.ones((len(values), 384), dtype=np.float32)

            with patch.object(
                CodeSemanticEmbedder,
                "_get_model",
                return_value=FakeModel(),
            ):
                result = manager.rebuild_semantic_bundle(clear_existing=True)

            asset_root = root / "model_assets" / "v3" / "model-id"
            self.assertEqual(result["mode"], "full_local")
            self.assertEqual(result["effect_slot_count"], 1)
            self.assertTrue((asset_root / "knowledge_base.json").is_file())
            self.assertTrue((asset_root / "code_embeddings.npy").is_file())
            self.assertTrue((asset_root / "code_embeddings_idx.json").is_file())


if __name__ == "__main__":
    unittest.main()
