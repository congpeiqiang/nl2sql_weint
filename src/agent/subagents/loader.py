"""
子智能体 YAML 配置加载器。

从 YAML 配置文件目录读取子智能体定义，转换为 DeepAgents
create_deep_agent() 所需的 SubAgent dict 格式。

参考: ERP_OPENCLAW/src/agent/subagents/loader.py
"""

from pathlib import Path
from typing import Dict, List

import yaml

CONFIGS_DIR = Path(__file__).parent / "configs"


def load_subagent_configs(configs_dir: Path = None) -> List[Dict]:
    """加载指定目录下所有 .yaml 子智能体配置文件。"""
    if configs_dir is None:
        configs_dir = CONFIGS_DIR

    configs = []
    if not configs_dir.exists():
        print(f"[WARNING] 子智能体配置目录不存在: {configs_dir}")
        return configs

    for yaml_file in sorted(configs_dir.glob("*.yaml")):
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception as e:
            print(f"[ERROR] 解析 {yaml_file.name}: {e}")
            continue

        # 支持 system_prompt_file：从文件读取
        if data.get("system_prompt_file") and not data.get("system_prompt"):
            prompt_path = Path(__file__).parents[1] / data["system_prompt_file"]
            if prompt_path.exists():
                data["system_prompt"] = prompt_path.read_text(encoding="utf-8")
            else:
                print(f"[ERROR] {yaml_file.name}: system_prompt_file 不存在: {prompt_path}")
                continue

        required = ["name", "description", "system_prompt", "tools"]
        missing = [k for k in required if not data.get(k)]
        if missing:
            print(f"[ERROR] {yaml_file.name} 缺少: {missing}")
            continue

        configs.append(data)
        print(f"[INFO] 加载子智能体: {data['name']}")

    return configs


def resolve_subagent_tools(
    configs: List[Dict],
    available_tools: List,
) -> List[Dict]:
    """将 YAML 中工具名称字符串解析为实际工具对象。"""
    tool_index = {}
    for t in available_tools:
        name = getattr(t, "name", None)
        if name:
            tool_index[name] = t

    subagents = []
    for config in configs:
        resolved = []
        for pattern in config.get("tools", []):
            matched = False
            for tool_name, tool_obj in tool_index.items():
                if pattern in tool_name:
                    resolved.append(tool_obj)
                    matched = True
            if not matched:
                print(f"[WARNING] {config['name']}: 工具 '{pattern}' 未匹配")

        seen = set()
        unique = []
        for t in resolved:
            n = getattr(t, "name", id(t))
            if n not in seen:
                seen.add(n)
                unique.append(t)

        entry = {
            "name": config["name"],
            "description": config["description"],
            "system_prompt": config["system_prompt"],
            "tools": unique,
        }
        if config.get("skills"):
            entry["skills"] = config["skills"]
        subagents.append(entry)
        print(f"[INFO] {config['name']}: {len(unique)} 个工具")

    return subagents
