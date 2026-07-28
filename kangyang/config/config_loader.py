# -*- coding: utf-8 -*-
"""
统一配置加载器

功能：
  1. 从 YAML 文件加载端点和路由配置
  2. YAML 缺失/损坏时自动回退到代码内置默认值
  3. 支持运行时缓存和热重载
  4. 支持与自动发现结果合并（route_discovery.py）

用法：
  from kangyang.config import config_loader

  endpoints = config_loader.get_endpoints()
  routes    = config_loader.get_routes()
  config_loader.reload()           # 热重载
  config_loader.merge_routes(discovered)  # 合并自动发现的路由
"""
import os
import logging
import threading

logger = logging.getLogger(__name__)

_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
_ENDPOINTS_YAML = os.path.join(_CONFIG_DIR, "endpoints.yaml")
_ROUTES_YAML = os.path.join(_CONFIG_DIR, "routes.yaml")

# ─── 内置默认值（YAML 缺失时的回退）─────────────────────────

_DEFAULT_ENDPOINTS = [
    {"module": "elderlyCare", "name": "入住处理", "path": "/dev-api/elderlyCare/EnrollmentProcessing/list"},
    {"module": "elderlyCare", "name": "入住概览", "path": "/dev-api/elderlyCare/enrollmentView/list"},
    {"module": "basicinformation", "name": "长者档案", "path": "/dev-api/basicinformation/community/list"},
    {"module": "enrollmentApply", "name": "入住申请", "path": "/dev-api/enrollmentApply/enrollmentApply/list"},
    {"module": "oldCare", "name": "老人护理", "path": "/dev-api/oldCare/oldCare/list"},
    {"module": "basicinformation", "name": "社区管理", "path": "/dev-api/basicinformation/communityManagement/list"},
    {"module": "basicinformation", "name": "医生信息", "path": "/dev-api/basicinformation/doctor/list"},
    {"module": "basicinformation", "name": "护士信息", "path": "/dev-api/basicinformation/nurse/list"},
    {"module": "careDashBoard", "name": "护理记录", "path": "/dev-api/careDashBoard/careRecord/list"},
    {"module": "contract", "name": "合同管理", "path": "/dev-api/contract/contract/list"},
    {"module": "nurseLevel", "name": "护理等级", "path": "/dev-api/nurseLevel/nurseLevel/list"},
    {"module": "devManagement", "name": "床位管理", "path": "/dev-api/devManagement/bedroomsManagement/list"},
    {"module": "devManagement", "name": "分配床位", "path": "/dev-api/device/bedroom/list"},
    {"module": "devManagement", "name": "设备分配", "path": "/dev-api/assigner/assigner/list"},
    {"module": "devManagement", "name": "熙康设备", "path": "/dev-api/personbasicinfo/personbasicinfo/list"},
    {"module": "devManagement", "name": "设备办理", "path": "/dev-api/securityEquipment/equipment/list"},
    {"module": "ability", "name": "能力评估", "path": "/dev-api/ability/ability/list"},
    {"module": "ability", "name": "抑郁评估", "path": "/dev-api/depression/depression/list"},
    {"module": "activities", "name": "活动管理", "path": "/dev-api/activities/activities/list"},
    {"module": "securityEquipment", "name": "安全设备", "path": "/dev-api/securityEquipment/equipment/list"},
    {"module": "endDevice", "name": "终端设备", "path": "/dev-api/endDevice/endDevice/list"},
    {"module": "watchData", "name": "心率监测", "path": "/dev-api/watchData/watchHeartrate/watchHeartrate/list"},
    {"module": "watchData", "name": "血氧监测", "path": "/dev-api/watchData/watchOxygen/watchOxygen/list"},
    {"module": "system", "name": "用户列表", "path": "/dev-api/system/user/list"},
    {"module": "system", "name": "角色列表", "path": "/dev-api/system/role/list"},
    {"module": "system", "name": "字典数据", "path": "/dev-api/system/dict/data/list"},
]

