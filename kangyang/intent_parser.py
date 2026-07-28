# -*- coding: utf-8 -*-
"""
智能意图解析器 v5 —— 将用户的自然语言需求解析为可执行的爬取规则

v5 优化（精准度提升）：
  1. 多维加权评分引擎：精确匹配(1.0) / 别名匹配(0.8) / 部分匹配(0.4) / 模糊匹配(0.3)
     —— 替代 v3 的扁平打分，区分匹配质量层次
  2. 同义词组扩展：SYNONYM_GROUPS 将近义词聚类，"老人家"→"老人"，"看护"→"护理"
     —— 覆盖用户不同的表达方式和口语习惯
  3. 缩写/简称自动展开：ABBREVIATION_MAP，"体检"→"能力评估"，"护工"→"护士信息"
  4. 上下文消歧：当多端点同时匹配时，用字段级别提示选择最佳端点
     —— 如"老人的心率"优先匹配心率监测而非老人护理
  5. 否定/排除处理：识别"不要""排除""除了"等否定词，排除无关端点
  6. 用户反馈学习：基于历史查询和用户修正记录动态调整权重
  7. 智能字段过滤优化：减少无关字段返回，提高数据精准度

输入: "获取长者档案页面的所有长者姓名以及联系方式"
输出: { target_api, fields, description, confidence, reasoning }
"""
import json
import re
import logging
import urllib.request
from difflib import SequenceMatcher
from kangyang.api_crawler import KNOWN_ENDPOINTS
from kangyang.page_scraper import KNOWN_ROUTES
from kangyang.query_feedback import get_feedback_boost, record_query
from kangyang.enum_decoder import decode_row_values, _load_endpoint_enums

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# 1. 端点别名词典 —— 每个端点对应的中文关键词（用户可能用到的表述）
# ══════════════════════════════════════════════════════════════
ENDPOINT_ALIASES = {
    # ── 入住处理 ──
    "/dev-api/elderlyCare/EnrollmentProcessing/list": [
        "入住处理", "入住登记", "入住办理", "入院处理", "入住审批", "入住流程",
        "退住处理", "退住登记", "退住办理", "出院处理", "出院登记", "出院办理",
        "离院", "退院", "出院",
    ],
    # ── 入住概览 / 老人信息 ──
    "/dev-api/elderlyCare/enrollmentView/list": [
        "入住概览", "入住总览", "入住信息", "入住情况",
        "入住人员", "入住老人", "长者信息", "老人信息", "老人数据", "老人",
        "入住列表", "在住人员", "在住老人",
    ],
    # ── 长者档案 / 老人档案：真实后端路径 basicinformation/community/list（返回 oldName/oldPhone 等字段）──
    "/dev-api/basicinformation/community/list": [
        "长者档案", "老人档案", "档案管理", "长者资料", "老人资料",
        "长者信息档案", "老人信息档案", "档案信息",
    ],
    # ── 入住申请 ──
    "/dev-api/enrollmentApply/enrollmentApply/list": [
        "入住申请", "申请入住", "入住申请表", "入院申请",
    ],
    # ── 护理记录 / 日常护理 ──
    "/dev-api/oldCare/oldCare/list": [
        "老人护理", "护理记录", "护理信息", "老人看护", "护理服务", "日常护理",
        "护理情况", "护理数据", "护理详情", "身体护理", "身体状况", "身体健康",
        "健康记录", "健康数据", "老人身体", "身体数据", "健康",
    ],
    # 注：basicinformation/community/list 实际为长者档案接口，社区信息别名已移除以避免冲突
    # ── 社区管理 ──
    "/dev-api/basicinformation/communityManagement/list": [
        "社区管理", "社区管理员", "社区组织",
    ],
    # ── 医生信息 ──
    "/dev-api/basicinformation/doctor/list": [
        "医生信息", "医生列表", "医生资料", "医师", "医生", "大夫",
    ],
    # ── 护士/护工信息 ──
    "/dev-api/basicinformation/nurse/list": [
        "护士信息", "护士列表", "护士资料", "护工", "护理人员", "护士", "护工信息",
        "护理员信息", "护理员资料", "护工列表", "看护人员",
    ],
    # ── 护理看板/记录 ──
    "/dev-api/careDashBoard/careRecord/list": [
        "护理记录", "护理看板", "护理日志", "照护记录", "看护记录", "服务记录",
        "照护服务", "护理台账", "护理档案",
    ],
    # ── 合同管理 ──
    "/dev-api/contract/contract/list": [
        "合同管理", "合同信息", "合同列表", "服务合同", "入住合同", "合同",
        "合同记录", "合同查询", "合同到期", "合同续签", "合同台账",
    ],
    # ── 护理等级 ──
    "/dev-api/nurseLevel/nurseLevel/list": [
        "护理等级", "护理级别", "护理等级评估", "照护等级", "护理分级", "护理级别评估",
        "护理评级", "照护级别", "护理标准",
    ],
    # ── 床位/房间管理 ──
    "/dev-api/devManagement/bedroomsManagement/list": [
        "床位管理", "床位信息", "房间管理", "床位列表", "床位", "房间",
        "房间信息", "房间列表", "床铺", "床位查询", "住房", "房源",
    ],
    # ── 能力评估 ──
    # 注意：不要放光杆"评估"二字！否则"压疮评估/跌倒评估/营养评估"等所有含"评估"的
    # 查询都会误匹配到这里。必须带前缀限定词（能力/自理/功能/日常生活/老人体检）。
    "/dev-api/ability/ability/list": [
        "能力评估", "能力评定", "老人能力评估", "照护评估", "自理能力", "能力评估表",
        "老人评估", "评估结果", "健康评估", "身体评估", "功能评估", "日常生活评估",
        "老人体检",
        # 已移除光杆 "评估" —— 2026-07-23 修复：避免吞掉所有 XX评估 类查询
    ],
    # ── 活动管理 ──
    "/dev-api/activities/activities/list": [
        "活动管理", "活动信息", "活动列表", "文体活动", "康乐活动", "活动",
        "活动安排", "娱乐活动", "社工活动", "文娱", "康复活动",
    ],
    # ── 安全设备 ──
    "/dev-api/securityEquipment/equipment/list": [
        "安全设备", "安防设备", "安全设备信息", "监控设备", "报警设备", "安全设备列表",
        "安保设备", "消防设备", "应急设备",
    ],
    # ── 终端/智能设备 ──
    "/dev-api/endDevice/endDevice/list": [
        "终端设备", "设备管理", "智能设备", "物联网设备", "终端", "设备列表",
        "智能终端", "穿戴设备", "手环", "智能手表",
    ],
    # ── 心率监测 ──
    "/dev-api/watchData/watchHeartrate/watchHeartrate/list": [
        "心率监测", "心率数据", "心跳监测", "心率记录", "心率", "心跳",
        "心率信息", "脉搏", "心跳数据", "心脏监测",
    ],
    # ── 血氧监测 ──
    "/dev-api/watchData/watchOxygen/watchOxygen/list": [
        "血氧监测", "血氧数据", "血氧记录", "血氧饱和度", "血氧",
        "血氧信息", "氧饱和度", "血氧量", "氧气监测", "氧气数据", "氧气",
    ],
    # ── 系统用户 ──
    "/dev-api/system/user/list": [
        "用户列表", "用户信息", "系统用户", "账号管理", "用户", "账号",
    ],
    # ── 系统角色 ──
    "/dev-api/system/role/list": [
        "角色列表", "角色信息", "权限角色", "角色",
    ],
    # ── 字典数据 ──
    "/dev-api/system/dict/data/list": [
        "字典数据", "字典列表", "数据字典", "字典", "枚举值",
    ],
}

# ══════════════════════════════════════════════════════════════
# 1.5 语义端点映射表 —— 口语化/非标准表述 → 正确的端点
#     这些是"意思一样但字面不一样"的映射
# ══════════════════════════════════════════════════════════════
SEMANTIC_ENDPOINT_MAP = {
    # 氧气 = 血氧（用户常说"氧气"但系统叫"血氧"）
    "氧气": "/dev-api/watchData/watchOxygen/watchOxygen/list",
    "氧气监测": "/dev-api/watchData/watchOxygen/watchOxygen/list",
    "氧饱和度": "/dev-api/watchData/watchOxygen/watchOxygen/list",
    # 心跳 = 心率
    "心跳监测": "/dev-api/watchData/watchHeartrate/watchHeartrate/list",
    "心跳数据": "/dev-api/watchData/watchHeartrate/watchHeartrate/list",
    "脉搏": "/dev-api/watchData/watchHeartrate/watchHeartrate/list",
    # 身体/健康 = 护理记录
    "身体状况": "/dev-api/oldCare/oldCare/list",
    "身体健康": "/dev-api/oldCare/oldCare/list",
    "健康数据": "/dev-api/oldCare/oldCare/list",
    "健康记录": "/dev-api/oldCare/oldCare/list",
    "身体数据": "/dev-api/oldCare/oldCare/list",
    # 退住/出院 = 入住概览（通常退住数据也在入住表中，通过状态字段区分）
    "退住": "/dev-api/elderlyCare/enrollmentView/list",
    "退住情况": "/dev-api/elderlyCare/enrollmentView/list",
    "退住记录": "/dev-api/elderlyCare/enrollmentView/list",
    "出院": "/dev-api/elderlyCare/enrollmentView/list",
    "出院情况": "/dev-api/elderlyCare/enrollmentView/list",
    "离院": "/dev-api/elderlyCare/enrollmentView/list",
    # 身份证 = 老人信息（身份证在老人档案中）
    "身份证": "/dev-api/elderlyCare/enrollmentView/list",
    "身份证信息": "/dev-api/elderlyCare/enrollmentView/list",
    # 缴费/收款 = 合同管理（金额在合同表中）
    "缴费": "/dev-api/contract/contract/list",
    "收款": "/dev-api/contract/contract/list",
    "费用": "/dev-api/contract/contract/list",
    # 过敏 = 老人护理/护理记录
    "过敏": "/dev-api/oldCare/oldCare/list",
    "过敏信息": "/dev-api/oldCare/oldCare/list",
    "过敏史": "/dev-api/oldCare/oldCare/list",
    # 医保 = 入住概览/老人档案
    "医保": "/dev-api/elderlyCare/enrollmentView/list",
    "医保信息": "/dev-api/elderlyCare/enrollmentView/list",
    "医保类型": "/dev-api/elderlyCare/enrollmentView/list",
    # 饮食 = 护理记录
    "饮食": "/dev-api/oldCare/oldCare/list",
    "饮食偏好": "/dev-api/oldCare/oldCare/list",
    "饮食禁忌": "/dev-api/oldCare/oldCare/list",
    "膳食": "/dev-api/oldCare/oldCare/list",
    # 排班 = 护理记录/护理看板
    "排班": "/dev-api/careDashBoard/careRecord/list",
    "排班信息": "/dev-api/careDashBoard/careRecord/list",
    "排班表": "/dev-api/careDashBoard/careRecord/list",
    "值班": "/dev-api/careDashBoard/careRecord/list",
    # 房屋/住房 = 床位管理
    "房屋": "/dev-api/devManagement/bedroomsManagement/list",
    "住房": "/dev-api/devManagement/bedroomsManagement/list",
    "空房": "/dev-api/devManagement/bedroomsManagement/list",
}

# 反向索引：关键词 → 端点路径（用于快速查找）
_ALIAS_INDEX = {}
for _path, _aliases in ENDPOINT_ALIASES.items():
    for _alias in _aliases:
        _ALIAS_INDEX[_alias] = _path
    # 同时用端点 name 做索引
for _ep in KNOWN_ENDPOINTS:
    _ALIAS_INDEX[_ep["name"]] = _ep["path"]
# 语义映射表也加入索引
for _kw, _path in SEMANTIC_ENDPOINT_MAP.items():
    _ALIAS_INDEX[_kw] = _path


# ══════════════════════════════════════════════════════════════
# 1.6 页面采集检测 —— 识别用户是否想爬取网页而非调API
# ══════════════════════════════════════════════════════════════

# URL 模式：完整URL、Vue路由、带协议头的地址
_URL_PATTERN = re.compile(r'(https?://[^\s，,、。]+|/[a-zA-Z][^\s，,、。]{2,})')

# 页面采集触发词 —— 用户明确说要爬网页/表单/表格
_PAGE_SCRAPE_KEYWORDS = [
    "爬取页面", "采集页面", "抓取页面", "页面采集", "页面爬取",
    "网页数据", "网页内容", "网页表单", "网页表格",
    "表单数据", "表单内容", "表格数据", "页面表格",
    "从页面", "从网页", "爬取网页", "采集网页",
    "页面上", "页面中", "网页上", "打开页面",
]

