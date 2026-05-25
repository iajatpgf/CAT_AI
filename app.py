import streamlit as st
import requests
import json
import time
import re
from datetime import datetime
from urllib.parse import quote
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# ====================== 安全配置区（从Streamlit Secrets读取，无硬编码密钥）======================
def get_secret(key):
    """安全获取Secrets，统一错误处理"""
    try:
        return st.secrets[key]
    except KeyError:
        st.error(f"❌ 缺少必要配置：请在Streamlit Secrets中添加 `{key}`")
        st.info("本地开发请在 .streamlit/secrets.toml 中配置，部署请在Streamlit Cloud后台添加")
        return None

# 大模型API（通义千问）
LLM_API_KEY = get_secret("DASHSCOPE_API_KEY")
LLM_API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
LLM_MODEL = "qwen3.7-max"  # 推荐使用qwen-max-0428或更高版本

# 搜索API（Serper）
SERPER_API_KEY = get_secret("SERPER_API_KEY")

# ====================== 锂电领域顶级期刊列表（Wiley优先）======================
TOP_JOURNALS_BY_PUBLISHER = {
    "Wiley": [
        "Advanced Materials",
        "Advanced Energy Materials",
        "Advanced Functional Materials",
        "Angewandte Chemie International Edition",
        "Advanced Science",
        "Small Methods",
        "Batteries & Supercaps",
        "Chemistry - A European Journal",
        "ChemSusChem",
        "Advanced Sustainable Systems",
        "Energy Technology",
        "ChemElectroChem"
    ],
    "Nature/Springer": [
        "Nature",
        "Science",
        "Cell",
        "Nature Energy",
        "Nature Materials",
        "Nature Nanotechnology",
        "Nature Communications",
        "Science Advances",
        "Energy & Environmental Science",
        "Journal of the American Chemical Society",
        "ACS Energy Letters",
        "Nano Letters",
        "ACS Nano",
        "Journal of Materials Chemistry A",
        "Small"
    ],
    "Elsevier": [
        "Journal of Power Sources",
        "Electrochimica Acta",
        "Solid State Ionics",
        "Electrochemistry Communications",
        "Journal of Energy Chemistry",
        "Carbon",
        "Nano Energy"
    ]
}
TOP_JOURNALS = [journal for journals in TOP_JOURNALS_BY_PUBLISHER.values() for journal in journals]

# ====================== 锂电行业头部企业列表 ======================
TOP_BATTERY_COMPANIES = [
    "宁德时代", "CATL", "比亚迪", "BYD", "中创新航", "CALB", "国轩高科", "Gotion",
    "亿纬锂能", "EVE Energy", "欣旺达", "Sunwoda", "孚能科技", "Farasis Energy",
    "鹏辉能源", "Great Power", "赣锋锂业", "Ganfeng Lithium", "天齐锂业", "Tianqi Lithium",
    "特斯拉", "Tesla", "LG新能源", "LG Energy Solution", "松下", "Panasonic",
    "三星SDI", "Samsung SDI", "SK On", "Northvolt", "QuantumScape", "Solid Power"
]

# ====================== 中英文关键词映射表 ======================
KEYWORD_TRANSLATION = {
    "锂电池": "lithium battery",
    "固态电池": "solid-state battery",
    "固态电解质": "solid electrolyte",
    "硫化物电解质": "sulfide electrolyte",
    "氧化物电解质": "oxide electrolyte",
    "聚合物电解质": "polymer electrolyte",
    "锂金属电池": "lithium metal battery",
    "硅负极": "silicon anode",
    "高镍正极": "high-nickel cathode",
    "三元材料": "NCM cathode",
    "磷酸铁锂": "LFP cathode",
    "电解液": "electrolyte",
    "电解液添加剂": "electrolyte additive",
    "隔膜": "separator",
    "SEI膜": "SEI film",
    "锂枝晶": "lithium dendrite",
    "快充": "fast charging",
    "4680电池": "4680 battery",
    "钠离子电池": "sodium ion battery",
    "全固态电池": "all-solid-state battery",
    "半固态电池": "semi-solid-state battery",
    "无负极电池": "anode-free battery",
    "电池回收": "battery recycling",
    "热管理": "thermal management",
    "电池安全": "battery safety"
}

# ====================== 增强型工具函数（修复超时+Wiley论文bug）======================
def search_web(query, num_results=10, gl="cn", hl="zh-cn"):
    if not SERPER_API_KEY:
        return {}
    url = "https://google.serper.dev/search"
    payload = json.dumps({
        "q": query,
        "num": num_results,
        "gl": gl,
        "hl": hl,
        "tbs": "qdr:y"
    })
    headers = {'X-API-KEY': SERPER_API_KEY, 'Content-Type': 'application/json'}
    try:
        response = requests.post(url, headers=headers, data=payload, timeout=30)
        return response.json()
    except Exception as e:
        st.warning(f"网络搜索超时: {str(e)}")
        return {}

