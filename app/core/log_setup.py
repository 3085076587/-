"""定时任务专用日志：明文写入 logs/scheduler.log。

- 带时间戳（年-月-日 时:分:秒），方便回看每次任务的启动/成功/失败时刻；
- 按天轮转，保留 30 天历史（scheduler.log、scheduler.log.2026-11-01 …）；
- 同时保留一份终端输出，便于宝塔「项目日志」实时查看；
- 只挂到独立的 "task" logger，不污染 uvicorn 主日志。
"""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "scheduler.log"
LOGGER_NAME = "task"

_FORMAT = "%(asctime)s | %(levelname)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup_task_logging() -> logging.Logger:
    """初始化并返回定时任务 logger（幂等，重复调用不会挂多个 handler）。"""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    if _configured:
        return logger

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # 不向 root 传递，避免与 uvicorn 主日志重复

    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

    file_handler = TimedRotatingFileHandler(
        LOG_FILE, when="midnight", backupCount=30, encoding="utf-8"
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    _configured = True
    return logger


def get_task_logger() -> logging.Logger:
    """获取定时任务 logger（若尚未初始化则先初始化）。"""
    logger = logging.getLogger(LOGGER_NAME)
    if not _configured:
        return setup_task_logging()
    return logger