# 页面路由的关键词映射（用于从口语匹配到已知路由）
# ⚠️ 2026-07-24 已从后端菜单系统 (/dev-api/system/menu/list) 重建。
#    值为完整的 /dev-api/{module}/{entity}/list 路径（可直接用于 API 调用），
#    不再是旧的短路径（如 /assessment/pressureUlcer，已确认全部 404）。
#    来源：权限标识 perms 格式 {module}:{entity}:list → API 路径推导
PAGE_ROUTE_ALIASES = {
    # ══ 2026-07-24 二次重建 ══
    # 来源：后端 /dev-api/system/menu/list 提取全部业务菜单的 perms，
    #       对每个 {module}:{entity}:list 生成 /dev-api/{module}/{entity}/list 并【真实探测】，
    #       仅保留 HTTP 200 的接口（共 63 个），彻底剔除 xiKang / watchData / sleep /
    #       defense / expenditure 等“菜单有配置但后端未部署（404）”的死路径。
    # 约定：key = /{module}/{entity}（与 generic_api_resolver.build_candidate_api_paths 配合，
    #       原样变体即正确的 /dev-api/{module}/{entity}/list）。
    # 歧义处理：单字实体名（体温/血压/血糖/血氧/心电/脉率/血脂/尿酸/三围/身高体重/人体成分/中医体质）
    #           只在对应实体出现，避免被“熙康设备数据”等泛指词误抢。
    # ── 养老评估 ──
    "/pressure/pressure":      ["压疮评估", "压疮", "褥疮", "压力性损伤", "褥疮评估"],
    "/depression/depression":  ["抑郁评估", "抑郁"],
    "/anxiety/anxiety":        ["焦虑评估", "焦虑"],
    "/morse/morse":            ["跌倒风险评估", "跌倒风险", "Morse评估", "跌倒评估"],
    "/riskICVD/riskICVD":      ["ICVD风险评估", "ICVD", "脑血管风险评估", "脑卒中风险评估", "卒中风险"],
    "/ability/ability":        ["老人能力评估", "能力评估", "老人综合评估", "ADL评估"],
    "/evaluation/assessmentView": ["评估总览", "养老评估总览", "评估概览"],
    # ── 养老管理 ──
    "/elderlyCare/EnrollmentProcessing": ["入托办理", "入住办理", "入住处理", "入托"],
    "/elderlyCare/exitcare":   ["退托管理", "退托", "退住管理"],
    "/basicinformation/community": ["长者档案", "老人档案", "档案管理", "长者资料", "老人资料", "档案"],
    "/basicinformation/communityManagement": ["社区管理", "社区信息"],
    "/basicinformation/doctor": ["医生管理", "医生信息", "医生列表"],
    "/basicinformation/nurse":  ["护士管理", "护士信息", "护理员信息", "护工信息", "护士列表"],
    # ── 护理服务 ──
    "/oldCare/oldCare":        ["长者需求", "老人需求", "需求列表"],
    "/careDashBoard/careRecord": ["护理记录", "护理看板", "照护记录", "护理日志"],
    "/nurseLevel/nurseLevel":  ["护理级别", "护理等级", "护理分级"],
    "/careDashBoard/careplan": ["护理计划", "照护计划"],
    "/nursingProject/nursingProject": ["护理服务项目", "护理项目", "服务项目"],
    # ── 熙康健康监测（真实路径为 /{entity}/{entity}/list）──
    "/bloodOxygen/bloodOxygen": ["熙康血氧数据", "熙康血氧", "血氧", "血氧监测", "血氧数据"],
    "/bloodPressure/bloodPressure": ["熙康血压数据", "熙康血压", "血压", "血压监测", "血压数据"],
    "/bloodSugar/bloodSugar":  ["熙康血糖数据", "熙康血糖", "血糖", "血糖监测"],
    "/temperature/temperature": ["熙康体温", "体温", "体温数据", "体温监测", "熙康体温数据"],
    "/ecg/ecg":                ["熙康心电数据", "熙康心电", "心电", "心电图", "心电监测"],
    "/pulseRate/pulseRate":    ["熙康脉率数据", "脉率", "脉搏", "脉率数据"],
    "/heightWeight/heightWeight": ["熙康身高体重数据", "身高体重", "身高体重数据"],
    "/dimensional/dimensional": ["熙康三维数据", "三维数据", "三围", "三围数据"],
    "/composition/composition": ["熙康人体成分", "人体成分", "成分分析"],
    "/lipids/lipids":          ["熙康血脂数据", "血脂", "血脂数据"],
    "/uricacid/uricacid":      ["熙康尿酸数据", "尿酸", "尿酸数据"],
    "/tcmconstitution/tcmconstitution": ["中医体质", "体质辨识"],
    # ── 睡眠监控（真实路径）──
    "/breathingrate/breathingrate": ["睡眠呼吸", "呼吸率", "睡眠呼吸率"],
    "/duration/duration":      ["睡眠时长", "睡眠时长数据"],
    "/interrupt/interrupt":    ["睡眠中断", "中断数据"],
    "/pattern/pattern":        ["睡眠规律", "睡眠规律性"],
    "/sleep_score/sleep_score": ["睡眠评分", "睡眠评分概览", "睡眠分数"],
    "/staging/staging":        ["睡眠分期", "分期数据"],
    "/heartrate/heartrate":    ["睡眠心率", "心率", "心率监测", "睡眠心率监测"],
    "/variability/variability": ["心率变异", "心率变异性", "HRV"],
    # ── 健康管理 ──
    "/healthy/archives":       ["健康报告", "健康档案报告", "健康档案"],
    "/healthy/assessment":     ["健康评估", "健康评价"],
    "/healthy/interventions":  ["健康干预", "干预记录"],
    # ── 活动管理 ──
    "/activities/activities":  ["活动建立", "活动管理", "活动列表"],
    # ── 费项管理（真实路径）──
    "/feeDetails/feedetails":  ["缴费明细", "费用明细", "预缴明细"],
    "/feeview/feeview":        ["费用总览", "费用概览"],
    # ── 设备管理 ──
    "/devManagement/bedroomsManagement": ["床位管理", "房间管理", "床位列表", "房间列表"],
    "/device/bedroom":         ["分配床位", "床位分配"],
    "/securityEquipment/equipment": ["设备管理", "安防设备", "安全设备"],
    "/configDevice/configkeys": ["设备配置", "设备参数"],
    "/assigner/assigner":      ["设备分配", "分配设备"],
    "/endDevice/endDevice":    ["终端报警数据", "终端报警"],
    "/endDeviceLowPower/endDeviceLow": ["终端低电数据", "低电数据", "终低电数据"],
    "/hostDevice/hostDevice":  ["开关机数据", "主机开关机"],
    # ── 安防 / 报警 ──
    "/fallGait/fallen":        ["相机跌倒报警", "跌倒报警", "相机跌倒", "跌倒检测"],
    "/SocSendData/SosSenddata": ["主机报警", "SOS报警", "安防报警", "主机SOS报警"],
    # ── 消息 / 通知 / 小度 ──
    "/MessagesNotification/notification": ["消息通知", "通知消息"],
    "/InactivityNotification/oldNotification": ["老人不活跃预警通知", "不活跃预警", "不活跃通知"],
    "/reminderReply/reminderReplyData": ["提醒答复", "提醒回复"],
    "/BaiduCallRecords/CallRecords": ["小度通话记录", "小度通话"],
    "/baiduCallHelp/callhelp": ["小度紧急呼救", "小度呼救", "紧急呼救"],
    "/BaiduMessageService/baiduservice": ["百度服务留言", "百度留言"],
    "/baiduPushNotification/baiduNotification": ["百度推送通知", "百度推送"],
    "/telephoneData/telephone": ["Sos通话", "SOS通话", "SOS电话"],
    "/realdata/realdata":      ["实时数据", "实时监测数据"],
}

# 构建页面路由反向索引
_PAGE_ROUTE_INDEX = {}

def _rebuild_page_route_index():
    """重建页面路由反向索引（配置热重载时调用）"""
    global _PAGE_ROUTE_INDEX
    _PAGE_ROUTE_INDEX = {}
    for _route, _aliases in PAGE_ROUTE_ALIASES.items():
        for _a in _aliases:
            _PAGE_ROUTE_INDEX[_a] = _route
    for _r in KNOWN_ROUTES:
        _PAGE_ROUTE_INDEX[_r["name"]] = _r["route"]
        # 也加入 SEMANTIC_ENDPOINT_MAP 中的映射
    for _kw, _api_path in SEMANTIC_ENDPOINT_MAP.items():
        # 找对应的页面路由
        for _r in KNOWN_ROUTES:
            if _r["name"] in _ALIAS_INDEX.get(_kw, ""):
                _PAGE_ROUTE_INDEX[_kw] = _r["route"]
                break

_rebuild_page_route_index()


# ══════════════════════════════════════════════════════════════
# 2. 字段同义词表 —— 常见中文表述 → 可能的 JSON key
#    按业务域分组，便于扩展
# ══════════════════════════════════════════════════════════════
FIELD_SYNONYMS = {
    # ── 个人信息类 ──
    "姓名": ["name", "elderlyName", "elderName", "personName", "userName", "realName", "fullname", "nickName", "oldName"],
    "老人姓名": ["elderlyName", "elderName", "name", "personName", "oldName"],
    "长者姓名": ["elderlyName", "elderName", "name", "personName", "oldName"],
    "性别": ["sex", "gender", "oldGender"],
    "年龄": ["age", "elderlyAge", "elderAge", "oldAge"],
    "出生日期": ["birthday", "birthDate", "dateOfBirth", "bornDate", "oldBirthday"],
    "身份证号": ["idCard", "idNumber", "identityCard", "cardNo", "idcard", "oldCertificatecode"],
    "身份证": ["idCard", "idNumber", "identityCard", "cardNo", "oldCertificatecode"],
    "民族": ["nation", "ethnicity", "nationality", "oldNation"],
    "籍贯": ["nativePlace", "hometown", "origin"],
    "婚姻状况": ["maritalStatus", "marriage", "marital"],
    "学历": ["education", "educationLevel", "degree", "degreeEducation"],
    "照片": ["photo", "avatar", "picture", "image", "headImg"],
    "身高": ["height", "bodyHeight", "stature"],
    "体重": ["weight", "bodyWeight"],

    # ── 联系方式类 ──
    "联系方式": ["phone", "contactPhone", "contact", "mobile", "telephone", "contactInfo", "phoneNumber", "oldPhone", "oldTelephone"],
    "联系电话": ["phone", "contactPhone", "telephone", "mobile", "phoneNumber", "oldPhone", "oldTelephone"],
    "手机号": ["mobile", "phone", "phoneNumber", "mobilePhone", "oldPhone"],
    "电话": ["phone", "telephone", "phoneNo", "oldPhone", "oldTelephone"],
    "紧急联系人": ["emergencyContact", "emergencyName", "emergency", "contactPerson"],
    "紧急联系电话": ["emergencyPhone", "emergencyContactPhone", "emergencyTel"],
    "家庭住址": ["homeAddress", "address", "homeAddr", "oldAddress"],
    "地址": ["address", "homeAddress", "addr", "location", "detailAddress", "oldAddress", "currentAddress"],
    "家属姓名": ["familyName", "relativeName", "kinName", "guardianName"],
    "家属电话": ["familyPhone", "relativePhone", "kinPhone", "guardianPhone"],
    "家属": ["familyName", "relativeName", "guardianName"],

    # ── 入住信息类 ──
    "入住日期": ["checkinDate", "enrollmentDate", "checkInDate", "admissionDate", "enterDate", "inDate"],
    "入住状态": ["status", "checkinStatus", "enrollmentStatus", "liveStatus"],
    "入住类型": ["checkinType", "enrollmentType", "admissionType"],
    "房间号": ["roomNo", "roomNumber", "room", "roomId"],
    "床位号": ["bedNo", "bedNumber", "bedId", "bed"],
    "床位": ["bedNo", "bedNumber", "bedId", "bed", "床位号"],
    "楼栋": ["building", "buildingNo", "block"],
    "楼层": ["floor", "floorNo"],
    # 新增入住相关
    "退住日期": ["checkoutDate", "exitDate", "leaveDate", "dischargeDate", "outDate", "endDate"],
    "退住原因": ["checkoutReason", "exitReason", "leaveReason", "dischargeReason"],
    "入住天数": ["stayDays", "days", "duration", "inDays"],
    "护理房间": ["roomNo", "roomNumber", "wardNo", "nurseRoom"],

    # ── 护理类 ──
    "护理等级": ["nurseLevel", "careLevel", "nursingLevel", "level", "careGrade"],
    "护理级别": ["nurseLevel", "careLevel", "nursingLevel", "level"],
    "护理员": ["nurseName", "caregiverName", "nurse", "caregiver"],
    "护理员姓名": ["nurseName", "caregiverName"],
    "护理日期": ["careDate", "nurseDate", "recordDate", "careTime"],
    "护理内容": ["careContent", "nurseContent", "careItem", "serviceContent"],
    "护理时间": ["careTime", "nurseTime", "duration", "serviceTime"],
    "体温": ["temperature", "temp", "bodyTemp"],
    "血压": ["bloodPressure", "pressure", "bp", "systolic", "diastolic"],
    "心率": ["heartRate", "heartrate", "pulse", "hr", "heartRateValue"],
    "血氧": ["oxygen", "bloodOxygen", "spo2", "oxygenSaturation", "oxygenValue"],
    "血糖": ["bloodSugar", "glucose", "sugar", "bloodGlucose"],
    # 新增护理相关
    "身体状况": ["healthStatus", "physicalCondition", "bodyStatus", "health"],
    "过敏信息": ["allergy", "allergyInfo", "allergicHistory", "allergyHistory"],
    "过敏": ["allergy", "allergyInfo", "allergicHistory"], 
    "过敏史": ["allergyHistory", "allergicHistory", "allergy"],
    "饮食偏好": ["dietPreference", "diet", "foodPreference", "dietaryPreference"],
    "饮食禁忌": ["dietRestriction", "foodTaboo", "dietTaboo", "foodRestriction"],
    "禁忌": ["taboo", "restriction", "contraindication", "dietRestriction"],
    "医保类型": ["medicalInsurance", "insuranceType", "medicalType", "insurance"],
    "医保": ["medicalInsurance", "insuranceType", "medicalType"],
    "排班": ["schedule", "shift", "duty", "roster", "arrangement"],
    "排班信息": ["schedule", "shift", "duty", "roster"],
    "照护记录": ["careRecord", "nurseRecord", "careNote"],
    "护理备注": ["careRemark", "nurseRemark", "remark", "note"],

    # ── 合同/费用类 ──
    "合同编号": ["contractNo", "contractId", "contractCode", "code"],
    "合同名称": ["contractName", "title", "name"],
    "签订日期": ["signDate", "contractDate", "signTime", "signedDate"],
    "到期日期": ["expireDate", "endDate", "expiryDate", "validUntil"],
    "合同金额": ["amount", "contractAmount", "totalAmount", "price"],
    "合同状态": ["contractStatus", "status", "state"],
    # 新增费用相关
    "缴费金额": ["amount", "payAmount", "paymentAmount", "fee", "paidAmount"],
    "收款方": ["payee", "receiver", "recipient", "beneficiary"],
    "缴费日期": ["payDate", "paymentDate", "paidDate"],
    "费用类型": ["feeType", "chargeType", "costType", "paymentType"],
    "费用": ["amount", "fee", "charge", "cost", "price", "totalAmount"],

    # ── 设备类 ──
    "设备名称": ["deviceName", "equipmentName", "name"],
    "设备类型": ["deviceType", "equipmentType", "type"],
    "设备状态": ["deviceStatus", "status", "equipmentStatus"],
    "设备编号": ["deviceNo", "deviceId", "equipmentNo", "code"],

    # ── 评估类（新增） ──
    "评估日期": ["assessDate", "evaluationDate", "assessTime", "evaluationTime"],
    "评估结果": ["assessResult", "evaluationResult", "result", "level"],
    "评估类型": ["assessType", "evaluationType", "type"],
    "评估分数": ["score", "assessScore", "totalScore", "grade"],
    "评估人": ["assessor", "evaluator", "assessByName", "evaluatePerson"],

    # ── 活动类（新增） ──
    "活动名称": ["activityName", "name", "title"],
    "活动日期": ["activityDate", "date", "activityTime"],
    "活动地点": ["activityLocation", "location", "place", "venue"],
    "参与人数": ["participantCount", "attendeeCount", "people"],

    # ── 系统/通用字段 ──
    "编号": ["id", "no", "code", "number"],
    "创建时间": ["createTime", "createdTime", "createdAt", "createDate", "gmtCreate"],
    "更新时间": ["updateTime", "updatedTime", "updatedAt", "updateDate", "gmtModified"],
    "备注": ["remark", "remarks", "note", "comment", "description", "memo"],
    "状态": ["status", "state", "enable"],
    "负责人": ["manager", "personInCharge", "chargePerson", "leader"],
    "描述": ["description", "desc", "remark", "memo", "content"],
    "名称": ["name", "title", "label"],
    "原因": ["reason", "cause", "explanation", "factor"],
    "日期": ["date", "recordDate", "dateTime", "day"],
    "时间": ["time", "recordTime", "dateTime", "datetime"],
}


# ══════════════════════════════════════════════════════════════
# 2.5 同义词组 —— 将近义词聚类，任一词出现都可匹配
#    格式: (标准词, [同义词列表])
# ══════════════════════════════════════════════════════════════
SYNONYM_GROUPS = [
    ("老人", ["长者", "老人家", "住户", "入院老人", "入住老人", "在院老人", "长者"]),
    ("护理", ["看护", "照护", "照料", "照顾", "陪护", "看顾", "养护"]),
    ("入住", ["入院", "入住院", "入住登记", "入住办理", "办入住", "入住手续"]),
    ("退住", ["出院", "离院", "退院", "离开", "退出", "搬出", "搬离"]),
    ("护理员", ["护工", "看护员", "陪护员", "护理员", "看护人员", "照顾人员"]),
    ("医生", ["医师", "大夫", "医者", "诊疗师"]),
    ("床位", ["床铺", "床位号", "铺位", "病床"]),
    ("房间", ["房屋", "居室", "住房", "房号", "寝室"]),
    ("合同", ["协议", "契约", "合约", "签约"]),
    ("心率", ["心跳", "脉搏", "心电"]),
    ("血氧", ["氧气", "氧饱和度", "血氧饱和度", "含氧量"]),
    ("血压", ["血压值", "动脉压"]),
    ("体温", ["体表温度", "热度", "体温度数"]),
    ("血糖", ["血糖值", "葡萄糖值"]),
    ("评估", ["评定", "评测", "考核", "测评"]),
    ("活动", ["文娱", "康乐", "文体活动", "娱乐", "社工活动", "康复活动"]),
    ("设备", ["装置", "器材", "器械", "设施"]),
    ("安全", ["安防", "安保", "安全防护"]),
    ("缴费", ["付款", "交费", "支付", "费用", "收费"]),
    ("身份证", ["证件号", "身份号", "证件号码", "身份证明"]),
    ("联系方式", ["电话", "手机", "联系电话", "手机号", "手机号码", "通讯方式"]),
    ("家属", ["家人", "亲属", "亲人", "家人信息"]),
    ("医保", ["医疗保险", "医保类型", "医疗险"]),
    ("过敏", ["过敏史", "过敏信息", "过敏源", "过敏反应"]),
    ("饮食", ["膳食", "餐食", "进食", "饮食偏好", "饭食"]),
]