_DEFAULT_ROUTES = [
    {"name": "入住处理", "route": "/elderly/checkin", "module": "老人管理"},
    {"name": "入住概览", "route": "/elderly/overview", "module": "老人管理"},
    {"name": "长者档案", "route": "/elderly/files", "module": "老人管理"},
    {"name": "入住申请", "route": "/elderly/apply", "module": "老人管理"},
    {"name": "老人护理", "route": "/elderly/nursing", "module": "老人护理"},
    {"name": "社区信息", "route": "/base/community", "module": "基本信息"},
    {"name": "社区管理", "route": "/base/communityManage", "module": "基本信息"},
    {"name": "医生信息", "route": "/base/doctor", "module": "基本信息"},
    {"name": "护士信息", "route": "/base/nurse", "module": "基本信息"},
    {"name": "护理记录", "route": "/nursing/record", "module": "护理看板"},
    {"name": "合同管理", "route": "/contract/manage", "module": "合同管理"},
    {"name": "护理等级", "route": "/nursing/level", "module": "护理等级"},
    {"name": "床位管理", "route": "/device/bed", "module": "设备管理"},
    {"name": "能力评估", "route": "/assessment/ability", "module": "能力评估"},
    {"name": "活动管理", "route": "/activity/manage", "module": "活动管理"},
    {"name": "安全设备", "route": "/safety/device", "module": "安全设备"},
    {"name": "终端设备", "route": "/terminal/device", "module": "终端设备"},
    {"name": "心率监测", "route": "/health/heartrate", "module": "健康监测"},
    {"name": "血氧监测", "route": "/health/spo2", "module": "健康监测"},
    {"name": "体温监测", "route": "/health/temperature", "module": "健康监测"},
    {"name": "血压监测", "route": "/health/bloodPressure", "module": "健康监测"},
    {"name": "用户列表", "route": "/system/user", "module": "系统管理"},
    {"name": "角色列表", "route": "/system/role", "module": "系统管理"},
    {"name": "字典数据", "route": "/system/dict", "module": "系统管理"},
]


# ─── YAML 加载 ──────────────────────────────────────────────