def search_scholar(query, num_results=20, as_ylo=2024, as_yhi=2026, english_only=True,
                   publisher_priority="wiley", top_journals_only=True):
    if not SERPER_API_KEY:
        return {}
    url = "https://google.serper.dev/scholar"
    english_query = translate_to_english(query)
    final_query = english_query
    
    if publisher_priority == "wiley":
        wiley_query = f"site:onlinelibrary.wiley.com {english_query}"
        final_query = wiley_query
        st.info("🔍 正在优先搜索Wiley出版社论文...")
    elif top_journals_only:
        journal_query = " OR ".join([f"source:{journal}" for journal in TOP_JOURNALS[:20]])
        final_query = f"{english_query} ({journal_query})"
    
    payload = json.dumps({
        "q": final_query,
        "num": num_results,
        "as_ylo": as_ylo,
        "as_yhi": as_yhi,
        "hl": "en",
        "lr": "lang_en",
        "scisbd": 2,
        "as_sdt": "0"
    })
    headers = {'X-API-KEY': SERPER_API_KEY, 'Content-Type': 'application/json'}
    try:
        response = requests.post(url, headers=headers, data=payload, timeout=35)
        results = response.json()
        
        if publisher_priority == "wiley" and len(results.get("organic", [])) < 8:
            st.info("Wiley结果较少，正在补充其他顶级期刊论文...")
            additional_results = search_scholar(query, num_results=10, as_ylo=as_ylo, as_yhi=as_yhi,
                                                english_only=english_only, publisher_priority="none",
                                                top_journals_only=True)
            if "organic" in additional_results:
                results["organic"] = results.get("organic", []) + additional_results["organic"]
        return results
    except Exception as e:
        st.warning(f"学术搜索超时: {str(e)}")
        return {}

def search_patents(query, num_results=10, as_ylo=2023):
    if not SERPER_API_KEY:
        return {}
    url = "https://google.serper.dev/search"
    payload = json.dumps({
        "q": f"{query} lithium battery patent",
        "num": num_results,
        "tbm": "pts",
        "tbs": f"date:r:{as_ylo}0101:20991231",
        "hl": "en",
        "lr": "lang_en"
    })
    headers = {'X-API-KEY': SERPER_API_KEY, 'Content-Type': 'application/json'}
    try:
        response = requests.post(url, headers=headers, data=payload, timeout=30)
        return response.json()
    except Exception as e:
        st.warning(f"专利搜索超时: {str(e)}")
        return {}

def search_news(query, num_results=20, time_range="qdr:m3", include_companies=True):
    if not SERPER_API_KEY:
        return {}
    url = "https://google.serper.dev/news"
    final_query = query
    
    if include_companies:
        companies_query = " OR ".join(TOP_BATTERY_COMPANIES[:15])
        final_query = f"{query} ({companies_query})"
    
    payload = json.dumps({
        "q": final_query,
        "num": num_results,
        "tbs": time_range,
        "gl": "us",
        "hl": "en"
    })
    headers = {'X-API-KEY': SERPER_API_KEY, 'Content-Type': 'application/json'}
    try:
        response = requests.post(url, headers=headers, data=payload, timeout=30)
        results = response.json()
        
        if include_companies:
            chinese_payload = json.dumps({
                "q": query,
                "num": 10,
                "tbs": time_range,
                "gl": "cn",
                "hl": "zh-cn"
            })
            chinese_response = requests.post(url, headers=headers, data=chinese_payload, timeout=30)
            chinese_results = chinese_response.json()
            if "news" in chinese_results:
                results["news"] = results.get("news", []) + chinese_results["news"]
        return results
    except Exception as e:
        st.warning(f"新闻搜索超时: {str(e)}")
        return {}

def translate_to_english(chinese_query):
    for cn, en in KEYWORD_TRANSLATION.items():
        if cn in chinese_query:
            chinese_query = chinese_query.replace(cn, en)
    
    if any('\u4e00' <= c <= '\u9fff' for c in chinese_query):
        translation_prompt = f"""
        将以下锂电领域的中文搜索关键词翻译成专业的英文搜索关键词：
        {chinese_query}
        要求：
        1. 使用学术领域常用的专业术语
        2. 保持简洁，适合作为Google Scholar搜索关键词
        3. 不要添加额外解释
        """
        english_query = llm_call(translation_prompt, system_prompt="你是专业的科技翻译专家", temperature=0.1)
        return english_query.strip()
    return chinese_query

# ====================== 核心修复：带重试机制的大模型调用（解决Read timed out）======================
@retry(
    stop=stop_after_attempt(3),  # 最多重试3次
    wait=wait_exponential(multiplier=1, min=2, max=15),  # 等待时间：2s→4s→8s
    retry=retry_if_exception_type((
        requests.exceptions.ReadTimeout,
        requests.exceptions.ConnectionError,
        requests.exceptions.ChunkedEncodingError
    ))
)
def llm_call(prompt,
             system_prompt="你是资深锂电电化学研发专家，拥有10年以上行业经验，精通材料科学、电化学原理和电池工程技术。能够准确解读Wiley、Nature等出版社的英文顶刊论文，提取核心数据和创新点。",
             temperature=0.3, max_tokens=12000):
    if not LLM_API_KEY:
        return "⚠️ 请先配置通义千问API密钥"
    
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "top_p": 0.8
    }
    
    try:
        # 关键修复：拆分连接超时(15s)和读取超时(240s)，大幅降低海外节点超时概率
        response = requests.post(
            LLM_API_URL,
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=(15, 240)
        )
        response.raise_for_status()  # 主动抛出HTTP错误(401/429/500等)
        response_json = response.json()
        
        if "choices" in response_json and len(response_json["choices"]) > 0:
            return response_json["choices"][0]["message"]["content"]
        else:
            return f"❌ 大模型返回异常: {json.dumps(response_json, ensure_ascii=False)}"
    
    except requests.exceptions.ReadTimeout:
        st.warning("⏳ 大模型响应超时，正在自动重试...")
        raise  # 触发重试机制
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 429:
            return "❌ API调用频率超限，请稍后再试"
        elif e.response.status_code == 401:
            return "❌ API密钥无效，请检查配置"
        else:
            return f"❌ HTTP错误: {e.response.status_code} - {e.response.text}"
    except Exception as e:
        return f"❌ 大模型调用失败: {str(e)}"

