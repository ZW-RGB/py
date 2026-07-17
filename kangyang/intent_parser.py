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
        "入住概览", "入住总览", "入住信息", "入住情况", "长者档案", "老人档案",
        "入住人员", "入住老人", "长者信息", "老人信息", "老人数据", "老人",
        "入住列表", "在住人员", "在住老人",
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
    # ── 社区信息 ──
    "/dev-api/basicinformation/community/list": [
        "社区信息", "社区资料", "社区列表", "社区",
    ],
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
    "/dev-api/ability/ability/list": [
        "能力评估", "能力评定", "老人能力评估", "照护评估", "自理能力", "能力评估表",
        "老人评估", "评估结果", "健康评估", "身体评估", "功能评估", "日常生活评估",
        "评估", "老人体检",
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
# 与 ENDPOINT_ALIASES 保持一致的命名习惯
PAGE_ROUTE_ALIASES = {
    "/elderly/checkin": ["入住处理", "入住登记", "入住办理", "退住处理", "出院处理"],
    "/elderly/overview": ["入住概览", "入住总览", "入住情况", "老人列表", "长者列表", "在住老人"],
    "/elderly/apply": ["入住申请", "申请入住"],
    "/elderly/nursing": ["老人护理", "日常护理", "护理服务"],
    "/base/community": ["社区信息", "社区资料"],
    "/base/communityManage": ["社区管理"],
    "/base/doctor": ["医生信息", "医生列表"],
    "/base/nurse": ["护士信息", "护士列表", "护理员信息", "护工信息"],
    "/nursing/record": ["护理记录", "护理看板", "护理日志", "照护记录"],
    "/contract/manage": ["合同管理", "合同列表", "合同信息"],
    "/nursing/level": ["护理等级", "护理级别"],
    "/device/bed": ["床位管理", "房间管理", "床位列表", "房间列表"],
    "/assessment/ability": ["能力评估", "老人评估", "健康评估"],
    "/assessment/overview": ["评估总览", "养老评估总览", "养老评估"],
    "/assessment/pressureUlcer": ["压疮评估", "压疮", "褥疮评估", "压力性损伤评估"],
    "/assessment/depression": ["抑郁评估", "抑郁"],
    "/assessment/anxiety": ["焦虑评估", "焦虑"],
    "/assessment/fall": ["跌倒评估", "跌倒"],
    "/assessment/icvd": ["ICVD评估", "ICVD", "脑血管评估", "脑卒中评估"],
    "/assessment/elderlyAbility": ["老人能力评估", "老人综合评估"],
    "/activity/manage": ["活动管理", "活动列表"],
    "/safety/device": ["安全设备", "安防设备"],
    "/terminal/device": ["终端设备", "智能设备"],
    "/health/heartrate": ["心率监测", "心率页面", "心跳监测"],
    "/health/spo2": ["血氧监测", "血氧页面", "氧气监测"],
    "/health/temperature": ["体温监测", "体温页面"],
    "/health/bloodPressure": ["血压监测", "血压页面"],
    "/system/user": ["用户列表", "用户管理"],
    "/system/role": ["角色列表", "角色管理"],
    "/system/dict": ["字典数据", "数据字典"],
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
    "姓名": ["name", "elderlyName", "elderName", "personName", "userName", "realName", "fullname", "nickName"],
    "老人姓名": ["elderlyName", "elderName", "name", "personName"],
    "长者姓名": ["elderlyName", "elderName", "name", "personName"],
    "性别": ["sex", "gender"],
    "年龄": ["age", "elderlyAge", "elderAge"],
    "出生日期": ["birthday", "birthDate", "dateOfBirth", "bornDate"],
    "身份证号": ["idCard", "idNumber", "identityCard", "cardNo", "idcard"],
    "身份证": ["idCard", "idNumber", "identityCard", "cardNo"],
    "民族": ["nation", "ethnicity", "nationality"],
    "籍贯": ["nativePlace", "hometown", "origin"],
    "婚姻状况": ["maritalStatus", "marriage", "marital"],
    "学历": ["education", "educationLevel", "degree"],
    "照片": ["photo", "avatar", "picture", "image", "headImg"],
    "身高": ["height", "bodyHeight", "stature"],
    "体重": ["weight", "bodyWeight"],

    # ── 联系方式类 ──
    "联系方式": ["phone", "contactPhone", "contact", "mobile", "telephone", "contactInfo", "phoneNumber"],
    "联系电话": ["phone", "contactPhone", "telephone", "mobile", "phoneNumber"],
    "手机号": ["mobile", "phone", "phoneNumber", "mobilePhone"],
    "电话": ["phone", "telephone", "phoneNo"],
    "紧急联系人": ["emergencyContact", "emergencyName", "emergency", "contactPerson"],
    "紧急联系电话": ["emergencyPhone", "emergencyContactPhone", "emergencyTel"],
    "家庭住址": ["homeAddress", "address", "homeAddr"],
    "地址": ["address", "homeAddress", "addr", "location", "detailAddress"],
    "家属姓名": ["familyName", "relativeName", "kinName", "guardianName"],
    "家属电话": ["familyPhone", "relativePhone", "kinPhone", "guardianPhone"],
    "家属": ["familyName", "relativeName", "guardianName"],

    # ── 入住信息类 ──
    "入住日期": ["checkinDate", "enrollmentDate", "checkInDate", "admissionDate", "enterDate", "inDate"],
    "入住状态": ["status", "checkinStatus", "enrollmentStatus", "liveStatus"],
    "入住类型": ["checkinType", "enrollmentType", "admissionType"],
    "房间号": ["roomNo", "roomNumber", "room", "roomId"],
    "床位号": ["bedNo", "bedNumber", "bedId", "bed"],
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
    "老人档案": "入住概览",
    "长者档案": "入住概览",
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
    if matched_endpoint_keywords:
        # 清理 matched_keywords 中的标注符号（如"老人~老人家"取前半部分）
        clean_kws = []
        for kw in matched_endpoint_keywords:
            clean_kw = kw.split("~")[0].split("→")[0].strip()
            if clean_kw:
                clean_kws.append(clean_kw)
        long_kws = sorted(
            [kw for kw in set(clean_kws) if len(kw) >= 3],
            key=len, reverse=True
        )
        for kw in long_kws:
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

    # Step 5: 按分隔符拆分
    parts = _FIELD_DELIMITERS.split(cleaned_query)

    extracted_fields = []
    for part in parts:
        part = part.strip().strip('，,、。.!！?？ \t\n\r')
        if not part or len(part) < 2:
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

def _detect_page_scrape_intent(user_query: str) -> dict:
    """
    检测用户是否想要爬取网页（而非调 API）

    触发条件（任一满足）：
      1. 包含完整 URL（http:// 或 https://）
      2. 包含 Vue 路由路径（/xxx/yyy）
      3. 包含页面采集关键词（"爬取页面""表单数据"等）

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


def _build_page_intent_result(page_intent: dict) -> dict:
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

    # v5: 如果有反馈 boost，传递历史字段建议
    if best.get("feedback_fields") and not fields:
        fields = best.get("feedback_fields", [])

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

    # 构建字段描述
    field_descriptions = {}
    for f in fields:
        synonyms = FIELD_SYNONYMS.get(f, [])
        if synonyms:
            field_descriptions[f] = f"用户需要的数据字段，可能的JSON key: {', '.join(synonyms[:3])}"
        else:
            field_descriptions[f] = "用户需要的自定义字段"

    matched_kw = best.get("matched_keywords", [])
    reasoning_parts = [f"规则匹配({match_type})"]
    reasoning_parts.append(f"关键词 [{', '.join(matched_kw)}]")
    reasoning_parts.append(f"端点 [{ep['name']}]")
    if has_field_hint:
        reasoning_parts.append("字段提示消歧")
    if has_feedback:
        reasoning_parts.append(f"反馈boost(+{best.get('feedback_boost', 0)})")
    reasoning = " → ".join(reasoning_parts)

    # v5: 候选端点列表增加匹配类型和反馈信息
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
  "alternative_apis": ["备选端点路径（如有歧义）"]
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

    # Step 0: 检测是否是页面采集意图
    page_intent = _detect_page_scrape_intent(user_query)
    if page_intent.get("is_page"):
        logger.info(f"检测到页面采集意图: {page_intent.get('url_or_route')} (by={page_intent.get('matched_by')})")
        result = _build_page_intent_result(page_intent)
        # 记录页面采集的反馈
        try:
            record_query(user_query, result)
        except Exception:
            pass  # 反馈记录失败不影响主流程
        return result

    # Step 1: 规则预匹配
    rule_result = _rule_based_parse(user_query)

    # Step 2: 高置信度直接返回
    if rule_result and rule_result["confidence"] >= 0.85:
        logger.info(f"规则匹配高置信度({rule_result['confidence']}), 跳过 LLM: {rule_result['api_name']}")
        result = _validate_and_fix_intent(rule_result)
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
                result = _validate_and_fix_intent(rule_result)
                try:
                    record_query(user_query, result)
                except Exception:
                    pass
                return result
            return llm_result

        # Step 4: 融合规则与 LLM 结果
        merged = _merge_rule_and_llm(rule_result, llm_result)
        result = _validate_and_fix_intent(merged)

        # v5: 否定词后验过滤 —— 如果 LLM 返回了被否定的端点，回退到规则结果
        negated = _extract_negated_terms(user_query)
        if negated and result.get("target_api"):
            result_name = result.get("api_name", "")
            for neg_term in negated:
                if neg_term in result_name:
                    logger.info(f"LLM 返回了被否定的端点 [{result_name}]，回退到规则结果")
                    if rule_result:
                        result = _validate_and_fix_intent(rule_result)
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
            result = _validate_and_fix_intent(rule_result)
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

    # 补充字段描述
    if not llm_result.get("field_descriptions"):
        llm_result["field_descriptions"] = rule_result.get("field_descriptions", {})

    # 记录规则匹配的关键词
    llm_result["rule_matched_keywords"] = rule_result.get("matched_keywords", [])
    llm_result["endpoint_candidates"] = rule_result.get("endpoint_candidates", [])

    return llm_result


# ══════════════════════════════════════════════════════════════
# 10. 后验校验与修正
# ══════════════════════════════════════════════════════════════

def _validate_and_fix_intent(intent: dict) -> dict:
    """校验意图解析结果并修复常见问题"""
    if not intent or "error" in intent:
        return intent

    target_api = intent.get("target_api", "")

    # 1. 验证 target_api 存在
    if target_api:
        valid_paths = {ep["path"] for ep in KNOWN_ENDPOINTS}
        if target_api not in valid_paths:
            # 尝试模糊匹配
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

def execute_custom_crawl(host: str, username: str, password: str, intent: dict) -> list:
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

    # 1. 拉取原始数据
    try:
        raw_rows = client.get_list(target_api)
    except Exception as e:
        return [{"error": f"API 调用失败: {e}"}]

    if not raw_rows:
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
        # 附带所有可用列名，供前端展示
        return [
            {col: row.get(col, "") for col in priority_keys}
            for row in raw_rows
        ]

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

    # 4. 按字段映射过滤数据
    results = []
    for row in raw_rows:
        item = {}
        for user_field in columns_order:
            raw_key = field_mapping.get(user_field, user_field)
            value = row.get(raw_key, "")
            # 如果值为空，尝试同义词表中的其他 key
            if not value:
                synonyms = FIELD_SYNONYMS.get(user_field, [])
                for syn in synonyms:
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

    return results
