import logging
import sys

APP_LOGGER_NAME = "DATABRICKS LOGS"
BASE_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

LOGGER_MAPPING = {
    "notset": logging.NOTSET,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


def setup_applevel_logger(
    logger_name: str = APP_LOGGER_NAME,
    file_name: str = None,
    log_format: str = BASE_FORMAT,
    log_level: int = logging.WARNING,
    file_log_level: int = None,
) -> logging.Logger:
    """
    Set up a "root" logger for the app/repository,

    :param logger_name: name of the logger to display, defaults to
    APP_LOGGER_NAME
    :type logger_name: str, optional
    :param file_name: output file name for logs file (currently handle by
    FileHandler and not RotatingFileHandler), defaults to None
    :type file_name: str, optional
    :param log_format: wanted format for the logs
    :type log_format: str
    :param log_level: log level used for stream (and file if file level is None
    )
    :type log_level: int, optional
    :param file_log_level: log level used for files
    :type file_log_level: int, optional

    :return: a configured logger
    :rtype: logging.Logger
    """
    logger = logging.getLogger(logger_name)
    formatter = logging.Formatter(log_format)
    logger.setLevel(log_level)
    streamhandler = logging.StreamHandler(sys.stdout)
    streamhandler.setLevel(log_level)
    streamhandler.setFormatter(formatter)
    logger.handlers.clear()
    logger.addHandler(streamhandler)
    if file_name:
        if not file_log_level:
            file_log_level = log_level
        filehandler = logging.FileHandler(file_name)
        filehandler.setLevel(file_log_level)
        filehandler.setFormatter(formatter)
        logger.addHandler(filehandler)
    return logger


def get_logger(module_name: str, root_logger_name: str = APP_LOGGER_NAME) -> logging.Logger:
    """
    Get a module level logger (should have been setup at app level before in
    order to follow config and format)

    :param module_name: name of the module usually __name__
    :type module_name: str
    :param root_logger_name: name of the root logger to get child from,
    defaults to APP_LOGGER_NAME
    :type root_logger_name: str, optional

    :return: module level logger
    :rtype: logging.Logger
    """
    return logging.getLogger(root_logger_name).getChild(module_name)
