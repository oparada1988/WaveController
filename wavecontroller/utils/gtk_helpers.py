from contextlib import contextmanager


@contextmanager
def blocked_handler(widget, handler_id):
    """Temporarily blocks a signal handler while setting a widget's value programmatically, to avoid feedback loops."""
    if handler_id:
        widget.handler_block(handler_id)
    try:
        yield
    finally:
        if handler_id:
            widget.handler_unblock(handler_id)
