"""
Model Provider Abstraction for SpatialAnalysisAgent
Supports multiple AI model providers including OpenAI, local models, and open-source alternatives
"""

import os
import sys
import requests
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
import configparser


# ---------------------------------------------------------------------------
# 用户可编辑的模型配置 (models.toml)
# ---------------------------------------------------------------------------
# 这些表是为兼容旧代码而保留的"运行时缓存"。真实数据全部来自同目录下的
# models.toml,用户改完配置文件重启 QGIS 即生效;若配置文件不存在或解析
# 失败,会自动回退到下面这一份硬编码默认值,确保插件仍能启动。
# ---------------------------------------------------------------------------

_DEFAULT_PROVIDER_MODELS = {
    "openai":     ["gpt-4o", "gpt-4o-mini", "gpt-4", "gpt-5", "gpt-5.1", "o1", "o3-mini"],
    "deepseek":   ["deepseek-chat", "deepseek-reasoner"],
    "anthropic":  ["claude-sonnet-4-20250514", "claude-haiku-4-5-20251001"],
    "gemini":     ["gemini-2.5-pro", "gemini-2.5-flash"],
    "openrouter": ["Claude-Opus-4.6", "GPT-5.5"],
    "minimax":    ["MiniMax-Text-01", "abab6.5s-chat"],
    "ollama":     [],
}
_DEFAULT_DISPLAY_NAMES = {
    "openai": "OpenAI", "deepseek": "DeepSeek", "anthropic": "Anthropic",
    "gemini": "Google Gemini", "openrouter": "OpenRouter", "minimax": "MiniMax",
    "ollama": "本地 Ollama",
}
_DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "openrouter": "https://openrouter.ai/api/v1",
    "minimax": "https://api.minimax.chat/v1",
    "ollama": "http://localhost:11434",
}
_DEFAULT_KEY_PREFIXES = {
    "openai": ["sk-"], "anthropic": ["sk-ant-"], "openrouter": ["sk-or-"],
    "gemini": ["AIza"], "minimax": ["eyJ"],
    "deepseek": [], "ollama": [],
}
_DEFAULT_OVERRIDES = {
    "gpt-5": "gpt5", "gpt-5.1": "gpt5", "gpt-5.2": "gpt5",
}


def _load_models_config():
    """加载同目录下 models.toml。返回 (providers_dict, overrides_dict)。
    失败时回退到内置默认值,保证插件可启动。
    """
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'models.toml')

    # 选一个可用的 TOML 解析器:tomllib(3.11+) > tomli > toml
    parser = None
    try:
        import tomllib as _tomllib
        def parser(p):
            with open(p, 'rb') as f:
                return _tomllib.load(f)
    except ImportError:
        try:
            import tomli as _tomli
            def parser(p):
                with open(p, 'rb') as f:
                    return _tomli.load(f)
        except ImportError:
            try:
                import toml as _toml
                def parser(p):
                    return _toml.load(p)
            except ImportError:
                parser = None

    providers = {
        name: {
            "display_name": _DEFAULT_DISPLAY_NAMES.get(name, name),
            "base_url":     _DEFAULT_BASE_URLS.get(name, ""),
            "key_prefix":   _DEFAULT_KEY_PREFIXES.get(name, []),
            "models":       list(models),
        }
        for name, models in _DEFAULT_PROVIDER_MODELS.items()
    }
    overrides = dict(_DEFAULT_OVERRIDES)

    if parser is None or not os.path.isfile(cfg_path):
        if parser is None:
            print("[models.toml] 未找到 TOML 解析器,使用内置默认模型列表")
        return providers, overrides

    try:
        data = parser(cfg_path)
    except Exception as e:
        print(f"[models.toml] 解析失败,使用内置默认值: {e}")
        return providers, overrides

    file_providers = (data or {}).get("providers", {}) or {}
    for name, spec in file_providers.items():
        if not isinstance(spec, dict):
            continue
        providers[name] = {
            "display_name": spec.get("display_name", _DEFAULT_DISPLAY_NAMES.get(name, name)),
            "base_url":     spec.get("base_url", _DEFAULT_BASE_URLS.get(name, "")),
            "key_prefix":   list(spec.get("key_prefix", _DEFAULT_KEY_PREFIXES.get(name, []))),
            "models":       list(spec.get("models", _DEFAULT_PROVIDER_MODELS.get(name, []))),
        }

    file_overrides = (data or {}).get("overrides", {}) or {}
    if isinstance(file_overrides, dict):
        overrides.update({str(k): str(v) for k, v in file_overrides.items()})

    return providers, overrides


