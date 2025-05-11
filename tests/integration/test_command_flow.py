from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select  # Added func

from staring_misaka.db_models import (
    LLMModel,
    MonitoredGroup,
    NewUser,
    PendingAdminAction,
    Prompt,
    QueuedLLMCheck,
)
from tests.conftest import (
    TEST_CHAT_ID,
    TEST_CHAT_ID_2,
    TEST_GROUP_ADMIN_ID,
    TEST_NEW_USER_ID,  # For admin deny ban test
    TEST_REGULAR_USER_ID,
    TEST_SUPER_ADMIN_ID,
    assert_user_approved,  # Import helper
)

# Mark all tests in this file as async
pytestmark = pytest.mark.asyncio


async def test_start_command(command_handlers, mock_telegram_client):
    """
    GIVEN a user sends /start
    WHEN the start_handler is called
    THEN a help message with a list of commands should be replied.
    """
    # Arrange
    mock_event = MagicMock(
        is_private=True,  # /start can be used in private or group
        chat_id=TEST_SUPER_ADMIN_ID,  # or any chat_id
        sender_id=TEST_SUPER_ADMIN_ID,
        text="/start",
        reply=AsyncMock()
    )

    # Act
    await command_handlers.start_handler(mock_event)

    # Assert
    mock_event.reply.assert_called_once()
    reply_text = mock_event.reply.call_args[0][0]

    assert "Welcome to Staring Misaka Bot!" in reply_text
    assert "/add_group" in reply_text
    assert "/remove_group" in reply_text
    assert "/config_group <setting_name> <value>" in reply_text
    assert "approval_required <true|false>" in reply_text
    assert "If true, detected spam will require manual approval before a ban." in reply_text
    assert "For Super Admins" not in reply_text
    assert "Web UI" not in reply_text # Assuming Web UI is for super admins, not general end users via /start
    assert "If you need further assistance, please contact the bot operator." in reply_text


async def test_add_group_command_by_admin(
        db_session, mock_telegram_client, command_handlers, event_handlers
):
    """
    GIVEN a user who is an admin in a chat (but not super admin)
    WHEN they use /add_group in that chat
    THEN the group should be added to MonitoredGroup table and cache updated.
    """
    # Arrange
    chat_id_to_add = TEST_CHAT_ID_2
    admin_user_id = TEST_GROUP_ADMIN_ID  # This user is mocked as admin of TEST_CHAT_ID_2 in conftest

    # Ensure the group doesn't exist yet
    existing = await db_session.get(MonitoredGroup, chat_id_to_add)
    assert existing is None

    mock_event = MagicMock(
        is_private=False,
        chat_id=chat_id_to_add,
        sender_id=admin_user_id,
        text="/add_group",
        reply=AsyncMock()
    )

    # Act
    await command_handlers.add_group_handler(mock_event)
    # REMOVED: await db_session.commit() # Commit is handled by command_handler's get_db_session context manager

    # Assert
    # The db_session fixture will roll back, so to check the state *after* the handler's commit
    # but before our test's rollback, we need to ensure the handler's session committed.
    # For assertion, we rely on the same session; the data will be visible.
    new_group = await db_session.get(MonitoredGroup, chat_id_to_add)
    assert new_group is not None
    assert new_group.chat_id == chat_id_to_add
    assert new_group.added_by_user_id == admin_user_id

    mock_event.reply.assert_called_once_with(
        "✅ Group successfully added for monitoring! New users' first messages will be checked."
    )
    event_handlers.update_monitored_chats_cache.assert_called_once()