# 构建反向索引: 同义词 → 标准词
_SYNONYM_INDEX = {}
for _standard, _syns in SYNONYM_GROUPS:
    for _syn in _syns:
        _SYNONYM_INDEX[_syn] = _standard
    # 标准词也索引自身
    _SYNONYM_INDEX[_standard] = _standard


# ══════════════════════════════════════════════════════════════
# 2.6 缩写/简称映射表 —— 用户常用简称 → 标准表述
# ══════════════════════════════════════════════════════════════
ABBREVIATION_MAP = {
    "体检": "能力评估",
    "护工": "护士信息",
    "床位管理": "床位管理",
    "安全": "安全设备",
    "终端": "终端设备",
    "心率数据": "心率监测",
    "血氧数据": "血氧监测",
    "合同到期": "合同管理",
    "费用": "合同管理",
    "排班": "护理记录",
    "值班": "护理记录",
    "医保": "入住概览",
    "手环": "终端设备",
    "智能手表": "终端设备",
    "空房": "床位管理",
}


# ══════════════════════════════════════════════════════════════
# 2.7 否定/排除词表 —— 识别用户不想查的内容
# ══════════════════════════════════════════════════════════════
_NEGATION_PATTERN = re.compile(
    r'(?:不要|不包括|不包含|排除|除了|不要查|不需要|忽略|跳过|去掉|去除|剔除)\s*'
    r'(.+?)(?:的|和|与|以及|及|，|,|。|$)'
)


# ══════════════════════════════════════════════════════════════
# 3. 字段连接词模式 —— 从自然语言中提取多个字段
# ══════════════════════════════════════════════════════════════
# 用于从 "获取姓名、联系方式和地址" 这类描述中拆分出独立字段
_FIELD_DELIMITERS = re.compile(
    r'[、,，;；]|以及|和|与|还有|包括|包含|及其'
)

# "所有XX" / "全部XX" 模式
_ALL_PATTERN = re.compile(r'(?:所有|全部|全部的|所有的)\s*(.+)', re.DOTALL)

# "XX的YY" 模式（从属关系）
_POSSESSIVE_PATTERN = re.compile(r'(.+?)的(.+)')


def _get_llm_config():
    """读取 LLM 配置"""
    from kangyang.llm.config_loader import get_llm_config
    return get_llm_config()


def _build_endpoint_desc():
    """构建 API 端点描述列表（含别名），用于注入 Prompt"""
    lines = []
    for ep in KNOWN_ENDPOINTS:
        aliases = ENDPOINT_ALIASES.get(ep["path"], [])
        alias_str = "、".join(aliases[:4]) if aliases else ""
        line = f"  - 表名: {ep['name']} | 模块: {ep['module']} | API路径: {ep['path']}"
        if alias_str:
            line += f" | 别名关键词: {alias_str}"
        lines.append(line)
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# 4. 规则预匹配：端点识别（v5 多维加权评分引擎）
# ══════════════════════════════════════════════════════════════

# ── 评分权重常量 ──
SCORE_EXACT_NAME    = 1.0   # 端点名称精确匹配
SCORE_SEMANTIC      = 0.9   # 语义映射匹配
SCORE_ALIAS_EXACT   = 0.8   # 别名精确匹配（完整出现）
SCORE_SYNONYM       = 0.6   # 同义词组匹配
SCORE_PARTIAL       = 0.4   # 部分关键词匹配
SCORE_FUZZY         = 0.3   # 模糊匹配
SCORE_MODULE        = 0.15  # 模块名匹配
SCORE_PATH_SEG      = 0.1   # 路径片段匹配
SCORE_ABBR          = 0.7   # 缩写展开匹配


def _expand_query_with_synonyms(query: str) -> str:
    """
    用同义词组扩展查询文本
    
    如果用户说"老人家"，扩展后也会包含"老人"
    如果用户说"看护"，扩展后也会包含"护理"
    返回扩展后的查询（原始 + 同义词）
    """
    expanded_parts = [query]
    for syn, standard in _SYNONYM_INDEX.items():
        if syn in query and syn != standard:
            expanded_parts.append(standard)
    return " ".join(set(expanded_parts))


def _extract_negated_terms(query: str) -> set:
    """
    提取否定/排除的关键词（v5 修正：精确提取实体词，去除尾部干扰词）

    "不要老人数据" → {"老人"}
    "除了合同以外的信息" → {"合同"}
    "排除心率，只要血氧" → {"心率"}
    """
    negated = set()
    for m in _NEGATION_PATTERN.finditer(query):
        term = m.group(1).strip()
        # 去除尾部干扰词（的数据/数据/的信息/信息/的记录/记录/的列表/以外/的等）
        term = re.sub(
            r'(以外|的数据|的信息|的记录|的列表|的详情|的内容|的数据|的|了|数据|信息|记录|列表|详情|内容)$',
            '', term
        ).strip()
        # 去除前部的"的"
        term = re.sub(r'^的', '', term).strip()
        if len(term) >= 2:
            negated.add(term)
    return negated


def _check_field_endpoint_hint(query: str, endpoint_path: str) -> float:
    """
    字段-端点关联消歧：检查查询中的字段是否更匹配某个端点
    
    如"老人的心率" → 心率字段 → 心率监测端点 > 老人护理端点
    返回额外的消歧加分
    """
    boost = 0.0
    
    # 字段 → 优先端点 映射表
    FIELD_ENDPOINT_HINT = {
        "心率": "/dev-api/watchData/watchHeartrate/watchHeartrate/list",
        "心跳": "/dev-api/watchData/watchHeartrate/watchHeartrate/list",
        "脉搏": "/dev-api/watchData/watchHeartrate/watchHeartrate/list",
        "血氧": "/dev-api/watchData/watchOxygen/watchOxygen/list",
        "氧气": "/dev-api/watchData/watchOxygen/watchOxygen/list",
        "体温": "/dev-api/oldCare/oldCare/list",
        "血压": "/dev-api/oldCare/oldCare/list",
        "血糖": "/dev-api/oldCare/oldCare/list",
        "过敏": "/dev-api/oldCare/oldCare/list",
        "饮食": "/dev-api/oldCare/oldCare/list",
        "护理等级": "/dev-api/nurseLevel/nurseLevel/list",
        "护理级别": "/dev-api/nurseLevel/nurseLevel/list",
        "合同编号": "/dev-api/contract/contract/list",
        "合同金额": "/dev-api/contract/contract/list",
        "到期日期": "/dev-api/contract/contract/list",
        "房间号": "/dev-api/devManagement/bedroomsManagement/list",
        "床位号": "/dev-api/devManagement/bedroomsManagement/list",
        "身份证": "/dev-api/elderlyCare/enrollmentView/list",
        "医保": "/dev-api/elderlyCare/enrollmentView/list",
        "评估结果": "/dev-api/ability/ability/list",
        "评估分数": "/dev-api/ability/ability/list",
    }
    
    for field_name, hint_path in FIELD_ENDPOINT_HINT.items():
        if field_name in query and endpoint_path == hint_path:
            boost += 0.3  # 字段提示加分
            break
    
    return boost


def _rule_based_match_endpoint(user_query: str) -> list:
    """
    v5 多维加权评分引擎 —— 用关键词匹配找到候选端点，按匹配得分排序

    评分维度：
      L1 语义映射表匹配（score=0.9）—— "氧气"→"血氧监测"等
      L2 端点名称精确匹配（score=1.0）
      L3 别名精确匹配（score=0.8，按长度加权）
      L4 缩写展开匹配（score=0.7）
      L5 同义词组匹配（score=0.6）—— "老人家"→"老人"
      L6 部分关键词匹配（score=0.4）—— "老人"匹配"老人档案"
      L7 模糊匹配（score=0.3）
      L8 模块名/路径片段匹配（score=0.1~0.15）

    额外机制：
      - 否定词排除："不要XX" → 排除匹配XX的端点
      - 字段-端点消歧：字段级提示选择最佳端点
      - 同义词扩展：将查询中的同义词标准化后匹配
      - 反馈学习：基于历史数据 boost 确认过的端点

    返回: [{"endpoint": ep_dict, "score": float, "matched_keywords": [...]}]
    """
    query = user_query.strip()

    # Step 0: 提取否定词，用于排除端点
    negated_terms = _extract_negated_terms(query)

    # Step 1: 同义词扩展 —— 将查询中的口语表述标准化
    expanded_query = _expand_query_with_synonyms(query)

    # Step 2: 缩写展开 —— "体检"→"能力评估"等
    abbr_expanded = query
    for abbr, full in ABBREVIATION_MAP.items():
        if abbr in abbr_expanded:
            abbr_expanded = abbr_expanded.replace(abbr, full)

    # v5: 同义词替换版本 —— 将查询中的同义词替换为标准词
    #     "老人家档案" → "老人档案"，使别名 "老人档案" 能精确匹配
    synonym_replaced = query
    for syn, standard in sorted(_SYNONYM_INDEX.items(), key=lambda x: -len(x[0])):
        if syn != standard and syn in synonym_replaced:
            synonym_replaced = synonym_replaced.replace(syn, standard)

    # 用扩展后的查询做匹配，但也保留原始查询
    match_query = f"{expanded_query} {abbr_expanded}"

    candidates = []
    seen_paths = set()

    # ── L1: 语义映射表优先匹配 ──
    for semantic_term, target_path in SEMANTIC_ENDPOINT_MAP.items():
        if semantic_term in query or semantic_term in match_query:
            for ep in KNOWN_ENDPOINTS:
                if ep["path"] == target_path:
                    candidates.append({
                        "endpoint": ep,
                        "score": SCORE_SEMANTIC,
                        "matched_keywords": [semantic_term],
                        "match_type": "semantic",
                    })
                    seen_paths.add(target_path)
                    break

    # ── L2~L8: 逐端点多维评分 ──
    for ep in KNOWN_ENDPOINTS:
        if ep["path"] in seen_paths:
            continue

        path = ep["path"]
        name = ep["name"]
        module = ep["module"]
        aliases = ENDPOINT_ALIASES.get(path, [])

        score = 0.0
        matched = []
        match_types = []

        # 检查是否被否定词排除
        is_excluded = False
        for neg_term in negated_terms:
            if neg_term in name or any(neg_term in alias for alias in aliases):
                is_excluded = True
                break
        if is_excluded:
            continue

        # L2: 端点名称精确匹配
        if name in query or name in match_query or name in synonym_replaced:
            score += SCORE_EXACT_NAME
            matched.append(name)
            match_types.append("exact_name")

        # L3: 别名精确匹配（取最长匹配，支持同义词替换后的匹配）
        best_alias_score = 0.0
        best_alias = None
        for alias in sorted(aliases, key=len, reverse=True):
            if alias in query or alias in match_query or alias in synonym_replaced:
                # 长别名权重更高
                weight = SCORE_ALIAS_EXACT * min(len(alias) / 4.0, 1.2)
                if weight > best_alias_score:
                    best_alias_score = weight
                    best_alias = alias
        if best_alias:
            score += best_alias_score
            matched.append(best_alias)
            match_types.append("alias_exact")

        # L4: 缩写展开匹配
        for abbr, full in ABBREVIATION_MAP.items():
            if abbr in query and (full == name or full in name):
                score += SCORE_ABBR
                matched.append(f"{abbr}→{full}")
                match_types.append("abbreviation")
                break

        # L5: 同义词组匹配（v5 修正：方向是 用户用了同义词 → 标准词在别名中）
        synonym_matched = False
        for alias in aliases:
            if synonym_matched:
                break
            for syn, standard in _SYNONYM_INDEX.items():
                if syn != standard and syn in query and standard in alias:
                    # 用户用了同义词 syn（如"老人家"），标准词 standard（如"老人"）在别名中
                    if syn not in matched and standard not in matched:
                        score += SCORE_SYNONYM
                        matched.append(f"{syn}={standard}")
                        match_types.append("synonym")
                        synonym_matched = True
                        break

        # L6: 部分关键词匹配（短词如"老人"匹配包含它的别名如"老人档案"）
        partial_added = set()
        for alias in aliases:
            if alias in matched:
                continue
            query_words = _extract_key_phrases(query, min_len=2, max_len=4)
            for qw in query_words:
                if qw in alias and qw not in partial_added and qw not in matched:
                    weight = SCORE_PARTIAL * min(len(qw) / 4.0, 1.0)
                    score += weight
                    partial_added.add(qw)
                    if alias not in matched:
                        matched.append(alias)
                        match_types.append("partial")

        # L7: 模糊匹配（仅在分值较低时启用，避免过度匹配）
        if score < 0.5:
            best_fuzzy = _fuzzy_match_key(name, aliases + [name], threshold=0.75)
            if best_fuzzy and best_fuzzy not in matched:
                sim = SequenceMatcher(None, query, best_fuzzy).ratio()
                if sim > 0.5:
                    score += SCORE_FUZZY * sim
                    matched.append(f"~{best_fuzzy}")
                    match_types.append("fuzzy")

        # L8: 模块名和路径片段匹配（低权重）
        if module and module.lower() in query.lower():
            score += SCORE_MODULE
            matched.append(module)
            match_types.append("module")

        path_segments = [s for s in path.split("/") if s and s not in ("dev-api", "list")]
        for seg in path_segments:
            seg_lower = seg.lower()
            if seg_lower in query.lower():
                score += SCORE_PATH_SEG
                matched.append(seg)

        # ── 字段-端点消歧加分 ──
        field_boost = _check_field_endpoint_hint(query, path)
        if field_boost > 0:
            score += field_boost
            match_types.append("field_hint")

        if score > 0:
            candidates.append({
                "endpoint": ep,
                "score": round(score, 3),
                "matched_keywords": matched,
                "match_type": "+".join(set(match_types)) if match_types else "alias",
            })

    # ── 应用用户反馈 boost ──
    candidates = get_feedback_boost(user_query, candidates)

    # 按得分降序排列，同等分数时按关键词在查询中出现的位置排序（越靠前越好）
    candidates.sort(key=lambda x: (
        -x["score"],
        min((query.find(kw.split("~")[0].split("→")[0]) for kw in x.get("matched_keywords", []) if query.find(kw.split("~")[0].split("→")[0]) >= 0), default=9999)
    ))
    return candidates


def _extract_key_phrases(text: str, min_len: int = 2, max_len: int = 4) -> list:
    """
    从查询文本中提取关键短语（滑动窗口）

    用于部分关键词匹配：如果用户说"老人身体"，提取["老人", "人身体", "老人身", "身体"]
    然后检查这些短语是否出现在任何别名中
    """
    # 先去除动词前缀
    cleaned = re.sub(
        r'(获取|提取|查找|查看|获取到|拿到|导出|需要的|想要的|想要|需要|找出|调取|拉取|查询|搜索|找|看|所有|全部)\s*',
        '', text
    )
    phrases = []
    for win_len in range(max_len, min_len - 1, -1):
        for i in range(len(cleaned) - win_len + 1):
            phrase = cleaned[i:i + win_len]
            if phrase:
                phrases.append(phrase)
    return phrases


# ══════════════════════════════════════════════════════════════
# 5. 规则预匹配：字段提取
# ══════════════════════════════════════════════════════════════

