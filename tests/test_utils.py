"""Test Utilities

Common utilities used across test modules.
"""

import asyncio


async def wait_for_condition(condition_fn, timeout=5.0, poll_interval=0.01, description="condition"):
    """Wait for a condition to become true, with timeout.

    Args:
        condition_fn: Callable that returns True when condition is met (can be async or sync)
        timeout: Maximum time to wait in seconds
        poll_interval: How often to check the condition in seconds
        description: Description of what we're waiting for (for error messages)

    Raises:
        TimeoutError: If condition is not met within timeout

    Example:
        await wait_for_condition(
            lambda: len(processed_messages) > 0,
            timeout=2.0,
            description="messages to be processed"
        )
    """
    import inspect
    start_time = asyncio.get_event_loop().time()

    while True:
        # Check if condition is met
        if inspect.iscoroutinefunction(condition_fn):
            result = await condition_fn()
        else:
            result = condition_fn()

        if result:
            return

        # Check timeout
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed >= timeout:
            raise TimeoutError(f"Timeout waiting for {description} after {timeout}s")

        # Wait before next check
        await asyncio.sleep(poll_interval)
