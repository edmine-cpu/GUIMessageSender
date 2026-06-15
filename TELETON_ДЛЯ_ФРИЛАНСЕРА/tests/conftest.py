import asyncio
import inspect

import pytest


def _run_coroutine(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass
        loop.close()


def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: mark test as asyncio-based")


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    test_func = pyfuncitem.obj
    if inspect.iscoroutinefunction(test_func):
        funcargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
        _run_coroutine(test_func(**funcargs))
        return True
    return None