# ====================== 增强型思考过程展示 ======================
class ThinkingLogger:
    def __init__(self):
        self.steps = []
        self.container = st.empty()
    
    def add_step(self, icon, title, status="running", details=None):
        step = {"icon": icon, "title": title, "status": status, "details": details, "time": datetime.now()}
        self.steps.append(step)
        self.render()
    
    def update_step(self, index, status="complete", details=None):
        if 0 <= index < len(self.steps):
            self.steps[index]["status"] = status
            if details:
                self.steps[index]["details"] = details
            self.render()
    
    def render(self):
        with self.container.container():
            st.markdown("### 🧠 Agent实时思考过程")
            for i, step in enumerate(self.steps):
                status_icon = {
                    "running": "⏳",
                    "complete": "✅",
                    "error": "❌",
                    "thinking": "🤔",
                    "analyzing": "🔬",
                    "translating": "🌐",
                    "searching_wiley": "📚",
                    "searching_companies": "🏭"
                }.get(step["status"], "⏳")
                st.markdown(f"{status_icon} **{step['icon']} {step['title']}**")
                if step["details"] and step["status"] != "running":
                    with st.expander("查看详情", expanded=False):
                        st.markdown(step["details"])
            st.divider()

# ====================== 增强型核心Agent引擎 ======================
class LithiumBatteryAgent:
    def __init__(self):
        self.logger = ThinkingLogger()
        self.references = []
    
    def add_reference(self, item, item_type):
        ref_id = len(self.references) + 1
        ref = {
            "id": ref_id,
            "type": item_type,
            "title": item.get("title", ""),
            "link": item.get("link", ""),
            "snippet": item.get("snippet", ""),
            "source": item.get("source", ""),
            "date": item.get("date", "")
        }
        if item_type == "scholar":
            ref["authors"] = item.get("authors", "")
            ref["citations"] = item.get("citations", 0)
            ref["publication"] = item.get("publication", "")
            for publisher, journals in TOP_JOURNALS_BY_PUBLISHER.items():
                if ref["publication"] in journals:
                    ref["publisher"] = publisher
                    break
        elif item_type == "patent":
            ref["patent_number"] = item.get("patentNumber", "")
            ref["assignee"] = item.get("assignee", "")
            ref["filing_date"] = item.get("filingDate", "")
        elif item_type == "news":
            ref["company"] = self.identify_company(item.get("title", "") + " " + item.get("snippet", ""))
        self.references.append(ref)
        return ref_id
    
    def identify_company(self, text):
        for company in TOP_BATTERY_COMPANIES:
            if company.lower() in text.lower():
                return company
        return None
    
    def run(self, task, agent_type, scholar_params=None, news_params=None):
        self.references = []
        if scholar_params is None:
            scholar_params = {
                "num_results": 20,
                "as_ylo": 2024,
                "as_yhi": 2026,
                "english_only": True,
                "publisher_priority": "wiley",
                "top_journals_only": True
            }
        if news_params is None:
            news_params = {
                "num_results": 20,
                "time_range": "qdr:m3",
                "include_companies": True
            }
        
        # 步骤1：深度任务理解与智能拆解
        self.logger.add_step("📋", "深度任务理解与智能拆解", "thinking")
        decomposition_prompt = f"""
        作为锂电研发专家，将以下任务拆解为4-6个具体的执行步骤。
        任务：{task}
        功能类型：{agent_type}
        要求：
        1. 每个步骤明确需要搜索的内容和使用的工具
        2. 针对"追新补盲"任务，必须包含：
           - 最新行业新闻（过去3个月）
           - 头部企业动态（宁德时代、比亚迪、特斯拉等）
           - 2024-2026年Wiley出版社优先的英文顶刊论文
           - 2023年后国际专利
        3. 针对"第一性原理解释"任务，必须包含：基础原理搜索和权威教材/文献验证
        4. 针对"跨界提取营养"任务，必须包含：目标行业技术搜索和锂电应用可行性分析
        5. 针对"头脑风暴"任务，必须包含：现有技术瓶颈搜索和跨领域创新点搜索
        6. 所有学术搜索必须优先使用Wiley出版社的期刊，然后是Nature/Springer和Elsevier
        输出格式：严格JSON数组
        [
            {{"步骤名称": "xxx", "搜索关键词": "xxx", "工具类型": "web/scholar/patent/news/none", "说明": "xxx"}},
            {{"步骤名称": "xxx", "搜索关键词": "xxx", "工具类型": "web/scholar/patent/news/none", "说明": "xxx"}}
        ]
        """
        steps_response = llm_call(decomposition_prompt, temperature=0.1)
        try:
            if "```json" in steps_response:
                steps_response = steps_response.split("```json")[1].split("```")[0].strip()
            steps = json.loads(steps_response)
        except Exception as e:
            st.warning(f"任务拆解失败，使用默认流程: {str(e)}")
            steps = self.get_default_steps(task, agent_type)
        self.logger.update_step(0, "complete", f"已拆解为{len(steps)}个执行步骤")
        
        # 步骤2：并行执行搜索任务
        all_results = {
            "web": [],
            "scholar": [],
            "patent": [],
            "news": []
        }
        for i, step in enumerate(steps):
            step_num = i + 1
            self.logger.add_step("🔍", f"步骤{step_num}/{len(steps)}: {step['步骤名称']}", "running")
            results = {}
            tool_type = step["工具类型"]
            if tool_type == "web":
                results = search_web(step["搜索关键词"])
                if "organic" in results:
                    for item in results["organic"]:
                        item["ref_id"] = self.add_reference(item, "web")
                    all_results["web"].extend(results["organic"])
            elif tool_type == "scholar":
                self.logger.update_step(step_num, "translating", "正在将中文关键词转换为专业英文术语")
                english_keywords = translate_to_english(step["搜索关键词"])
                self.logger.update_step(step_num, "searching_wiley", f"正在优先搜索Wiley出版社论文: {english_keywords}")
                results = search_scholar(step["搜索关键词"], **scholar_params)
                if "organic" in results:
                    for item in results["organic"]:
                        item["ref_id"] = self.add_reference(item, "scholar")
                    all_results["scholar"].extend(results["organic"])
            elif tool_type == "patent":
                results = search_patents(step["搜索关键词"])
                if "organic" in results:
                    for item in results["organic"]:
                        item["ref_id"] = self.add_reference(item, "patent")
                    all_results["patent"].extend(results["organic"])
            elif tool_type == "news":
                self.logger.update_step(step_num, "searching_companies", "正在搜索头部企业最新动态")
                results = search_news(step["搜索关键词"], **news_params)
                if "news" in results:
                    for item in results["news"]:
                        item["ref_id"] = self.add_reference(item, "news")
                    all_results["news"].extend(results["news"])
            
            result_count = len(results.get("organic", [])) if tool_type in ["web", "scholar", "patent"] else len(
                results.get("news", []))
            if result_count > 0:
                self.logger.update_step(step_num, "complete", f"找到{result_count}条相关信息")
            else:
                self.logger.update_step(step_num, "complete", "未找到相关信息")
        
        # 步骤3：深度信息分析与结构化处理
        self.logger.add_step("🔬", "深度信息分析与结构化处理", "analyzing")
        if agent_type == "追新补盲":
            analysis_prompt = self.get_news_analysis_prompt(task, all_results)
        elif agent_type == "跨界提取营养":
            analysis_prompt = self.get_cross_domain_analysis_prompt(task, all_results)
        elif agent_type == "第一性原理解释":
            analysis_prompt = self.get_first_principle_analysis_prompt(task, all_results)
        else:
            analysis_prompt = self.get_brainstorm_analysis_prompt(task, all_results)
        
        final_answer = llm_call(analysis_prompt, temperature=0.4, max_tokens=16000)
        self.logger.update_step(len(steps) + 1, "complete", "深度分析完成")
        
        # 步骤4：生成格式化参考文献
        self.logger.add_step("📝", "生成专业参考文献列表", "running")
        references_text = self.generate_references()
        self.logger.update_step(len(steps) + 2, "complete", "报告生成完成")
        
        return final_answer + "\n\n" + references_text
    
    def get_default_steps(self, task, agent_type):
        if agent_type == "追新补盲":
            return [
                {"步骤名称": "最新行业新闻搜索", "搜索关键词": task, "工具类型": "news",
                 "说明": "搜索过去3个月的相关新闻动态"},
                {"步骤名称": "头部企业动态搜索", "搜索关键词": task, "工具类型": "news",
                 "说明": "专门搜索宁德时代、比亚迪、特斯拉等头部企业动态"},
                {"步骤名称": "2024-2026年Wiley优先英文顶刊论文检索", "搜索关键词": task, "工具类型": "scholar",
                 "说明": "优先检索Wiley出版社的顶级期刊论文"},
                {"步骤名称": "最新国际专利分析", "搜索关键词": task, "工具类型": "patent",
                 "说明": "分析2023年以后申请的国际专利"},
                {"步骤名称": "综合信息整合与分析", "搜索关键词": "", "工具类型": "none",
                 "说明": "整合所有信息，生成专业报告"}
            ]
        elif agent_type == "第一性原理解释":
            return [
                {"步骤名称": "基础原理搜索", "搜索关键词": task, "工具类型": "web",
                 "说明": "搜索相关的基础物理化学原理"},
                {"步骤名称": "权威文献验证", "搜索关键词": task + " electrochemical principle", "工具类型": "scholar",
                 "说明": "查找权威教材和综述文献"},
                {"步骤名称": "最新研究进展", "搜索关键词": task + " latest research", "工具类型": "scholar",
                 "说明": "了解该领域的最新理论发展"},
                {"步骤名称": "深度原理解释", "搜索关键词": "", "工具类型": "none",
                 "说明": "从第一性原理出发进行详细解释"}
            ]
        else:
            return [
                {"步骤名称": "综合信息搜索", "搜索关键词": task, "工具类型": "web"},
                {"步骤名称": "学术文献检索", "搜索关键词": task, "工具类型": "scholar"},
                {"步骤名称": "深度分析与总结", "搜索关键词": "", "工具类型": "none"}
            ]
    
    def get_news_analysis_prompt(self, task, all_results):
        return f"""
        作为锂电研发领域的资深专家，请基于以下搜索到的信息，为用户生成一份专业的"追新补盲"技术情报报告。
        用户任务：{task}
        搜索结果：
        1. 最新新闻({len(all_results['news'])}条): {json.dumps(all_results['news'], ensure_ascii=False, indent=2)}
        2. 学术论文({len(all_results['scholar'])}篇): {json.dumps(all_results['scholar'], ensure_ascii=False, indent=2)}
        3. 专利信息({len(all_results['patent'])}条): {json.dumps(all_results['patent'], ensure_ascii=False, indent=2)}
        4. 网页信息({len(all_results['web'])}条): {json.dumps(all_results['web'], ensure_ascii=False, indent=2)}
        请严格按照以下格式输出报告：
        # 技术情报报告：{task}
        ## 一、核心要点速览
        用3-5条精炼的要点总结最重要的信息，让研发人员快速掌握全局。
        ## 二、头部企业最新动态（重点增强）
        按企业分类整理重要动态，每个企业包含：
        - 企业名称
        - 时间、来源
        - 事件概述（技术突破、产品发布、产能扩张、战略合作等）
        - 技术/商业影响分析
        - 对我们研发工作的启示
        ## 三、行业整体动态与趋势
        整理行业层面的重要新闻和趋势，包含：
        - 政策法规变化
        - 市场规模与预测
        - 技术发展趋势
        - 产业链动态
        ## 四、学术前沿与技术突破（Wiley优先）
        对每篇重要论文进行深度解析，**优先分析Wiley出版社的论文和2026年发表的最新研究**，包含：
        - 论文标题、作者、发表期刊、发表时间[引用编号]
        - 出版社标记（Wiley/Nature/Springer/Elsevier）
        - 核心研究问题
        - 关键实验数据和结果（必须具体，如循环次数、容量、效率、阻抗等）
        - 核心结论
        - 底层化学/电化学原理（详细解释）
        - 主要创新点（与之前工作的区别）
        - 存在的局限性和挑战
        - 商业化前景评估
        ## 五、头部企业国际专利布局分析
        对每个重要专利进行详细分析，包含：
        - 专利标题、申请人、申请日期[引用编号]
        - 专利技术要点
        - 保护范围分析
        - 技术优势与创新点
        - 可能的应用场景
        - 对我们的技术路线影响
        ## 六、知识盲区识别与补充建议
        基于以上信息，识别出当前可能存在的知识盲区，并给出具体的补充学习建议。
        ## 七、AI独立思考与判断
        这是最重要的部分！请从资深研发专家的角度进行独立思考：
        1. 信息可信度评估（区分炒作和真实技术突破）
        2. 不同来源信息的一致性与矛盾点分析
        3. 技术发展趋势预测（短期1-2年，中期3-5年）
        4. 存在的技术瓶颈和挑战
        5. 对我们研发工作的具体建议（可落地的行动项）
        6. 潜在的风险和机会
        重要要求：
        1. 在文中引用信息时，必须使用[数字]标记对应的参考文献
        2. 所有链接必须保留，确保可以直接点击跳转
        3. 化学原理部分要准确、深入，使用专业术语
        4. 数据要具体，避免模糊表述
        5. 分析要客观中立，既有优点也要指出不足
        6. 重点突出Wiley出版社的论文和2026年发表的最新研究成果
        7. 头部企业动态要详细，包含技术细节和商业影响
        """
    
    def get_cross_domain_analysis_prompt(self, task, all_results):
        return f"""
        作为锂电研发领域的资深专家，请基于以下搜索到的信息，为用户生成一份专业的"跨界提取营养"分析报告。
        用户任务：{task}
        搜索结果：
        {json.dumps(all_results, ensure_ascii=False, indent=2)}
        请严格按照以下格式输出报告：
        # 跨界技术应用分析报告：{task}
        ## 一、锂电行业痛点分析
        深入分析当前锂电行业面临的核心痛点和技术瓶颈。
        ## 二、跨界技术提取
        从其他行业（航空航天、生物医学、材料科学、半导体、汽车等）提取可迁移的技术，对每个技术进行详细分析：
        - 技术名称和来源行业
        - 技术原理和核心优势[引用编号]
        - 在原行业的应用情况和效果
        - 关键技术参数和数据
        ## 三、锂电应用可行性分析
        对每个跨界技术在锂电领域的应用进行深入分析：
        - 应用场景和解决的具体问题
        - 技术迁移的可行性评估
        - 可能的技术难点和挑战
        - 预期效果和性能提升
        - 成本分析
        ## 四、具体落地路径
        提出具体的技术落地路径和实验方案建议：
        - 第一阶段：技术验证（需要做哪些实验）
        - 第二阶段：原型开发（关键参数优化）
        - 第三阶段：中试与量产（工艺和设备要求）
        ## 五、AI独立思考与判断
        1. 最有前景的3-5个跨界技术
        2. 技术迁移的风险评估
        3. 与现有技术路线的兼容性分析
        4. 潜在的知识产权问题
        5. 对我们研发工作的具体建议
        重要要求：
        1. 在文中引用信息时，必须使用[数字]标记对应的参考文献
        2. 所有链接必须保留，确保可以直接点击跳转
        3. 分析要具体、可落地，避免空泛的建议
        4. 重点关注技术原理的相通性和可迁移性
        """
    
    def get_first_principle_analysis_prompt(self, task, all_results):
        return f"""
        作为锂电电化学领域的资深专家，请基于以下搜索到的信息，从第一性原理出发，为用户详细解释相关的物理化学原理。
        用户任务：{task}
        搜索结果：
        {json.dumps(all_results, ensure_ascii=False, indent=2)}
        请严格按照以下格式输出报告：
        # 第一性原理解释报告：{task}
        ## 一、基本概念与定义
        清晰定义相关的基本概念和术语。
        ## 二、从物理化学基本定律出发的原理解释
        这是核心部分！请从热力学、动力学、量子化学等基本定律出发，深入解释相关原理：
        - 涉及的基本物理化学定律
        - 详细的推导过程（如有必要）
        - 关键公式和物理意义
        - 微观层面的机理分析（原子/分子级别）
        - 宏观现象与微观机理的联系
        ## 三、影响因素与规律
        分析影响该过程/现象的关键因素：
        - 温度、压力、浓度等热力学因素
        - 反应速率、扩散系数等动力学因素
        - 材料结构、表面性质等材料因素
        - 各因素之间的相互作用和定量关系
        ## 四、实验验证与数据支持
        引用权威的实验数据和研究结果来验证上述原理：
        - 经典实验方法和结果[引用编号]
        - 最新的研究进展和发现[引用编号]
        - 不同研究结果的比较和分析
        ## 五、在锂电池中的应用与实例
        结合具体的锂电池应用场景，说明该原理的实际应用：
        - 在电池设计中的应用
        - 在材料选择中的指导意义
        - 在工艺优化中的作用
        - 常见问题的原理解释
        ## 六、常见误区与澄清
        澄清该领域常见的误解和错误认识。
        ## 七、推荐学习资源
        推荐权威的教材、综述论文和学习资料。
        重要要求：
        1. 原理部分要准确、深入、系统，不能停留在表面
        2. 公式要正确，物理意义要解释清楚
        3. 微观机理要清晰，用通俗易懂的语言解释复杂的概念
        4. 引用权威来源，确保信息的可靠性
        5. 在文中引用信息时，必须使用[数字]标记对应的参考文献
        """
    
    def get_brainstorm_analysis_prompt(self, task, all_results):
        return f"""
        作为锂电研发领域的资深专家，请基于以下搜索到的信息，与用户进行一场专业的头脑风暴，提出有科学依据的创新点子。
        用户任务：{task}
        搜索结果：
        {json.dumps(all_results, ensure_ascii=False, indent=2)}
        请严格按照以下格式输出报告：
        # 创新头脑风暴报告：{task}
        ## 一、现有技术现状与瓶颈分析
        深入分析当前技术现状和存在的核心瓶颈：
        - 主流技术路线及其优缺点
        - 关键性能指标和限制因素
        - 尚未解决的科学问题和技术难题
        ## 二、创新方向与点子
        从多个维度提出创新点子，每个点子包含：
        - 创新点名称
        - 核心思路和科学依据
        - 预期解决的问题和性能提升
        - 技术可行性评估
        - 可能的风险和挑战
        请从以下维度提出创新点子：
        1. 材料创新（新的电极材料、电解质、隔膜等）
        2. 结构创新（新的电池结构、电极设计等）
        3. 工艺创新（新的制备工艺、改性方法等）
        4. 系统创新（新的电池管理系统、热管理系统等）
        5. 跨界创新（从其他行业借鉴的技术）
        ## 三、创新点子优先级评估
        对提出的创新点子进行优先级评估，使用以下标准：
        - 技术可行性（1-5分）
        - 预期效果（1-5分）
        - 研发周期（1-5分）
        - 成本效益（1-5分）
        - 综合评分和排名
        ## 四、高潜力创新点深入分析
        对排名前3的高潜力创新点进行深入分析：
        - 详细的技术方案
        - 关键技术难点和突破路径
        - 需要开展的实验工作
        - 预期的技术路线图
        ## 五、AI独立思考与建议
        1. 最有可能实现突破的创新方向
        2. 需要重点关注的研究领域
        3. 与行业发展趋势的契合度分析
        4. 对我们研发工作的具体建议
        重要要求：
        1. 创新点子要有科学依据，不能是空想
        2. 要敢于提出颠覆性的想法，但也要客观评估可行性
        3. 分析要具体，有可操作性
        4. 在文中引用信息时，必须使用[数字]标记对应的参考文献
        """
    
    def generate_references(self):
        if not self.references:
            return ""
        references_text = "\n---\n## 📚 参考文献\n\n"
        types = {
            "scholar": "📄 学术论文（Wiley优先）",
            "patent": "🔬 国际专利文献",
            "news": "📰 最新新闻与企业动态",
            "web": "🌐 网页资源"
        }
        for ref_type, type_name in types.items():
            type_refs = [ref for ref in self.references if ref["type"] == ref_type]
            if type_refs:
                references_text += f"### {type_name}\n\n"
                if ref_type == "scholar":
                    wiley_refs = [ref for ref in type_refs if ref.get("publisher") == "Wiley"]
                    other_refs = [ref for ref in type_refs if ref.get("publisher") != "Wiley"]
                    type_refs = wiley_refs + other_refs
                if ref_type == "news":
                    company_refs = {}
                    other_refs = []
                    for ref in type_refs:
                        if ref.get("company"):
                            if ref["company"] not in company_refs:
                                company_refs[ref["company"]] = []
                            company_refs[ref["company"]].append(ref)
                        else:
                            other_refs.append(ref)
                    for company, refs in company_refs.items():
                        references_text += f"**🏭 {company}**\n\n"
                        for ref in refs:
                            references_text += self.format_reference(ref) + "\n\n"
                    if other_refs:
                        references_text += "**🌍 行业动态**\n\n"
                        for ref in other_refs:
                            references_text += self.format_reference(ref) + "\n\n"
                else:
                    for ref in type_refs:
                        references_text += self.format_reference(ref) + "\n\n"
        return references_text
    
    def format_reference(self, ref):
        ref_line = f"[{ref['id']}] "
        if ref["type"] == "scholar" and ref.get("authors"):
            ref_line += f"{ref['authors']}. "
        ref_line += f"**[{ref['title']}]({ref['link']})**"
        if ref["type"] == "scholar":
            if ref.get("publication"):
                publisher = ref.get("publisher", "")
                if publisher == "Wiley":
                    ref_line += f". 📚 **{ref['publication']}** (Wiley)"
                elif publisher in ["Nature/Springer", "Elsevier"]:
                    ref_line += f". *{ref['publication']}* ({publisher})"
                else:
                    ref_line += f". *{ref['publication']}*"
            if ref.get("date"):
                ref_line += f", {ref['date']}"
            if ref.get("citations"):
                ref_line += f" (被引{ref['citations']}次)"
        elif ref["type"] == "patent":
            if ref.get("assignee"):
                ref_line += f". {ref['assignee']}"
            if ref.get("date"):
                ref_line += f", {ref['date']}"
        else:
            if ref.get("source"):
                ref_line += f". {ref['source']}"
            if ref.get("date"):
                ref_line += f", {ref['date']}"
        return ref_line