_PROVIDERS, _MODEL_OVERRIDES = _load_models_config()

# 老代码到处直接读 PROVIDER_MODELS[<name>],为兼容保留一个字典,内容从 toml 派生。
PROVIDER_MODELS = {name: list(spec["models"]) for name, spec in _PROVIDERS.items()}
# 让一些旧代码里曾出现过的、但 toml 里没显式声明的 provider 名(比如 gibd)有兜底。
PROVIDER_MODELS.setdefault("gibd", PROVIDER_MODELS.get("openai", []))


# Add current directory to path
current_script_dir = os.path.dirname(os.path.abspath(__file__))
if current_script_dir not in sys.path:
    sys.path.append(current_script_dir)



class ModelProvider(ABC):
    """Abstract base class for AI model providers"""
    
    @abstractmethod
    def create_client(self, config: Dict[str, Any]):
        """Create and return a client for the model provider"""
        pass
    
    @abstractmethod
    def generate_completion(self, request_id, client, model: str, messages: List[Dict], **kwargs):
        """Generate completion using the provider's API"""
        pass
    
    @abstractmethod
    def validate_config(self, config: Dict[str, Any]) -> bool:
        """Validate configuration for this provider"""
        pass


class OpenAIProvider(ModelProvider):
    """OpenAI Provider 保留原有逻辑"""

    def create_client(self, config: Dict[str, Any]):
        from openai import OpenAI
        api_key = config.get('api_key') or ''
        if 'gibd-services' in api_key:
            base_url = f"https://www.gibd.online/api/openai/{api_key}"
            client = None
        else:
           # 仅改造 default base_url 指向真实 OpenAI（阶段四改造要求）
            base_url = config.get('base_url', 'https://api.openai.com/v1')
            client = OpenAI(api_key=api_key, base_url=base_url)
        self.api_key = api_key
        self.base_url = base_url
        return client
    
    def generate_completion(self, request_id, client, model: str, messages: List[Dict], **kwargs):
        import requests, json

        stream = kwargs.get("stream", False)

        # --- If using proxy key -------
        if 'gibd-services' in (self.api_key or ''):
            url = self.base_url
            payload = {"service_name":"GIS Copilot",
                       "question_id": request_id,
                       "model": model,
                       "messages": messages,
                       "stream":stream}
            response = requests.post(url, json=payload, stream=stream)

            # if not payload["stream"]:
            if not stream:
                # ---- Non-streaming ----
                try:
                    data = response.json()

                    # Check for error in response
                    if "error" in data:
                        error_msg = data.get("error", "Unknown error")
                        print(f"Error: {error_msg}")
                        raise Exception(f"API Error: {error_msg}")

                    class DummyChoice:
                        pass
                    class DummyMessage:
                        pass

                    message = DummyMessage()
                    message.content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    choice = DummyChoice()
                    choice.message = message
                    response.choices = [choice]
                except json.JSONDecodeError as e:
                    print(f"Error: Failed to parse response - {str(e)}")
                    raise Exception(f"Failed to parse API response: {str(e)}")
                except Exception as e:
                    # Re-raise if it's already our custom error
                    if "API Error:" in str(e):
                        raise
                    print(f"Error: {str(e)}")
                    response.choices = []

                return response

            else:
                # ---- Streaming mode ----
                def stream_generator():
                    if response.status_code != 200:
                        # Try to parse error message from response
                        try:
                            error_data = response.json()
                            if "error" in error_data:
                                error_msg = error_data.get("error", "Unknown error")
                                print(f"Error: {error_msg}")
                                yield f"[ERROR] {error_msg}"
                                return
                        except:
                            pass
                        yield f"[ERROR] {response.text}"
                        return

                    for line in response.iter_lines(decode_unicode=True):
                        if not line:
                            continue
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if data_str == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data_str)
                                # Check for error in streaming chunk
                                if 'error' in chunk:
                                    error_msg = chunk.get("error", "Unknown error")
                                    print(f"Error: {error_msg}")
                                    yield f"[ERROR] {error_msg}"
                                    return
                                if 'choices' in chunk and len(chunk['choices']) > 0:
                                    delta = chunk['choices'][0].get('delta', {})
                                    content = delta.get('content', '')
                                    if content:
                                        yield content  # token-by-token streaming
                            except json.JSONDecodeError:
                                continue

                # return a generator so user can iterate over streamed tokens
                return stream_generator()

        else:
            return client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=kwargs.get('temperature', 0),
                stream=kwargs.get('stream', stream)
            )
    
    def validate_config(self, config: Dict[str, Any]) -> bool:
        return 'api_key' in config and config['api_key'].strip() != ''


