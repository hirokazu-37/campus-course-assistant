class ValidationError(ValueError):
    """输入或数据结构不符合约定。"""


class StorageError(RuntimeError):
    """存储损坏、写入失败或目录被其他进程锁定。"""


class TaskNotFoundError(LookupError):
    """任务 ID 不存在。"""


class ModelError(RuntimeError):
    """模型连接、拒绝、截断或输出校验失败；消息不得包含密钥/响应正文。"""
