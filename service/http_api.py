# Link HTTP 与 WebSocket 服务模块，向 WebUI 和外部插件提供稳定接口

from __future__ import annotations

import asyncio
import importlib.util
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any, Mapping

from aiohttp import WSMsgType, web

from app_config import ServiceConfig, load_app_config
from core.link_events import EventStreamClosed
from service.asset_manager import (
    DEFAULT_CDB_URL,
    DEFAULT_SCRIPT_REPOSITORY,
    DEFAULT_SEMANTIC_URL,
)
from service.model_repository import MAX_PACKAGE_BYTES, validate_gkg_filename
from service.session_manager import LinkSessionManager, SessionNotRunningError
from link_version import __version__


API_VERSION = "galatea.link.api.v1"
MANAGER_KEY = web.AppKey("galatea_link_manager", LinkSessionManager)
CONFIG_KEY = web.AppKey("galatea_link_service_config", ServiceConfig)
TOKEN_KEY = web.AppKey("galatea_link_api_token", str)
WEBUI_ROOT = Path(__file__).resolve().parent / "webui"
BRAND_LOGO_PATH = WEBUI_ROOT.parents[1] / "docs" / "logo.png"


class ApiRequestError(ValueError):
    # 初始化带稳定错误码的客户端请求异常
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# 从明文配置或环境变量读取服务访问令牌
def resolve_api_token(config: ServiceConfig) -> str:
    configured = config.api_token.strip()
    if configured:
        return configured
    if not config.api_token_env:
        return ""
    return os.environ.get(config.api_token_env, "").strip()


# 拒绝无令牌的非本机监听配置
def validate_service_security(config: ServiceConfig, token: str) -> None:
    local_hosts = {"127.0.0.1", "::1", "localhost"}
    if config.host.casefold() not in local_hosts and not token:
        raise ValueError("Link 对外监听时必须配置 service.api_token 或令牌环境变量")


# 构造统一的 JSON 成功响应
def _json_response(data: Any, *, status: int = 200) -> web.Response:
    return web.json_response(
        {"api_version": API_VERSION, "data": data},
        status=status,
    )


# 构造不会泄露内部堆栈的 JSON 错误响应
def _error_response(code: str, message: str, status: int) -> web.Response:
    return web.json_response(
        {
            "api_version": API_VERSION,
            "error": {"code": code, "message": message},
        },
        status=status,
    )


# 判断浏览器来源是否属于同源或显式许可列表
def _origin_allowed(request: web.Request, config: ServiceConfig) -> bool:
    origin = request.headers.get("Origin", "").strip()
    if not origin:
        return True
    same_origin = f"{request.scheme}://{request.host}"
    return origin == same_origin or origin in config.allowed_origins


# 为显式许可的浏览器来源添加最小跨域响应头
def _apply_cors_headers(
    request: web.Request,
    response: web.StreamResponse,
) -> web.StreamResponse:
    origin = request.headers.get("Origin", "").strip()
    config = request.app[CONFIG_KEY]
    if origin and origin in config.allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Vary"] = "Origin"
    return response


