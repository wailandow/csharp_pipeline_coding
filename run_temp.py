from projects.strategy_selector.graph import graph
from core.config import load as load_config

'''
Перед чистовым запуском:
- почистить папку логов (logs)
- убрать файлы БД (data)
- проверить папку с тестовыми данныме Candle
'''

cfg = load_config()
#print(cfg["llm"]["strategy_step1"])


result = graph.invoke({
    "strategy_descriptions": [
        """
Напиши код для реализации ....
""",
    ],
})