# ====================== 页面配置 ======================
st.set_page_config(
    page_title="OpenClaw风格锂电研发Agent",
    page_icon="🔋",
    layout="wide",
    initial_sidebar_state="expanded"
)

# 自定义样式
st.markdown("""
<style>
    .main-header {
        font-size: 2.2rem;
        font-weight: 700;
        color: #165DFF;
        margin-bottom: 0.5rem;
    }
    .thinking-box {
        background-color: #f0f7ff;
        border-radius: 12px;
        padding: 1.5rem;
        margin-bottom: 1.5rem;
        border-left: 4px solid #165DFF;
    }
    .ai-judgment {
        background-color: #fff8e6;
        border-radius: 8px;
        padding: 1rem;
        margin: 1rem 0;
        border-left: 4px solid #ff9500;
    }
    .reference-box {
        background-color: #f8f9fa;
        border-radius: 8px;
        padding: 1rem;
        margin: 1rem 0;
        border-left: 4px solid #6c757d;
    }
    .wiley-paper {
        background-color: #e6f7ff;
        border-radius: 4px;
        padding: 2px 6px;
        font-weight: bold;
    }
    .company-news {
        background-color: #f6ffed;
        border-radius: 4px;
        padding: 2px 6px;
        font-weight: bold;
    }
    .stTabs [data-baseweb="tab-list"] {
        gap: 2px;
    }
    .stTabs [data-baseweb="tab"] {
        height: 50px;
        white-space: pre-wrap;
        background-color: #f0f2f6;
        border-radius: 4px 4px 0px 0px;
        gap: 1px;
        padding-top: 10px;
        padding-bottom: 10px;
    }
    .stTabs [aria-selected="true"] {
        background-color: #ffffff;
    }
</style>
""", unsafe_allow_html=True)