def _rule_based_extract_fields(user_query: str, matched_endpoint_keywords: list = None) -> list:
    """
    从自然语言中提取用户想要的字段名（v5 增强）

    优化策略：
      1. 先检查 "所有/全部" 模式 → 返回空列表表示全部
      2. 从查询中移除已匹配的端点关键词（表名/模块名），避免误提取
      3. 移除动作动词前缀和"XX的"/"XX中的" 从属前缀
      4. 按分隔符拆分候选字段
      5. 与 FIELD_SYNONYMS 的 key 做精确/模糊匹配
      6. v5新增：同义词组标准化 —— "联系电话"→"联系方式"
      7. v5新增：否定词排除 —— "不要身份证" → 排除"身份证"
      8. v5新增：精确度阈值提升 —— 模糊匹配阈值从 0.65 提升到 0.70
      9. 对复合短语做智能拆分（如"老人身体状况"→"身体状况"）
     10. 未匹配字段尽量保留原样，作为候选（让后续LLM或用户判断）
    """
    query = user_query.strip()

    # v5: 提取否定词，排除不想查的字段
    negated_fields = _extract_negated_terms(query)

    # 检查 "所有" 模式
    all_match = _ALL_PATTERN.search(query)
    if all_match:
        after_all = all_match.group(1).strip()
        if any(kw in after_all for kw in ["字段", "数据", "信息", "记录", "内容"]):
            return []

    # Step 1: 移除已匹配的端点关键词，避免"合同管理的合同编号"被误拆
    cleaned_query = query
    clean_kws = []  # 已清理的端点关键词（供后续反馈字段清洗复用）
    if matched_endpoint_keywords:
        # 清理 matched_keywords 中的标注符号（如"老人~老人家"取前半部分）
        for kw in matched_endpoint_keywords:
            clean_kw = kw.split("~")[0].split("→")[0].strip()
            if clean_kw:
                clean_kws.append(clean_kw)
        long_kws = sorted(
            [kw for kw in set(clean_kws) if len(kw) >= 3],
            key=len, reverse=True
        )
        for kw in long_kws:
            # 仅在「跟从属词或标点边界」时剔除端点关键词，避免误伤"心率数据"这类字段请求
            cleaned_query = re.sub(
                re.escape(kw) + r'(?=的|中的|里|内|页面|列表|模块|表)',
                '，', cleaned_query
            )
            cleaned_query = re.sub(
                r'(^|[，,、\s])\s*' + re.escape(kw) + r'\s*(?=[，,、]|$)',
                r'\1', cleaned_query
            )

    # Step 2: 移除常见动作动词
    cleaned_query = re.sub(
        r'(获取|提取|查找|查看|获取到|拿到|导出|需要的|想要的|想要|需要|找出|调取|拉取|查询|搜索|找|看)\s*',
        '', cleaned_query
    )

    # Step 3: 移除 "XX的" 和 "XX中的" 从属前缀
    cleaned_query = re.sub(r'[，,、\s]*的\s*', '，', cleaned_query)
    cleaned_query = re.sub(r'[，,、\s]*中的\s*', '，', cleaned_query)
    cleaned_query = re.sub(r'[，,、\s]*之\s*', '，', cleaned_query)

    # Step 4: 清理页面/列表/模块等上下文词
    cleaned_query = re.sub(r'(页面|列表|模块|系统|表格|中|里|内)', '，', cleaned_query)

    # Step 4b: 清理"所有"/"全部"等量词前缀
    cleaned_query = re.sub(r'所有|全部|每个|每位|各个|各', '', cleaned_query)

    # Step 4c: 剥离"行数限制"数量表达式（前50条 / 50条数据 / 第10条 等）
    #           —— 关键：避免"50条"被当成一个数据字段去匹配
    cleaned_query = _LIMIT_STRIP_RE.sub('，', cleaned_query)

    # Step 5: 按分隔符拆分
    parts = _FIELD_DELIMITERS.split(cleaned_query)

    extracted_fields = []
    for part in parts:
        part = part.strip().strip('，,、。.!！?？ \t\n\r')
        if not part or len(part) < 2:
            continue

        # 防御：含数字+量词（条/个/行/位）或纯数字的片段，绝不应作为字段
        if re.search(r'\d\s*(?:条|个|行|位)', part) or re.match(r'^\d+$', part):
            continue

        # v5: 排除否定字段
        if part in negated_fields:
            continue

        # 清理字段名后缀
        part = re.sub(r'\s*(信息|数据|字段|内容|情况|详情)$', '', part).strip()
        if not part or len(part) < 2:
            continue

        # v5: 同义词组标准化 —— "联系电话"→"联系方式"等
        for syn, standard in _SYNONYM_INDEX.items():
            if part == syn and syn != standard:
                part = standard
                break

        # 精确匹配同义词表
        if part in FIELD_SYNONYMS:
            if part not in negated_fields:
                extracted_fields.append(part)
            continue

        # 检查是否是非字段干扰词（端点关键词等），跳过模糊匹配
        if part in {"老人", "长者", "社区", "合同", "设备", "活动", "用户", "角色", "字典"}:
            continue

        # v5: 模糊匹配阈值提升（0.65 → 0.70），减少误匹配
        best_match = _fuzzy_match_key(part, list(FIELD_SYNONYMS.keys()), threshold=0.70)
        if best_match:
            if best_match not in negated_fields:
                extracted_fields.append(best_match)
            continue

        # 智能拆分复合短语
        if len(part) >= 4:
            sub_fields = _smart_split_compound(part)
            if sub_fields:
                extracted_fields.extend(f for f in sub_fields if f not in negated_fields)
                continue

        # 尝试去除前缀后匹配
        prefix_stripped = re.sub(r'^(老人|长者|病人|住户|入住)的?\s*', '', part)
        if prefix_stripped != part and prefix_stripped in FIELD_SYNONYMS:
            if prefix_stripped not in negated_fields:
                extracted_fields.append(prefix_stripped)
            continue
        if prefix_stripped != part:
            best_fs = _fuzzy_match_key(prefix_stripped, list(FIELD_SYNONYMS.keys()), threshold=0.70)
            if best_fs:
                if best_fs not in negated_fields:
                    extracted_fields.append(best_fs)
                continue

        # 如果没匹配到同义词但长度合理，保留作为候选字段
        non_field_words = {
            "页面", "系统", "平台", "管理", "列表", "所有", "全部", "数据",
            "信息", "字段", "内容", "情况", "详情", "需要", "想要", "获取",
            "提取", "查看", "查找", "导出", "以及", "和", "与", "或",
            "老人", "长者", "老人姓名", "社区", "合同", "设备", "活动", "用户",
            "角色", "字典", "床位", "房间", "护理", "心率", "血氧", "血糖",
            "采集", "爬取", "抓取", "表单", "表格",
            "老人家", "看护", "照护", "住户",
        }
        if 2 <= len(part) <= 8 and part not in non_field_words:
            if part not in negated_fields:
                extracted_fields.append(part)

    # 去重保持顺序
    seen = set()
    result = []
    for f in extracted_fields:
        if f not in seen:
            seen.add(f)
            result.append(f)

    return result


def _smart_split_compound(phrase: str) -> list:
    """
    智能拆分复合短语为已知字段

    "老人身体状况" → 尝试匹配"身体状况"（整体）、"身体"（子串）
    跳过非字段词（端点关键词等）
    返回匹配到的字段列表
    """
    n = len(phrase)
    results = []

    # 非字段词（端点关键词，不应作为字段提取）
    non_field_prefix = {"老人", "长者", "病人", "住户"}

    # 从长到短尝试子串匹配
    for sub_len in range(min(n, 6), 1, -1):
        for i in range(n - sub_len + 1):
            sub = phrase[i:i + sub_len]
            if sub in FIELD_SYNONYMS:
                results.append(sub)

    # 去重
    seen = set()
    uniq = []
    for r in results:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq


def _fuzzy_match_key(query: str, candidates: list, threshold: float = 0.6) -> str:
    """使用字符串相似度模糊匹配"""
    best_score = 0
    best_match = None
    for cand in candidates:
        score = SequenceMatcher(None, query, cand).ratio()
        if score > best_score:
            best_score = score
            best_match = cand
    return best_match if best_score >= threshold else None


# ══════════════════════════════════════════════════════════════
# 5.5 页面采集意图检测
# ══════════════════════════════════════════════════════════════

def _api_signal_score(query: str) -> float:
    """估算查询指向 API 端点的信号强度（最优端点匹配得分）。用于页面/API 判定竞争。"""
    try:
        cands = _rule_based_match_endpoint(query)
        if cands:
            return float(cands[0]["score"])
    except Exception:
        pass
    return 0.0


def _detect_page_scrape_intent(user_query: str) -> dict:
    """
    检测用户是否想要爬取网页（而非调 API）

    触发条件（任一满足）：
      1. 包含完整 URL（http:// 或 https://）
      2. 包含 Vue 路由路径（/xxx/yyy）
      3. 包含页面采集关键词（"爬取页面""表单数据"等）

    P1-b: 对于「XX页面」这类由实体/关键词触发的页面意图，会与 API 端点竞争。
          采用「信号分高者胜」：当 API 端点信号明显更强时，交由 API 路径处理，
          避免把"获取XX页面的数据"误判为 DOM 采集（DOM 更慢更脆）。

    返回: {"is_page": True, "url_or_route": "...", "matched_by": "url|keyword|route", ...}
          or {"is_page": False}
    """
    query = user_query.strip()

    # ===== 检测1: 完整 URL =====
    url_match = re.search(r'(https?://[^\s，,、。\u4e00-\u9fff]+)', query)
    if url_match:
        url = url_match.group(1).rstrip('，,、。')
        remaining = query.replace(url_match.group(1), '')
        # 传入空列表让字段提取器做通用清理（去除动作词等）
        fields = _rule_based_extract_fields(remaining, matched_endpoint_keywords=[])
        # 查找匹配的已知路由
        route_name = "自定义URL"
        for r in KNOWN_ROUTES:
            if r["route"] in url:
                route_name = r["name"]
                break
        return {
            "is_page": True,
            "url_or_route": url,
            "source_type": "page",
            "route_name": route_name,
            "fields": fields,
            "matched_by": "url",
            "confidence": 0.95,
        }

    # ===== 检测2: Vue 路由路径 =====
    route_match = re.search(r'(/[a-zA-Z][^\s，,、。\u4e00-\u9fff]{2,})', query)
    if route_match:
        route = route_match.group(1).rstrip('，,、。')
        # 确认这是一个已知路由或类路由模式
        is_likely_route = False
        for r in KNOWN_ROUTES:
            if r["route"] == route or r["route"].startswith(route):
                is_likely_route = True
                break
        if not is_likely_route:
            # 检查是否是类路由格式 /xxx/yyy
            if re.match(r'^/[a-zA-Z]+/[a-zA-Z]+', route):
                is_likely_route = True

        if is_likely_route:
            remaining = query.replace(route_match.group(1), '')
            fields = _rule_based_extract_fields(remaining, matched_endpoint_keywords=[])
            # 找对应的路由名
            route_name = "自定义路由"
            for r in KNOWN_ROUTES:
                if r["route"] == route:
                    route_name = r["name"]
                    break
            return {
                "is_page": True,
                "url_or_route": route,
                "source_type": "page",
                "route_name": route_name,
                "fields": fields,
                "matched_by": "route",
                "confidence": 0.90,
            }

    # ===== 检测3: 页面关键词 + 页面路由别名 =====
    has_page_kw = any(kw in query for kw in _PAGE_SCRAPE_KEYWORDS)

    # v5.1: 检测 "XX页面" "XX网页" 模式 —— 即使用户没用采集关键词，
    #       但只要查询中出现"页面"/"网页"且前面有实体名，就尝试页面匹配
    page_entity_match = re.search(r'([\u4e00-\u9fff\w]{2,10})\s*(页面|网页)', query)
    if page_entity_match and not has_page_kw:
        # 用户提到了"XX页面"，设置 has_page_kw = True 走页面路由匹配
        has_page_kw = True

    # 尝试从查询中匹配已知页面路由
    matched_route = None
    for alias, route in sorted(_PAGE_ROUTE_INDEX.items(), key=lambda x: -len(x[0])):
        if alias in query:
            # 检查是否确实在说页面（而非 API）
            if has_page_kw:
                matched_route = route
                break
            # 或者用户用了"表单""表格"等明确指页面的词
            if any(kw in query for kw in ["表单", "表格", "页面上", "网页"]):
                matched_route = route
                break

    if matched_route:
        # P1-b: 信号分高者胜——若 API 端点信号明显更强，交给 API 路径（DOM 更慢更脆）
        if _api_signal_score(query) >= 0.85:
            return {"is_page": False}
        route_name = "页面"
        for r in KNOWN_ROUTES:
            if r["route"] == matched_route:
                route_name = r["name"]
                break
        # 传入已匹配的页面关键字用于清理
        matched_kw = [alias for alias, route in sorted(_PAGE_ROUTE_INDEX.items(), key=lambda x: -len(x[0])) if alias in query][:3]
        fields = _rule_based_extract_fields(query, matched_endpoint_keywords=matched_kw)
        return {
            "is_page": True,
            "url_or_route": matched_route,
            "source_type": "page",
            "route_name": route_name,
            "fields": fields,
            "matched_by": "keyword",
            "confidence": 0.85,
        }

    # ===== 检测4: 明确说"表单"或"表格"但没有API端点匹配 =====
    if any(kw in query for kw in ["表单", "页面表格", "网页表格"]):
        ep_candidates = _rule_based_match_endpoint(query)
        if not ep_candidates or ep_candidates[0]["score"] < 0.5:
            # 没有合适的API端点 → 尝试页面采集
            # 用关键词匹配找最接近的页面路由
            best_route = _fuzzy_match_route(query)
            if best_route:
                route_name = "页面"
                for r in KNOWN_ROUTES:
                    if r["route"] == best_route:
                        route_name = r["name"]
                        break
                fields = _rule_based_extract_fields(query)
                return {
                    "is_page": True,
                    "url_or_route": best_route,
                    "source_type": "page",
                    "route_name": route_name,
                    "fields": fields,
                    "matched_by": "fallback",
                    "confidence": 0.65,
                }

    # ===== 检测5: "XX页面"模式，但无精确别名匹配 → 模糊路由匹配 =====
    if page_entity_match:
        entity_name = page_entity_match.group(1)
        # 尝试模糊匹配页面路由
        best_route = _fuzzy_match_route(entity_name)
        if best_route:
            # P1-b: 信号分高者胜——若 API 端点信号更强，交给 API 路径
            if _api_signal_score(query) >= 0.7:
                return {"is_page": False}
            route_name = "页面"
            for r in KNOWN_ROUTES:
                if r["route"] == best_route:
                    route_name = r["name"]
                    break
            fields = _rule_based_extract_fields(query, matched_endpoint_keywords=[entity_name])
            return {
                "is_page": True,
                "url_or_route": best_route,
                "source_type": "page",
                "route_name": route_name,
                "fields": fields,
                "matched_by": "page_entity",
                "confidence": 0.70,
            }

    return {"is_page": False}


def _fuzzy_match_route(query: str) -> str:
    """模糊匹配最接近的页面路由（搜索别名 + 路由名称）"""
    best_score = 0
    best_route = None
    # 搜索页面路由别名
    for alias, route in sorted(_PAGE_ROUTE_INDEX.items(), key=lambda x: -len(x[0])):
        score = SequenceMatcher(None, query, alias).ratio()
        # 长关键词给bonus
        if len(alias) >= 3:
            score += 0.05
        if score > best_score:
            best_score = score
            best_route = route
    # 也搜索路由名称
    if best_score < 0.5:
        for r in KNOWN_ROUTES:
            score = SequenceMatcher(None, query, r["name"]).ratio()
            if len(r["name"]) >= 3:
                score += 0.05
            if score > best_score:
                best_score = score
                best_route = r["route"]
    if best_score >= 0.4:
        return best_route
    return None


def _build_page_intent_result(page_intent: dict, user_query: str = "") -> dict:
    """构建页面采集意图的标准输出结构"""
    url_or_route = page_intent["url_or_route"]
    fields = page_intent.get("fields", [])

    field_descriptions = {}
    for f in fields:
        synonyms = FIELD_SYNONYMS.get(f, [])
        if synonyms:
            field_descriptions[f] = f"可能的表格列名: {', '.join(synonyms[:3])}"
        else:
            field_descriptions[f] = "用户需要的页面字段"

    # 是否分页（默认对列表类页面启用）
    is_paginated = page_intent.get("matched_by") != "url" or any(
        kw in str(url_or_route) for kw in ["list", "overview", "checkin", "nursing", "manage"]
    )

    # 解析查询中的过滤条件（时间/状态/关键词），页面模式同样需要应用
    filters = extract_filters(user_query) if user_query else None
    # 解析行数限制（前N条 / N条数据 等），页面模式同样需要应用
    limit = extract_limit(user_query) if user_query else None

    return {
        "source_type": "page",
        "url_or_route": url_or_route,
        "route_name": page_intent.get("route_name", "页面"),
        "fields": fields,
        "field_descriptions": field_descriptions,
        "description": f"从页面 [{page_intent.get('route_name', url_or_route)}] 中采集数据"
                       + (f"，提取字段: {', '.join(fields)}" if fields else "（全部表格数据）"),
        "confidence": page_intent.get("confidence", 0.85),
        "reasoning": f"页面采集模式: 用户指定了URL/路由 [{url_or_route}] (匹配方式: {page_intent.get('matched_by')})",
        "rule_based": True,
        "matched_by": page_intent.get("matched_by", "keyword"),
        "is_paginated": is_paginated,
        "filters": filters,
        "limit": limit,
    }

