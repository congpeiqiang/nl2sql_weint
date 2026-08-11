import os
from dotenv import load_dotenv
load_dotenv()
print('CHART_ENGINE from env:', os.getenv('CHART_ENGINE'))