# ====================== 侧边栏 ======================
with st.sidebar:
    st.markdown('<div class="main-header">🔋 锂电研发智能Agent</div>', unsafe_allow_html=True)
    st.markdown("**实时思考 · 真实来源 · AI判断**")
    st.divider()
    
    page = st.radio(
        "核心功能",
        ["🚀 追新补盲", "🌉 跨界提取营养", "⚛️ 第一性原理解释", "💡 头脑风暴参与者"],
        index=0
    )
    st.divider()
    
    with st.expander("⚙️ 高级设置", expanded=True):
        st.subheader("学术搜索设置（Wiley优先模式）")
        num_scholar_results = st.slider("论文搜索结果数", 5, 30, 20)
        scholar_start_year = st.slider("论文起始年份", 2020, 2026, 2024)
        scholar_end_year = st.slider("论文结束年份", 2024, 2026, 2026)
        publisher_priority = st.selectbox(
            "出版社优先级",
            ["wiley", "all_top_journals", "none"],
            index=0,
            format_func=lambda x: {
                "wiley": "📚 优先Wiley出版社",
                "all_top_journals": "📄 所有顶级期刊",
                "none": "🌐 无限制"
            }[x],
            help="选择Wiley优先将首先搜索Wiley出版社的所有顶级期刊"
        )
        top_journals_only = st.checkbox("只搜索顶级期刊", value=True, help="限定在20+本顶级期刊")
        english_only = st.checkbox("只显示英文论文", value=True, help="过滤中文论文")
        
        st.subheader("新闻搜索设置（大厂动态增强）")
        num_news_results = st.slider("新闻搜索结果数", 10, 30, 20)
        news_time_range = st.selectbox(
            "新闻时间范围",
            ["qdr:w", "qdr:m", "qdr:m3", "qdr:y"],
            index=2,
            format_func=lambda x: {
                "qdr:w": "过去1周",
                "qdr:m": "过去1个月",
                "qdr:m3": "过去3个月",
                "qdr:y": "过去1年"
            }[x]
        )
        include_companies = st.checkbox("包含头部企业动态", value=True, help="专门搜索宁德时代、比亚迪、特斯拉等企业")
        
        st.subheader("其他设置")
        num_other_results = st.slider("其他搜索结果数", 5, 20, 10)
        patent_start_year = st.slider("专利起始年份", 2020, 2026, 2023)
        
        st.subheader("大模型设置")
        temperature = st.slider("模型温度", 0.0, 1.0, 0.3, 0.1)
        max_tokens = st.slider("最大输出长度", 4000, 20000, 12000, 1000)
    
    st.divider()
    st.subheader("📡 API状态")
    if LLM_API_KEY:
        st.success("✅ 通义千问API已配置")
    else:
        st.error("❌ 通义千问API未配置")
    if SERPER_API_KEY:
        st.success("✅ Serper搜索API已配置")
    else:
        st.error("❌ Serper搜索API未配置")
        st.info("免费获取：https://serper.dev")
    
    st.divider()
    st.markdown("**模型**: " + LLM_MODEL)
    st.markdown("**搜索**: Google Scholar(Wiley优先) + 全网 + 国际专利 + 大厂新闻")
    st.markdown("**更新**: 2026年5月版（超时修复+安全部署版）")

