"""每日信息聚合管道：采集 → 归一化 → 去重 → 打分 → 渲染 Markdown。

设计原则
--------
* 只用标准库（urllib / tomllib / ElementTree），CI 里不需要 pip install。
* 采集与发布解耦：管道只负责产出 content/posts/<date>.md。
* 失败要显式：条目太少直接退出码非 0，让 CI 停在构建之前。
"""

__version__ = "0.1.0"
