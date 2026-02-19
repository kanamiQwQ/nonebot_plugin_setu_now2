import asyncio
import aiohttp
import time
import random
import io
from typing import Optional, List, Dict, Any
from PIL import Image
from nonebot import on_command, logger, get_bot
from nonebot.adapters.onebot.v11 import (
    MessageSegment, MessageEvent, Bot, Message,
    MessageSegment as MS
)
from nonebot.params import CommandArg
from nonebot.typing import T_State
from pydantic import BaseModel
from datetime import datetime

# ========== 核心配置 ==========
# 普通API列表（非R18，无tag关键词时使用）
NORMAL_API_LIST = [
    "https://api.anosu.top/img/?sort=setu",
    "https://api.anosu.top/img/?sort=pixiv&size=original",
    "https://api.suyanw.cn/api/mao.php",
    "https://t.alcy.cc/mp"
]
# Lolicon 专属API配置
LOLICON_NORMAL_API = "https://api.lolicon.app/setu/v2"  # 通用Lolicon API（tag关键词时调用）
LOLICON_R18_API = "https://api.lolicon.app/setu/v2?r18=1"  # R18 Lolicon API
# 冷却时间（秒）
COOLDOWN_TIME = 15
# 每日调用次数限制（普通用户）
DAILY_LIMIT = 10
# 超级用户ID列表（无限调用，无冷却/次数限制）
SUPER_USERS = {2376280479}  # 替换为实际超级用户QQ号
user_cooldown: Dict[int, float] = {}
# 存储用户每日调用次数
user_daily_count: Dict[int, Dict[str, int]] = {}
BOT_QQ = 3572614547  # 替换为机器人真实QQ
clean_task_started = False

# ========== 可自定义的合并转发配置 ==========
FORWARD_CONFIG = {
    "name": "世纪歌姬Kanami",          
    "avatar_url": "https://q1.qlogo.cn/g?b=qq&nk=123456789&s=3572614547"  # 替换为你的头像链接
}

# ========== 数据模型 ==========
class LoliconData(BaseModel):
    pid: int
    p: int
    uid: int
    title: str
    author: str
    r18: bool
    width: int
    height: int
    tags: List[str]
    ext: str
    aiType: int
    uploadDate: int
    urls: Dict[str, str]

class LoliconResponse(BaseModel):
    error: str
    data: List[LoliconData]

class AnosuResponse(BaseModel):
    code: int
    imgurl: Optional[str] = None
    tags: Optional[List[str]] = None
    title: Optional[str] = None

# ========== 工具函数：超级用户判断 ==========
def is_super_user(user_id: int) -> bool:
    """判断是否为超级用户"""
    return user_id in SUPER_USERS

# ========== 工具函数：每日次数检查 ==========
def check_daily_limit(user_id: int) -> bool:
    """检查用户每日调用次数是否超限，超级用户直接返回True"""
    if is_super_user(user_id):
        return True
    
    today = datetime.now().strftime("%Y-%m-%d")
    # 初始化用户每日数据
    if user_id not in user_daily_count:
        user_daily_count[user_id] = {"date": today, "count": 0}
    
    # 跨天重置计数
    if user_daily_count[user_id]["date"] != today:
        user_daily_count[user_id] = {"date": today, "count": 0}
    
    # 检查是否超限
    if user_daily_count[user_id]["count"] >= DAILY_LIMIT:
        return False
    
    # 未超限则计数+1
    user_daily_count[user_id]["count"] += 1
    return True

# ========== 图片处理核心函数 ==========
async def download_img(img_url: str) -> Optional[Image.Image]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(img_url, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning(f"下载图片失败，状态码: {resp.status}")
                    return None
                img_bytes = await resp.read()
                img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
                return img
    except Exception as e:
        logger.error(f"下载图片异常: {e}")
        return None

def process_img(img: Image.Image) -> Optional[Image.Image]:
    try:
        width, height = img.size
        if width > 0 and height > 0:
            pixels = img.load()
            pixels[width-1, 0] = (0, 0, 0, 0)  # 右上角像素设为透明
        flipped_img = img.transpose(Image.FLIP_LEFT_RIGHT)
        return flipped_img
    except Exception as e:
        logger.error(f"图片处理异常: {e}")
        return None

def img_to_bytes(img: Image.Image) -> Optional[bytes]:
    try:
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format='PNG')
        img_byte_arr.seek(0)
        return img_byte_arr.getvalue()
    except Exception as e:
        logger.error(f"图片转字节流异常: {e}")
        return None