# ====================== 主内容区 ======================
agent_type = page.split(" ", 1)[1]
st.title(page)

function_descriptions = {
    "追新补盲": "**✅ Wiley论文+大厂动态双重增强**。优先搜索Wiley出版社所有顶级期刊的2026年最新论文，专门定向搜索宁德时代、比亚迪、特斯拉等10+头部企业的最新动态。提供结构化的技术情报报告，包含每篇论文的核心数据、化学原理、创新点和局限性分析。",
    "跨界提取营养": "从航空航天、生物医学、半导体等其他行业提取可迁移技术，深入分析在锂电领域的应用可行性，给出具体的落地路径和实验方案。",
    "第一性原理解释": "从热力学、动力学、量子化学等基本定律出发，结合经典教材和最新研究，深入解释电化学现象和机理，澄清常见误区。",
    "头脑风暴参与者": "基于全网最新信息，从材料、结构、工艺、系统、跨界等多个维度进行创新头脑风暴，提出有科学依据的创新点子并进行优先级评估。"
}
st.markdown(f'<div class="thinking-box">{function_descriptions[agent_type]}</div>', unsafe_allow_html=True)

user_input = st.text_area(
    "请输入您的问题或任务",
    height=120,
    placeholder=f"例如：{
    '2026年硫化物固态电解质最新研究进展与商业化前景' if agent_type == '追新补盲' else
    '如何从生物细胞膜结构设计新型SEI膜解决锂金属电池问题' if agent_type == '跨界提取营养' else
    '从热力学和动力学角度解释锂枝晶生长的根本原因及抑制方法' if agent_type == '第一性原理解释' else
    '下一代高能量密度锂电池负极材料的颠覆性创新方向'
    }"
)