class OllamaProvider(ModelProvider):
    """Ollama local provider for gpt-oss-20b and other models"""
    
    def create_client(self, config: Dict[str, Any]):
        # Use OpenAI SDK with Ollama's compatible endpoint (exact match to LLM_SERVER_TESTING_v1.py)
        from openai import OpenAI
        base_url = config.get('base_url', 'http://128.118.54.16:11434/v1')
        api_key = config.get('api_key', 'no-api')
        
        # Debug logging
        # print(f"[DEBUG] OllamaProvider creating client with:")
        # print(f"[DEBUG] - base_url: {base_url}")
        # print(f"[DEBUG] - api_key: {api_key}")
        # print(f"[DEBUG] - config received: {config}")
        
        return OpenAI(
            base_url=base_url,
            api_key=api_key
        )
    
    def generate_completion(self, request_id, client, model: str, messages: List[Dict], **kwargs):
        return client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=kwargs.get('temperature', 0),
            stream=kwargs.get('stream', False)
        )
    
    def validate_config(self, config: Dict[str, Any]) -> bool:
        # For Ollama, we just need the base URL to be reachable
        return True  # Simplified validation

# 新增
class DeepSeekProvider(OpenAIProvider):
    """DeepSeek Provider 兼容 OpenAI协议"""
    def create_client(self, config):
        from openai import OpenAI
        api_key = config.get('api_key', '')
        self.api_key = api_key
        self.base_url = "https://api.deepseek.com"
        return OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    
class OpenRouterProvider(OpenAIProvider):
    """OpenRouter Provider 兼容 OpenAI协议"""
    def create_client(self, config):
        from openai import OpenAI
        api_key = config.get('api_key', '')
        self.api_key = api_key
        self.base_url = "https://openrouter.ai/api/v1"
        return OpenAI(api_key=api_key, base_url=self.base_url)
        