# ========== 工具函数：清理图片链接后缀 ==========
def clean_img_url(img_url: str) -> str:
    if "," in img_url:
        img_url = img_url.split(",")[0]
    if img_url.startswith(("http://", "https://")):
        return img_url.strip()
    return ""

# ========== 工具函数：格式化标签 ==========
def format_tags(tags: List[str]) -> str:
    """仅返回「标签1 | 标签2」格式"""
    if len(tags) == 0:
        return "无标签"
    return " | ".join(tags[:2])

# ========== 工具函数：修复Lolicon Tag格式 ==========
def format_lolicon_tags(tag_str: str) -> List[str]:
    """
    修复Lolicon API的tag格式：
    - 输入："萝莉 少女 白丝"
    - 输出：["萝莉|少女", "白丝"]（组内OR，组间AND）
    """
    if not tag_str:
        return []
    
    # 拆分标签为列表
    tags = [t.strip() for t in tag_str.split() if t.strip()]
    # 每2个标签为一组（可根据需求调整分组规则）
    group_size = 2
    formatted_tags = []
    for i in range(0, len(tags), group_size):
        group = tags[i:i+group_size]
        formatted_tags.append("|".join(group))
    
    # 限制AND组数量（API限制最多3组）
    return formatted_tags[:3]

# ========== 工具函数：从Lolicon API获取图片（通用/ R18 通用） ==========
async def get_setu_from_lolicon(tag: str = "", r18: bool = False) -> Dict[str, Any]:
    """
    从Lolicon API获取图片：
    - 修复tag参数格式问题
    - r18=True → 调用R18 API
    - r18=False → 调用通用API
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    }
    
    # 格式化tag参数（核心修复点）
    formatted_tags = format_lolicon_tags(tag)
    
    # 构建请求参数
    params = {
        "r18": 1 if r18 else 0,
        "num": 1,
        "size": ["original"]
    }
    if formatted_tags:
        params["tag"] = formatted_tags
    
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            # 使用POST请求（更适配复杂tag格式，GET也可但POST更稳定）
            async with session.post(
                LOLICON_NORMAL_API,
                json=params,
                headers=headers
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"Lolicon API 返回状态码: {resp.status}")
                    return {}
                
                raw_data = await resp.json()
                lolicon_data = LoliconResponse(**raw_data)
                
                if not lolicon_data.error and lolicon_data.data:
                    first_item = lolicon_data.data[0]
                    clean_url = clean_img_url(first_item.urls.get("original", ""))
                    return {
                        "img_url": clean_url,
                        "tags": first_item.tags,
                        "r18": first_item.r18
                    }
                else:
                    logger.warning(f"Lolicon API 返回空数据，错误信息: {lolicon_data.error}")
                    return {}
    except Exception as e:
        logger.error(f"请求Lolicon API异常: {e}")
        return {}

# ========== 工具函数：从普通API获取图片（无tag关键词+非R18时使用） ==========
async def get_normal_setu_from_api(api_url: str, tag: str = "") -> Dict[str, Any]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    }
    
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(api_url, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning(f"普通API {api_url} 返回状态码: {resp.status}")
                    return {}
                
                if "anosu.top" in api_url:
                    try:
                        data = await resp.json()
                        anosu_data = AnosuResponse(**data)
                        if anosu_data.imgurl:
                            clean_url = clean_img_url(anosu_data.imgurl)
                            return {
                                "img_url": clean_url,
                                "tags": anosu_data.tags or ["无标签"]
                            }
                    except Exception as e:
                        logger.error(f"解析Anosu API失败: {e}")
                        return {}
                
                elif "suyanw.cn" in api_url or "alcy.cc" in api_url:
                    raw_url = str(resp.url)
                    clean_url = clean_img_url(raw_url)
                    user_tag = tag or "随机"
                    tags = [user_tag, "无详细标签"]
                    return {
                        "img_url": clean_url,
                        "tags": tags
                    }
                
                return {}
    except Exception as e:
        logger.error(f"请求普通API异常: {e}")
        return {}

# ========== 核心函数：根据关键词/ R18 选择对应API ==========
async def get_setu(raw_tag: str = "", has_tag_keyword: bool = False, r18: bool = False) -> Dict[str, Any]:
    """
    核心逻辑：
    1. 有tag关键词 → 强制调用Lolicon API
    2. 无tag关键词 + R18 → 调用Lolicon R18 API
    3. 无tag关键词 + 非R18 → 轮询普通API
    """
    # 清理标签（移除tag/r18关键词）
    clean_tag = raw_tag.replace("tag", "").replace("r18", "").strip()
    
    # 1. 有tag关键词 → 强制调用Lolicon API
    if has_tag_keyword:
        logger.info(f"检测到tag关键词，强制调用Lolicon API（R18: {r18}）")
        return await get_setu_from_lolicon(clean_tag, r18)
    
    # 2. 无tag关键词 + R18 → 调用Lolicon R18 API
    elif r18:
        return await get_setu_from_lolicon(clean_tag, r18)
    
    # 3. 无tag关键词 + 非R18 → 轮询普通API
    else:
        random_api_list = random.sample(NORMAL_API_LIST, len(NORMAL_API_LIST))
        for api in random_api_list:
            normal_data = await get_normal_setu_from_api(api, clean_tag)
            if normal_data and normal_data.get("img_url"):
                return normal_data
        
        logger.error("所有API均获取失败")
        return {}

# ========== 工具函数：构建合并转发节点 ==========
def build_forward_nodes(setu_data: Dict[str, Any], img_bytes: bytes) -> List[Dict]:
    """仅输出标签，无多余内容"""
    formatted_tags = format_tags(setu_data["tags"])
    r18_tag = "[R18] " if setu_data.get("r18", False) else ""
    
    content_text = f"{r18_tag}🏷️ 标签：{formatted_tags}"
    
    text_node = {
        "type": "node",
        "data": {
            "name": FORWARD_CONFIG["name"],
            "uin": str(BOT_QQ),
            "content": Message(content_text.strip()),
            "avatar": FORWARD_CONFIG["avatar_url"]
        }
    }
    
    img_node = {
        "type": "node",
        "data": {
            "name": FORWARD_CONFIG["name"],
            "uin": str(BOT_QQ),
            "content": MS.image(img_bytes),
            "avatar": FORWARD_CONFIG["avatar_url"]
        }
    }
    
    return [text_node, img_node]

# ========== 冷却数据清理 ==========
async def clean_cooldown_data():
    while True:
        await asyncio.sleep(3600)
        current_time = time.time()
        # 清理冷却数据
        expired_users = [uid for uid, t in user_cooldown.items() if current_time - t > 3600]
        for uid in expired_users:
            del user_cooldown[uid]
        # 清理过期的每日计数（保留7天内数据）
        today = datetime.now().strftime("%Y-%m-%d")
        expired_count_users = []
        for uid, data in user_daily_count.items():
            if data["date"] != today:
                expired_count_users.append(uid)
        for uid in expired_count_users[:100]:  # 限制单次清理数量
            del user_daily_count[uid]
        logger.info(f"清理过期数据：冷却记录{len(expired_users)}条，每日计数{len(expired_count_users)}条")

# ========== 指令注册 ==========
setu_cmd = on_command("setu", aliases={"色图", "涩图"}, priority=5, block=True)

# ========== 指令处理逻辑 ==========
@setu_cmd.handle()
async def handle_setu(bot: Bot, event: MessageEvent, state: T_State, arg: Message = CommandArg()):
    global clean_task_started
    if not clean_task_started:
        asyncio.create_task(clean_cooldown_data())
        clean_task_started = True
        logger.info("✅ 冷却/计数数据清理任务已成功启动")
    
    # 1. 获取用户ID
    user_id = event.user_id
    
    # 2. 检查超级用户：超级用户跳过冷却和次数限制
    if not is_super_user(user_id):
        # 检查每日调用次数
        if not check_daily_limit(user_id):
            await setu_cmd.finish(f"⚠️ 今日调用次数已达上限（{DAILY_LIMIT}次），超级用户无此限制哦~")
        
        # 检查冷却限制
        current_time = time.time()
        if user_id in user_cooldown:
            last_time = user_cooldown[user_id]
            if current_time - last_time < COOLDOWN_TIME:
                remaining = int(COOLDOWN_TIME - (current_time - last_time))
                await setu_cmd.finish(f"⏳ 冷却中！请等待{remaining}秒后再请求~")
        
        # 更新冷却时间
        user_cooldown[user_id] = current_time
    
    # 3. 解析指令参数
    raw_tag = arg.extract_plain_text().strip().lower()
    has_tag_keyword = "tag" in raw_tag  # 检测是否包含tag关键词
    r18 = "r18" in raw_tag             # 检测是否包含r18关键词
    
    logger.info(f"用户 {user_id} 请求色图，原始标签: {raw_tag}，tag关键词: {has_tag_keyword}，R18: {r18}，超级用户: {is_super_user(user_id)}")
    
    # 4. 发送加载提示
    tip_text = "正在从Lolicon接口获取并处理色图，请稍等..." if has_tag_keyword else (
        "正在获取并处理R18色图，请稍等..." if r18 else "正在获取并处理色图，请稍等..."
    )
    await setu_cmd.send(tip_text)
    
    # 5. 获取图片元数据（根据tag关键词/R18选择API）
    setu_data = await get_setu(raw_tag, has_tag_keyword, r18)
    if not setu_data or not setu_data.get("img_url"):
        fail_text = "😭 Lolicon接口获取失败" if has_tag_keyword else (
            "😭 R18色图获取失败" if r18 else "😭 所有接口都获取失败了，请稍后再试！"
        )
        await setu_cmd.finish(fail_text)
    
    # 6. 下载图片
    img = await download_img(setu_data["img_url"])
    if not img:
        await setu_cmd.finish("😭 图片下载失败，请稍后再试！")
    
    # 7. 处理图片
    processed_img = process_img(img)
    if not processed_img:
        await setu_cmd.finish("😭 图片处理失败，请稍后再试！")
    
    # 8. 转为字节流
    img_bytes = img_to_bytes(processed_img)
    if not img_bytes:
        await setu_cmd.finish("😭 图片格式转换失败，请稍后再试！")
    
    # 9. 补充R18标签（如需）
    if r18 and "r18" not in setu_data["tags"]:
        setu_data["tags"].append("r18")
    
    try:
        # 10. 发送合并转发
        forward_nodes = build_forward_nodes(setu_data, img_bytes)
        if event.group_id:
            await bot.call_api(
                "send_group_forward_msg",
                group_id=event.group_id,
                messages=forward_nodes
            )
        else:
            await bot.call_api(
                "send_private_forward_msg",
                user_id=user_id,
                messages=forward_nodes
            )
        logger.info(f"用户 {user_id} 的处理后色图发送成功（tag关键词: {has_tag_keyword}，R18: {r18}）")
    
    except Exception as e:
        # 降级发送：仅保留标签
        logger.error(f"发送聊天记录失败: {e}")
        formatted_tags = format_tags(setu_data["tags"])
        r18_tag = "[R18] " if setu_data.get("r18", False) else ""
        
        fallback_msg = (
            f"{r18_tag}🏷️ 标签: {formatted_tags}\n"
            f"{MS.image(img_bytes)}"
        )
        await setu_cmd.send(f"😥 聊天记录发送失败，降级发送：\n{fallback_msg}")