async def test_add_group_command_by_non_admin(
        db_session, mock_telegram_client, command_handlers, event_handlers
):
    """
    GIVEN a user who is NOT an admin in a chat
    WHEN they use /add_group in that chat
    THEN the group should NOT be added and an error message sent.
    """
    # Arrange
    chat_id_to_add = TEST_CHAT_ID_2
    non_admin_user_id = TEST_REGULAR_USER_ID

    # Safeguard: Ensure the group doesn't exist from a previous test due to rollback failure.
    # This is more for robustness; db_session should handle perfect rollback.
    existing_group = await db_session.get(MonitoredGroup, chat_id_to_add)
    if existing_group:
        await db_session.delete(existing_group)
        await db_session.flush()  # Use flush instead of commit inside tests with db_session fixture
        # await db_session.commit() # Commit this cleanup action if it happens # Original comment

    mock_event = MagicMock(
        is_private=False,
        chat_id=chat_id_to_add,
        sender_id=non_admin_user_id,
        text="/add_group",
        reply=AsyncMock()
    )

    original_iter_participants = mock_telegram_client.iter_participants

    async def mock_iter_participants_no_admin_for_this_user(c_id, *args, filter=None, **kwargs):
        if c_id == chat_id_to_add and filter and filter.__name__ == 'ChannelParticipantsAdmins':
            # Yield a different admin, or no one
            yield MagicMock(spec=MagicMock, id=TEST_SUPER_ADMIN_ID + 10,
                            is_admin=True)  # Corrected to ensure different admin
        else:  # Fallback to original mock for other cases if necessary
            async for p in original_iter_participants(c_id, *args, filter=filter, **kwargs):
                yield p
        if False: yield

    mock_telegram_client.iter_participants = mock_iter_participants_no_admin_for_this_user

    # Act
    await command_handlers.add_group_handler(mock_event)
    # REMOVED: await db_session.commit() # Commit is handled by command_handler's get_db_session

    # Assert
    group = await db_session.get(MonitoredGroup, chat_id_to_add)
    assert group is None

    mock_event.reply.assert_called_once_with(
        "Only group administrators can add this group for monitoring."
    )
    event_handlers.update_monitored_chats_cache.assert_not_called()  # Ensure cache not updated

    # Restore original mock if it was changed for this test specifically
    mock_telegram_client.iter_participants = original_iter_participants


async def test_add_group_command_already_added(
        db_session, mock_telegram_client, command_handlers, event_handlers, monitored_group
        # Use monitored_group fixture
):
    """
    GIVEN a group that is already monitored
    WHEN an admin uses /add_group in that chat
    THEN an appropriate message should be sent and no DB changes.
    """
    # Arrange
    # monitored_group fixture ensures TEST_CHAT_ID is already in MonitoredGroup
    admin_user_id = TEST_SUPER_ADMIN_ID  # Super admin can also do this

    mock_event = MagicMock(
        is_private=False,
        chat_id=TEST_CHAT_ID,  # Use the already monitored chat
        sender_id=admin_user_id,
        text="/add_group",
        reply=AsyncMock()
    )
    initial_count = await db_session.scalar(select(func.count(MonitoredGroup.chat_id)))

    # Act
    await command_handlers.add_group_handler(mock_event)

    # Assert
    final_count = await db_session.scalar(select(func.count(MonitoredGroup.chat_id)))
    assert final_count == initial_count  # No new group added

    mock_event.reply.assert_called_once_with(
        "This group is already being monitored by the bot."
    )
    event_handlers.update_monitored_chats_cache.assert_not_called()  # Cache shouldn't be updated if no change


async def test_remove_group_command_by_admin(
        db_session, mock_telegram_client, command_handlers, event_handlers, monitored_group
):
    """
    GIVEN a monitored group with associated data (NewUser, PendingAdminAction, QueuedLLMCheck)
    WHEN an admin uses /remove_group
    THEN the group and all associated data should be removed.
    """
    # Arrange
    # monitored_group fixture ensures TEST_CHAT_ID is added
    admin_user_id = TEST_SUPER_ADMIN_ID
    chat_to_remove = TEST_CHAT_ID

    # Add associated data
    db_session.add(NewUser(user_id=TEST_NEW_USER_ID, chat_id=chat_to_remove))
    # For PendingAdminAction, need an admin_message_id. Let's assume one.
    db_session.add(PendingAdminAction(
        admin_message_id=99901, user_to_act_on_id=TEST_NEW_USER_ID,
        original_chat_id=chat_to_remove, original_message_id=123,
        message_text_preview="Test pending"
    ))
    # For QueuedLLMCheck, needs message_context_json
    from staring_misaka.dto import MessageContext  # Local import for DTO
    queued_context = MessageContext(user_id=TEST_NEW_USER_ID, chat_id=chat_to_remove, message_id=456,
                                    message_text="Test queued")
    db_session.add(QueuedLLMCheck(
        message_context_json=queued_context.model_dump(mode='json'),
        reason_for_queueing="Test remove reason"
    ))
    await db_session.flush()

    mock_event = MagicMock(
        is_private=False, chat_id=chat_to_remove, sender_id=admin_user_id,
        text="/remove_group", reply=AsyncMock()
    )

    # Act
    await command_handlers.remove_group_handler(mock_event)

    # Assert
    db_session.expire_all()  # Ensure fresh read
    assert await db_session.get(MonitoredGroup, chat_to_remove) is None
    assert await db_session.scalar(select(NewUser).where(NewUser.chat_id == chat_to_remove)) is None
    assert await db_session.scalar(
        select(PendingAdminAction).where(PendingAdminAction.original_chat_id == chat_to_remove)) is None
    # For QueuedLLMCheck, the query in handler is `message_context_json['chat_id'].as_integer() == chat_id`
    # We can test this by trying to find any item that might have matched
    remaining_queued = await db_session.execute(
        select(QueuedLLMCheck).where(QueuedLLMCheck.message_context_json['chat_id'].as_integer() == chat_to_remove)
    )
    assert remaining_queued.scalars().first() is None

    mock_event.reply.assert_called_once_with(
        "❌ Group removed from monitoring. Associated new user entries, pending actions, and queued checks have been cleared."
    )
    event_handlers.update_monitored_chats_cache.assert_called_once()