def _load_yaml(filepath):
    """加载 YAML 文件，返回 dict 或 None"""
    if not os.path.isfile(filepath):
        return None
    try:
        import yaml
        with open(filepath, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.warning(f"YAML 加载失败 {filepath}: {e}")
        return None


# ─── 配置管理器 ──────────────────────────────────────────────

class ConfigLoader:
    """线程安全的配置加载器，支持缓存和热重载"""

    def __init__(self):
        self._lock = threading.Lock()
        self._endpoints_cache = None
        self._routes_cache = None
        self._endpoints_source = None   # "yaml" or "default"
        self._routes_source = None
        self._routes_merged = set()     # 跟踪已合并的自动发现路由
        # 首次加载
        self._load_all()

    def _load_all(self):
        """从 YAML 文件加载全部配置"""
        # 加载端点
        yaml_data = _load_yaml(_ENDPOINTS_YAML)
        if yaml_data and "endpoints" in yaml_data and isinstance(yaml_data["endpoints"], list):
            self._endpoints_cache = yaml_data["endpoints"]
            self._endpoints_source = "yaml"
            logger.info(f"从 endpoints.yaml 加载了 {len(self._endpoints_cache)} 个 API 端点")
        else:
            self._endpoints_cache = list(_DEFAULT_ENDPOINTS)
            self._endpoints_source = "default"
            logger.info("使用内置默认 API 端点（endpoints.yaml 不可用）")

        # 加载路由
        yaml_data = _load_yaml(_ROUTES_YAML)
        if yaml_data and "routes" in yaml_data and isinstance(yaml_data["routes"], list):
            self._routes_cache = yaml_data["routes"]
            self._routes_source = "yaml"
            logger.info(f"从 routes.yaml 加载了 {len(self._routes_cache)} 个前端路由")
        else:
            self._routes_cache = list(_DEFAULT_ROUTES)
            self._routes_source = "default"
            logger.info("使用内置默认前端路由（routes.yaml 不可用）")

    # ── 公共 API ────────────────────────────────

    def get_endpoints(self):
        """获取 API 端点列表"""
        with self._lock:
            return list(self._endpoints_cache) if self._endpoints_cache else list(_DEFAULT_ENDPOINTS)

    def get_routes(self):
        """获取前端路由列表（含自动发现的）"""
        with self._lock:
            return list(self._routes_cache) if self._routes_cache else list(_DEFAULT_ROUTES)

    def get_endpoints_source(self):
        """返回端点来源: 'yaml' | 'default'"""
        return self._endpoints_source

    def get_routes_source(self):
        """返回路由来源: 'yaml' | 'discovered' | 'merged' | 'default'"""
        return self._routes_source

    def reload(self):
        """热重载：重新从 YAML 文件加载配置"""
        with self._lock:
            self._load_all()
            logger.info(f"配置已重载: 端点={self._endpoints_source}, 路由={self._routes_source}")

    def save_endpoints(self, new_eps):
        """
        合并并落盘端点配置（不覆盖已有、按 path 去重）。

        Args:
            new_eps: 新增/覆盖的端点列表 [{module, name, path}, ...]
        Returns:
            (ok: bool, total: int)
        """
        with self._lock:
            existing = {e.get("path"): e for e in (self._endpoints_cache or []) if e.get("path")}
            for ne in new_eps:
                p = ne.get("path")
                if not p:
                    continue
                # 保留已有 name/module 作默认（用户未填时用）
                old = existing.get(p, {})
                merged = {
                    "module": ne.get("module") or old.get("module") or "",
                    "name": ne.get("name") or old.get("name") or "",
                    "path": p,
                }
                # 枚举解码映射 / 样本值（自动发现解码用，保留已有）
                if ne.get("field_enums"):
                    merged["field_enums"] = ne["field_enums"]
                elif old.get("field_enums"):
                    merged["field_enums"] = old["field_enums"]
                if ne.get("field_samples"):
                    merged["field_samples"] = ne["field_samples"]
                elif old.get("field_samples"):
                    merged["field_samples"] = old["field_samples"]
                existing[p] = merged
            merged_list = list(existing.values())
            self._endpoints_cache = merged_list
            self._endpoints_source = "yaml"
        try:
            import yaml
            with open(_ENDPOINTS_YAML, "w", encoding="utf-8") as f:
                yaml.safe_dump({"endpoints": merged_list}, f, allow_unicode=True, sort_keys=False)
            logger.info(f"已保存 {len(merged_list)} 个端点到 {_ENDPOINTS_YAML}")
            return True, len(merged_list)
        except Exception as e:
            logger.error(f"写入 endpoints.yaml 失败: {e}")
            return False, 0

    def merge_routes(self, discovered_routes, strategy="append"):
        """
        合并自动发现的路由

        Args:
            discovered_routes: 自动发现的路由列表 [{name, route, module}]
            strategy: "append" 追加新模式 | "replace" 完全替换

        Returns:
            (added_count, total_count): 新增数和总数
        """
        with self._lock:
            existing_routes = {r["route"]: r for r in self._routes_cache}

            if strategy == "replace":
                self._routes_cache = list(discovered_routes)
                self._routes_source = "discovered"
                self._routes_merged = set(r["route"] for r in discovered_routes)
                return len(discovered_routes), len(discovered_routes)

            # append 模式：只添加新路由，不覆盖已有的
            added = 0
            for dr in discovered_routes:
                route_key = dr.get("route", "")
                if route_key and route_key not in existing_routes:
                    self._routes_cache.append(dr)
                    existing_routes[route_key] = dr
                    self._routes_merged.add(route_key)
                    added += 1

            if added > 0:
                self._routes_source = "merged"

            return added, len(self._routes_cache)

    def reset_routes(self):
        """重置路由为 YAML 或默认值"""
        with self._lock:
            yaml_data = _load_yaml(_ROUTES_YAML)
            if yaml_data and "routes" in yaml_data and isinstance(yaml_data["routes"], list):
                self._routes_cache = list(yaml_data["routes"])
                self._routes_source = "yaml"
            else:
                self._routes_cache = list(_DEFAULT_ROUTES)
                self._routes_source = "default"
            self._routes_merged.clear()

    def get_status(self):
        """获取配置状态摘要"""
        with self._lock:
            return {
                "endpoints_count": len(self._endpoints_cache),
                "endpoints_source": self._endpoints_source,
                "endpoints_yaml": _ENDPOINTS_YAML,
                "endpoints_yaml_exists": os.path.isfile(_ENDPOINTS_YAML),
                "routes_count": len(self._routes_cache),
                "routes_source": self._routes_source,
                "routes_yaml": _ROUTES_YAML,
                "routes_yaml_exists": os.path.isfile(_ROUTES_YAML),
                "routes_auto_discovered": len(self._routes_merged),
            }


# ─── 全局单例 ──────────────────────────────────────────────

_config_loader = ConfigLoader()

# 暴露为模块级懒加载属性，兼容旧代码 `from kangyang.api_crawler import KNOWN_ENDPOINTS`
def get_endpoints():
    return _config_loader.get_endpoints()

def get_routes():
    return _config_loader.get_routes()

def reload_config():
    _config_loader.reload()

def merge_discovered_routes(discovered, strategy="append"):
    return _config_loader.merge_routes(discovered, strategy)

def get_config_status():
    return _config_loader.get_status()

def reset_routes():
    _config_loader.reset_routes()

def save_endpoints(eps):
    return _config_loader.save_endpoints(eps)


# 延迟属性：每次访问时动态获取最新值
class _LazyConfig:
    """惰性配置代理，兼容旧代码中的 KNOWN_ENDPOINTS / KNOWN_ROUTES 模块属性"""
    @property
    def endpoints(self):
        return _config_loader.get_endpoints()

    @property
    def routes(self):
        return _config_loader.get_routes()

lazy = _LazyConfig()
