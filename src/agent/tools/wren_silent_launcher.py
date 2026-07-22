"""
@File    :  wren_silent_launcher.py
@Author  :  CongPeiQiang
@Time    :  2026/7/22 15:54
@Desc    :  
"""
# !/usr/bin/env python
# wren_silent_launcher.py - 静默启动 Wren MCP 服务器
import subprocess
import sys
import os
import platform


def main():
    # Wren 可执行文件路径
    wren_exe = r"D:\code_work_space\llm\nl2sql\.venv\Scripts\wren.EXE"

    # 构建完整命令（保留所有参数）
    cmd = [wren_exe] + sys.argv[1:]

    # 创建子进程，重定向所有输出
    if platform.system() == "Windows":
        # Windows: 使用 CREATE_NO_WINDOW 避免弹出窗口
        creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0

        # 关键：使用 PIPE 保持 stdin 通信，重定向 stdout/stderr
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.PIPE,
            creationflags=creationflags,
            text=False  # 使用字节模式
        )
    else:
        # Linux/Mac
        with open(os.devnull, 'w') as devnull:
            process = subprocess.Popen(
                cmd,
                stdout=devnull,
                stderr=devnull,
                stdin=subprocess.PIPE
            )

    # 等待进程结束（Wren 会持续运行直到被终止）
    process.wait()


if __name__ == "__main__":
    main()