col1, col2 = st.columns([3, 1])
with col1:
    run_button = st.button("🚀 启动Agent分析", type="primary", use_container_width=True)
with col2:
    export_button = st.button("📄 导出报告", use_container_width=True, disabled=True)

if run_button:
    if not user_input:
        st.warning("⚠️ 请先输入您的问题")
    elif not LLM_API_KEY or not SERPER_API_KEY:
        st.error("❌ 请先配置所有必要的API密钥")
    else:
        agent = LithiumBatteryAgent()
        scholar_params = {
            "num_results": num_scholar_results,
            "as_ylo": scholar_start_year,
            "as_yhi": scholar_end_year,
            "english_only": english_only,
            "publisher_priority": publisher_priority,
            "top_journals_only": top_journals_only
        }
        news_params = {
            "num_results": num_news_results,
            "time_range": news_time_range,
            "include_companies": include_companies
        }
        result = agent.run(user_input, agent_type, scholar_params, news_params)
        
        st.markdown("---")
        st.markdown("### 📄 最终分析报告")
        st.markdown(result, unsafe_allow_html=True)
        
        # 启用导出功能
        st.download_button(
            label="📄 导出Markdown报告",
            data=result,
            file_name=f"锂电研发报告_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md",
            mime="text/markdown",
            use_container_width=True
        )

