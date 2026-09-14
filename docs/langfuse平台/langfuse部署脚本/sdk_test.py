import time
from langfuse import Langfuse

langfuse = Langfuse(
    public_key="pk-c64d8d357fa8e8b90e8aefb8183a2cea",
    secret_key="sk-3791dd2504c88ca7502c987768ac35e907366db9260eec07",
    host="http://127.0.0.1:3010",
)
with langfuse.start_as_current_observation(name="v4-sdk-e2e", as_type="chain", input="ping", output="pong"):
    with langfuse.start_as_current_observation(name="gen-sql", as_type="generation", model="gpt-4o",
                                               input={"q": "count?"}, output="42"):
        pass
langfuse.flush()
time.sleep(3)
print("TRACE_SENT")