def _rule_based_parse(user_query: str) -> dict:
    """
    纯规则预匹配，不调用 LLM（v5 增强）

    返回结构与 LLM 输出一致，但带 rule_based=True 标记
    如果匹配失败返回 None

    v5 增强：
      - 置信度计算考虑匹配类型质量（exact > alias > partial > fuzzy）
      - 多端点竞争时更精确的消歧
      - 反馈 boost 信息融入 reasoning
    """
    ep_candidates = _rule_based_match_endpoint(user_query)

    if not ep_candidates:
        return None

    best = ep_candidates[0]
    ep = best["endpoint"]
    score = best["score"]
    match_type = best.get("match_type", "alias")

    # 提取字段（传入已匹配的端点关键词用于清理）
    matched_kw = best.get("matched_keywords", [])
    fields = _rule_based_extract_fields(user_query, matched_endpoint_keywords=matched_kw)

    # 二次过滤：剔除被误当成字段的端点名/别名（如"长者档案""入住概览"），
    # 但保留真实字段同义词（如"心率""血氧""血压"——它们同时也是端点别名）
    _ep_kw_set = set()
    for _kw in matched_kw:
        _ck = _kw.split("~")[0].split("→")[0].strip()
        if _ck:
            _ep_kw_set.add(_ck)
    if _ep_kw_set and fields:
        fields = [f for f in fields if (f not in _ep_kw_set) or (f in FIELD_SYNONYMS)]

    # v5: 反馈历史字段建议（必须清洗，避免把端点名/数量词/动词当作字段缓存并反灌）
    if best.get("feedback_fields") and not fields:
        _fb = best.get("feedback_fields", [])
        # 端点名/别名绝不可能是字段，但若该词本身是已知字段同义词（如"心率"）则保留
        _fb_set = set(clean_kws)
        _fb = [f for f in _fb if (f not in _fb_set) or (f in FIELD_SYNONYMS)]
        _fb = _sanitize_fields(_fb)
        if _fb:
            fields = _fb

    # v5: 置信度计算 —— 考虑匹配类型质量
    has_exact = "exact_name" in match_type
    has_semantic = "semantic" in match_type
    has_alias = "alias_exact" in match_type
    has_field_hint = "field_hint" in match_type
    has_feedback = "feedback_boost" in best

    if score >= 2.0:
        confidence = min(0.95, 0.75 + score * 0.1)
    elif score >= 1.0:
        confidence = 0.6 + (score - 1.0) * 0.15
    else:
        confidence = 0.35 + score * 0.25

    # 多候选竞争时降低置信度
    if len(ep_candidates) > 1 and ep_candidates[1]["score"] > score * 0.7:
        confidence *= 0.8

    # 字段提示加分
    if has_field_hint:
        confidence = min(confidence + 0.05, 0.98)

    # 反馈确认加分
    if has_feedback:
        confidence = min(confidence + 0.03, 0.98)

    confidence = min(confidence, 0.98)

    # 反馈学习覆盖：用户已为相似查询明确纠正/确认过端点时，信任学习结果，
    # 把置信度抬到 0.85 以上，使 parse_intent 跳过 LLM 直接采用，
    # 避免 LLM 凭「父级实体名匹配」把纠正后的子项端点又覆盖回去。
    _feedback_override = best.get("match_type") == "feedback_learned"
    if _feedback_override:
        confidence = max(confidence, 0.9)

    # 构建字段描述（提前构建，低置信度拦截可能提前 return 引用）
    field_descriptions = {}
    for f in fields:
        synonyms = FIELD_SYNONYMS.get(f, [])
        if synonyms:
            field_descriptions[f] = f"用户需要的数据字段，可能的JSON key: {', '.join(synonyms[:3])}"
        else:
            field_descriptions[f] = "用户需要的自定义字段"

    # v5: 候选端点列表（提前构建，低置信度拦截可能提前 return 引用）
    endpoint_candidates = []
    for c in ep_candidates[:3]:
        cand = {
            "name": c["endpoint"]["name"],
            "score": round(c["score"], 2),
            "match_type": c.get("match_type", ""),
        }
        if c.get("feedback_boost"):
            cand["feedback_boost"] = c["feedback_boost"]
        endpoint_candidates.append(cand)

    # ── 低置信度拦截（2026-07-23 修复）──
    # 当置信度 < 0.5 且最佳匹配仅靠 partial/fuzzy/path_seg 时，
    # 说明端点库里很可能没有用户真正想要的接口。
    # 此时返回空 target_api + 明确提示，而非强行返回一个错误结果。
    _LOW_CONF_THRESHOLD = 0.50
    _weak_match_types = {"partial", "fuzzy", "module", "path_seg", "alias"}
    _is_weak = (
        confidence < _LOW_CONF_THRESHOLD
        and not has_exact
        and not has_semantic
        and not has_alias
        and all(t in _weak_match_types for t in match_type.split("+") if t)
    )
    if _is_weak:
        logger.warning(
            f"低置信度拦截: query={user_query[:40]!r} "
            f"best={ep['name']}({ep['path']}) conf={confidence:.2f} "
            f"type={match_type} score={score}"
        )
        # 返回"未找到"标记结果，让前端展示友好提示
        _cand_summary = ", ".join(
            f"{c['endpoint']['name']}({c['score']:.2f})" for c in ep_candidates[:5]
        )
        return {
            "target_api": "",           # 空路径 = 未匹配
            "api_name": "",
            "module": "",
            "fields": fields,
            "field_descriptions": field_descriptions if fields else {},
            "description": (
                f"未找到与「{user_query}」匹配的端点。"
                f"端点库中可能没有此接口，请先通过捕获面板录入。"
                f"(候选: {_cand_summary})"
            ),
            "confidence": round(confidence, 3),
            "reasoning": f"低置信度拦截(conf={confidence:.2f}, type={match_type}, score={score:.2f}), 候选: {_cand_summary}",
            "rule_based": True,
            "matched_keywords": matched_kw,
            "endpoint_candidates": endpoint_candidates,
            "limit": extract_limit(user_query),
            "_no_match": True,          # 前端可据此显示特殊 UI
        }

    matched_kw = best.get("matched_keywords", [])
    reasoning_parts = [f"规则匹配({match_type})"]
    reasoning_parts.append(f"关键词 [{', '.join(matched_kw)}]")
    reasoning_parts.append(f"端点 [{ep['name']}]")
    if has_field_hint:
        reasoning_parts.append("字段提示消歧")
    if has_feedback:
        reasoning_parts.append(f"反馈boost(+{best.get('feedback_boost', 0)})")
    reasoning = " → ".join(reasoning_parts)


    return {
        "target_api": ep["path"],
        "api_name": ep["name"],
        "module": ep["module"],
        "fields": fields,
        "field_descriptions": field_descriptions,
        "description": f"从{ep['name']}中提取" + ("、".join(fields) if fields else "全部字段"),
        "confidence": round(confidence, 3),
        "reasoning": reasoning,
        "rule_based": True,
        "matched_keywords": matched_kw,
        "endpoint_candidates": endpoint_candidates,
        "limit": extract_limit(user_query),
        "_feedback_override": _feedback_override,
    }


# ══════════════════════════════════════════════════════════════
# 7. LLM 增强解析 Prompt
# ══════════════════════════════════════════════════════════════

INTENT_PARSE_PROMPT = """你是一个智能爬虫规则解析器。用户会用自然语言描述想从康养平台获取什么数据。
你的任务是将用户的自然语言意图，解析为结构化的爬取规则。

## 可用的 API 端点列表（含别名关键词）：
{endpoints}

## 解析规则：
1. **端点匹配优先级**：先看用户描述中是否包含端点的"别名关键词"，有则优先选择该端点
2. **字段提取**：从用户描述中提取明确要求的字段名。注意以下常见模式：
   - "获取XX和YY" → 字段 [XX, YY]
   - "提取XX、YY以及ZZ" → 字段 [XX, YY, ZZ]
   - "所有字段/全部数据" → fields 返回空数组 []
   - "XX的信息" → 可能指整个XX模块，也可能是XX字段
3. **歧义处理**：如果用户描述模糊（如"老人数据"可能指入住概览或护理记录），选择语义最接近的端点，并在 reasoning 中说明备选项
4. **多端点提示**：如果用户需求可能涉及多个端点，在 reasoning 中标注"可能还需要查看: [其他端点名]"
5. **字段规范化**：提取的字段名用中文，保持用户的原始表述
6. 如果用户提到了页面而非数据表，优先匹配与该页面同名的 API 端点
7. **行数限制（重要）**：用户说的「数量」是指"取多少行数据"，绝非一个字段名，请解析到 limit 字段：
   - "前50条数据" / "取前50条" / "只要50条" / "50条记录" → limit = 50（取前50行）
   - "第10条" → limit = 1（仅取第10条这一行）
   - "后30条" / "最后30条" → limit = 30（取末尾30行）
   - 没有数量表达时 limit = null（返回全部匹配数据）
   - 注意："50条"这种带量词的数字绝对不要放进 fields 数组

{rule_hint}

## 输出格式（严格 JSON，不要包含任何其他内容）：
{{
  "target_api": "完全匹配的 API 路径",
  "api_name": "对应的表名",
  "fields": ["字段1", "字段2", ...],
  "field_descriptions": {{"字段1": "这是用户想要的长者姓名", ...}},
  "description": "一句话总结你要提取什么",
  "confidence": 0.0-1.0,
  "reasoning": "匹配依据说明",
  "alternative_apis": ["备选端点路径（如有歧义）"],
  "limit": 50
}}

## 用户需求：
{user_query}

请直接输出 JSON："""


def _build_rule_hint(rule_result: dict, user_query: str = "") -> str:
    """构建规则预匹配的提示信息，注入 LLM Prompt"""
    if not rule_result and not user_query:
        return ""

    lines = []

    if rule_result:
        lines.append("## 规则预匹配结果（供参考，你可以修正）：")
        lines.append(f"- 预匹配端点: {rule_result['api_name']} ({rule_result['target_api']})")
        lines.append(f"- 预匹配字段: {rule_result['fields'] if rule_result['fields'] else '全部字段'}")
        rlimit = rule_result.get("limit")
        if rlimit:
            lines.append(f"- 预匹配行数限制: {rlimit['mode']}={rlimit['limit']}（取前/后/单条 N 行，不要当作字段）")
        lines.append(f"- 规则置信度: {rule_result['confidence']}")
        if rule_result.get("endpoint_candidates"):
            cands = ", ".join(f"{c['name']}(score={c['score']})" for c in rule_result["endpoint_candidates"])
            lines.append(f"- 候选端点排名: {cands}")
        lines.append("- 如果你认为规则匹配的端点不正确，可以选择其他端点，但需要在 reasoning 中说明原因")

    # v5: 否定词提示
    if user_query:
        negated = _extract_negated_terms(user_query)
        if negated:
            lines.append(f"\n## 重要：用户已明确排除以下内容：{', '.join(negated)}")
            lines.append("- 请不要选择与上述排除内容相关的端点")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# 8. 字段映射 Prompt（增强版）
# ══════════════════════════════════════════════════════════════

EXTRACT_PROMPT = """你是一个数据字段映射器。下面是从康养平台 API 返回的原始 JSON 数据。

## 需要提取的字段：
{fields}

## 字段说明：
{field_descriptions}

## 已知的中文字段到英文JSON key的映射关系（参考，可能有误，请以实际数据为准）：
{known_mappings}

## 原始数据（前 3 条样例）：
{sample_data}

## 原始数据中所有可用的 key 列表：
{available_keys}

## 请完成以下任务：
1. 确认每个用户字段在原始数据中对应的实际 key 名称
2. 使用上面"已知映射关系"作为参考，但以实际数据中的 key 为准
3. 如果用户字段在数据中找不到对应的 key，标记到 missing_fields 中
4. 不要遗漏任何用户指定的字段

请输出纯 JSON：
{{
  "field_mapping": {{"用户要求的中文字段名": "实际数据中的JSON key", ...}},
  "available_fields": ["实际存在的字段key列表"],
  "missing_fields": ["数据中找不到的字段"],
  "columns_order": ["最终展示的列顺序（用户字段名）"]
}}
"""


# ══════════════════════════════════════════════════════════════
# 8.5 智能模式对话分支（chat vs crawl 分类 + LLM 闲聊）
# ══════════════════════════════════════════════════════════════

# 对话类意图的显式信号（命中即倾向聊天，除非同时命中强爬取信号）
_CHAT_PATTERNS = [
    # 身份/自我介绍
    r'你是谁', r'你是什么', r'你叫什么', r'你的名字', r'介绍一下你', r'介绍下你',
    r'你能做什么', r'你会什么', r'你有哪些功能', r'你有什么用', r'你的作用',
    r'你是什么助手', r'你是干啥的',
    # 问候
    r'^[\s,，。.!！?？]*?(你好|您好|hi|hello|嗨|在吗|在不在)[\s,，。.!！?？]*?$',
    # 帮助/用法
    r'怎么用', r'如何使用', r'怎么操作', r'如何操作', r'使用说明', r'功能介绍',
    r'帮助', r'教程', r'指引', r'怎么玩',
    # 感谢
    r'^[\s,，。.!！?？]*?(谢谢|感谢|多谢|thanks|thank you)[\s,，。.!！?？]*?$',
    # 纯知识问答（无爬取信号时）
    r'什么是', r'为什么', r'如何', r'怎样', r'怎么', r'哪个好', r'区别',
    r'是怎么回事', r'有什么意义', r'是什么意思',
]

# 强爬取信号（命中即判为爬取，优先于聊天）
_CRAWL_PATTERNS = [
    r'获取', r'提取', r'查询', r'爬取', r'爬', r'抓取', r'采集', r'导出',
    r'统计', r'分析', r'看看', r'找(?!不)', r'列表', r'记录',
    r'数据$', r'数据吗', r'记录吗', r'信息吗', r'名单', r'明细',
    r'全部', r'所有', r'每条', r'每位',
]

_CHAT_RE = [re.compile(p, re.IGNORECASE) for p in _CHAT_PATTERNS]
_CRAWL_RE = [re.compile(p, re.IGNORECASE) for p in _CRAWL_PATTERNS]


def is_chat_query(query: str) -> bool:
    """判断用户查询是否为「闲聊 / 问答」类意图（而非数据爬取）。

    策略（高优先级在前）：
      1. 命中任一强爬取信号 → 直接判为 crawl（避免「你能帮我获取X」被当闲聊）。
      2. 命中对话信号且未命中爬取信号 → 判为 chat。
      3. 两者皆无 → 保守判为 crawl（走原有解析，不误闯入闲聊）。
    """
    q = (query or "").strip()
    if not q:
        return False

    has_crawl = any(rx.search(q) for rx in _CRAWL_RE)
    if has_crawl:
        return False

    has_chat = any(rx.search(q) for rx in _CHAT_RE)
    return has_chat


def _call_text_llm(config: dict, messages: list, timeout: int = 120) -> str:
    """调用 LLM 返回纯文本（对话用，不强制 JSON）。失败时抛异常由上层兜底。"""
    url = config["api_base"].rstrip("/") + "/chat/completions"
    api_key = config["api_key"]
    model = config["model"]

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 800,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    })
    resp = urllib.request.urlopen(req, timeout=timeout)
    data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