# 示例问题
st.markdown("### 💡 示例问题")
example_questions = {
    "追新补盲": [
        "2026年Wiley出版社固态电池最新研究",
        "2026年硫化物固态电解质最新研究进展",
        "宁德时代和比亚迪2026年最新电池技术",
        "特斯拉4680电池最新量产进展与技术突破"
    ],
    "跨界提取营养": [
        "从生物细胞膜结构设计新型SEI膜",
        "借鉴航天相变热管理技术解决电池快充发热问题",
        "利用3D打印技术制造高性能复合电极",
        "从半导体晶圆工艺借鉴材料表面原子级改性技术"
    ],
    "第一性原理解释": [
        "为什么石墨的理论容量是372mAh/g？从晶体结构和嵌入机理解释",
        "电解液电化学窗口的物理本质及拓宽方法",
        "锂离子在硫化物固态电解质中的传输机理",
        "SEI膜形成的热力学和动力学过程及影响因素"
    ],
    "头脑风暴参与者": [
        "如何实现锂电池10分钟充满电且循环寿命超过10000次",
        "无负极锂电池的技术瓶颈与颠覆性解决方案",
        "废旧锂电池直接回收的创新方法与工艺",
        "未来10年可能颠覆锂电池行业的黑科技"
    ]
}
cols = st.columns(2)
for i, q in enumerate(example_questions[agent_type][:4]):
    with cols[i % 2]:
        if st.button(q, use_container_width=True, key=f"example_{i}"):
            st.session_state.user_input = q
            st.rerun()

# 底部信息
st.markdown("---")
st.markdown("""
<div style="text-align: center; color: #6c757d; font-size: 0.8rem;">
    🔋 锂电研发智能Agent | OpenClaw风格 | 基于通义千问和Serper API构建<br>
    ✅ 已修复Wiley出版社论文搜索bug | ✅ 已修复大模型超时问题 | ✅ 安全隐藏API密钥<br>
    📚 支持Wiley、Nature、Science、Advanced Materials等20+本顶级期刊<br>
    🏭 覆盖宁德时代、比亚迪、特斯拉等10+头部锂电企业<br>
    本工具仅供研发参考，重要决策请结合专业知识和实际情况综合判断
</div>
""", unsafe_allow_html=True)
