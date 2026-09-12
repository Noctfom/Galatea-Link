# 模型协议 V3 代码语义向量模块，按效果槽增量生成和校验嵌入资产

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from .semantic_assets import (
    CODE_EMBEDDINGS_INDEX_FILENAME,
    build_expected_code_semantic_keys,
    validate_code_semantic_assets,
    validate_semantic_bundle,
)


class CodeSemanticEmbedder:
    # 初始化代码语义模型名称并延迟加载深度学习依赖
    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        self.model_name = model_name
        self.model: Any | None = None

    # 在首次需要计算向量时加载 Sentence Transformers
    def _get_model(self) -> Any:
        if self.model is None:
            try:
                import torch
                from sentence_transformers import SentenceTransformer
            except ImportError as error:
                raise RuntimeError(
                    "本机语义化需要安装 requirements-semantic.txt"
                ) from error
            device = "cuda" if torch.cuda.is_available() else "cpu"
            self.model = SentenceTransformer(self.model_name, device=device)
        return self.model

    # 返回当前嵌入模型的固定输出维度
    def _get_expected_dimension(self) -> int:
        if self.model is not None:
            return int(self.model.get_sentence_embedding_dimension())
        if self.model_name == "all-MiniLM-L6-v2":
            return 384
        return int(self._get_model().get_sentence_embedding_dimension())

    # 按卡号和效果槽稳定收集需要编码的 Lua 代码
    @staticmethod
    def _collect_effect_code(
        knowledge_base: dict[str, Any],
    ) -> list[tuple[str, str]]:
        entries = []
        for card_id in sorted(knowledge_base, key=lambda value: int(value)):
            data = knowledge_base[card_id]
            effects = sorted(
                data.get("effects", []),
                key=lambda effect: int(effect.get("slot", 1) or 1),
            )
            for effect in effects:
                slot_idx = int(effect.get("slot", 1) or 1) - 1
                if not 0 <= slot_idx < 8:
                    continue
                entries.append(
                    (
                        f"{card_id}_{slot_idx}",
                        str(effect.get("raw_code", "")),
                    )
                )
        return entries

    # 原子替换向量矩阵和对应索引文件
    @staticmethod
    def _write_embedding_pair(
        output_path: Path,
        embeddings: np.ndarray,
        key_to_idx: dict[str, int],
    ) -> None:
        index_path = output_path.with_name(CODE_EMBEDDINGS_INDEX_FILENAME)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        embedding_temporary = None
        index_temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{output_path.name}.",
                suffix=".tmp",
                dir=output_path.parent,
                delete=False,
            ) as stream:
                embedding_temporary = Path(stream.name)
                np.save(stream, embeddings, allow_pickle=False)
            with tempfile.NamedTemporaryFile(
                mode="w",
                prefix=f".{index_path.name}.",
                suffix=".tmp",
                dir=index_path.parent,
                encoding="utf-8",
                delete=False,
            ) as stream:
                index_temporary = Path(stream.name)
                json.dump(key_to_idx, stream, ensure_ascii=False)
            os.replace(embedding_temporary, output_path)
            embedding_temporary = None
            os.replace(index_temporary, index_path)
            index_temporary = None
        finally:
            for temporary in (embedding_temporary, index_temporary):
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    # 增量生成缺失效果槽向量并返回构建统计
    def generate_embeddings(
        self,
        kb_file: str | Path,
        output_file: str | Path,
        *,
        incremental: bool = True,
    ) -> dict[str, Any]:
        knowledge_path = Path(kb_file).expanduser().resolve()
        output_path = Path(output_file).expanduser().resolve()
        if not knowledge_path.is_file():
            raise FileNotFoundError(f"找不到语义知识库: {knowledge_path}")
        with knowledge_path.open("r", encoding="utf-8") as stream:
            knowledge_base = json.load(stream)
        if not isinstance(knowledge_base, dict):
            raise ValueError("knowledge_base.json 必须是 JSON 对象")
        entries = self._collect_effect_code(knowledge_base)
        current_keys = build_expected_code_semantic_keys(knowledge_base)
        existing = None
        if incremental:
            try:
                existing = validate_code_semantic_assets(output_path.parent)
            except (OSError, ValueError):
                existing = None
        if existing is not None:
            key_to_idx = dict(existing["index"])
            stale_keys = set(key_to_idx).difference(current_keys)
            if (
                stale_keys
                or int(existing["shape"][1]) != self._get_expected_dimension()
            ):
                existing = None
        rebuilt = existing is None
        added_count = 0
        if rebuilt:
            keys = [key for key, _ in entries]
            codes = [code for _, code in entries]
            if codes:
                embeddings = np.asarray(
                    self._get_model().encode(
                        codes,
                        batch_size=128,
                        show_progress_bar=False,
                    ),
                    dtype=np.float32,
                )
            else:
                embeddings = np.zeros(
                    (0, self._get_expected_dimension()),
                    dtype=np.float32,
                )
            key_to_idx = {key: index for index, key in enumerate(keys)}
            added_count = len(keys)
        else:
            key_to_idx = dict(existing["index"])
            new_entries = [entry for entry in entries if entry[0] not in key_to_idx]
            if not new_entries:
                validated = validate_semantic_bundle(
                    output_path.parent,
                    knowledge_base_filename=knowledge_path.name,
                )
                return {
                    "rebuilt": False,
                    "added_effect_slot_count": 0,
                    "effect_slot_count": validated["effect_slot_count"],
                    "embedding_shape": list(validated["shape"]),
                }
            embeddings = np.load(existing["embedding_path"], allow_pickle=False)
            new_vectors = np.asarray(
                self._get_model().encode(
                    [code for _, code in new_entries],
                    batch_size=128,
                    show_progress_bar=False,
                ),
                dtype=embeddings.dtype,
            )
            start_index = int(embeddings.shape[0])
            embeddings = np.concatenate((embeddings, new_vectors), axis=0)
            for offset, (key, _) in enumerate(new_entries):
                key_to_idx[key] = start_index + offset
            added_count = len(new_entries)
        self._write_embedding_pair(output_path, embeddings, key_to_idx)
        validated = validate_semantic_bundle(
            output_path.parent,
            knowledge_base_filename=knowledge_path.name,
        )
        return {
            "rebuilt": rebuilt,
            "added_effect_slot_count": added_count,
            "effect_slot_count": validated["effect_slot_count"],
            "embedding_shape": list(validated["shape"]),
        }