class AnthropicProvider(ModelProvider):
    """Anthropic Claude Provider.

    Anthropic 没有 OpenAI 兼容端点,这里用 requests 直接打 Messages API,
    再把响应包装成 OpenAI SDK 风格的对象,让下游 helper.unified_llm_call
    不用区分。流式用 SSE 自己解,非流式 JSON 解析后填 choices[0].message.content。
    """

    ANTHROPIC_VERSION = "2023-06-01"

    def __init__(self):
        self.api_key = None
        self.base_url = "https://api.anthropic.com/v1/messages"

    def create_client(self, config):
        self.api_key = (config.get('api_key') or '').strip()
        self.base_url = config.get('base_url') or "https://api.anthropic.com/v1/messages"
        if not self.base_url.endswith('/messages'):
            self.base_url = self.base_url.rstrip('/') + '/v1/messages'
        return None  # Anthropic 没用 SDK,client 字段保持 None

    def _split_system_messages(self, messages):
        """Anthropic 要求 system 单独放,不能混在 messages 数组里。"""
        system_parts = []
        chat_messages = []
        for msg in messages:
            role = msg.get('role', 'user')
            content = msg.get('content', '')
            if role == 'system':
                system_parts.append(content)
            elif role in ('user', 'assistant'):
                chat_messages.append({'role': role, 'content': content})
            else:
                chat_messages.append({'role': 'user', 'content': content})
        return "\n\n".join(system_parts), chat_messages

    def generate_completion(self, request_id, client, model, messages, **kwargs):
        import requests, json
        stream = bool(kwargs.get("stream", False))
        system_text, chat_msgs = self._split_system_messages(messages)

        payload = {
            "model": model,
            "messages": chat_msgs,
            "max_tokens": int(kwargs.get('max_tokens', 8192)),
            "stream": stream,
        }
        if system_text:
            payload["system"] = system_text
        temperature = kwargs.get('temperature')
        if temperature is not None and temperature != 0:
            payload["temperature"] = float(temperature)

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

        if not stream:
            resp = requests.post(self.base_url, headers=headers, json=payload, timeout=120)
            if resp.status_code != 200:
                raise Exception(f"Anthropic API error {resp.status_code}: {resp.text[:500]}")
            data = resp.json()
            content_blocks = data.get("content", [])
            text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")

            class _Msg: pass
            class _Choice: pass
            class _Usage: pass
            class _Resp: pass
            msg = _Msg(); msg.content = text
            choice = _Choice(); choice.message = msg
            usage = _Usage()
            u = data.get("usage", {})
            usage.prompt_tokens = u.get("input_tokens")
            usage.completion_tokens = u.get("output_tokens")
            out = _Resp()
            out.choices = [choice]
            out.usage = usage
            return out

        # 流式:Anthropic 用 SSE。yield OpenAI 风格的 chunk(带 .choices[0].delta.content)。
        def stream_generator():
            resp = requests.post(self.base_url, headers=headers, json=payload,
                                 stream=True, timeout=120)
            if resp.status_code != 200:
                err_text = resp.text[:500]
                class _Delta: pass
                class _Choice: pass
                class _Chunk: pass
                d = _Delta(); d.content = f"[Anthropic ERROR {resp.status_code}] {err_text}"
                c = _Choice(); c.delta = d
                ch = _Chunk(); ch.choices = [c]
                yield ch
                return

            for raw in resp.iter_lines(decode_unicode=True):
                if not raw or not raw.startswith("data: "):
                    continue
                data_str = raw[len("data: "):]
                if data_str == "[DONE]":
                    break
                try:
                    evt = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                etype = evt.get("type")
                if etype == "content_block_delta":
                    delta = evt.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            class _D: pass
                            class _C: pass
                            class _Ck: pass
                            d = _D(); d.content = text
                            c = _C(); c.delta = d
                            ck = _Ck(); ck.choices = [c]
                            yield ck
                elif etype == "message_stop":
                    break
                elif etype == "error":
                    err_text = json.dumps(evt.get("error", {}))[:500]
                    class _D: pass
                    class _C: pass
                    class _Ck: pass
                    d = _D(); d.content = f"[Anthropic stream ERROR] {err_text}"
                    c = _C(); c.delta = d
                    ck = _Ck(); ck.choices = [c]
                    yield ck
                    break

        return stream_generator()

    def validate_config(self, config):
        key = (config.get('api_key') or '').strip()
        return key.startswith('sk-ant-')