async def test_remove_group_command_by_non_admin(
        db_session, mock_telegram_client, command_handlers, event_handlers, monitored_group
):
    """
    GIVEN a monitored group
    WHEN a non-admin uses /remove_group
    THEN the group should NOT be removed and an error message sent.
    """
    # Arrange
    non_admin_user_id = TEST_REGULAR_USER_ID
    chat_to_remove = TEST_CHAT_ID

    mock_event = MagicMock(
        is_private=False, chat_id=chat_to_remove, sender_id=non_admin_user_id,
        text="/remove_group", reply=AsyncMock()
    )

    original_iter_participants = mock_telegram_client.iter_participants

    async def mock_iter_participants_no_admin_for_this_user(c_id, *args, filter=None, **kwargs):
        if c_id == chat_to_remove and filter and filter.__name__ == 'ChannelParticipantsAdmins':
            yield MagicMock(spec=MagicMock, id=TEST_SUPER_ADMIN_ID + 10, is_admin=True)  # Yield a different admin
        else:
            async for p in original_iter_participants(c_id, *args, filter=filter, **kwargs): yield p
        if False: yield

    mock_telegram_client.iter_participants = mock_iter_participants_no_admin_for_this_user

    # Act
    await command_handlers.remove_group_handler(mock_event)

    # Assert
    assert await db_session.get(MonitoredGroup, chat_to_remove) is not None  # Still exists
    mock_event.reply.assert_called_once_with(
        "Only group administrators or the bot super admin can remove this group."
    )
    event_handlers.update_monitored_chats_cache.assert_not_called()
    mock_telegram_client.iter_participants = original_iter_participants


@pytest.mark.parametrize(
    "setting_key,value_to_set,db_field_name,expected_db_value,expected_reply_suffix",
    [
        ("approval_required", "true", "require_admin_approval_for_ban", True, "Admin approval for bans set to: True"),
        ("approval_required", "false", "require_admin_approval_for_ban", False,
         "Admin approval for bans set to: False"),
        ("preban_message", "true", "pre_ban_message_enabled", True, "Pre-ban notification message set to: True"),
        ("preban_message", "false", "pre_ban_message_enabled", False, "Pre-ban notification message set to: False"),
        ("delete_messages", "true", "delete_recent_messages_on_ban", True,
         "Deletion of recent messages on ban set to: True"),
        ("delete_messages", "false", "delete_recent_messages_on_ban", False,
         "Deletion of recent messages on ban set to: False"),
        ("delete_count", "5", "num_messages_to_delete_on_ban", 5, "Number of messages to delete on ban set to: 5"),
        ("delete_count", "0", "num_messages_to_delete_on_ban", 0, "Number of messages to delete on ban set to: 0"),
    ],
)
async def test_config_group_simple_settings(
        db_session, command_handlers, monitored_group: MonitoredGroup,  # Get the group object
        setting_key, value_to_set, db_field_name, expected_db_value, expected_reply_suffix
):
    """Tests configuration of simple boolean and numeric group settings."""
    # Arrange
    # monitored_group fixture ensures TEST_CHAT_ID is added and provides the group object.
    # Default monitored_group has approval_required=False, preban_message=True, delete_messages=True, delete_count=1
    # This test will change them and verify.

    mock_event = MagicMock(
        is_private=False, chat_id=TEST_CHAT_ID, sender_id=TEST_SUPER_ADMIN_ID,
        text=f"/config_group {setting_key} {value_to_set}", reply=AsyncMock()
    )

    # Act
    await command_handlers.config_group_handler(mock_event)

    # Assert
    await db_session.refresh(monitored_group)  # Refresh from DB
    assert getattr(monitored_group, db_field_name) == expected_db_value
    mock_event.reply.assert_called_with(f"✅ Setting Updated! {expected_reply_suffix}")


