"""
@File    :  model.py
@Author  :  CongPeiQiang
@Time    :  2026/7/19 11:04
@Desc    :  
"""
import logging
from langchain_core.language_models import ModelProfile

from agent.settings.setting import settings

# 配置日志
logger = logging.getLogger(__name__)

# 创建文本处理模型
def create_deepseek_model():
    """创建文本处理模型"""
    from langchain_deepseek import ChatDeepSeek
    try:
        model = ChatDeepSeek(
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
            model=settings.LLM_MODEL,
            temperature=0.3,
            extra_body={"thinking": {"type": "disabled"}},
        )
        model.profile = ModelProfile(max_input_tokens=120000)
        return model
    except ImportError:
        logger.warning("langchain_deepseek not available")
        return None
    except Exception as e:
        logger.error(f"Failed to create text model: {e}")
        return None

def create_text_model():
    """创建文本处理模型"""
    from langchain_openai import ChatOpenAI
    try:
        model = ChatOpenAI(
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
            model=settings.LLM_MODEL,
            temperature=0.3,
            extra_body={"thinking": {"type": "disabled"}},

        )
        model.profile = ModelProfile(max_input_tokens=120000)
        return model
    except ImportError:
        logger.warning("langchain_deepseek not available")
        return None
    except Exception as e:
        logger.error(f"Failed to create text model: {e}")
        return None

deepseek_model = create_deepseek_model()