class GeminiProvider(OpenAIProvider):
    """Google Gemini Provider —— 走官方 OpenAI 兼容端点。

    参考:https://ai.google.dev/gemini-api/docs/openai
    """
    def create_client(self, config):
        from openai import OpenAI
        self.api_key = (config.get('api_key') or '').strip()
        self.base_url = (config.get('base_url')
                         or "https://generativelanguage.googleapis.com/v1beta/openai/")
        return OpenAI(api_key=self.api_key, base_url=self.base_url)

    def validate_config(self, config):
        key = (config.get('api_key') or '').strip()
        return key.startswith('AIza')


class MiniMaxProvider(OpenAIProvider):
    """MiniMax Provider —— OpenAI 兼容端点。"""
    def create_client(self, config):
        from openai import OpenAI
        self.api_key = (config.get('api_key') or '').strip()
        self.base_url = config.get('base_url') or "https://api.minimax.chat/v1"
        return OpenAI(api_key=self.api_key, base_url=self.base_url)

    def validate_config(self, config):
        key = (config.get('api_key') or '').strip()
        # MiniMax 的 JWT key 以 "eyJ" 开头
        return key.startswith('eyJ')


class GPT5Provider(ModelProvider):
    """Specialized provider for GPT-5 and GPT-5.1 with different API structure"""

    def __init__(self):
        self.api_key = None
        self.base_url = None

    def create_client(self, config: Dict[str, Any]):
        from openai import OpenAI
        self.api_key = (config.get('api_key') or '').strip()
        self.base_url = None
        if 'gibd-services' in self.api_key:
            self.base_url = f"https://www.gibd.online/api/openai/{self.api_key}"
            client = None
        else:
            client = OpenAI(api_key=self.api_key)
        return client

    def generate_completion(self, request_id, client, model: str, messages: List[Dict], **kwargs):
        import requests, json

        stream = kwargs.get("stream", False)

        # --- If using proxy key (GIBD service) -------
        if 'gibd-services' in (self.api_key or ''):
            url = self.base_url

            # Get reasoning effort from kwargs
            effort_level = kwargs.get('reasoning_effort', 'medium')

            # Map reasoning effort based on model
            if model == 'gpt-5.1':
                # GPT-5.1 only supports: none, low, high
                effort_mapping = {
                    'none': 'none',
                    'low': 'low',
                    'minimal': 'low',
                    'medium': 'low',  # Map medium to low
                    'high': 'high'
                }
                effort_level = effort_mapping.get(effort_level, 'low')

            payload = {
                "service_name": "GIS Copilot",
                "question_id": request_id,
                "model": model,
                "messages": messages,
                "stream": stream,
                "reasoning_effort": effort_level,  # Explicitly pass reasoning effort
                **{k: v for k, v in kwargs.items() if k not in ['reasoning_effort', 'stream']}
            }

            response = requests.post(url, json=payload, stream=stream)

            if not stream:
                # ---- Non-streaming ----
                try:
                    data = response.json()

                    # Check for error in response
                    if "error" in data:
                        error_msg = data.get("error", "Unknown error")
                        print(f"Error: {error_msg}")
                        raise Exception(f"API Error: {error_msg}")

                    class DummyChoice:
                        pass
                    class DummyMessage:
                        pass

                    message = DummyMessage()
                    message.content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    choice = DummyChoice()
                    choice.message = message
                    response.choices = [choice]
                except json.JSONDecodeError as e:
                    print(f"Error: Failed to parse response - {str(e)}")
                    raise Exception(f"Failed to parse API response: {str(e)}")
                except Exception as e:
                    # Re-raise if it's already our custom error
                    if "API Error:" in str(e):
                        raise
                    print(f"Error: {str(e)}")
                    response.choices = []

                return response
            else:
                # ---- Streaming mode ----
                def stream_generator():
                    if response.status_code != 200:
                        # Try to parse error message from response
                        try:
                            error_data = response.json()
                            if "error" in error_data:
                                error_msg = error_data.get("error", "Unknown error")
                                print(f"Error: {error_msg}")
                                yield f"[ERROR] {error_msg}"
                                return
                        except:
                            pass
                        yield f"[ERROR] {response.text}"
                        return

                    for line in response.iter_lines(decode_unicode=True):
                        if not line:
                            continue
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if data_str == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data_str)
                                # Check for error in streaming chunk
                                if 'error' in chunk:
                                    error_msg = chunk.get("error", "Unknown error")
                                    print(f"Error: {error_msg}")
                                    yield f"[ERROR] {error_msg}"
                                    return
                                if 'choices' in chunk and len(chunk['choices']) > 0:
                                    delta = chunk['choices'][0].get('delta', {})
                                    content = delta.get('content', '')
                                    if content:
                                        yield content  # token-by-token streaming
                            except json.JSONDecodeError:
                                continue

                # return a generator so user can iterate over streamed tokens
                return stream_generator()

        # --- If using direct OpenAI key -------
        else:
            try:
                # First try the specialized GPT-5/GPT-5.1 API format if available
                # Convert messages to GPT-5/5.1 input format
                input_data = []
                for msg in messages:
                    role = 'developer' if msg['role'] == 'system' else msg['role']
                    input_data.append({'role': role, 'content': msg['content']})

                # Get reasoning effort from kwargs
                effort_level = kwargs.get('reasoning_effort', 'medium')

                # Map reasoning effort based on model
                if model == 'gpt-5.1':
                    # GPT-5.1 only supports: none, low, high
                    effort_mapping = {
                        'none': 'none',
                        'low': 'low',
                        'minimal': 'low',
                        'medium': 'low',  # Map medium to low
                        'high': 'high'
                    }
                    effort_level = effort_mapping.get(effort_level, 'low')
                else:
                    # GPT-5 supports: minimal, low, medium, high
                    # Keep original effort level for GPT-5
                    pass

                reasoning = {"effort": effort_level}

                return client.responses.create(
                    model=model,
                    input=input_data,
                    reasoning=reasoning,
                    **{k: v for k, v in kwargs.items() if k not in ['reasoning_effort']}
                )
            except AttributeError:
                # Fallback to standard OpenAI chat completions API if responses.create doesn't exist
                print(f"{model} specialized API not available, falling back to standard chat completions...")

                # Add reasoning effort to the system message if provided
                effort_level = kwargs.get('reasoning_effort', 'medium')
                default_effort = 'low' if model == 'gpt-5.1' else 'medium'

                if effort_level and effort_level != default_effort:
                    # Enhance system message with reasoning instructions
                    for msg in messages:
                        if msg['role'] == 'system':
                            msg['content'] += f" Please use {effort_level} reasoning effort for this task."
                            break

                return client.chat.completions.create(
                    model=model,
                    messages=messages,
                    **{k: v for k, v in kwargs.items() if k not in ['reasoning_effort']}
                )
            except Exception as e:
                print(f"{model} API error: {str(e)}")
                # Final fallback to standard format
                return client.chat.completions.create(
                    model=model,
                    messages=messages,
                    **{k: v for k, v in kwargs.items() if k not in ['reasoning_effort']}
                )

    def validate_config(self, config: Dict[str, Any]) -> bool:
        return 'api_key' in config and config['api_key'].strip() != ''

