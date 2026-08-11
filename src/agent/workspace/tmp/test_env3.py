import os
# 模拟预置环境变量 CHART_ENGINE=semiotic
os.environ['CHART_ENGINE'] = 'semiotic'
from dotenv import load_dotenv
load_dotenv(override=True)
print('CHART_ENGINE after override load_dotenv:', os.getenv('CHART_ENGINE'))