_CHAT_SYSTEM_PROMPT = (
    "你是「康养大数据统计分析平台」的智能助手，集成在数据采集控制台的智能模式里。"
    "你的职责有两部分：\n"
    "1) 闲聊与答疑：回答用户关于平台、功能、康养领域的一般性问题，语气友好、简洁、专业。\n"
    "2) 引导数据获取：当用户想从平台提取数据时，告诉他可以用自然语言描述需求，"
    "例如「获取压疮评估的数据」「查询本月心率异常的长者」，系统会自动解析并爬取。\n"
    "如果用户的问题其实是在要数据，请友好地提示他换一种表述，而不是假装自己能直接给数据。\n"
    "回答使用简体中文，控制在 200 字以内，不要输出代码块或 JSON。"
)


def generate_chat_reply(query: str, history: list = None) -> str:
    """生成对话回复。LLM 不可用时返回兜底话术。"""
    try:
        config = _get_llm_config()
    except Exception as e:
        logger.warning(f"[对话] 获取 LLM 配置失败: {e}")
        return ("我是康养大数据平台的智能助手。你可以直接用语告诉我你想采集的数据，"
                "比如「获取压疮评估的数据」，我会自动解析并爬取；也可以问我平台功能相关问题。")

    messages = [{"role": "system", "content": _CHAT_SYSTEM_PROMPT}]
    if history:
        for turn in history[-6:]:
            if isinstance(turn, dict) and turn.get("role") in ("user", "assistant"):
                messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": query})

    try:
        return _call_text_llm(config, messages)
    except Exception as e:
        logger.error(f"[对话] LLM 调用失败: {e}")
        return ("抱歉，当前对话模型暂时不可用，无法生成回复。\n"
                "如果你是想采集数据，可以直接描述需求（如「获取压疮评估的数据」），"
                "我会自动解析后端接口并爬取。")


def smart_parse(user_query: str) -> dict:
    """智能模式统一入口：先分类 chat / crawl，再分发。

    - 聊天类：返回 {"type": "chat", "reply": "...", "target_api": "", "confidence": 1.0}
    - 爬取类：委托原 parse_intent（含灵活层兜底、反馈记录等完整逻辑）
    """
    if is_chat_query(user_query):
        logger.info(f"[智能模式] 识别为对话意图: {user_query[:40]}")
        reply = generate_chat_reply(user_query)
        return {
            "type": "chat",
            "reply": reply,
            "target_api": "",
            "api_name": "",
            "confidence": 1.0,
            "description": user_query,
            "_query": user_query,
        }
    return parse_intent(user_query)


# ══════════════════════════════════════════════════════════════
# 9. 主函数：parse_intent
# ══════════════════════════════════════════════════════════════

def parse_intent(user_query: str) -> dict:
    """
    将用户的自然语言需求解析为爬取规则（v5 增强）

    流程：
      0. 检测是否是页面采集意图
      1. 规则预匹配（v5: 多维加权评分 + 同义词扩展 + 否定词排除 + 字段消歧 + 反馈 boost）
      2. 如果规则置信度 >= 0.85 → 直接返回（跳过 LLM），记录反馈
      3. 否则 → 调用 LLM，注入规则提示，融合结果
      4. 后验校验与修正
      5. v5: 自动记录解析结果到反馈系统
    """
    # v5: 空查询快速拒绝
    if not user_query or not user_query.strip():
        return {"error": "输入为空，无法解析"}

    # 解析查询中的过滤条件（时间/状态/关键词）—— 供采集时按约束取数
    filters = extract_filters(user_query)

    # Step 0: 检测是否是页面采集意图
    page_intent = _detect_page_scrape_intent(user_query)
    if page_intent.get("is_page"):
        logger.info(f"检测到页面采集意图: {page_intent.get('url_or_route')} (by={page_intent.get('matched_by')})")
        result = _build_page_intent_result(page_intent, user_query)
        # 记录页面采集的反馈
        try:
            record_query(user_query, result)
        except Exception:
            pass  # 反馈记录失败不影响主流程
        return result

    # Step 1: 规则预匹配
    rule_result = _rule_based_parse(user_query)

    # Step 1.5: 低置信度/未匹配拦截（2026-07-23 修复）
    # 如果规则引擎判断"端点库里没有这个接口"，直接返回提示，
    # 不再浪费 LLM 调用（LLM 也看不到这个端点，同样会猜错）。
    if rule_result and rule_result.get("_no_match"):
        logger.info(f"规则匹配未找到端点, 直接返回提示: {rule_result.get('description', '')[:60]}")
        try:
            record_query(user_query, rule_result)
        except Exception:
            pass
        return rule_result

    # Step 2: 高置信度直接返回
    # P1-c: 端点竞争激烈（最优与次优得分差距过小）时，强制走 LLM 消歧，
    #       避免规则分数虚高把「选错端点」固化下来，跳过宝贵的纠错环节。
    skip_llm = bool(rule_result and rule_result["confidence"] >= 0.85)
    if skip_llm and len(rule_result.get("endpoint_candidates", [])) > 1:
        cands = rule_result["endpoint_candidates"]
        try:
            top, second = cands[0]["score"], cands[1]["score"]
            if top - second < 0.12:
                logger.info(f"端点竞争激烈(差距{top - second:.2f}<0.12)，强制 LLM 消歧: {rule_result['api_name']}")
                skip_llm = False
        except (KeyError, TypeError):
            pass

    if skip_llm:
        logger.info(f"规则匹配高置信度({rule_result['confidence']}), 跳过 LLM: {rule_result['api_name']}")
        result = _validate_and_fix_intent(rule_result, filters)
        # v5: 记录解析结果
        try:
            record_query(user_query, result)
        except Exception:
            pass
        return result

    # Step 3: 调用 LLM，注入规则提示
    try:
        llm_config = _get_llm_config()
        rule_hint = _build_rule_hint(rule_result, user_query) if rule_result else _build_rule_hint(None, user_query)

        prompt = INTENT_PARSE_PROMPT.format(
            endpoints=_build_endpoint_desc(),
            user_query=user_query,
            rule_hint=rule_hint,
        )

        llm_result = _call_llm(llm_config, prompt)

        # 如果 LLM 失败，回退到规则结果
        if "error" in llm_result:
            logger.warning(f"LLM 解析失败, 回退到规则结果: {llm_result.get('error')}")
            if rule_result:
                result = _validate_and_fix_intent(rule_result, filters)
                try:
                    record_query(user_query, result)
                except Exception:
                    pass
                return result
            return llm_result

        # Step 4: 融合规则与 LLM 结果
        merged = _merge_rule_and_llm(rule_result, llm_result)
        result = _validate_and_fix_intent(merged, filters)

        # v5: 否定词后验过滤 —— 如果 LLM 返回了被否定的端点，回退到规则结果
        negated = _extract_negated_terms(user_query)
        if negated and result.get("target_api"):
            result_name = result.get("api_name", "")
            for neg_term in negated:
                if neg_term in result_name:
                    logger.info(f"LLM 返回了被否定的端点 [{result_name}]，回退到规则结果")
                    if rule_result:
                        result = _validate_and_fix_intent(rule_result, filters)
                    break

        # v5: 记录解析结果
        try:
            record_query(user_query, result)
        except Exception:
            pass

        return result

    except Exception as e:
        logger.error(f"parse_intent 异常: {e}")
        if rule_result:
            result = _validate_and_fix_intent(rule_result, filters)
            try:
                record_query(user_query, result)
            except Exception:
                pass
            return result
        return {"error": str(e)}


def _merge_rule_and_llm(rule_result: dict, llm_result: dict) -> dict:
    """融合规则预匹配和 LLM 结果"""
    if not rule_result:
        return llm_result

    # 以 LLM 结果为主，但补充规则信息
    if not llm_result.get("target_api"):
        # LLM 没有返回端点，使用规则的
        llm_result["target_api"] = rule_result["target_api"]
        llm_result["api_name"] = rule_result["api_name"]

    # 如果 LLM 没有返回字段，使用规则提取的
    if not llm_result.get("fields"):
        llm_result["fields"] = rule_result["fields"]

    # 剔除 LLM 把「端点名/别名」误当成字段的情况（如"长者档案""入住概览"）
    # —— 仅当字段恰好等于本次匹配到的端点关键词时才丢弃，避免误删真实字段
    _matched_kws = set()
    for _kw in rule_result.get("matched_keywords", []) if rule_result else []:
        _ck = _kw.split("~")[0].split("→")[0].strip()
        if _ck:
            _matched_kws.add(_ck)
    if _matched_kws and llm_result.get("fields"):
        llm_result["fields"] = [
            f for f in llm_result["fields"]
            if f not in _matched_kws or f in FIELD_SYNONYMS
        ]

    # 补充字段描述
    if not llm_result.get("field_descriptions"):
        llm_result["field_descriptions"] = rule_result.get("field_descriptions", {})

    # 记录规则匹配的关键词
    llm_result["rule_matched_keywords"] = rule_result.get("matched_keywords", [])
    llm_result["endpoint_candidates"] = rule_result.get("endpoint_candidates", [])

    # 融合行数限制：规则解析（确定性、已评估验证）优先；仅当规则未识别时才用 LLM 的
    rule_limit = _normalize_limit(rule_result.get("limit")) if rule_result else None
    llm_limit = _normalize_limit(llm_result.get("limit"))
    if rule_limit:
        llm_result["limit"] = rule_limit
    elif llm_limit:
        llm_result["limit"] = llm_limit

    return llm_result


# ══════════════════════════════════════════════════════════════
# 10. 后验校验与修正
# ══════════════════════════════════════════════════════════════

# 字段噪声前缀：这些词绝不可能是数据字段，出现即丢弃（来自动词/连词/数量残留）
_GARBAGE_FIELD_PREFIX = re.compile(
    r'^(查一下|查|获取|提取|查找|查看|找出|调取|拉取|查询|搜索|找|看|想要|需要|只要|'
    r'前|后|取前|只要前|以及|和|与|还有|包括|包含|获取前)'
)


def _sanitize_fields(fields, user_query: str = ""):
    """
    对最终字段列表做最后一道清洗，剔除被误当成字段的噪声：
      - 数量表达式：50条 / 只要前 / 前30 等（含数字+量词，或纯数字）
      - 动词/连词前缀：查一下 / 获取 / 只要前 / 和 / 与 ...
    规则提取与 LLM 提取的结果都会过这一关，确保 limit 类意图绝不会变成字段。
    """
    out = []
    for f in fields:
        if not isinstance(f, str):
            continue
        f = f.strip()
        if not f or len(f) < 2:
            continue
        # 含数字+量词，或纯数字 —— 绝非字段
        if re.search(r'\d\s*(?:条|个|行|位)', f) or re.match(r'^\d+$', f):
            continue
        # 动词/连词前缀开头 —— 绝非字段
        if _GARBAGE_FIELD_PREFIX.match(f):
            continue
        out.append(f)
    # 去重保序
    seen = set()
    res = []
    for f in out:
        if f not in seen:
            seen.add(f)
            res.append(f)
    return res


def _validate_and_fix_intent(intent: dict, filters: dict = None) -> dict:
    """校验意图解析结果并修复常见问题"""
    if not intent or "error" in intent:
        return intent

    # 附加从查询中解析出的过滤条件（时间/状态/关键词），供采集时应用
    if filters is not None:
        intent["filters"] = filters

    # 归一化行数限制（规则/LLM 可能返回 int 或 dict 或不合法值）
    if "limit" in intent:
        intent["limit"] = _normalize_limit(intent.get("limit"))

    target_api = intent.get("target_api", "")

    # 1. 验证 target_api 存在
    if target_api:
        valid_paths = {ep["path"] for ep in KNOWN_ENDPOINTS}
        # 运行时端点别名表（来自后端真实探测）中的路径也视为合法，
        # 避免把「灵活的/学习的」端点误判为无效而模糊改写成其它端点。
        try:
            valid_paths.update(PAGE_ROUTE_ALIASES.keys())
        except Exception:
            pass
        if target_api not in valid_paths:
            # 反馈学习明确给出的端点直接信任，不做模糊改写
            if not intent.get("_feedback_override"):
                best_path = _fuzzy_match_key(target_api, list(valid_paths), threshold=0.6)
                if best_path:
                    logger.info(f"修正 target_api: {target_api} → {best_path}")
                    intent["target_api"] = best_path
                    # 同步更新 api_name
                    for ep in KNOWN_ENDPOINTS:
                        if ep["path"] == best_path:
                            intent["api_name"] = ep["name"]
                            intent["module"] = ep.get("module", "")
                            break

    # 2. 确保 api_name 存在
    if not intent.get("api_name") and target_api:
        for ep in KNOWN_ENDPOINTS:
            if ep["path"] == target_api:
                intent["api_name"] = ep["name"]
                intent["module"] = ep.get("module", "")
                break

    # 3. 确保 fields 是列表
    if intent.get("fields") is None:
        intent["fields"] = []
    elif isinstance(intent["fields"], str):
        intent["fields"] = [intent["fields"]]

    # 3.5 字段清洗：剔除被误当成字段的数量词(50条) / 动词(查一下/只要前) 等噪声
    if intent.get("fields"):
        intent["fields"] = _sanitize_fields(intent["fields"], intent.get("_query", ""))

    # 4. 确保 field_descriptions 是字典
    if not isinstance(intent.get("field_descriptions"), dict):
        intent["field_descriptions"] = {}

    # 5. 确保 confidence 是浮点数
    conf = intent.get("confidence", 0)
    try:
        intent["confidence"] = float(conf)
    except (ValueError, TypeError):
        intent["confidence"] = 0.5

    # 6. 补充缺失的字段描述
    for f in intent["fields"]:
        if f not in intent["field_descriptions"]:
            synonyms = FIELD_SYNONYMS.get(f, [])
            if synonyms:
                intent["field_descriptions"][f] = f"可能的JSON key: {', '.join(synonyms[:3])}"
            else:
                intent["field_descriptions"][f] = "用户需要的字段"

    return intent


# ══════════════════════════════════════════════════════════════
# 11. 字段映射：规则 + LLM 融合
# ══════════════════════════════════════════════════════════════

def _rule_based_field_mapping(user_fields: list, available_keys: list) -> dict:
    """
    用同义词表直接映射字段，不调用 LLM

    返回: {
        "field_mapping": {"用户字段": "JSON key"},
        "unmapped": ["未匹配到的字段"],
    }
    """
    field_mapping = {}
    unmapped = []

    # 构建反向索引: lower(key) → original key
    key_index = {}
    for k in available_keys:
        key_index[k.lower()] = k
        # 也尝试驼峰转下划线
        snake = re.sub(r'([A-Z])', r'_\1', k).lower().lstrip('_')
        key_index[snake] = k

    for user_field in user_fields:
        synonyms = FIELD_SYNONYMS.get(user_field, [])
        matched = False

        # 精确匹配
        for syn in synonyms:
            if syn in available_keys:
                field_mapping[user_field] = syn
                matched = True
                break
            # 大小写不敏感
            if syn.lower() in key_index:
                field_mapping[user_field] = key_index[syn.lower()]
                matched = True
                break

        if not matched and synonyms:
            # 模糊匹配：在 available_keys 中找最接近的同义词
            for syn in synonyms:
                best_key = _fuzzy_match_key(syn, available_keys, threshold=0.75)
                if best_key:
                    field_mapping[user_field] = best_key
                    matched = True
                    break

        if not matched:
            # 尝试直接用字段名匹配
            if user_field in available_keys:
                field_mapping[user_field] = user_field
                matched = True
            elif user_field.lower() in key_index:
                field_mapping[user_field] = key_index[user_field.lower()]
                matched = True

        if not matched:
            unmapped.append(user_field)

    return {"field_mapping": field_mapping, "unmapped": unmapped}


def _nonempty_rate(rows: list, key) -> float:
    """计算某列非空行占比（用于采样打分，判断映射 key 是否真的有数据）。"""
    if not rows:
        return 0.0
    n = sum(1 for r in rows if str(r.get(key, "")).strip())
    return n / len(rows)