class ModelProviderFactory:
    _providers = {
        'openai': OpenAIProvider(),
        'deepseek': DeepSeekProvider(),
        'openrouter': OpenRouterProvider(),
        'anthropic': AnthropicProvider(),
        'gemini': GeminiProvider(),
        'minimax': MiniMaxProvider(),
        'ollama': OllamaProvider(),
        'gpt5': GPT5Provider(),
        'gibd': OpenAIProvider(),
        'glm': OllamaProvider(),
        'qwen': OllamaProvider(),
    }
    _active_provider = None

    # 从 models.toml 派生:每个 provider 的 models 列表 → "model → provider" 反向映射,
    # 再叠加 [overrides] 节。用户改完 toml 重启 QGIS 即生效。
    @staticmethod
    def _build_model_providers_map():
        m = {}
        for pname, pspec in _PROVIDERS.items():
            for model_name in pspec.get("models", []):
                m.setdefault(model_name, pname)
        m.update(_MODEL_OVERRIDES)
        return m

    _model_providers = None  # 实际值由下方 ModelProviderFactory._model_providers = ... 填充
    
    @classmethod
    def set_active_provider(cls, provider_name: str):
        cls._active_provider = provider_name

    @classmethod
    def _is_ollama_model(cls, model: str) -> bool:
        # 直接从 models.toml 的 [providers.ollama].models 派生
        ollama_models = {m.lower() for m in PROVIDER_MODELS.get('ollama', [])}
        return model.lower() in ollama_models

    @classmethod
    def get_provider(cls, model: str) -> ModelProvider:
        if cls._is_ollama_model(model):
            return cls._providers['ollama']
        if model in ['gpt-5', 'gpt-5.1', 'gpt-5.2']:
            return cls._providers['gpt5']
        # 先查 _model_providers 映射表
        if model in cls._model_providers:
            provider_name = cls._model_providers[model]
            if provider_name in cls._providers:
                return cls._providers[provider_name]
        # 再查动态设置的 active_provider（detect_provider 检测结果）
        if cls._active_provider and cls._active_provider in cls._providers:
            return cls._providers[cls._active_provider]
        return cls._providers['openai']


