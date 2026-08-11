import os
# 清除环境变量，模拟无预置环境变量的情况
os.environ.pop('CHART_ENGINE', None)
from dotenv import load_dotenv
load_dotenv()
print('CHART_ENGINE after load_dotenv:', os.getenv('CHART_ENGINE'))