async def test_config_group_prompt_settings(
        db_session, command_handlers, monitored_group: MonitoredGroup, setup_queue_test
        # setup_queue_test for default prompt
):
    """Tests configuration of group-specific prompt."""
    # Arrange
    admin_user_id = TEST_SUPER_ADMIN_ID
    chat_id_to_config = TEST_CHAT_ID

    # Create a custom prompt for this test
    custom_prompt_name = f"CustomGroupPrompt_{TEST_CHAT_ID}"
    custom_prompt = Prompt(name=custom_prompt_name, text="Custom prompt: {message_text} for this group.")
    db_session.add(custom_prompt)
    await db_session.flush()
    custom_prompt_id = custom_prompt.id

    async def send_config_command(value_str: str):
        mock_event = MagicMock(
            is_private=False, chat_id=chat_id_to_config, sender_id=admin_user_id,
            text=f"/config_group group_prompt {value_str}", reply=AsyncMock()
        )
        await command_handlers.config_group_handler(mock_event)
        return mock_event

    # Test setting by ID
    event_id = await send_config_command(str(custom_prompt_id))
    await db_session.refresh(monitored_group)
    assert monitored_group.custom_prompt_id == custom_prompt_id
    event_id.reply.assert_called_with(
        f"✅ Setting Updated! Group prompt set to: '{custom_prompt_name}' (ID: {custom_prompt_id}).")

    # Test setting by Name (quoted if it had spaces, but this one doesn't)
    event_name = await send_config_command(f'"{custom_prompt_name}"')  # Use quotes for robustness
    await db_session.refresh(monitored_group)
    assert monitored_group.custom_prompt_id == custom_prompt_id
    event_name.reply.assert_called_with(
        f"✅ Setting Updated! Group prompt set to: '{custom_prompt_name}' (ID: {custom_prompt_id}).")

    # Test resetting to none/default
    event_none = await send_config_command("none")
    await db_session.refresh(monitored_group)
    assert monitored_group.custom_prompt_id is None
    event_none.reply.assert_called_with("✅ Setting Updated! Group prompt reset to global default.")

    # Test setting non-existent prompt
    event_invalid = await send_config_command("NonExistentPromptName123")
    event_invalid.reply.assert_called_with("❌ Prompt 'NonExistentPromptName123' not found.")


async def test_config_group_model_settings(
        db_session, command_handlers, monitored_group: MonitoredGroup, setup_queue_test
        # setup_queue_test for default model
):
    """Tests configuration of group-specific LLM model."""
    # Arrange
    admin_user_id = TEST_SUPER_ADMIN_ID
    chat_id_to_config = TEST_CHAT_ID

    # Create a custom model for this test
    custom_model_name = f"CustomGroupModel_{TEST_CHAT_ID}"
    custom_model = LLMModel(name=custom_model_name, api_identifier="custom-group-api-v1", provider="OpenAI")
    db_session.add(custom_model)
    await db_session.flush()
    custom_model_id = custom_model.id

    async def send_config_command(value_str: str):
        mock_event = MagicMock(
            is_private=False, chat_id=chat_id_to_config, sender_id=admin_user_id,
            text=f"/config_group group_model {value_str}", reply=AsyncMock()
        )
        await command_handlers.config_group_handler(mock_event)
        return mock_event

    # Test setting by ID
    event_id = await send_config_command(str(custom_model_id))
    await db_session.refresh(monitored_group)
    assert monitored_group.custom_model_id == custom_model_id
    event_id.reply.assert_called_with(
        f"✅ Setting Updated! Group model set to: '{custom_model_name}' (ID: {custom_model_id}).")

    # Test setting by Name
    event_name = await send_config_command(f'"{custom_model_name}"')
    await db_session.refresh(monitored_group)
    assert monitored_group.custom_model_id == custom_model_id
    event_name.reply.assert_called_with(
        f"✅ Setting Updated! Group model set to: '{custom_model_name}' (ID: {custom_model_id}).")

    # Test resetting to none/default
    event_reset = await send_config_command("reset")
    await db_session.refresh(monitored_group)
    assert monitored_group.custom_model_id is None
    event_reset.reply.assert_called_with("✅ Setting Updated! Group model reset to global default.")

    # Test setting non-existent model
    event_invalid = await send_config_command("NonExistentModelName456")
    event_invalid.reply.assert_called_with("❌ Model 'NonExistentModelName456' not found.")