# 类定义完成后,根据 toml 配置填充 model→provider 反向映射
ModelProviderFactory._model_providers = ModelProviderFactory._build_model_providers_map()


# API Key 智能识别 —— 完全基于 models.toml 配置驱动
def detect_provider(api_key: str) -> dict:
    """根据 API key 前缀识别 provider。
    - 单一前缀(如 sk-ant-、sk-or-、AIza)直接匹配。
    - 多个前缀冲突(典型如 sk- 同时被 OpenAI / DeepSeek 使用)走联网探测,
      探测顺序按 _PROVIDERS 迭代顺序。
    - 未识别返回 unknown,UI 上会提示用户手动选择。
    """
    result = {"provider": "unknown", "base_url": None, "models": [], "display_name": "未知"}
    if not api_key:
        return result

    def _pack(name):
        spec = _PROVIDERS.get(name, {})
        return {
            "provider": name,
            "base_url": spec.get("base_url", ""),
            "models":   list(spec.get("models", [])),
            "display_name": spec.get("display_name", name),
        }

    # GIBD 代理 key 走特殊 base_url 模式 —— 仍由代码逻辑承担(URL 含 api_key 拼接),
    # 但 models 列表交给 toml 决定;如果 toml 里没显式声明 gibd 段,就借用 openai 的列表。
    if api_key.startswith("gibd-services-"):
        gibd_models = PROVIDER_MODELS.get("gibd") or PROVIDER_MODELS.get("openai", [])
        result.update({
            "provider": "gibd",
            "base_url": f"https://www.gibd.online/api/openai/{api_key}",
            "models": list(gibd_models),
            "display_name": _PROVIDERS.get("gibd", {}).get("display_name", "GIBD"),
        })
        return result

    # 用 toml 里 key_prefix 字段做前缀匹配。匹配到的前缀越长(更具体)越优先。
    candidates = []
    for name, spec in _PROVIDERS.items():
        for prefix in spec.get("key_prefix", []):
            if prefix and api_key.startswith(prefix):
                candidates.append((len(prefix), name))
    candidates.sort(reverse=True)  # 长前缀优先

    if len(candidates) == 1:
        return {**result, **_pack(candidates[0][1])}

    if len(candidates) > 1:
        # 多个 provider 都用同一前缀(如 sk-)→ 联网探测
        # 按前缀长度优先级依次尝试 /models 接口
        for _, name in candidates:
            spec = _PROVIDERS.get(name, {})
            base = (spec.get("base_url") or "").rstrip("/")
            if not base:
                continue
            try:
                resp = requests.get(
                    f"{base}/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=5,
                )
                if resp.status_code == 200:
                    return {**result, **_pack(name)}
            except Exception:
                continue
        # 都失败 → 返回最具体的那一个作为兜底
        return {**result, **_pack(candidates[0][1])}

    return result

