# 只读提取 Core V4 的推理协议快照并输出供 apply_patch 使用的补丁

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
import subprocess
from pathlib import Path


# 读取完整源码及抽象语法树且不导入或执行 Core 模块
def read_source(root, name):
    source = (root / name).read_text(encoding="utf-8-sig")
    return source, ast.parse(source)


# 选取指定定义并保留原有源码及注释
def select_source(source, tree, names):
    lines = source.splitlines()
    return "\n\n".join(
        "\n".join(lines[node.lineno - 1:node.end_lineno])
        for node in tree.body
        if getattr(node, "name", None) in names
        or isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(isinstance(target, ast.Name) and target.id in names
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target]))
    ) + "\n"


# 为所有新增函数补充中文功能注释并保留快照来源
def annotate(source, label):
    lines = source.replace("。", "").splitlines()
    tree = ast.parse("\n".join(lines))
    additions = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node)
            description = doc.splitlines()[0] if doc and any("\u4e00" <= c <= "\u9fff" for c in doc.splitlines()[0]) else f"复用 Core V4 的 {node.name} 协议逻辑"
            start = min([node.lineno] + [item.lineno for item in node.decorator_list]) - 1
            additions[start] = " " * node.col_offset + "# " + description
    for index in sorted(additions, reverse=True):
        lines.insert(index, additions[index])
    return f"# {label}，固定复用 Core V4 协议且不依赖外部 Core 目录\n\n" + "\n".join(lines) + "\n"


# 仅将 Core 内部导入改为隔离的相对导入
def relative_imports(source, modules):
    import re
    for module in modules:
        source = re.sub(rf"(?m)^(\s*)from {module} import ", rf"\1from .{module} import ", source)
    return source


class NumpyCalls(ast.NodeTransformer):
    # 将编码器的张量包装替换为数值等价的 NumPy 包装
    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "torch":
            if node.func.attr in {"from_numpy", "tensor", "ones"}:
                method = "ones" if node.func.attr == "ones" else "asarray"
                node.func = ast.Attribute(ast.Name("np", ast.Load()), method, ast.Load())
        if isinstance(node.func, ast.Attribute) and node.func.attr == "unsqueeze":
            return ast.copy_location(ast.Call(ast.Attribute(ast.Name("np", ast.Load()), "expand_dims", ast.Load()), [node.func.value, *node.args], []), node)
        return node

    # 将 Torch 的协议存储类型映射到对应 NumPy 类型
    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.value, ast.Name) and node.value.id == "torch":
            node.value.id = "np"
            if node.attr == "long":
                node.attr = "int64"
            elif node.attr == "bool":
                node.attr = "bool_"
        return node