async def test_config_group_error_conditions(
        db_session, command_handlers, monitored_group: MonitoredGroup
):
    """Tests various error conditions for /config_group command."""
    # Arrange
    admin_user_id = TEST_SUPER_ADMIN_ID
    chat_id_to_config = TEST_CHAT_ID

    async def send_config_command(command_text: str):
        mock_event = MagicMock(
            is_private=False, chat_id=chat_id_to_config, sender_id=admin_user_id,
            text=command_text, reply=AsyncMock()
        )
        await command_handlers.config_group_handler(mock_event)
        return mock_event

    # Test invalid value for delete_count
    event_invalid_delete_count = await send_config_command("/config_group delete_count abc")
    event_invalid_delete_count.reply.assert_called_with("Invalid number for delete_count. Must be 0 or greater.")

    # Test unknown setting
    event_unknown_setting = await send_config_command("/config_group unknown_setting value")
    event_unknown_setting.reply.assert_called_with(
        "❓ Unknown setting 'unknown_setting'. See command help for available settings.")

    # Test insufficient arguments
    event_insufficient_args = await send_config_command("/config_group approval_required")
    assert "Usage: /config_group <setting_name> <value>" in event_insufficient_args.reply.call_args[0][0]


async def test_admin_denies_ban_reply_flow(
        db_session, mock_telegram_client, command_handlers, action_service,
        monitored_group, new_user_in_group  # setup_queue_test not needed here
):
    """
    GIVEN a pending admin action for a ban
    WHEN the admin replies "no"
    THEN the user should be approved (NewUser removed) and PendingAdminAction removed.
    """
    # Arrange
    admin_user_id = TEST_SUPER_ADMIN_ID
    user_to_act_on = TEST_NEW_USER_ID
    original_chat_id = TEST_CHAT_ID
    admin_notification_msg_id = 78901  # Arbitrary ID for the bot's message to admin

    # Create a PendingAdminAction record
    pending_action = PendingAdminAction(
        admin_message_id=admin_notification_msg_id,
        user_to_act_on_id=user_to_act_on,
        original_chat_id=original_chat_id,
        original_message_id=12345,  # Arbitrary
        message_text_preview="This is spam, please ban.",
        proposed_action="ban",
        llm_reason_for_action="LLM detected spam."
    )
    db_session.add(pending_action)
    await db_session.flush()
    pending_action_id = pending_action.id

    # new_user_in_group fixture ensures user_to_act_on is in NewUser table for original_chat_id

    mock_admin_reply_no = MagicMock(
        is_private=True, sender_id=admin_user_id,
        reply_to_msg_id=admin_notification_msg_id,
        text="no", reply=AsyncMock()
    )

    # Act
    await command_handlers.admin_reply_handler(mock_admin_reply_no)

    # Assert
    db_session.expire_all()  # Ensure fresh read

    # User should be approved
    await assert_user_approved(db_session, user_to_act_on, original_chat_id)

    # PendingAdminAction should be removed
    pending_action_record_after = await db_session.get(PendingAdminAction, pending_action_id)
    assert pending_action_record_after is None, "PendingAdminAction was not deleted."

    mock_admin_reply_no.reply.assert_called_with(
        f"Action denied for user {user_to_act_on}. User marked as approved for now."
    )
    mock_telegram_client.kick_participant.assert_not_called()
    mock_telegram_client.delete_messages.assert_not_called()
