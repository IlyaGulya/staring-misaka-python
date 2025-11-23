# E2E Test Fixtures Guide

This document explains the E2E test fixture architecture, dependencies, and usage patterns.

## Fixture Dependency Graph

```
e2e_world (function-scoped) ← main entry point for tests
├── e2e_bot_env (function)
│   ├── e2e_bot_client (session)
│   │   ├── e2e_config (session)
│   │   └── event_loop (session)
│   ├── e2e_session_factory (session)
│   ├── e2e_config (session)
│   ├── e2e_bot_token (session)
│   ├── e2e_test_group_id (function)
│   │   ├── e2e_admin_client (session)
│   │   ├── e2e_bot_client (session)
│   │   ├── e2e_user_client (session)
│   │   └── e2e_bot_username (session)
│   ├── mock_llm_e2e (session)
│   └── e2e_admin_client (session)
├── e2e_user_client (session)
├── e2e_admin_client (session)
├── e2e_test_group_id (function)
└── e2e_session_factory (session)
```

## Session-Scoped Fixtures (Persist Across All Tests)

### `event_loop`
**Location:** `tests/conftest.py:18-24`
**Purpose:** Provides shared asyncio event loop for all async fixtures and tests
**Why session-scoped:** Telegram clients need persistent connection across tests

### `e2e_config`
**Location:** `tests/conftest.py:273-306`
**Purpose:** Loads `.env.test` configuration (API keys, sessions, DC info)
**Validates:** IP addresses against test server whitelist (kill-switch)

### `e2e_bot_client`
**Location:** `tests/conftest.py:319-368`
**Purpose:** Real bot client connected to Telegram test servers
**Note:** Handlers registered on this client persist (see Phase 3 of refactoring plan)

### `e2e_user_client`
**Location:** `tests/conftest.py:370-407`
**Purpose:** Real user client (StringSession-based)

### `e2e_admin_client`
**Location:** `tests/conftest.py:410-457`
**Purpose:** Real admin client with IP validation
**Why separate from user:** Admin has elevated permissions, different session

### `e2e_bot_username`
**Location:** `tests/conftest.py:460-466`
**Purpose:** Fetches bot username once for all tests

### `mock_llm_e2e`
**Location:** `tests/conftest.py:610-620`
**Purpose:** Mock LLM that can be configured per-test
**Default:** `is_spam = False`
**Reset:** By `reset_e2e_state` autouse fixture between tests

### `e2e_session_factory`
**Location:** `tests/conftest.py:623-653`
**Purpose:** SQLAlchemy session factory with isolated temp DB
**Creates:** Fresh SQLite database for E2E tests only

## Function-Scoped Fixtures (Recreated Per Test)

### `e2e_test_group_id`
**Location:** `tests/conftest.py:560-649`
**Purpose:** Creates fresh Telegram supergroup for each test
**Setup:**
1. Admin creates private supergroup
2. Bot added and promoted to admin (ban/delete permissions)
3. User invited via invite link (with retry logic for expired links)

**Teardown:** Deletes group completely

**Why function-scoped:** Each test needs isolated Telegram environment

### `e2e_bot_env`
**Location:** `tests/conftest.py:469-557`
**Purpose:** Initializes bot application for each test
**Setup:**
1. Creates `E2EBotConfig` with current test group ID
2. Admin sends `/start` to establish peer relationship
3. Calls `create_bot()` with existing `e2e_bot_client`
4. Creates and starts `QueueProcessor` in background
5. Calls `bot.catch_up()` to process pending updates

**Teardown:**
1. Stops queue processor
2. Cancels background task
3. **Note:** Does NOT disconnect bot client (session-scoped)

**Returns:** Dict with `bot`, `queue_processor`, `config`

### `e2e_world`
**Location:** `tests/conftest.py:656-683`
**Purpose:** Bundles all clients, DB, and helpers into single object
**Type:** `E2EWorld` dataclass from `tests/e2e_world.py`
**Usage:** Main entry point for all E2E tests

## Autouse Fixtures

### `reset_e2e_state`
**Location:** `tests/test_e2e_telegram.py:39-75`
**When:** Runs AFTER each test
**Purpose:**
1. Resets `mock_llm_e2e` to default state
2. Clears all database tables
3. Resets `AdminSettings.require_approval = False`

**Note:** Does NOT handle Telegram cleanup (done by `e2e_test_group_id`)

## E2EWorld Helper Methods

The `E2EWorld` class provides high-level helpers that encapsulate common patterns:

### Telegram Helpers
- `assert_bot_is_admin()` - Verify bot has proper permissions
- `wait_for_message_from_bot(predicate, timeout)` - Wait for specific bot message
- `user_send(text)` - User sends message to group
- `admin_send(peer, text)` - Admin sends message
- `user_join_group()` - Simulate user join (creates NewUser entry)