# 生成不包含训练入口或源资产编译器的独立协议文件
def build_snapshot(root):
    outputs, origins = {}, {}
    modules = {
        "data_types", "deck_protocol", "game_constants", "event_history", "public_observation",
        "core_message_protocol", "public_hint_semantics", "semantic_lookup", "card_vocab",
        "protocol_schema", "deck_encoder", "event_encoder", "public_context_encoder",
        "auxiliary_heads", "auxiliary_targets", "goal_planner", "feature_encoder", "galatea_net",
        "card_reader",
    }
    full = {"data_types", "deck_protocol", "game_constants", "event_history", "public_observation",
            "core_message_protocol", "deck_encoder", "event_encoder", "public_context_encoder",
            "auxiliary_heads", "goal_planner", "protocol_schema", "galatea_net"}
    for name in sorted(full):
        source, tree = read_source(root, f"{name}.py")
        origins[f"{name}.py"] = hashlib.sha256(source.encode()).hexdigest()
        # 不携带命令行自检块
        for node in reversed(tree.body):
            if isinstance(node, ast.If) and "__name__" in ast.unparse(node.test):
                source = "\n".join(source.splitlines()[:node.lineno - 1]) + "\n"
        source = relative_imports(source, modules)
        if name == "protocol_schema":
            source = source.replace("from .auxiliary_heads import", "from .schema_constants import").replace("from .goal_planner import", "from .schema_constants import").replace("from .deck_encoder import", "from .schema_constants import")
        outputs[f"{name}.py"] = annotate(source, f"V4 {name} 推理协议快照")

    source, tree = read_source(root, "auxiliary_targets.py")
    origins["auxiliary_targets.py"] = hashlib.sha256(source.encode()).hexdigest()
    target_names = {"AUXILIARY_TARGET_FORMAT_VERSION", "PLAN_PAIR_ROLE_NONE", "PLAN_PAIR_ROLE_FIRST", "PLAN_PAIR_ROLE_SECOND", "AuxiliaryHorizon", "AUXILIARY_HORIZON_COUNT", "AUXILIARY_LP_DIM", "AUXILIARY_ZONE_DIM", "AUXILIARY_CHAIN_DIM"}
    outputs["auxiliary_targets.py"] = annotate("from enum import IntEnum\nfrom .event_history import TRANSITION_ZONE_NAMES\n\n" + select_source(source, tree, target_names), "V4 辅助头推理所需的维度与枚举")
    constant_parts = ["from .auxiliary_targets import *\nDECK_LATENT_COUNT = 8\nDECK_SECTION_COUNT = 4\n"]
    for name in ("auxiliary_heads", "goal_planner"):
        source, tree = read_source(root, f"{name}.py")
        names = {target.id for node in tree.body if isinstance(node, ast.Assign) for target in node.targets if isinstance(target, ast.Name) and target.id.isupper()}
        constant_parts.append(select_source(source, tree, names))
    outputs["schema_constants.py"] = annotate("\n".join(constant_parts), "V4 结构哈希使用的纯数值常量")

    source, tree = read_source(root, "card_vocab.py")
    origins["card_vocab.py"] = hashlib.sha256(source.encode()).hexdigest()
    names = {"CARD_VOCAB_FILENAME", "CARD_VOCAB_FORMAT_VERSION", "FIRST_CARD_TOKEN_ID", "CARD_VOCAB_RESERVED_TOKENS", "MAX_CARD_VOCAB_FILE_BYTES", "_canonical_mapping_payload", "calculate_card_vocab_hash", "CardVocabulary", "load_card_vocabulary"}
    source = "import hashlib\nimport json\nfrom pathlib import Path\nDEFAULT_CARD_VOCAB_CAPACITY = 20000\n\n" + select_source(source, tree, names)
    source += '\n# 读取当前推理上下文的精确词表\ndef get_default_card_vocabulary():\n    from .runtime_context import current_assets\n    return current_assets().vocabulary\n'
    outputs["card_vocab.py"] = annotate(source, "V4 只读精确词表及前缀校验")

    source, tree = read_source(root, "semantic_lookup.py")
    origins["semantic_lookup.py"] = hashlib.sha256(source.encode()).hexdigest()
    names = {"STATIC_SEMANTIC_LOOKUP_FORMAT_VERSION", "SEMANTIC_EFFECT_SLOTS", "SEMANTIC_CATEGORY_SLOTS", "SEMANTIC_REQUIREMENT_SLOTS", "SEMANTIC_RELATION_SLOTS", "SEMANTIC_REQUIREMENT_CAPACITY", "SEMANTIC_CATEGORY_CAPACITY", "MAX_STATIC_SEMANTIC_TABLE_BYTES", "MAX_STATIC_SEMANTIC_CATALOG_BYTES", "_canonical_array_bytes", "_sha256_file", "_compiled_asset_paths", "_deserialize_runtime_bindings", "_deserialize_card_slots", "_read_compiled_catalog", "_validate_static_npz_container", "StaticSemanticLookup", "_COMPILED_ARRAY_NAMES", "load_static_semantic_assets"}
    source = "import hashlib\nimport json\nimport zipfile\nfrom pathlib import Path\nimport numpy as np\nfrom .card_vocab import FIRST_CARD_TOKEN_ID, get_default_card_vocabulary\nfrom .public_hint_semantics import PUBLIC_HINT_BINDING_FORMAT_VERSION, serialize_public_hint_catalog, deserialize_public_hint_catalog\nSTATIC_SEMANTIC_TABLE_FILENAME = 'semantic_lookup_v1.npz'\nSTATIC_SEMANTIC_CATALOG_FILENAME = 'semantic_lookup_v1.json'\nCODE_EMBEDDINGS_FILENAME = 'code_embeddings.npy'\nCODE_EMBEDDINGS_INDEX_FILENAME = 'code_embeddings_idx.json'\n\n" + select_source(source, tree, names)
    source = source.replace("from code_semantic_provenance import CODE_EMBEDDINGS_META_FILENAME", "CODE_EMBEDDINGS_META_FILENAME = 'code_embeddings_meta.json'")
    source = source.replace("    _register_lookup_catalogs(semantic_card_slots, runtime_effect_bindings, public_hint_bindings)\n", "")
    source += '\n# 读取显式绑定的编译语义且禁止自动重建\ndef get_static_semantic_lookup(directory=".", *, card_vocabulary=None, **kwargs):\n    from .runtime_context import current_assets\n    assets = current_assets()\n    return assets.lookup\n\n# 编码器通过推理上下文使用轻量目录而不修改进程全局目录\ndef register_static_semantic_runtime_catalog(**kwargs):\n    return get_static_semantic_lookup().runtime_effect_bindings\n'
    outputs["semantic_lookup.py"] = annotate(source, "V4 编译语义只读加载与逻辑身份校验")

    source, tree = read_source(root, "public_hint_semantics.py")
    origins["public_hint_semantics.py"] = hashlib.sha256(source.encode()).hexdigest()
    source = select_source(source, tree, {"PUBLIC_HINT_BINDING_FORMAT_VERSION", "serialize_public_hint_catalog", "deserialize_public_hint_catalog"})
    source += '\n# 在当前模型资产中解析提示的精确来源\ndef resolve_public_hint(desc):\n    from .runtime_context import current_assets\n    return current_assets().lookup.public_hint_bindings.get(int(desc), (0, -1, 0))\n'
    outputs["public_hint_semantics.py"] = annotate(source, "V4 提示目录身份与只读来源查询")

    source, tree = read_source(root, "feature_encoder.py")
    origins["feature_encoder.py"] = hashlib.sha256(source.encode()).hexdigest()
    tree = NumpyCalls().visit(tree)
    # visit_Attribute 在 visit_Call 前执行，因此包装函数统一按 NumPy 名称处理
    rendered = ast.unparse(ast.fix_missing_locations(tree)).replace("import torch\n", "")
    rendered = rendered.replace("np.from_numpy(", "np.asarray(").replace("np.tensor(", "np.asarray(")
    rendered = relative_imports(rendered, modules)
    rendered = rendered.split("if __name__ == '__main__':")[0]
    rendered = rendered.replace("GalateaEncoder", "CoreV4NumpyEncoder")
    outputs["feature_encoder.py"] = annotate(rendered, "V4 字段构造快照的纯 NumPy 数值等价版本")
    return outputs, origins


# 输出补丁供工具应用且不向 Core 或 Link 直接写入文件
def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("core_root", type=Path)
    parser.add_argument("--module", action="append")
    args = parser.parse_args()
    commit = subprocess.run(["git", "-C", str(args.core_root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    if commit != "1b0fa025994b6eabb8c58dc1ed2090bfbea28d04":
        raise RuntimeError("Core 来源提交发生变化，请先审查协议差异再更新快照生成器")
    outputs, origins = build_snapshot(args.core_root.resolve())
    outputs["source_manifest.json"] = json.dumps({"source": "Galatea-Core", "commit": "1b0fa025994b6eabb8c58dc1ed2090bfbea28d04", "model_protocol_version": 4, "schema_revision": 14, "files_sha256": origins}, ensure_ascii=False, indent=2) + "\n"
    print("*** Begin Patch")
    for name, source in outputs.items():
        if args.module and name not in args.module:
            continue
        print(f"*** Add File: model_protocols/v4/{name}")
        print("\n".join("+" + line for line in source.splitlines()))
    print("*** End Patch")


if __name__ == "__main__":
    main()