def _rescore_by_nonempty_rate(field_mapping: dict, fields: list, sample_rows: list, available_keys: list) -> dict:
    """
    P1: 用「采样非空率」对规则/LLM 选出的 key 做二次校验。

    仅当当前 key 非空率偏低(<0.5)且存在明显更优(高 >=0.1)的候选时才改选，
    避免把「名字像但不含数据」的 key 当作映射目标（典型：姓名映射到 nickName 而非 realName）。
    高置信(非空率>=0.5)的映射保持不变，避免误改。
    """
    for user_field in fields:
        if user_field not in field_mapping:
            continue
        current = field_mapping[user_field]
        cur_rate = _nonempty_rate(sample_rows, current)
        if cur_rate >= 0.5:
            continue
        candidates = set(FIELD_SYNONYMS.get(user_field, []))
        candidates.add(current)
        for k in available_keys:
            if _fuzzy_match_key(user_field, [k], threshold=0.75) == k:
                candidates.add(k)
        best, best_rate = current, cur_rate
        for c in candidates:
            if c in available_keys and c != current:
                r = _nonempty_rate(sample_rows, c)
                if r > best_rate + 0.1:
                    best, best_rate = c, r
        if best != current:
            logger.info(
                f"[智能模式] 字段[{user_field}]映射由 {current}(非空率{cur_rate:.2f}) "
                f"改选 {best}({best_rate:.2f})")
            field_mapping[user_field] = best
    return field_mapping


def extract_field_mapping(sample_rows: list, fields: list, field_descriptions: dict) -> dict:
    """
    用 LLM + 规则分析 API 返回的原始数据，找到用户字段与原始 key 的对应关系

    优化策略：
      1. 先用规则同义词表直接映射（快速、准确）
      2. 对规则无法映射的字段，调用 LLM
      3. 后验校验：确认映射的 key 确实存在于数据中
    """
    if not sample_rows:
        return {
            "field_mapping": {},
            "available_fields": [],
            "missing_fields": list(fields),
            "columns_order": list(fields),
        }

    available_keys = list(sample_rows[0].keys())

    # Step 1: 规则映射
    rule_mapping = _rule_based_field_mapping(fields, available_keys)
    field_mapping = rule_mapping["field_mapping"]
    unmapped = rule_mapping["unmapped"]

    # Step 2: 对未映射的字段调用 LLM
    if unmapped:
        try:
            llm_config = _get_llm_config()

            # 构建已知映射提示
            known_mappings = {}
            for f in fields:
                if f in field_mapping:
                    known_mappings[f] = field_mapping[f]
                else:
                    known_mappings[f] = FIELD_SYNONYMS.get(f, [])

            sample = sample_rows[:3]
            prompt = EXTRACT_PROMPT.format(
                fields=json.dumps(unmapped, ensure_ascii=False),
                field_descriptions=json.dumps(
                    {f: field_descriptions.get(f, "") for f in unmapped},
                    ensure_ascii=False
                ),
                known_mappings=json.dumps(known_mappings, ensure_ascii=False, indent=2),
                sample_data=json.dumps(sample, ensure_ascii=False, indent=2),
                available_keys=json.dumps(available_keys, ensure_ascii=False),
            )

            llm_result = _call_llm(llm_config, prompt)

            if "error" not in llm_result:
                llm_mapping = llm_result.get("field_mapping", {})
                # 合并 LLM 结果
                for user_field, json_key in llm_mapping.items():
                    if json_key in available_keys:
                        field_mapping[user_field] = json_key
                    else:
                        # 模糊修正
                        fixed_key = _fuzzy_match_key(json_key, available_keys, threshold=0.6)
                        if fixed_key:
                            field_mapping[user_field] = fixed_key
                        else:
                            logger.warning(f"LLM 映射的 key 不存在于数据中: {json_key}")

        except Exception as e:
            logger.error(f"LLM 字段映射失败: {e}")

    # 重新计算未映射字段
    still_unmapped = [f for f in fields if f not in field_mapping]

    # P1: 采样非空率二次校验，修正「名字像但无数据」的映射
    field_mapping = _rescore_by_nonempty_rate(field_mapping, fields, sample_rows, available_keys)

    # 构建列顺序
    columns_order = [f for f in fields if f in field_mapping]
    # 未映射的字段放在最后
    columns_order.extend(still_unmapped)

    return {
        "field_mapping": field_mapping,
        "available_fields": available_keys,
        "missing_fields": still_unmapped,
        "columns_order": columns_order,
    }


# ══════════════════════════════════════════════════════════════
# 12. LLM 调用（增强 JSON 解析）
# ══════════════════════════════════════════════════════════════

def _call_llm(config: dict, prompt: str) -> dict:
    """调用 LLM API（OpenAI 兼容接口）"""
    url = config["api_base"].rstrip("/") + "/chat/completions"
    api_key = config["api_key"]
    model = config["model"]
    timeout = config.get("request_timeout", 120)

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "你是一个精确的 JSON 输出器。只输出合法 JSON，不要有任何其他文字。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    })

    content = ""
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]

        # 提取 JSON（增强版，处理更多边界情况）
        result = _extract_json_from_text(content)
        return result

    except json.JSONDecodeError:
        # 可能包裹在代码块中
        result = _extract_json_from_text(content)
        if "error" not in result:
            return result
        logger.error(f"LLM 返回无法解析为 JSON: {content[:500]}")
        return {"error": "LLM 返回格式异常", "raw": content[:500]}
    except Exception as e:
        logger.error(f"LLM 调用异常: {e}")
        return {"error": str(e)}


def _extract_json_from_text(text: str) -> dict:
    """从 LLM 返回的文本中提取 JSON，处理多种格式"""
    text = text.strip()

    # 1. 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. ```json 代码块
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 3. 找第一个 { 到最后一个 }（贪婪）
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    # 4. 修复常见 JSON 格式问题后重试
    fixed = text
    # 去除尾随逗号
    fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
    # 修复单引号
    fixed = fixed.replace("'", '"')
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    # 5. 尝试提取修复后的 JSON
    match = re.search(r"\{[\s\S]*\}", fixed)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return {"error": "无法提取 JSON", "raw": text[:500]}


# ══════════════════════════════════════════════════════════════
# 13. 执行爬取
# ══════════════════════════════════════════════════════════════

def execute_custom_crawl(host: str, username: str, password: str, intent: dict, query: str = "") -> list:
    """
    根据意图解析结果执行爬取

    流程：
      1. 登录平台
      2. 调用目标 API 获取原始数据
      3. 用规则+LLM分析原始数据的字段映射
      4. 按用户要求的字段过滤并输出
      5. 后验校验：如果某些字段映射后值为空，尝试其他候选 key
    """
    from kangyang.api_client import RuoYiApiClient

    client = RuoYiApiClient(host, username, password)
    if not client.login():
        err_detail = getattr(client, "last_error", "未知原因")
        return [{"error": f"登录失败: {err_detail}"}]

    target_api = intent.get("target_api", "")
    user_fields = intent.get("fields", [])
    field_descs = intent.get("field_descriptions", {})

    # ── 灵活层：实体级纠正保险 ──
    # 无论 parse_intent（规则/LLM）是否给出 target_api，都用基于【真实后端映射】的
    # 灵活层再解析一遍。若灵活层命中了与规则/LLM「不同的」实体接口（更贴合查询里的
    # 具体子项，如「熙康设备…体温」应命中体温而非被父级「熙康设备」抢成人员信息），
    # 则以灵活层为准。这根治「端点库把大类实体硬匹配成父级接口、忽略用户真正要的子项」。
    # 灵活层命中 = 实体别名精确出现在查询中 + 真实探测确认接口存在，误纠正概率极低；
    # 若与 target_api 相同则不动（高置信正确匹配不受影响）。
    if query and not intent.get("_user_corrected"):
        # 用户已在预览面板手动「修正」过接口，则尊重用户选择，灵活层不再覆盖
        try:
            from kangyang.generic_api_resolver import resolve_generic_endpoint
            resolved = resolve_generic_endpoint(query, client)
        except Exception as e:
            logger.warning(f"[智能模式] 灵活层解析异常: {e}")
            resolved = ""
        if resolved and resolved != target_api:
            logger.info(f"[智能模式] 灵活层纠正: {target_api or '(空)'} -> {resolved}")
            target_api = resolved
            intent["target_api"] = resolved
            if not intent.get("api_name"):
                intent["api_name"] = resolved
        elif (not target_api) and resolved:
            # 原兜底：target_api 为空时由灵活层补充
            logger.info(f"[智能模式] 灵活层兜底命中: {query} -> {resolved}")
            target_api = resolved
            intent["target_api"] = resolved
            if not intent.get("api_name"):
                intent["api_name"] = resolved

    # 未匹配端点时直接返回提示（不再尝试调用空路径 API）
    if not target_api:
        return [{"error": intent.get("description", "未找到匹配的 API 端点，请先通过捕获面板录入该接口。"), "_no_match": True}]

    # 加载该端点的枚举解码映射（编码 -> 中文），使输出贴近前端显示
    field_enums = _load_endpoint_enums(target_api)

    # 1. 拉取原始数据
    try:
        raw_rows = client.get_list(target_api)
    except Exception as e:
        return [{"error": f"API 调用失败: {e}"}]

    if not raw_rows:
        # 区分「真·空表」与「后端报错被误判为无数据」
        data_err = getattr(client, "last_data_error", "")
        if data_err:
            return [{
                "error": f"接口返回错误（并非无数据）: {data_err}",
                "note": "后端该接口存在坏数据(如生日字段序列化异常 HOUR_OF_DAY)，"
                        "已自动缩小分页跳过坏行，但本批次无可用行，请检查后端数据或缩小 pageSize",
            }]
        return [{"error": "目标接口无数据", "note": "该数据表可能为空"}]

    # 2. 如果用户没有指定字段，返回全部数据（但限制列数）
    if not user_fields:
        all_keys = list(raw_rows[0].keys())
        # 优先返回有意义的列，排除过长的描述性字段
        priority_keys = [k for k in all_keys if k not in (
            "remark", "description", "remarkTxt", "remarkContent", "createBy", "updateBy"
        )][:15]
        if len(priority_keys) < 5:
            priority_keys = all_keys[:15]
        # 附带所有可用列名，供前端展示；并按枚举映射解码（1/2 -> 男/女）
        out_rows = [
            decode_row_values({col: row.get(col, "") for col in priority_keys}, field_enums)
            for row in raw_rows
        ]
        return _apply_limit(out_rows, intent.get("limit"))

    # 3. 字段映射（规则 + LLM）
    mapping_result = extract_field_mapping(raw_rows, user_fields, field_descs)
    if "error" in mapping_result:
        # 映射失败，降级：直接返回前 10 列
        raw_cols = list(raw_rows[0].keys())[:10]
        return [
            {col: row.get(col, "") for col in raw_cols}
            for row in raw_rows
        ]

    field_mapping = mapping_result.get("field_mapping", {})
    columns_order = mapping_result.get("columns_order", user_fields)
    missing_fields = mapping_result.get("missing_fields", [])

    # 4+5. 按字段映射过滤数据 + 缺失/低置信度标注（抽为纯函数便于单测）
    results = _apply_field_mapping(raw_rows, field_mapping, columns_order, missing_fields, field_enums=field_enums)

    # 6. 应用查询中的过滤条件（时间/状态/关键词）—— 解决「返回全量而非约束数据」
    filters = intent.get("filters") or {}
    if filters and results:
        results, filter_stats = _apply_filters(results, filters, field_mapping, columns_order)
        if results:
            results[0]["_filter_stats"] = filter_stats
        logger.info(f"[智能模式] 过滤条件应用: {filter_stats}")

    # 7. 应用行数限制（前N条 / 后N条 / 第N条）—— 解决「要50条却被当字段/返回全量」
    limit_spec = intent.get("limit")
    if limit_spec and results:
        results = _apply_limit(results, limit_spec)
        if results:
            results[0]["_limit_applied"] = limit_spec

    return results


def _apply_field_mapping(raw_rows: list, field_mapping: dict, columns_order: list, missing_fields: list, field_enums: dict = None) -> list:
    """
    纯函数：按字段映射把原始行转为用户字段行，并标注不确定性。

    编码解码：若某字段命中 field_enums（按原始字段名），其取值会被解码为中文
    （如 gender=1 -> 男），使输出的是「前端显示的数据」而非数据库编码。

    关键正确性保证：
      - 空值兜底严格限定在本字段的同义词范围内，且跳过「已被其他字段作为主键占用」的
        key，防止 A 字段错误地显示 B 字段的值（跨字段错填）。
      - 输出后做覆盖率校验：某字段非空行占比 < 30% 时标记为 _low_confidence_fields，
        使「空/错填」数据显式暴露，而不是被当作可靠结果静默呈现。
    """
    # 预收集「被其他字段当作主键占用」的 key，避免一个 key 的值泄露给多个字段（交叉错填）
    primary_keys = set(field_mapping.values())
    results = []
    for row in raw_rows:
        item = {}
        for user_field in columns_order:
            raw_key = field_mapping.get(user_field, user_field)
            value = row.get(raw_key, "")
            # 编码解码：原始字段命中枚举映射时，把编码值转成中文标签
            if field_enums and raw_key in field_enums:
                sv = str(value) if value not in (None, "") else ""
                if sv in field_enums[raw_key]:
                    value = field_enums[raw_key][sv]
            # 若映射 key 为空，仅在本字段的同义词范围内回填（严格限定，避免跨字段错填）
            if not value:
                synonyms = FIELD_SYNONYMS.get(user_field, [])
                for syn in synonyms:
                    if syn == raw_key:
                        continue
                    # 跳过已被其他字段作为主键占用的 key，防止 A 字段显示 B 字段的值
                    if syn in primary_keys and syn != raw_key:
                        continue
                    if syn in row and row[syn]:
                        value = row[syn]
                        break
            item[user_field] = value
        results.append(item)

    # 5. 记录缺失字段信息
    if missing_fields:
        logger.warning(f"以下字段未能在数据中找到: {missing_fields}")
        # 在第一条记录中标注缺失字段
        if results:
            results[0]["_missing_fields"] = missing_fields

    # 5.1 覆盖率校验：标记低置信度字段，避免把「空/错填」数据当作可靠结果呈现
    # （例如映射选错 key 导致该字段绝大多数行为空，应显式提示而非静默输出）
    total = len(results)
    if total and columns_order:
        field_coverage = {}
        for uf in columns_order:
            non_empty = sum(1 for it in results if str(it.get(uf, "")).strip())
            field_coverage[uf] = (non_empty / total)
        low_confidence = [
            uf for uf in columns_order
            if uf not in missing_fields and field_coverage.get(uf, 0) < 0.3
        ]
        if low_confidence:
            logger.warning(
                f"[智能模式] 以下字段覆盖率偏低(<30%)，可能为错填/空值: {low_confidence} | "
                f"覆盖率={ {k: round(field_coverage[k], 2) for k in low_confidence} }"
            )
            if results:
                results[0]["_low_confidence_fields"] = low_confidence
                results[0]["_field_coverage"] = {k: round(v, 2) for k, v in field_coverage.items()}

    return results


# ══════════════════════════════════════════════════════════════
# 12. 过滤条件：解析（自然语言 → 结构化约束）与应用（行级约束满足）
#     解决「用户说本月入住的在院长者，却返回全量数据」这一最致命的不准确问题
# ══════════════════════════════════════════════════════════════

# 状态同义词：用户输入的状态词 → 可能出现在数据中的取值集合
_STATUS_SYNONYMS = {
    "在院": ["在院", "入住中", "在住", "住院中", "在床", "入住"],
    "出院": ["出院", "退住", "离院", "退院", "已退住"],
    "入住": ["入住", "入院", "在院"],
    "待审核": ["待审核", "待审", "未审核", "待审批", "待确认"],
    "已审核": ["已审核", "已审", "已审批", "已确认"],
    "启用": ["启用", "有效", "正常", "开启"],
    "禁用": ["禁用", "停用", "无效", "关闭", "作废"],
    "有效": ["有效", "启用", "正常"],
    "无效": ["无效", "禁用", "停用", "作废"],
    # 用户口语短语（同义归并到标准状态词）
    "退住": ["退住", "出院", "退院", "离院"],
}


