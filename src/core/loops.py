import asyncio


def selector_loop_factory():
    return asyncio.SelectorEventLoop()
