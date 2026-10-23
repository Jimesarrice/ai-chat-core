"""配置存储：默认值（schema.py）← config.json 深合并 → 运行时 Config 对象。

- 保存时写全量（用户能直接打开看、改）；
- 读取时缺失的键自动落回默认值（升级新增字段不用手工补）；
- 支持变更监听（热生效：日志级别、间隔等在下一次使用时读到新值）。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from app.config.schema import Config
from app.logbus import get_logger

log = get_logger("app.config")


def deep_merge(base: dict, patch: dict) -> dict:
    """patch 覆盖 base；dict 递归合并，其它类型直接替换。"""
    out = dict(base)
    for k, v in patch.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class ConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._cfg: Config = Config()
        self._listeners: list[Callable[[Config], None]] = []
        self._last_error: str = ""

    # ---- 读取 ----
    def load(self) -> Config:
        with self._lock:
            raw: dict = {}
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                except Exception as e:
                    self._last_error = f"config.json 解析失败（用默认配置）：{e}"
                    log.error(self._last_error)
                    raw = {}
            merged = deep_merge(self._defaults(), raw)
            try:
                self._cfg = Config.model_validate(merged)
                self._last_error = ""
            except ValidationError as e:
                self._last_error = f"配置校验失败（用默认配置）：{e.errors()[:3]}"
                log.error(self._last_error)
                self._cfg = Config()
            return self._cfg

    @staticmethod
    def _defaults() -> dict:
        return Config().model_dump(mode="json")

    def get(self) -> Config:
        with self._lock:
            return self._cfg

    def last_error(self) -> str:
        return self._last_error

    # ---- 保存（部分补丁） ----
    def save_patch(self, patch: dict) -> Config:
        """把 patch 深合并进当前配置，校验通过后落盘并通知监听者。"""
        with self._lock:
            current = self._cfg.model_dump(mode="json")
            merged = deep_merge(current, patch)
            cfg = Config.model_validate(merged)  # 校验失败则抛错给 API 层
            self._cfg = cfg
            self._write(cfg)
            listeners = list(self._listeners)
        log.info("配置已保存：%s", ", ".join(_top_keys(patch)))
        for fn in listeners:
            try:
                fn(cfg)
            except Exception:
                log.exception("配置监听器出错")
        return cfg

    def replace_all(self, data: dict) -> Config:
        with self._lock:
            merged = deep_merge(self._defaults(), data)
            cfg = Config.model_validate(merged)
            self._cfg = cfg
            self._write(cfg)
            listeners = list(self._listeners)
        log.info("配置已整体替换")
        for fn in listeners:
            try:
                fn(cfg)
            except Exception:
                log.exception("配置监听器出错")
        return cfg

    def _write(self, cfg: Config) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(cfg.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    # ---- 监听 ----
    def on_change(self, fn: Callable[[Config], None]) -> None:
        self._listeners.append(fn)

    # ---- UI 用：schema ----
    def ui_schema(self) -> dict:
        schema = Config.model_json_schema()
        sections = _sections_of(schema)
        defs = schema.get("$defs", {})
        for s in sections:
            s["fields"] = _fields_of(defs.get(s["model"], {}), s["model"])
        return {
            "sections": sections,
            "defs": {name: _fields_of(def_schema, name) for name, def_schema in defs.items()},
            "current": self.get().model_dump(mode="json"),
            "error": self._last_error,
            "file": str(self.path),
        }


def _top_keys(d: dict) -> list[str]:
    return list(d.keys()) if isinstance(d, dict) else []


def _sections_of(schema: dict) -> list[dict]:
    """从 model_json_schema 抽出顶层 section 列表（带 label/desc/order）。"""
    out: list[dict] = []
    defs = schema.get("$defs", {})
    for name, field in schema.get("properties", {}).items():
        ref = field.get("$ref", "")
        model_name = ref.split("/")[-1] if ref else ""
        section_schema = defs.get(model_name, {})
        # pydantic 会把 model_config json_schema_extra 的键合并到该模型 schema 的顶层
        meta = section_schema if isinstance(section_schema, dict) else {}
        out.append({
            "key": name,
            "label": meta.get("label", name),
            "desc": meta.get("desc", ""),
            "order": meta.get("order", 99),
            "icon": meta.get("icon", ""),
            "model": model_name,
        })
    out.sort(key=lambda s: s["order"])
    return out


def _fields_of(section_schema: dict, model_name: str) -> list[dict]:
    """抽出一节的字段清单（name/label/desc/ui/type/choices/default 等），给配置页渲染。"""
    fields: list[dict] = []
    required = set(section_schema.get("required") or [])
    for name, f in (section_schema.get("properties") or {}).items():
        meta = f if isinstance(f, dict) else {}
        ui = meta.get("ui") or _guess_ui(meta)
        item: dict = {
            "name": name,
            "label": meta.get("label", name),
            "desc": meta.get("desc", ""),
            "ui": ui,
            "required": name in required,
        }
        if "choices" in meta:
            item["choices"] = meta["choices"]
        elif "enum" in meta:
            item["choices"] = meta["enum"]
        if "default" in meta:
            item["default"] = meta["default"]
        # 类型描述（供 JSON 文本框的 placeholder 等）
        t = meta.get("type")
        if t == "array":
            item["type"] = "array"
        elif t == "object":
            item["type"] = "object"
            item["additional"] = meta.get("additionalProperties")
        elif t == "integer":
            item["type"] = "integer"
        elif t == "number":
            item["type"] = "number"
        elif t == "boolean":
            item["type"] = "boolean"
        else:
            item["type"] = "string"
        fields.append(item)
    return fields


def _guess_ui(meta: dict) -> str:
    t = meta.get("type")
    if t == "boolean":
        return "switch"
    if t in ("integer", "number"):
        return "number"
    if "enum" in meta or "choices" in meta:
        return "select"
    if t in ("array", "object"):
        return "json"
    return "text"


# 全局单例（main 里初始化时传入真实路径）
store: ConfigStore = ConfigStore(Path("config.json"))