# 模型协议 V3 Lua 语义解析模块，增量构建卡片效果知识库和 Hash 映射

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from .effect_slot_binding import apply_runtime_effect_bindings
from .semantic_assets import (
    CODE_SEMANTIC_FILENAMES,
    HASH_MAPPING_FILENAME,
    download_remote_semantic_bundle,
)


# 按首次出现顺序去重并稳定效果槽内容
def _stable_unique(values: list[Any]) -> list[Any]:
    return list(dict.fromkeys(values))


class YGOProLuaSemanticParser:
    # 初始化 YGOPro Lua 脚本目录和自定义效果索引
    def __init__(self, script_dir: str | Path) -> None:
        self.script_dir = Path(script_dir).expanduser().resolve()
        self.hash_registry: defaultdict[str, dict[str, Any]] = defaultdict(
            lambda: {"cards": [], "sample_code": ""}
        )

    # 从已有知识库重建可供增量解析接续的 Hash 索引
    def _rebuild_hash_registry(self, knowledge_base: dict[str, Any]) -> None:
        for card_id, card_data in knowledge_base.items():
            for effect in card_data.get("effects", []):
                slot = int(effect.get("slot", 1) or 1)
                card_label = f"{card_id}_E{slot}"
                for category in effect.get("categories", []):
                    if not str(category).startswith("CUSTOM_HASH_"):
                        continue
                    record = self.hash_registry[str(category)]
                    if card_label not in record["cards"]:
                        record["cards"].append(card_label)

    # 刷新已有卡片的 Lua 对象绑定而不改变旧语义标签
    @staticmethod
    def _refresh_card_runtime_bindings(
        filepath: Path,
        card_data: dict[str, Any],
        card_id: int,
    ) -> bool:
        try:
            lua_source = filepath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return False
        return apply_runtime_effect_bindings(card_data, lua_source, card_id)

    # 将无法归类的 Lua 操作代码规范化为稳定 Hash 标签
    def _hash_code_block(
        self,
        code_block: str,
        card_id: int,
        slot_idx: int,
    ) -> tuple[str, dict[str, list[str]]]:
        if not code_block:
            return "CUSTOM_HASH_EMPTY", {
                "numbers": [],
                "hexes": [],
                "constants": [],
            }
        extracted_numbers = re.findall(r"\b\d+\b", code_block)
        extracted_hexes = re.findall(r"0x[0-9a-fA-F]+", code_block)
        extracted_constants = re.findall(
            r"(RACE|ATTRIBUTE|CATEGORY|LOCATION|TYPE|PHASE|POS)_[A-Z_]+",
            code_block,
        )
        clean_code = code_block
        clean_code = re.sub(r"\b1\s*-\s*tp\b", "<OPPO>", clean_code)
        clean_code = re.sub(r"\b(tp|ep|rp)\b", "<PLAYER>", clean_code)
        clean_code = re.sub(
            r"\b(?:c\d+|s)\.[a-zA-Z0-9_]+\b",
            "<FUNC>",
            clean_code,
        )
        clean_code = re.sub(r"e:GetHandler\(\)", "<CARD>", clean_code)
        clean_code = re.sub(
            r"local\s+[a-zA-Z0-9_,\s]+\s*=",
            "=",
            clean_code,
        )
        clean_code = re.sub(r"~=\s*nil", "", clean_code)
        clean_code = re.sub(r"==\s*true", "", clean_code)
        clean_code = re.sub(r">\s*0", "", clean_code)
        clean_code = re.sub(r"\b(true|false)\b", "<BOOL>", clean_code)
        clean_code = re.sub(
            r"\b(tc|g|e|c|eg|ev|re|r|chk|chkc|mat|tg)\b",
            "<VAR>",
            clean_code,
        )
        clean_code = re.sub(r"\b\d+\b", "<NUM>", clean_code)
        clean_code = re.sub(r"0x[0-9a-fA-F]+", "<HEX>", clean_code)
        clean_code = re.sub(
            r"(RACE|ATTRIBUTE|CATEGORY|LOCATION|TYPE|PHASE|POS)_[A-Z_]+",
            "<CONST>",
            clean_code,
        )
        clean_code = re.sub(r"\s+", "", clean_code)
        hash_value = hashlib.md5(clean_code.encode("utf-8")).hexdigest()[:8]
        tag_name = f"CUSTOM_HASH_{hash_value.upper()}"
        card_label = f"{card_id}_E{slot_idx}"
        self.hash_registry[tag_name]["cards"].append(card_label)
        if not self.hash_registry[tag_name]["sample_code"]:
            self.hash_registry[tag_name]["sample_code"] = clean_code
        return tag_name, {
            "numbers": extracted_numbers,
            "hexes": extracted_hexes,
            "constants": extracted_constants,
        }

    # 解析单个 YGOPro 卡片脚本并生成固定效果槽语义
    def parse_file(self, filepath: str | Path) -> dict[str, Any] | None:
        path = Path(filepath)
        content = path.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"c(\d+)\.lua", path.name)
        if not match:
            return None
        card_id = int(match.group(1))
        card_data: dict[str, Any] = {
            "id": card_id,
            "summon_conditions": [],
            "effects": [],
        }
        procedure_pattern = (
            r"aux\.Add(Fusion|Synchro|Xyz|Link|Ritual|Pendulum)"
            r"[A-Za-z0-9_]*\((.*)\)"
        )
        for procedure_match in re.finditer(procedure_pattern, content):
            card_data["summon_conditions"].append(
                {
                    "type": procedure_match.group(1).upper(),
                    "raw_args": procedure_match.group(2).strip(),
                }
            )
        initial_start = content.find(".initial_effect(c)")
        if initial_start == -1:
            return card_data
        next_function = content.find("\nfunction ", initial_start)
        initial_body = (
            content[initial_start:]
            if next_function == -1
            else content[initial_start:next_function]
        )
        effect_creations = re.finditer(
            r"local\s+(e\d*)\s*=\s*Effect\.CreateEffect\(c\)",
            initial_body,
        )
        slot_idx = 1
        for creation in effect_creations:
            effect_name = creation.group(1)
            effect_slot: dict[str, Any] = {
                "slot": slot_idx,
                "type": [],
                "code": [],
                "range": [],
                "categories": [],
                "requirements": {
                    "setcodes": [],
                    "races": [],
                    "attributes": [],
                    "types": [],
                    "summon_types": [],
                    "locations": [],
                    "phases": [],
                    "reasons": [],
                    "positions": [],
                },
                "ref_codes": [],
            }
            property_pattern = rf"{effect_name}:Set([A-Za-z0-9_]+)\((.*?)\)"
            for property_match in re.finditer(property_pattern, initial_body):
                property_name = property_match.group(1)
                property_value = property_match.group(2)
                if property_name == "Type":
                    effect_slot["type"] = re.findall(
                        r"EFFECT_TYPE_[A-Z0-9_]+",
                        property_value,
                    )
                elif property_name == "Code":
                    effect_slot["code"] = [property_value.strip()]
                elif property_name == "Range":
                    effect_slot["range"] = re.findall(
                        r"LOCATION_[A-Z_]+",
                        property_value,
                    )
                elif property_name == "Category":
                    effect_slot["categories"].extend(
                        re.findall(r"CATEGORY_[A-Z_]+", property_value)
                    )
            bound_functions = []
            for function_type in ("Condition", "Cost", "Target", "Operation"):
                function_match = re.search(
                    rf"{effect_name}:Set{function_type}\((.*?)\)",
                    initial_body,
                )
                if function_match:
                    bound_functions.append(function_match.group(1).strip())
            pending_functions = list(bound_functions)
            processed_functions: set[str] = set()
            function_bodies = ""
            operation_code = ""
            while pending_functions:
                function_name = pending_functions.pop(0)
                if function_name in processed_functions:
                    continue
                processed_functions.add(function_name)
                function_start = content.find(f"function {function_name}(")
                if function_start == -1:
                    continue
                function_end = content.find("\nfunction ", function_start + 10)
                body = (
                    content[function_start:]
                    if function_end == -1
                    else content[function_start:function_end]
                )
                function_bodies += body + "\n"
                if "Operation" in function_name or "op" in function_name.lower():
                    operation_code += body + "\n"
                for sub_function in re.findall(
                    r"(?:c\d+|s)\.[a-zA-Z0-9_]+",
                    body,
                ):
                    if (
                        sub_function not in processed_functions
                        and sub_function not in pending_functions
                    ):
                        pending_functions.append(sub_function)
            requirements = effect_slot["requirements"]
            effect_slot["categories"].extend(
                re.findall(r"CATEGORY_[A-Z_]+", function_bodies)
            )
            requirements["setcodes"].extend(
                re.findall(
                    r"IsSetCard\((0x[0-9a-fA-F]+|[0-9]+)\)",
                    function_bodies,
                )
            )
            for key, pattern in (
                ("races", r"RACE_[A-Z_]+"),
                ("attributes", r"ATTRIBUTE_[A-Z_]+"),
                ("types", r"TYPE_[A-Z_]+"),
                ("summon_types", r"SUMMON_TYPE_[A-Z_]+"),
                ("locations", r"LOCATION_[A-Z_]+"),
                ("phases", r"PHASE_[A-Z0-9_]+"),
                ("reasons", r"REASON_[A-Z_]+"),
                ("positions", r"POS_[A-Z_]+"),
            ):
                requirements[key].extend(re.findall(pattern, function_bodies))
            effect_slot["categories"] = _stable_unique(effect_slot["categories"])
            for key in (
                "setcodes",
                "races",
                "attributes",
                "types",
                "summon_types",
                "locations",
                "phases",
                "reasons",
                "positions",
            ):
                requirements[key] = _stable_unique(requirements[key])
            if not effect_slot["categories"]:
                hash_tag, custom_parameters = self._hash_code_block(
                    operation_code,
                    card_id,
                    slot_idx,
                )
                effect_slot["categories"].append(hash_tag)
                requirements["custom_numbers"] = custom_parameters["numbers"]
                requirements["custom_hexes"] = custom_parameters["hexes"]
            else:
                requirements["custom_numbers"] = []
                requirements["custom_hexes"] = []
            effect_slot["raw_code"] = (
                function_bodies + "\n" + operation_code
            ).strip()
            card_data["effects"].append(effect_slot)
            slot_idx += 1
        apply_runtime_effect_bindings(card_data, content, card_id)
        return card_data

    # 批量增量解析脚本并原子写出知识库与 Hash 映射
    def run_batch(
        self,
        output_file: str | Path,
        *,
        clear_existing: bool = False,
        remote_url: str | None = None,
    ) -> dict[str, int]:
        output_path = Path(output_file).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        mapping_path = output_path.with_name(HASH_MAPPING_FILENAME)
        knowledge_base: dict[str, Any] = {}
        if clear_existing:
            for path in (
                output_path,
                mapping_path,
                *(output_path.with_name(name) for name in CODE_SEMANTIC_FILENAMES),
            ):
                path.unlink(missing_ok=True)
            self.hash_registry.clear()
        elif remote_url:
            bundle = download_remote_semantic_bundle(remote_url, output_path.parent)
            knowledge_base = bundle["knowledge_base"]
            remote_mapping = bundle.get("hash_mapping")
            if remote_mapping is not None:
                for key, value in remote_mapping.items():
                    self.hash_registry[key] = value
            else:
                self._rebuild_hash_registry(knowledge_base)
        elif output_path.is_file():
            with output_path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
            if not isinstance(loaded, dict):
                raise ValueError("knowledge_base.json 必须是 JSON 对象")
            knowledge_base = loaded
            if mapping_path.is_file():
                with mapping_path.open("r", encoding="utf-8") as stream:
                    old_registry = json.load(stream)
                if not isinstance(old_registry, dict):
                    raise ValueError("hash_mapping_report.json 必须是 JSON 对象")
                for key, value in old_registry.items():
                    self.hash_registry[key] = value
            else:
                self._rebuild_hash_registry(knowledge_base)
        if not self.script_dir.is_dir():
            raise FileNotFoundError(f"找不到 Lua 脚本目录: {self.script_dir}")
        old_hashes = set(self.hash_registry)
        old_hash_card_counts = {
            key: len(value["cards"])
            for key, value in self.hash_registry.items()
        }
        added_count = 0
        skipped_count = 0
        binding_update_count = 0
        for filepath in sorted(self.script_dir.glob("*.lua")):
            match = re.fullmatch(r"c(\d+)\.lua", filepath.name)
            if not match:
                continue
            card_id = match.group(1)
            if card_id in knowledge_base:
                if self._refresh_card_runtime_bindings(
                    filepath,
                    knowledge_base[card_id],
                    int(card_id),
                ):
                    binding_update_count += 1
                skipped_count += 1
                continue
            parsed = self.parse_file(filepath)
            if parsed and parsed["effects"]:
                knowledge_base[card_id] = parsed
                added_count += 1
        new_hashes = set(self.hash_registry).difference(old_hashes)
        merged_into_old = sum(
            len(self.hash_registry[key]["cards"]) - old_hash_card_counts[key]
            for key in old_hashes
        )
        self._write_json_atomically(output_path, knowledge_base)
        self._write_json_atomically(mapping_path, dict(self.hash_registry))
        return {
            "card_count": len(knowledge_base),
            "added_card_count": added_count,
            "skipped_card_count": skipped_count,
            "binding_update_count": binding_update_count,
            "new_hash_count": len(new_hashes),
            "merged_into_existing_hash_count": merged_into_old,
        }

    # 使用同目录临时文件原子保存语义 JSON
    @staticmethod
    def _write_json_atomically(path: Path, payload: Any) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
                encoding="utf-8",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