# ══════════════════════════════════════════════════════════════
# 12.5 行数限制解析
#     解决「用户要前50条数据，却被当成字段[50条]去匹配」的最致命不准确问题
#     —— 把 前N条 / 后N条 / 第N条 / 只要N条 / N条数据 等数量意图
#        正确解析为「取 N 行」，而不是当作一个数据字段。
# ══════════════════════════════════════════════════════════════

# 用于在字段提取阶段把数量表达式整段剔除（避免"50条"残留在候选字段中）
# 必须与 extract_limit() 的识别口径保持一致：覆盖 第N条 / 前N条 / 后N条 / 取前N / 只要前N / 只要N / N条数据 等
_LIMIT_STRIP_RE = re.compile(
    r'(?:第\s*\d+\s*(?:条|个|行|位)'
    r'|(?:后|最后|前|取前|只要前|取|只要|仅|仅取)\s*\d+\s*(?:条|个|行|位)'
    r'|\d+\s*(?:条|行|位)\s*(?:数据|记录|信息|内容|老人|长者|住户|养老)?'
    r'|\d+\s*个\s*(?:数据|记录|老人|长者|住户|养老|行)?)'
)


def extract_limit(user_query: str) -> dict:
    """
    解析自然语言中的「行数限制」意图。

    支持表达（语义 -> mode）：
      第50条                              -> single  取第50条（1行，offset=49）
      前50条 / 前50个 / 前50位            -> top     取前50行
      后50条 / 最后50条                    -> bottom  取末尾50行
      取50条 / 只要50条 / 仅50条 / 取前50条 -> top   取前50行
      50条数据 / 50条记录 / 50条 / 50行    -> top     取前50行
      50个老人 / 50个数据                  -> top     取前50行

    返回结构（未识别到数量时返回 None）：
      {"limit": int, "offset": int, "mode": "top"|"bottom"|"single", "raw": "匹配原文"}
    """
    q = (user_query or "").strip()
    if not q:
        return None

    # 1) 第N条 —— 单条记录（优先，避免被下面的通用规则再次匹配）
    m = re.search(r'第\s*(\d+)\s*条', q)
    if m:
        n = int(m.group(1))
        return {"limit": 1, "offset": max(0, n - 1), "mode": "single", "raw": m.group(0)}

    # 2) 后N条 / 最后N条 —— 取末尾 N 行
    m = re.search(r'(?:后|最后)\s*(\d+)\s*(?:条|个|行|位)', q)
    if m:
        n = int(m.group(1))
        return {"limit": n, "offset": 0, "mode": "bottom", "raw": m.group(0)}

    # 3) 前N条 / 前N个 / 前N位 / 取前N / 只要前N —— 取前 N 行
    m = re.search(r'(?:前|取前|只要前)\s*(\d+)\s*(?:条|个|行|位)', q)
    if m:
        n = int(m.group(1))
        return {"limit": n, "offset": 0, "mode": "top", "raw": m.group(0)}

    # 4) 取/只要/仅 + N条 —— 取前 N 行
    m = re.search(r'(?:取|只要|仅|仅取)\s*(\d+)\s*(?:条|个|行|位)', q)
    if m:
        n = int(m.group(1))
        return {"limit": n, "offset": 0, "mode": "top", "raw": m.group(0)}

    # 5) 通用：N条(数据/记录/...) / N行 / N位
    m = re.search(r'(\d+)\s*(?:条|行|位)\s*(?:数据|记录|信息|内容|老人|长者|住户|养老)?', q)
    if m:
        n = int(m.group(1))
        return {"limit": n, "offset": 0, "mode": "top", "raw": m.group(0)}

    # 6) 通用：N个(数据/老人/长者/...) —— 限定带量词上下文，避免"一个老人"误判为 limit=1
    m = re.search(r'(\d+)\s*个\s*(?:数据|记录|老人|长者|住户|养老|行)', q)
    if m:
        n = int(m.group(1))
        return {"limit": n, "offset": 0, "mode": "top", "raw": m.group(0)}

    return None


def _normalize_limit(val):
    """把规则/LLM 返回的 limit 归一化为标准结构；非法值返回 None。"""
    if val is None:
        return None
    if isinstance(val, dict):
        if isinstance(val.get("limit"), int) and val["limit"] > 0:
            mode = val.get("mode", "top")
            if mode not in ("top", "bottom", "single"):
                mode = "top"
            return {
                "limit": val["limit"],
                "offset": int(val.get("offset", 0) or 0),
                "mode": mode,
                "raw": val.get("raw", str(val["limit"])),
            }
        return None
    if isinstance(val, int) and val > 0:
        return {"limit": val, "offset": 0, "mode": "top", "raw": str(val)}
    return None


def extract_filters(user_query: str, reference_date=None) -> dict:
    """
    从自然语言查询中解析过滤条件（时间范围 / 状态 / 关键词）。

    返回结构（各子项可能为 None 表示未提取到）:
    {
        "time_range": {"start": "2026-07-01", "end": "2026-07-31", "raw": "本月"} | None,
        "status":     {"values": ["在院"], "raw": "在院"} | None,
        "keyword":    {"field": "姓名"|None, "value": "张", "raw": "张"} | None,
    }
    """
    q = (user_query or "").strip()
    if not q:
        return {"time_range": None, "status": None, "keyword": None}

    import re
    from datetime import date, timedelta

    if reference_date is None:
        reference_date = date.today()

    result = {"time_range": None, "status": None, "keyword": None}

    # ── 时间范围 ──
    tr = _parse_time_range(q, reference_date)
    if tr:
        result["time_range"] = tr

    # ── 状态 ──
    status_vals = []
    for status_word in _STATUS_SYNONYMS.keys():
        if status_word in q:
            # 排除「动作名词」误判：如"入住申请/出院登记"里的"入住/出院"是动作而非状态值
            if re.search(re.escape(status_word) + r"(申请|登记|办理|处理|记录|单|表|流程)", q):
                continue
            status_vals.append(status_word)
    # 也支持「状态为X」「X状态」等形态
    m = re.search(r"状态[为是:]?\s*([\u4e00-\u9fa5]{1,4})", q)
    if m and m.group(1) not in status_vals:
        status_vals.append(m.group(1))
    # 去重：若某状态词是另一匹配词的子串（如短词被长词包含），保留更具体的长词
    status_vals = _dedup_status_words(status_vals)
    # 展开为数据中可能出现的原值集合（如 退住→[退住,出院,退院,离院]），提升透明度
    expanded = []
    for w in status_vals:
        expanded.extend(_STATUS_SYNONYMS.get(w, [w]))
    expanded = list(dict.fromkeys(expanded))
    if expanded:
        result["status"] = {"values": expanded, "raw": "/".join(status_vals)}

    # ── 关键词（姓名包含X / 名字叫X / 姓X / 含X / 引号内容）──
    kw_field = None
    kw_value = None
    # 必须带显式指示词（包含/叫/是/：）才视为关键词过滤，避免把"长者姓名"当成按姓名过滤
    m = re.search(r"(?:姓名|名字|名称|长者名|老人名)(?:包含|叫|是|：|:)[\"\'：:]?\s*([^\"\'，。\s，。的]+)", q)
    if m:
        kw_field, kw_value = "姓名", m.group(1)
    else:
        # 负向先行：排除"姓名"字段词中的"姓"（姓后接名时不匹配），避免把"长者姓名"误判为按姓名过滤
        m = re.search(r"(?:姓(?!名)|叫|名为|名称是|姓名是)[\"\'：:]?\s*([\u4e00-\u9fa5]{1,4})", q)
        if m:
            kw_field, kw_value = "姓名", m.group(1)
        else:
            m = re.search(r"包含[\"\'：:]?\s*([^\"\'，。\s，。的]+)", q)
            if m:
                kw_value = m.group(1)
            else:
                m = re.search(r"[\"\'‘’]([^\"\'‘’]+)[\"\'’‘]", q)
                if m:
                    kw_value = m.group(1)
    # 循环剥离尾部被连带的助词/名词后缀（的/了/长者/老人/档案/数据/信息等）
    if kw_value:
        changed = True
        while changed:
            changed = False
            for _suf in ["的", "了", "长者", "老人", "档案", "数据", "信息", "情况", "列表", "记录", "详情"]:
                if kw_value.endswith(_suf):
                    kw_value = kw_value[: -len(_suf)]
                    changed = True
                    break
    if kw_value:
        result["keyword"] = {"field": kw_field, "value": kw_value, "raw": kw_value}

    return result


def _dedup_status_words(words: list) -> list:
    """移除被其他匹配词包含的较短状态词，保留更具体的长词。"""
    out = []
    for w in words:
        if any(w != o and w in o for o in words):
            continue
        out.append(w)
    return out


def _parse_time_range(text: str, reference_date) -> dict:
    """将自然语言时间描述解析为 {start, end, raw}（日期字符串 YYYY-MM-DD）。"""
    import re
    from datetime import date, timedelta

    t = text
    y = reference_date.year
    m = reference_date.month

    if re.search(r"本月|这个月|当月|本月内", t):
        start = date(y, m, 1)
        if m == 12:
            end = date(y + 1, 1, 1) - timedelta(days=1)
        else:
            end = date(y, m + 1, 1) - timedelta(days=1)
        return {"start": start.isoformat(), "end": end.isoformat(), "raw": "本月"}

    if re.search(r"上月|上个月|上一月", t):
        if m == 1:
            start = date(y - 1, 12, 1)
            end = date(y, 1, 1) - timedelta(days=1)
        else:
            start = date(y, m - 1, 1)
            end = date(y, m, 1) - timedelta(days=1)
        return {"start": start.isoformat(), "end": end.isoformat(), "raw": "上月"}

    if re.search(r"本周|这周|这一周", t):
        # 周一为一周起点
        monday = reference_date - timedelta(days=reference_date.weekday())
        sunday = monday + timedelta(days=6)
        return {"start": monday.isoformat(), "end": sunday.isoformat(), "raw": "本周"}

    if re.search(r"今年|本年|年内", t):
        return {"start": date(y, 1, 1).isoformat(), "end": date(y, 12, 31).isoformat(), "raw": "今年"}

    if re.search(r"今天|今日", t):
        return {"start": reference_date.isoformat(), "end": reference_date.isoformat(), "raw": "今天"}

    if re.search(r"昨天|昨日", t):
        d = reference_date - timedelta(days=1)
        return {"start": d.isoformat(), "end": d.isoformat(), "raw": "昨天"}

    # 最近/近 N 天
    m = re.search(r"(?:最近|近|过去)\s*(\d+)\s*天", t)
    if m:
        n = int(m.group(1))
        start = reference_date - timedelta(days=n - 1)
        return {"start": start.isoformat(), "end": reference_date.isoformat(), "raw": f"最近{n}天"}

    # 具体区间：A 到/至 B
    m = re.search(
        r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?\s*(?:到|至|-|~)\s*"
        r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?", t)
    if m:
        try:
            s = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            e = date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
            return {"start": s.isoformat(), "end": e.isoformat(), "raw": f"{s}~{e}"}
        except ValueError:
            pass

    # 具体单日：YYYY年MM月DD日 或 YYYY-MM-DD 或 YYYY/MM/DD
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?", t)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return {"start": d.isoformat(), "end": d.isoformat(), "raw": d.isoformat()}
        except ValueError:
            pass
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", t)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return {"start": d.isoformat(), "end": d.isoformat(), "raw": d.isoformat()}
        except ValueError:
            pass

    return None


def _apply_filters(rows: list, filters: dict, field_mapping: dict = None, user_fields: list = None):
    """
    纯函数：对行数据应用过滤条件（时间 / 状态 / 关键词）。

    返回 (filtered_rows, stats)。
    stats = {"applied": [...], "filtered_out": N, "not_applied": [...]}
      - applied: 实际生效的过滤维度
      - not_applied: 因找不到对应列而无法应用的维度（绝不因此清空数据）
    """
    if not filters or not rows:
        return rows, {"applied": [], "filtered_out": 0, "not_applied": []}

    applied = []
    not_applied = []
    out = list(rows)

    st = filters.get("status")
    if st and st.get("values"):
        out, ok = _filter_by_status(out, st["values"])
        (applied if ok else not_applied).append("status")

    tr = filters.get("time_range")
    if tr and tr.get("start"):
        out, ok = _filter_by_time(out, tr["start"], tr.get("end", tr["start"]))
        (applied if ok else not_applied).append("time")

    kw = filters.get("keyword")
    if kw and kw.get("value"):
        out, ok = _filter_by_keyword(out, kw.get("field"), kw["value"])
        if ok:
            applied.append("keyword")

    return out, {
        "applied": applied,
        "filtered_out": len(rows) - len(out),
        "not_applied": not_applied,
    }


def _apply_limit(rows: list, limit: dict):
    """
    按意图中的行数限制对结果集截断。

      mode="top"    -> 取前 N 行
      mode="bottom" -> 取末尾 N 行
      mode="single" -> 取第 offset 行（1 行）

    非法 / 空 limit 直接原样返回。
    """
    if not limit or not isinstance(limit, dict):
        return rows
    n = limit.get("limit")
    mode = limit.get("mode", "top")
    offset = limit.get("offset", 0)
    if not isinstance(n, int) or n <= 0 or not rows:
        return rows
    if mode == "bottom":
        out = rows[-n:]
    elif mode == "single":
        out = rows[offset:offset + 1] if 0 <= offset < len(rows) else []
    else:  # top
        out = rows[:n]
    logger.info(f"[智能模式] 应用行数限制: mode={mode} limit={n} offset={offset} -> {len(out)} 行")
    return out


def _candidate_column(keys, hints):
    """在行键中找第一个命中提示词（lower 包含）的列名；找不到返回 None。"""
    low_hints = [h.lower() for h in hints]
    for k in keys:
        kl = str(k).lower()
        if any(h in kl for h in low_hints):
            return k
    return None


def _filter_by_status(rows, values):
    target = set()
    for v in values:
        target.add(v)
        target.update(_STATUS_SYNONYMS.get(v, []))
    col = _candidate_column(list(rows[0].keys()), ["status", "state", "状态", "情况"])
    if not col:
        return rows, False
    out = []
    for r in rows:
        val = str(r.get(col, "")).strip()
        if val and (val in target or any(t in val for t in target)):
            out.append(r)
    return out, True


def _parse_value_date(val):
    """尽量把单元格值解析为 date；失败返回 None。"""
    from datetime import datetime, date
    s = str(val).strip()
    if not s:
        return None
    # 时间戳（秒/毫秒）
    if s.replace(".", "").isdigit():
        try:
            ts = float(s)
            if ts > 1e12:
                ts /= 1000
            return datetime.fromtimestamp(ts).date()
        except (ValueError, OSError):
            return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d",
                "%Y年%m月%d日", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _filter_by_time(rows, start, end):
    col = _candidate_column(
        list(rows[0].keys()),
        ["time", "date", "日期", "时间", "创建", "更新", "入住", "入院", "出生",
         "checkin", "enroll", "create", "update", "birth"],
    )
    if not col:
        return rows, False
    from datetime import date
    sd = _parse_value_date(start)
    ed = _parse_value_date(end)
    if not sd or not ed:
        return rows, False
    out = []
    for r in rows:
        d = _parse_value_date(r.get(col, ""))
        if d is None:
            # 无法解析的日期不强行丢弃（避免误删），保留
            out.append(r)
        elif sd <= d <= ed:
            out.append(r)
    return out, True


def _filter_by_keyword(rows, field, value):
    v = str(value).strip().lower()
    if not v:
        return rows, False
    keys = list(rows[0].keys())
    target_col = None
    if field:
        target_col = _candidate_column(keys, [field.lower()]) or _candidate_column(
            keys, ["name", "姓名", "名称", "名字"])
    if target_col:
        out = [r for r in rows if v in str(r.get(target_col, "")).lower()]
    else:
        # 未指定字段：任意列包含该关键词即保留
        out = [r for r in rows if any(v in str(r.get(k, "")).lower() for k in keys)]
    return out, True