def load_model_config():
    """Load configuration for all model providers.

    根据 API Key 前缀快速识别实际 provider，将 key 分配给正确的
    provider config，避免把 DeepSeek key 发到 OpenAI 等错误路由。
    注意：不做网络请求，仅靠前缀判断。
    """
    config = configparser.ConfigParser()
    config_path = os.path.join(current_script_dir, 'config.ini')
    config.read(config_path)

    model_config = {}

    # 读取统一的 API Key
    api_key = ''
    if 'API_Key' in config and 'OpenAI_key' in config['API_Key']:
        api_key = config['API_Key']['OpenAI_key']

    if api_key:
        # 根据前缀快速判断 key 归属（不发网络请求）
        if api_key.startswith('sk-or-'):                               # <--- 新增这块
            model_config['openrouter'] = {'api_key': api_key}
        elif api_key.startswith('gibd-services-'):
            # GIBD 代理 key：可以转发多种模型
            for pname in ['openai', 'deepseek', 'gpt5', 'gibd',
                          'anthropic', 'gemini', 'minimax', 'glm', 'qwen']:
                model_config[pname] = {'api_key': api_key}
        elif api_key.startswith('sk-ant-'):
            model_config['anthropic'] = {'api_key': api_key}
        elif api_key.startswith('eyJ'):
            model_config['minimax'] = {'api_key': api_key}
        elif api_key.startswith('AIza'):
            model_config['gemini'] = {'api_key': api_key}
        else:
            # sk- 开头的 key 可能是 OpenAI 或 DeepSeek。
            # 优先使用 detect_provider 检测结果（active_provider）
            # 来精确路由，避免把 DeepSeek key 发到 OpenAI。
            active = ModelProviderFactory._active_provider
            if active and active != 'unknown':
                # 检测结果可用，只分配给检测到的 provider
                model_config[active] = {'api_key': api_key}
                # 也分配给兼容 provider（如 gpt5 需要同一个 key）
                if active in ('openai', 'gibd'):
                    for compat in ['openai', 'gpt5', 'gibd']:
                        model_config[compat] = {'api_key': api_key}
                elif active == 'deepseek':
                    model_config['deepseek'] = {'api_key': api_key}
            else:
                # 没有检测结果（可能首次启动、检测未完成、或检测失败）
                # 分配给所有 OpenAI 兼容 provider，
                # 由各 Provider.create_client 设置正确的 base_url。
                for pname in ['openai', 'deepseek', 'gpt5', 'gibd']:
                    model_config[pname] = {'api_key': api_key}

    # Ollama 本地模型，不需要 API Key
    model_config['ollama'] = {
        'base_url': 'http://128.118.54.16:11434/v1',
        'api_key': 'no-api'
    }

    return model_config


def create_unified_client(model: str):
    """Create a unified client that can handle multiple providers.

    provider 查找优先级：
      1. _model_providers 精确映射（deepseek-chat → deepseek）
      2. _active_provider（detect_provider 检测结果）
      3. 兜底 'openai'
    """
    provider = ModelProviderFactory.get_provider(model)
    config = load_model_config()

    # Get provider-specific config —— 优先用精确映射，再用 active_provider
    if model in ModelProviderFactory._model_providers:
        provider_name = ModelProviderFactory._model_providers[model]
    elif ModelProviderFactory._active_provider:
        provider_name = ModelProviderFactory._active_provider
    else:
        provider_name = 'openai'

    provider_config = config.get(provider_name, {})

    if not provider.validate_config(provider_config):
        raise ValueError(f"Invalid configuration for {provider_name} provider")

    return provider.create_client(provider_config), provider


def generate_unified_completion(request_id: str, model: str, messages: List[Dict], **kwargs):
    """Generate completion using the appropriate provider for the model"""
    client, provider = create_unified_client(model)
    return provider.generate_completion(request_id, client, model, messages, **kwargs)