### Database Helpers
- `db_get_new_user(user_id)` - Get NewUser record
- `db_get_queue_item(user_id, message_id)` - Get MessageQueue record
- `db_is_queue_completed(user_id, message_id)` - Check queue status
- `db_is_banned(user_id)` - Check ban status
- `db_is_approved(user_id)` - Check approval status
- `db_has_pending_ban(user_id)` - Check for pending ban request
- `db_get_pending_ban(user_id)` - Get pending ban request

### High-Level Workflow Helpers (Added in Phase 4)
- `wait_for_user_tracked(user_id)` - Wait for NewUser creation
- `wait_for_queue_completion(user_id, message_id)` - Wait for queue processing
- `wait_for_user_banned(user_id)` - Wait for ban
- `wait_for_user_approved(user_id)` - Wait for approval
- `wait_for_pending_ban(user_id)` - Wait for pending ban request (returns object)
- `admin_approve_ban(pending_request, bot_username)` - Admin approves ban workflow
- `wait_for_ban_confirmation_in_group(user_id)` - Returns awaitable task for group message

### Composite Assertions
- `assert_user_banned_everywhere(user_id)` - Verify ban in DB AND Telegram
- `ensure_admin_peer(bot_username)` - Establish admin-bot relationship

## Writing New E2E Tests

### Basic Pattern

```python
import pytest
from unittest.mock import AsyncMock

@pytest.mark.e2e
@pytest.mark.asyncio(loop_scope="session")
class TestMyNewFeature:
    async def test_my_scenario(self, e2e_world, mock_llm_e2e):
        # 1. Configure mocks
        mock_llm_e2e.is_spam = AsyncMock(return_value=True)

        # 2. Get user info
        user = await e2e_world.user_client.get_me()
        user_id = user.id

        # 3. Set up test state
        await e2e_world.user_join_group()
        await e2e_world.wait_for_user_tracked(user_id)

        # 4. Trigger action
        msg = await e2e_world.user_send("Test message")

        # 5. Wait for processing
        await e2e_world.wait_for_queue_completion(user_id, msg.id)

        # 6. Assert results
        assert e2e_world.db_is_banned(user_id)
```

### Common Scenarios

**Admin Approval Test:**
```python
# Enable admin approval
with e2e_world._session() as session:
    admin_settings = session.query(AdminSettings).first()
    admin_settings.require_approval = True
    session.commit()

# Wait for pending ban and approve
pending_request = await e2e_world.wait_for_pending_ban(user_id)
await e2e_world.admin_approve_ban(pending_request, e2e_bot_username)
await e2e_world.wait_for_user_banned(user_id)
```

**Waiting for Bot Message:**
```python
# Start waiting BEFORE triggering action (avoids race condition)
user_id = 12345
wait_task = e2e_world.wait_for_ban_confirmation_in_group(user_id)
await trigger_ban_action()
message = await wait_task
assert "banned" in message.text.lower()
```

## Troubleshooting

### Tests Hang Forever
- Check that bot has admin permissions: `await e2e_world.assert_bot_is_admin()`
- Verify group ID is correct: `print(e2e_world.group_id)`
- Check queue processor is running: `print(e2e_world.queue_processor._running)`

### "IP is not a test server" Error
- Verify `.env.test` has correct `TEST_DC_IP`
- Check IP against whitelist in `server_validation.py` (at project root)
- Ensure you're not accidentally using production credentials

### Handler Registered Multiple Times
- This was fixed in Phase 3 of the refactoring
- Check `telegram.py` has the `_misaka_handlers_registered` guard

### Database State Pollution
- `reset_e2e_state` should clean DB between tests
- Check if test is modifying session-scoped fixtures
- Use `e2e_world._session()` for all DB operations

### Telegram Group Not Created
- Check admin client permissions
- Verify test server is responding (may be slow)
- Look for "Failed to create test group" in logs

## Performance Tips

1. **Minimize wait times:** Use short timeouts (2-3s) in local development
2. **Run specific tests:** `pixi run pytest tests/test_e2e_telegram.py::TestClass::test_name -v -m e2e`
3. **Skip E2E in CI:** Use `-m "not e2e"` for fast feedback loop
4. **Parallel execution:** NOT recommended - tests use shared bot client

## Safety Features

1. **IP Validation Kill-Switch:** Prevents production server connections
2. **Isolated Database:** E2E tests use separate temp SQLite DB
3. **Test Server Only:** All tests require Telegram test server credentials
4. **Group Cleanup:** Each test's group is deleted in teardown
5. **State Reset:** `reset_e2e_state` ensures clean slate between tests