# 从请求头或受保护 Cookie 读取访问令牌
def _request_token(request: web.Request) -> str:
    authorization = request.headers.get("Authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.casefold() == "bearer":
        return value.strip()
    return request.cookies.get("galatea_link_token", "").strip()


# 统一执行来源校验、令牌鉴权和异常转换
@web.middleware
async def api_middleware(
    request: web.Request,
    handler: Any,
) -> web.StreamResponse:
    if not request.path.startswith("/api/v1"):
        return await handler(request)

    config = request.app[CONFIG_KEY]
    if not _origin_allowed(request, config):
        return _error_response("origin_not_allowed", "请求来源未获授权", 403)

    if request.method == "OPTIONS":
        response = web.Response(status=204)
        response.headers["Access-Control-Allow-Methods"] = (
            "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        )
        response.headers["Access-Control-Allow-Headers"] = (
            "Authorization, Content-Type"
        )
        return _apply_cors_headers(request, response)

    public_paths = {"/api/v1/health", "/api/v1/capabilities"}
    token = request.app[TOKEN_KEY]
    if request.path not in public_paths and token:
        supplied = _request_token(request)
        if not supplied or not secrets.compare_digest(supplied, token):
            return _apply_cors_headers(
                request,
                _error_response("unauthorized", "访问令牌无效", 401),
            )

    try:
        response = await handler(request)
    except ApiRequestError as error:
        response = _error_response(error.code, str(error), error.status)
    except SessionNotRunningError as error:
        response = _error_response("session_not_running", str(error), 409)
    except PermissionError as error:
        response = _error_response("forbidden", str(error), 403)
    except FileExistsError as error:
        response = _error_response("already_exists", str(error), 409)
    except FileNotFoundError as error:
        response = _error_response("not_found", str(error), 404)
    except RuntimeError as error:
        response = _error_response("runtime_error", str(error), 409)
    except ValueError as error:
        response = _error_response("invalid_request", str(error), 400)
    except Exception:
        request.app.logger.exception("Galatea Link HTTP 请求处理失败")
        response = _error_response(
            "internal_error",
            "Link 服务内部错误",
            500,
        )
    return _apply_cors_headers(request, response)


# 读取并校验 JSON 对象请求体
async def _read_json_mapping(request: web.Request) -> Mapping[str, Any]:
    try:
        payload = await request.json()
    except Exception as error:
        raise ApiRequestError("invalid_json", "请求体必须是 JSON 对象") from error
    if not isinstance(payload, Mapping):
        raise ApiRequestError("invalid_json", "请求体必须是 JSON 对象")
    return payload


# 返回不触发模型加载的服务健康状态
async def handle_health(request: web.Request) -> web.Response:
    manager = request.app[MANAGER_KEY]
    return _json_response(
        {
            "service": "galatea-link",
            "version": __version__,
            "status": "ok",
            "session_state": manager.get_status()["state"],
        }
    )


# 返回外部接口与模型协议能力声明
async def handle_capabilities(request: web.Request) -> web.Response:
    return _json_response(
        {
            "external_api_versions": [API_VERSION],
            "link_version": __version__,
            "stable_model_protocols": [3],
            "game_protocol_profile": "ygopro",
            "game_protocol_family": "ygopro",
            "known_compatible_clients": ["mdpro3"],
            "transports": ["http", "websocket"],
            "model_management": {
                "catalog": True,
                "selection": True,
                "gkg_import": True,
                "gkg_package_format_versions": [2],
            },
            "asset_management": {
                "card_database_sync": True,
                "official_script_sync": True,
                "semantic_bundle_sync": True,
                "local_semantic_rebuild": True,
                "local_semantic_rebuild_optional_dependencies": True,
                "gkg_card_database": True,
                "gkg_meta_staples": True,
                "meta_staples_editing": True,
                "applies_to_model_protocol": 3,
            },
            "deck_management": {
                "catalog": True,
                "local_import": True,
                "local_delete": True,
                "local_card_edit": "pre_duel",
                "astrbot_import": True,
                "session_isolation": True,
                "current_temporary_edit": "pre_duel",
                "side_deck_sections": True,
                "live_bo3_sideboarding": False,
            },
            "configuration_management": {
                "offline_editing": True,
                "server_tcp_test": True,
                "llm_smoke_test": True,
                "secret_values_exposed": False,
                "secret_editing": True,
                "local_llm_api_key": True,
            },
            "remote_agent_decisions": {
                "supported": True,
                "protocol": "galatea.remote_decision.v1",
                "pending_observation": True,
                "submit_endpoint": "/api/v1/decisions/{request_id}",
            },
            "inference_backends": {
                "pytorch": (
                    "available"
                    if importlib.util.find_spec("torch") is not None
                    else "dependency_missing"
                ),
                "onnxruntime": (
                    "available"
                    if importlib.util.find_spec("onnxruntime") is not None
                    else "dependency_missing"
                ),
            },
        }
    )


# 建立浏览器 WebSocket 所需的同源鉴权 Cookie
async def handle_auth_session(request: web.Request) -> web.Response:
    response = _json_response({"authenticated": True})
    token = request.app[TOKEN_KEY]
    if token:
        response.set_cookie(
            "galatea_link_token",
            token,
            httponly=True,
            samesite="Strict",
            secure=request.secure,
            max_age=8 * 60 * 60,
        )
    return response


# 返回当前服务会话和游戏运行时状态
async def handle_status(request: web.Request) -> web.Response:
    return _json_response(request.app[MANAGER_KEY].get_status())


# 接收 AstrBot 插件心跳并返回 Link 会话状态
async def handle_astrbot_heartbeat(request: web.Request) -> web.Response:
    payload = await _read_json_mapping(request)
    result = request.app[MANAGER_KEY].record_astrbot_heartbeat(payload)
    return _json_response(result)


# 启动游戏会话且允许重复请求安全复用
async def handle_session_start(request: web.Request) -> web.Response:
    status = await request.app[MANAGER_KEY].start()
    return _json_response(status, status=202)


# 保存仅作用于下一次对局且不落盘的远程会话配置
async def handle_session_configure(request: web.Request) -> web.Response:
    payload = await _read_json_mapping(request)
    configuration = await request.app[MANAGER_KEY].configure_session(payload)
    return _json_response(configuration)


# 返回当前只存于内存且已经脱敏的远程会话配置
async def handle_session_configuration(request: web.Request) -> web.Response:
    configuration = request.app[MANAGER_KEY].get_session_configuration()
    return _json_response(configuration)


# 返回当前远程决策请求，供 AstrBot 断线恢复或主动轮询
async def handle_pending_decision(request: web.Request) -> web.Response:
    pending = request.app[MANAGER_KEY].get_pending_decision()
    return _json_response(pending)


# 停止游戏会话但保持远程服务继续运行
async def handle_session_stop(request: web.Request) -> web.Response:
    stopped = await request.app[MANAGER_KEY].stop()
    return _json_response({"stopped": stopped})


# 返回运行中的宏观控制接口快照
async def handle_controls_get(request: web.Request) -> web.Response:
    return _json_response(request.app[MANAGER_KEY].get_controls())


# 应用带乐观版本检查的宏观控制补丁
async def handle_controls_patch(request: web.Request) -> web.Response:
    payload = await _read_json_mapping(request)
    patch = payload.get("patch", payload)
    if not isinstance(patch, Mapping):
        raise ApiRequestError("invalid_controls", "patch 必须是 JSON 对象")
    expected_revision = payload.get("expected_revision")
    if expected_revision is not None:
        try:
            expected_revision = int(expected_revision)
        except (TypeError, ValueError) as error:
            raise ApiRequestError(
                "invalid_revision",
                "expected_revision 必须是整数",
            ) from error
    controls = await request.app[MANAGER_KEY].update_controls(
        patch,
        expected_revision=expected_revision,
    )
    return _json_response(controls)


# 返回脱敏后的服务器、Agent、LLM 和服务配置
async def handle_configuration_get(request: web.Request) -> web.Response:
    return _json_response(request.app[MANAGER_KEY].get_configuration())


# 保存下一次会话使用的非敏感配置补丁
async def handle_configuration_patch(request: web.Request) -> web.Response:
    payload = await _read_json_mapping(request)
    patch = payload.get("patch", payload)
    if not isinstance(patch, Mapping):
        raise ApiRequestError("invalid_configuration", "patch 必须是 JSON 对象")
    expected_revision = payload.get("expected_revision")
    if expected_revision is not None:
        try:
            expected_revision = int(expected_revision)
        except (TypeError, ValueError) as error:
            raise ApiRequestError(
                "invalid_revision",
                "expected_revision 必须是整数",
            ) from error
    configuration = await request.app[MANAGER_KEY].update_configuration(
        patch,
        expected_revision=expected_revision,
    )
    return _json_response(configuration)


# 保存或清除 Link 本机 LLM API Key 且不回传明文
async def handle_llm_api_key(request: web.Request) -> web.Response:
    payload = await _read_json_mapping(request)
    action = str(payload.get("action", "set")).strip().casefold()
    if action == "clear":
        api_key = None
    elif action == "set":
        api_key = payload.get("api_key")
        if not isinstance(api_key, str):
            raise ApiRequestError("invalid_api_key", "api_key 必须是字符串")
    else:
        raise ApiRequestError("invalid_secret_action", "action 必须是 set 或 clear")
    configuration = await request.app[MANAGER_KEY].update_llm_api_key(api_key)
    return _json_response(configuration)


# 使用服务器配置草稿检查 TCP 端口是否可达
async def handle_server_test(request: web.Request) -> web.Response:
    payload = await _read_json_mapping(request)
    patch = payload.get("patch", payload)
    if not isinstance(patch, Mapping):
        raise ApiRequestError("invalid_configuration", "patch 必须是 JSON 对象")
    result = await request.app[MANAGER_KEY].test_server_configuration(patch)
    return _json_response(result)


# 使用 LLM 配置草稿执行一次最小结构化输出请求
async def handle_llm_test(request: web.Request) -> web.Response:
    payload = await _read_json_mapping(request)
    patch = payload.get("patch", payload)
    if not isinstance(patch, Mapping):
        raise ApiRequestError("invalid_configuration", "patch 必须是 JSON 对象")
    result = await request.app[MANAGER_KEY].test_llm_configuration(patch)
    return _json_response(result)


# 返回模型协议 V3 仓库、当前选择和本地 GKG 包
async def handle_models_get(request: web.Request) -> web.Response:
    catalog = await asyncio.to_thread(
        request.app[MANAGER_KEY].get_model_catalog,
    )
    return _json_response(catalog)


# 返回只包含 Link 本地卡组的公开目录
async def handle_decks_get(request: web.Request) -> web.Response:
    catalog = await asyncio.to_thread(
        request.app[MANAGER_KEY].get_deck_catalog,
    )
    return _json_response(catalog)


# 接收 WebUI 提交的 YDK 文本并保存为 Link 本地卡组
async def handle_local_deck_import(request: web.Request) -> web.Response:
    payload = await _read_json_mapping(request)
    record = await request.app[MANAGER_KEY].import_local_deck(payload)
    return _json_response(record, status=201)


# 删除未被当前配置使用的 Link 本地卡组
async def handle_local_deck_delete(request: web.Request) -> web.Response:
    payload = await _read_json_mapping(request)
    deck_ref = payload.get("deck_ref")
    if not isinstance(deck_ref, str) or not deck_ref.strip():
        raise ApiRequestError("invalid_deck_ref", "deck_ref 必须是非空字符串")
    record = await request.app[MANAGER_KEY].delete_local_deck(deck_ref.strip())
    return _json_response(record)


# 按单卡操作修改停止状态下的 Link 本地卡组
async def handle_local_deck_patch(request: web.Request) -> web.Response:
    payload = await _read_json_mapping(request)
    deck_ref = payload.get("deck_ref")
    operations = payload.get("operations")
    if not isinstance(deck_ref, str) or not deck_ref.strip():
        raise ApiRequestError("invalid_deck_ref", "deck_ref 必须是非空字符串")
    if not isinstance(operations, list):
        raise ApiRequestError("invalid_deck_operations", "operations 必须是数组")
    record = await request.app[MANAGER_KEY].edit_local_deck(
        deck_ref.strip(),
        operations,
    )
    return _json_response(record)


# 返回当前 AstrBot 会话可见的本地和缓存卡组
async def handle_decks_query(request: web.Request) -> web.Response:
    payload = await _read_json_mapping(request)
    scope_id = payload.get("scope_id")
    if not isinstance(scope_id, str) or not scope_id.strip():
        raise ApiRequestError("invalid_deck_scope", "scope_id 必须是非空字符串")
    catalog = await asyncio.to_thread(
        request.app[MANAGER_KEY].get_deck_catalog,
        scope_id.strip(),
    )
    return _json_response(catalog)


# 导入当前 AstrBot 会话工具箱生成的 YDK 副本
async def handle_deck_import(request: web.Request) -> web.Response:
    payload = await _read_json_mapping(request)
    record = await request.app[MANAGER_KEY].import_astrbot_deck(payload)
    return _json_response(record, status=201)


# 将 Link 本地卡组复制到指定 AstrBot 会话隔离目录
async def handle_local_deck_copy(request: web.Request) -> web.Response:
    payload = await _read_json_mapping(request)
    scope_id = payload.get("scope_id")
    deck_ref = payload.get("deck_ref")
    instance_name = payload.get("instance_name", "AstrBot")
    if not isinstance(scope_id, str) or not scope_id.strip():
        raise ApiRequestError("invalid_deck_scope", "scope_id 必须是非空字符串")
    if not isinstance(deck_ref, str) or not deck_ref.strip():
        raise ApiRequestError("invalid_deck_ref", "deck_ref 必须是非空字符串")
    record = await request.app[MANAGER_KEY].copy_local_deck(
        scope_id.strip(),
        deck_ref.strip(),
        str(instance_name),
    )
    return _json_response(record, status=201)


# 修改当前远程会话选中的 AstrBot 临时对战卡组
async def handle_current_deck_patch(request: web.Request) -> web.Response:
    payload = await _read_json_mapping(request)
    scope_id = payload.get("scope_id")
    operations = payload.get("operations")
    if not isinstance(scope_id, str) or not scope_id.strip():
        raise ApiRequestError("invalid_deck_scope", "scope_id 必须是非空字符串")
    if not isinstance(operations, list):
        raise ApiRequestError("invalid_deck_operations", "operations 必须是数组")
    record = await request.app[MANAGER_KEY].edit_current_session_deck(
        scope_id,
        operations,
    )
    return _json_response(record)


# 保存下次 Link 会话使用的模型选择
async def handle_model_selection_patch(request: web.Request) -> web.Response:
    payload = await _read_json_mapping(request)
    filename = str(payload.get("filename", "")).strip()
    if not filename:
        raise ApiRequestError("missing_model", "filename 不能为空")
    expected_revision = payload.get("expected_revision")
    if expected_revision is not None:
        try:
            expected_revision = int(expected_revision)
        except (TypeError, ValueError) as error:
            raise ApiRequestError(
                "invalid_revision",
                "expected_revision 必须是整数",
            ) from error
    catalog = await request.app[MANAGER_KEY].select_model(
        filename,
        expected_revision=expected_revision,
    )
    return _json_response(catalog)


# 导入专用部署目录中已有的 GKG 包
async def handle_model_local_import(request: web.Request) -> web.Response:
    payload = await _read_json_mapping(request)
    filename = str(payload.get("filename", "")).strip()
    if not filename:
        raise ApiRequestError("missing_package", "filename 不能为空")
    activate = payload.get("activate", True)
    if not isinstance(activate, bool):
        raise ApiRequestError("invalid_activate", "activate 必须是布尔值")
    result = await request.app[MANAGER_KEY].import_local_model_package(
        filename,
        activate=activate,
    )
    return _json_response(result, status=201)


# 流式接收浏览器上传的 GKG 包并在验证后导入
async def handle_model_upload_import(request: web.Request) -> web.Response:
    if not request.content_type.startswith("multipart/"):
        raise ApiRequestError(
            "invalid_content_type",
            "GKG 导入必须使用 multipart/form-data",
        )
    reader = await request.multipart()
    package_name = None
    activate = True
    temporary_path = None
    total_size = 0
    try:
        while field := await reader.next():
            if field.name == "activate":
                raw_activate = (await field.text()).strip().casefold()
                activate = raw_activate not in {"0", "false", "no", "off"}
                continue
            if field.name != "package":
                continue
            if temporary_path is not None:
                raise ApiRequestError(
                    "duplicate_package",
                    "每次请求只能导入一个 GKG 包",
                )
            package_name = validate_gkg_filename(field.filename or "")
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=".web_gkg_upload_",
                suffix=".tmp",
                dir=request.app[MANAGER_KEY].get_model_package_directory(),
                delete=False,
            ) as output:
                temporary_path = Path(output.name)
                while True:
                    chunk = await field.read_chunk(size=1024 * 1024)
                    if not chunk:
                        break
                    total_size += len(chunk)
                    if total_size > MAX_PACKAGE_BYTES:
                        raise ApiRequestError(
                            "package_too_large",
                            "GKG 部署包超过 64 GiB 安全上限",
                            413,
                        )
                    output.write(chunk)
        if temporary_path is None or package_name is None or total_size <= 0:
            raise ApiRequestError("missing_package", "请求中缺少 GKG 文件")
        result = await request.app[MANAGER_KEY].import_model_package(
            temporary_path,
            package_name,
            activate=activate,
        )
        return _json_response(result, status=201)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


# 返回当前模型配套运行资产和 142 宣言兜底池状态
async def handle_assets_get(request: web.Request) -> web.Response:
    return _json_response(await request.app[MANAGER_KEY].get_asset_status())


# 同步 Link 自有 CDB 和官方 Lua 脚本目录
async def handle_card_data_sync(request: web.Request) -> web.Response:
    payload = await _read_json_mapping(request)
    force_scripts = payload.get("force_scripts", False)
    if not isinstance(force_scripts, bool):
        raise ApiRequestError("invalid_force_scripts", "force_scripts 必须是布尔值")
    result = await request.app[MANAGER_KEY].sync_card_data(
        cdb_url=str(payload.get("cdb_url") or DEFAULT_CDB_URL),
        script_repository=str(
            payload.get("script_repository") or DEFAULT_SCRIPT_REPOSITORY
        ),
        force_scripts=force_scripts,
    )
    return _json_response(result)


# 同步远程发布的完整模型协议 V3 语义资产
async def handle_semantic_assets_sync(request: web.Request) -> web.Response:
    payload = await _read_json_mapping(request)
    result = await request.app[MANAGER_KEY].sync_semantic_assets(
        str(payload.get("remote_url") or DEFAULT_SEMANTIC_URL)
    )
    return _json_response(result)


# 在完整环境中解析 Lua 并增量或全量生成模型协议 V3 语义资产
async def handle_semantic_assets_rebuild(request: web.Request) -> web.Response:
    payload = await _read_json_mapping(request)
    clear_existing = payload.get("clear_existing", False)
    if not isinstance(clear_existing, bool):
        raise ApiRequestError("invalid_clear_existing", "clear_existing 必须是布尔值")
    remote_value = payload.get("remote_url")
    if remote_value is not None and not isinstance(remote_value, str):
        raise ApiRequestError("invalid_remote_url", "remote_url 必须是字符串或空值")
    result = await request.app[MANAGER_KEY].rebuild_semantic_assets(
        remote_url=str(remote_value).strip() or None,
        clear_existing=clear_existing,
    )
    return _json_response(result)


# 修改当前模型资产目录中的 142 宣言兜底池
async def handle_meta_staples_patch(request: web.Request) -> web.Response:
    payload = await _read_json_mapping(request)
    card_codes = payload.get("card_codes")
    if not isinstance(card_codes, list):
        raise ApiRequestError("invalid_card_codes", "card_codes 必须是数组")
    result = await request.app[MANAGER_KEY].update_meta_staples(
        str(payload.get("operation") or ""),
        card_codes,
    )
    return _json_response(result)


# 返回最近一次传递给 LLM 的完整可见观察
async def handle_observation(request: web.Request) -> web.Response:
    observation = request.app[MANAGER_KEY].get_latest_observation()
    return _json_response(observation)


# 校验并提交远程 AstrBot 的当前合法动作
async def handle_remote_decision_submit(request: web.Request) -> web.Response:
    raw_request_id = request.match_info["request_id"]
    try:
        request_id = int(raw_request_id)
    except ValueError as error:
        raise ApiRequestError("invalid_request_id", "request_id 必须是整数") from error
    payload = await _read_json_mapping(request)
    result = await request.app[MANAGER_KEY].submit_remote_decision(
        request_id,
        payload,
    )
    return _json_response(result, status=202)


# 返回经过长度约束的游戏聊天历史
async def handle_chat_history(request: web.Request) -> web.Response:
    raw_limit = request.query.get("limit", "12")
    try:
        limit = int(raw_limit)
    except ValueError as error:
        raise ApiRequestError("invalid_limit", "limit 必须是整数") from error
    if not 1 <= limit <= 100:
        raise ApiRequestError("invalid_limit", "limit 必须位于 1 到 100")
    history = request.app[MANAGER_KEY].get_game_chat_history(limit)
    return _json_response(history)


# 将远程文本转发到游戏内聊天模块
async def handle_chat_send(request: web.Request) -> web.Response:
    payload = await _read_json_mapping(request)
    text = str(payload.get("text", "")).strip()
    if not text:
        raise ApiRequestError("empty_chat", "聊天文本不能为空")
    sent = await request.app[MANAGER_KEY].send_game_chat(text)
    return _json_response(sent, status=202)


# 将 AstrBot 等上层的社交消息投递到 Link 当前事件流
async def handle_external_message(request: web.Request) -> web.Response:
    payload = await _read_json_mapping(request)
    text = str(payload.get("text", "")).strip()
    source = str(payload.get("source", "external.client")).strip()
    sender_id = payload.get("sender_id")
    if not text:
        raise ApiRequestError("empty_external_message", "外部消息不能为空")
    if not source:
        raise ApiRequestError("empty_external_source", "外部消息来源不能为空")
    if sender_id is not None and not isinstance(sender_id, (str, int)):
        raise ApiRequestError("invalid_sender_id", "sender_id 必须是字符串或整数")
    event = await request.app[MANAGER_KEY].publish_external_message(
        text,
        source=source,
        sender_id=str(sender_id) if sender_id is not None else None,
    )
    return _json_response(event, status=202)


# 持续向单个远程客户端转发结构化 Link 事件
async def handle_events(request: web.Request) -> web.WebSocketResponse:
    manager = request.app[MANAGER_KEY]
    config = request.app[CONFIG_KEY]
    websocket = web.WebSocketResponse(heartbeat=30.0)
    await websocket.prepare(request)
    subscription = manager.subscribe_events(config.event_queue_size)
    await websocket.send_json(
        {
            "api_version": API_VERSION,
            "event": {
                "sequence": 0,
                "event_type": "service.connected",
                "payload": manager.get_status(),
            },
        }
    )
    event_task = None
    receive_task = None
    try:
        while not websocket.closed:
            event_task = asyncio.create_task(subscription.get())
            receive_task = asyncio.create_task(websocket.receive())
            done, pending = await asyncio.wait(
                {event_task, receive_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if event_task in done:
                event = event_task.result()
                await websocket.send_json(
                    {"api_version": API_VERSION, "event": event.to_dict()}
                )
            if receive_task in done:
                message = receive_task.result()
                if message.type in {
                    WSMsgType.CLOSE,
                    WSMsgType.CLOSED,
                    WSMsgType.ERROR,
                }:
                    break
    except EventStreamClosed:
        pass
    finally:
        subscription.close()
        for task in (event_task, receive_task):
            if task is not None and not task.done():
                task.cancel()
        await websocket.close()
    return websocket


# 接收由中间件完成的浏览器预检请求
async def handle_options(request: web.Request) -> web.Response:
    return web.Response(status=204)


# 返回 Link 自带的同源控制界面
async def handle_webui(request: web.Request) -> web.StreamResponse:
    config = request.app[CONFIG_KEY]
    if not config.webui_enabled:
        raise web.HTTPNotFound()
    return web.FileResponse(WEBUI_ROOT / "index.html")


# 返回文档目录中的 Link 品牌图片供控制台使用
async def handle_brand_logo(request: web.Request) -> web.StreamResponse:
    config = request.app[CONFIG_KEY]
    if not config.webui_enabled or not BRAND_LOGO_PATH.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(BRAND_LOGO_PATH)


# 在服务退出时回收模型、网络和事件资源
async def cleanup_service(app: web.Application) -> None:
    await app[MANAGER_KEY].close()


# 创建可测试且不立即监听端口的 aiohttp 应用
def create_http_app(
    manager: LinkSessionManager,
    config: ServiceConfig,
    *,
    api_token: str | None = None,
) -> web.Application:
    token = resolve_api_token(config) if api_token is None else api_token.strip()
    validate_service_security(config, token)
    app = web.Application(
        middlewares=[api_middleware],
        client_max_size=MAX_PACKAGE_BYTES + 16 * 1024 * 1024,
    )
    app[MANAGER_KEY] = manager
    app[CONFIG_KEY] = config
    app[TOKEN_KEY] = token
    app.add_routes(
        [
            web.get("/", handle_webui),
            web.get("/brand-logo.png", handle_brand_logo),
            web.get("/api/v1/health", handle_health),
            web.get("/api/v1/capabilities", handle_capabilities),
            web.post("/api/v1/auth/session", handle_auth_session),
            web.get("/api/v1/status", handle_status),
            web.post("/api/v1/integrations/astrbot/heartbeat", handle_astrbot_heartbeat),
            web.post("/api/v1/session/start", handle_session_start),
            web.post("/api/v1/session/configure", handle_session_configure),
            web.get("/api/v1/session/configuration", handle_session_configuration),
            web.post("/api/v1/session/stop", handle_session_stop),
            web.get("/api/v1/controls", handle_controls_get),
            web.patch("/api/v1/controls", handle_controls_patch),
            web.get("/api/v1/configuration", handle_configuration_get),
            web.patch("/api/v1/configuration", handle_configuration_patch),
            web.put("/api/v1/configuration/llm-api-key", handle_llm_api_key),
            web.post("/api/v1/configuration/server/test", handle_server_test),
            web.post("/api/v1/configuration/llm/test", handle_llm_test),
            web.get("/api/v1/models", handle_models_get),
            web.get("/api/v1/decks", handle_decks_get),
            web.post("/api/v1/decks/local", handle_local_deck_import),
            web.patch("/api/v1/decks/local", handle_local_deck_patch),
            web.delete("/api/v1/decks/local", handle_local_deck_delete),
            web.post("/api/v1/decks/query", handle_decks_query),
            web.post("/api/v1/decks/import", handle_deck_import),
            web.post("/api/v1/decks/copy-local", handle_local_deck_copy),
            web.patch("/api/v1/decks/current", handle_current_deck_patch),
            web.patch("/api/v1/models/selection", handle_model_selection_patch),
            web.post("/api/v1/models/import", handle_model_upload_import),
            web.post("/api/v1/models/import-local", handle_model_local_import),
            web.get("/api/v1/assets", handle_assets_get),
            web.post("/api/v1/assets/card-data/sync", handle_card_data_sync),
            web.post("/api/v1/assets/semantics/sync", handle_semantic_assets_sync),
            web.post("/api/v1/assets/semantics/rebuild", handle_semantic_assets_rebuild),
            web.patch("/api/v1/assets/meta-staples", handle_meta_staples_patch),
            web.get("/api/v1/observation", handle_observation),
            web.get("/api/v1/decisions/pending", handle_pending_decision),
            web.post("/api/v1/decisions/{request_id}", handle_remote_decision_submit),
            web.get("/api/v1/chat/history", handle_chat_history),
            web.post("/api/v1/chat", handle_chat_send),
            web.post("/api/v1/external-message", handle_external_message),
            web.get("/api/v1/events", handle_events),
            web.options("/api/v1/{tail:.*}", handle_options),
        ]
    )
    if config.webui_enabled:
        app.router.add_static(
            "/assets/",
            path=WEBUI_ROOT / "assets",
            name="webui-assets",
            show_index=False,
        )
    app.on_cleanup.append(cleanup_service)
    return app


# 从 YAML 配置启动独立 Link 网络服务
def run_http_service(config_path: str | Path = "config.yaml") -> None:
    resolved_path = Path(config_path).expanduser().resolve()
    app_config = load_app_config(resolved_path)
    manager = LinkSessionManager(resolved_path)
    app = create_http_app(manager, app_config.service)
    web.run_app(
        app,
        host=app_config.service.host,
        port=app_config.service.port,
        print=lambda message: print(f"🌐 {message}"),
    )
