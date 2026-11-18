import logging
import os
from logging.handlers import TimedRotatingFileHandler
from shared.config import settings

# Ensure logs directory exists
LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

def setup_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    
    # Check if handlers are already configured to avoid duplicate logs in case of re-initialization
    if not logger.handlers:
        # Determine log level from settings
        log_level = logging.DEBUG if settings.log_level.upper() == "DEBUG" else logging.INFO
        logger.setLevel(log_level)
        logger.propagate = False # Prevent double logging if root logger is also configured

        # Formatter with Timestamp, Name, Level, and Message
        formatter = logging.Formatter(
            fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

        # 1. TimedRotatingFileHandler: Creates a new file at midnight, keeps 60 days of history
        log_file_path = os.path.join(LOGS_DIR, "idp_worker.log")
        file_handler = TimedRotatingFileHandler(
            filename=log_file_path,
            when="midnight",
            interval=1,
            backupCount=60,
            encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(log_level)
        
        # 2. Console Handler (Optional but good for Docker logs)
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(log_level)

        logger.addHandler(file_handler)
        logger.addHandler(console_handler)

    return logger
