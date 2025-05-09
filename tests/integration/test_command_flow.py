# tests/integration/test_command_flow.py
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, func  # Added func

from staring_misaka.db_models import MonitoredGroup, NewUser, PendingAdminAction, QueuedLLMCheck, Prompt, LLMModel, \
    BannedUser
from tests.conftest import (
    TEST_CHAT_ID,
    TEST_CHAT_ID_2,
    TEST_GROUP_ADMIN_ID,
    TEST_REGULAR_USER_ID,
    TEST_SUPER_ADMIN_ID,
    TEST_NEW_USER_ID,  # For admin deny ban test
)

# Mark all tests in this file as async
pytestmark = pytest.mark.asyncio


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
        await db_session.flush() # Use flush instead of commit inside tests with db_session fixture
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
            yield MagicMock(spec=MagicMock, id=TEST_SUPER_ADMIN_ID + 10, is_admin=True) # Corrected to ensure different admin
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
    event_handlers.update_monitored_chats_cache.assert_not_called() # Ensure cache not updated

    # Restore original mock if it was changed for this test specifically
    mock_telegram_client.iter_participants = original_iter_participants


async def test_add_group_command_already_added(
        db_session, mock_telegram_client, command_handlers, event_handlers, monitored_group # Use monitored_group fixture
):
    """
    GIVEN a group that is already monitored
    WHEN an admin uses /add_group in that chat
    THEN an appropriate message should be sent and no DB changes.
    """
    # Arrange
    # monitored_group fixture ensures TEST_CHAT_ID is already in MonitoredGroup
    admin_user_id = TEST_SUPER_ADMIN_ID # Super admin can also do this

    mock_event = MagicMock(
        is_private=False,
        chat_id=TEST_CHAT_ID, # Use the already monitored chat
        sender_id=admin_user_id,
        text="/add_group",
        reply=AsyncMock()
    )
    initial_count = await db_session.scalar(select(func.count(MonitoredGroup.chat_id)))

    # Act
    await command_handlers.add_group_handler(mock_event)

    # Assert
    final_count = await db_session.scalar(select(func.count(MonitoredGroup.chat_id)))
    assert final_count == initial_count # No new group added

    mock_event.reply.assert_called_once_with(
        "This group is already being monitored by the bot."
    )
    event_handlers.update_monitored_chats_cache.assert_not_called() # Cache shouldn't be updated if no change


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
    from staring_misaka.dto import MessageContext # Local import for DTO
    queued_context = MessageContext(user_id=TEST_NEW_USER_ID, chat_id=chat_to_remove, message_id=456, message_text="Test queued")
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
    db_session.expire_all() # Ensure fresh read
    assert await db_session.get(MonitoredGroup, chat_to_remove) is None
    assert await db_session.scalar(select(NewUser).where(NewUser.chat_id == chat_to_remove)) is None
    assert await db_session.scalar(select(PendingAdminAction).where(PendingAdminAction.original_chat_id == chat_to_remove)) is None
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
             yield MagicMock(spec=MagicMock, id=TEST_SUPER_ADMIN_ID + 10, is_admin=True) # Yield a different admin
        else:
            async for p in original_iter_participants(c_id, *args, filter=filter, **kwargs): yield p
        if False: yield
    mock_telegram_client.iter_participants = mock_iter_participants_no_admin_for_this_user


    # Act
    await command_handlers.remove_group_handler(mock_event)

    # Assert
    assert await db_session.get(MonitoredGroup, chat_to_remove) is not None # Still exists
    mock_event.reply.assert_called_once_with(
        "Only group administrators or the bot super admin can remove this group."
    )
    event_handlers.update_monitored_chats_cache.assert_not_called()
    mock_telegram_client.iter_participants = original_iter_participants


async def test_config_group_command(
    db_session, mock_telegram_client, command_handlers, monitored_group, setup_queue_test
):
    """
    GIVEN a monitored group
    WHEN an admin uses /config_group with various valid and invalid settings
    THEN the group's configuration should be updated correctly or errors reported.
    """
    # monitored_group fixture ensures TEST_CHAT_ID is added.
    # setup_queue_test ensures default prompt/model exist and can be used.
    admin_user_id = TEST_SUPER_ADMIN_ID
    chat_id_to_config = TEST_CHAT_ID
    prompt_obj, model_obj = setup_queue_test # Get the actual prompt and model objects

    async def send_config_command(command_text: str):
        mock_event = MagicMock(
            is_private=False, chat_id=chat_id_to_config, sender_id=admin_user_id,
            text=command_text, reply=AsyncMock()
        )
        await command_handlers.config_group_handler(mock_event)
        return mock_event

    group = await db_session.get(MonitoredGroup, chat_id_to_config) # Get initial group state
    # Initial state for approval_required from monitored_group fixture is False.
    assert group.require_admin_approval_for_ban is False

    # Test 'approval_required'
    event = await send_config_command("/config_group approval_required true")
    await db_session.refresh(group) # Refresh the group object from DB
    assert group.require_admin_approval_for_ban is True
    event.reply.assert_called_with("✅ Setting Updated! Admin approval for bans set to: True")

    event = await send_config_command("/config_group approval_required false")
    await db_session.refresh(group)
    assert group.require_admin_approval_for_ban is False
    event.reply.assert_called_with("✅ Setting Updated! Admin approval for bans set to: False")

    # Test 'preban_message' (initial state True from fixture)
    await db_session.refresh(group) # Refresh before checking initial state if modified by other parts of test
    assert group.pre_ban_message_enabled is True # Default from fixture
    event = await send_config_command("/config_group preban_message false")
    await db_session.refresh(group)
    assert group.pre_ban_message_enabled is False
    event.reply.assert_called_with("✅ Setting Updated! Pre-ban notification message set to: False")

    # Test 'delete_messages' (initial state True from fixture)
    await db_session.refresh(group)
    assert group.delete_recent_messages_on_ban is True # Default from fixture
    event = await send_config_command("/config_group delete_messages false")
    await db_session.refresh(group)
    assert group.delete_recent_messages_on_ban is False
    event.reply.assert_called_with("✅ Setting Updated! Deletion of recent messages on ban set to: False")

    # Test 'delete_count' (initial state 1 from fixture)
    await db_session.refresh(group)
    assert group.num_messages_to_delete_on_ban == 1 # Default from fixture
    event = await send_config_command("/config_group delete_count 5")
    await db_session.refresh(group)
    assert group.num_messages_to_delete_on_ban == 5
    event.reply.assert_called_with("✅ Setting Updated! Number of messages to delete on ban set to: 5")

    event = await send_config_command("/config_group delete_count abc") # Invalid
    event.reply.assert_called_with("Invalid number for delete_count. Must be 0 or greater.")

    # Test 'group_prompt'
    new_prompt_name = "Custom Group Prompt TestConfig"
    # Ensure this prompt doesn't exist or use a unique name logic if needed
    existing_custom_prompt = await db_session.scalar(select(Prompt).where(Prompt.name == new_prompt_name))
    if existing_custom_prompt:
        # It's better to ensure test isolation by not deleting, but if tests run sequentially and this name is reused.
        # This part might be problematic if the prompt is referenced elsewhere.
        # For this specific test, let's assume it's okay or use an even more unique name.
        await db_session.delete(existing_custom_prompt)
        await db_session.flush()

    new_prompt = Prompt(name=new_prompt_name, text="Custom CFG: {message_text}")
    db_session.add(new_prompt)
    await db_session.flush()
    new_prompt_id = new_prompt.id

    event = await send_config_command(f"/config_group group_prompt {new_prompt_id}")
    await db_session.refresh(group)
    assert group.custom_prompt_id == new_prompt_id
    event.reply.assert_called_with(f"✅ Setting Updated! Group prompt set to: '{new_prompt.name}' (ID: {new_prompt_id}).")

    event = await send_config_command(f"/config_group group_prompt \"{new_prompt.name}\"") # By name with spaces
    await db_session.refresh(group)
    assert group.custom_prompt_id == new_prompt_id
    event.reply.assert_called_with(f"✅ Setting Updated! Group prompt set to: '{new_prompt.name}' (ID: {new_prompt_id}).")


    event = await send_config_command("/config_group group_prompt none")
    await db_session.refresh(group)
    assert group.custom_prompt_id is None
    event.reply.assert_called_with("✅ Setting Updated! Group prompt reset to global default.")

    event = await send_config_command("/config_group group_prompt NonExistentPrompt") # Invalid
    event.reply.assert_called_with("❌ Prompt 'NonExistentPrompt' not found.")

    # Test 'group_model'
    new_model_name = "Custom Group Model TestConfig"
    existing_custom_model = await db_session.scalar(select(LLMModel).where(LLMModel.name == new_model_name))
    if existing_custom_model:
        await db_session.delete(existing_custom_model)
        await db_session.flush()

    new_model = LLMModel(name=new_model_name, api_identifier="custom-cfg-api", provider="OpenAI") # Unique name
    db_session.add(new_model)
    await db_session.flush()
    new_model_id = new_model.id

    event = await send_config_command(f"/config_group group_model {new_model_id}")
    await db_session.refresh(group)
    assert group.custom_model_id == new_model_id
    event.reply.assert_called_with(f"✅ Setting Updated! Group model set to: '{new_model.name}' (ID: {new_model_id}).")

    event = await send_config_command(f"/config_group group_model \"{new_model.name}\"")
    await db_session.refresh(group)
    assert group.custom_model_id == new_model_id
    event.reply.assert_called_with(f"✅ Setting Updated! Group model set to: '{new_model.name}' (ID: {new_model_id}).")


    event = await send_config_command("/config_group group_model reset")
    await db_session.refresh(group)
    assert group.custom_model_id is None
    event.reply.assert_called_with("✅ Setting Updated! Group model reset to global default.")

    # Test invalid command
    event = await send_config_command("/config_group unknown_setting value")
    event.reply.assert_called_with("❓ Unknown setting 'unknown_setting'. See command help for available settings.")

    # Test insufficient arguments
    event = await send_config_command("/config_group approval_required")
    assert "Usage: /config_group <setting_name> <value>" in event.reply.call_args[0][0]


async def test_admin_denies_ban_reply_flow(
        db_session, mock_telegram_client, command_handlers, action_service,
        monitored_group, new_user_in_group # setup_queue_test not needed here
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
    admin_notification_msg_id = 78901 # Arbitrary ID for the bot's message to admin

    # Create a PendingAdminAction record
    pending_action = PendingAdminAction(
        admin_message_id=admin_notification_msg_id,
        user_to_act_on_id=user_to_act_on,
        original_chat_id=original_chat_id,
        original_message_id=12345, # Arbitrary
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
    db_session.expire_all() # Ensure fresh read

    # User should be removed from NewUser (approved)
    new_user_record = await db_session.get(NewUser, {"user_id": user_to_act_on, "chat_id": original_chat_id})
    assert new_user_record is None, "NewUser record was not deleted after admin denied ban."

    # PendingAdminAction should be removed
    pending_action_record_after = await db_session.get(PendingAdminAction, pending_action_id)
    assert pending_action_record_after is None, "PendingAdminAction was not deleted."

    # User should NOT be banned
    banned_user_record = await db_session.scalar(
        select(BannedUser).where(BannedUser.user_id == user_to_act_on, BannedUser.chat_id == original_chat_id)
    )
    assert banned_user_record is None, "User was incorrectly banned."

    mock_admin_reply_no.reply.assert_called_with(
        f"Action denied for user {user_to_act_on}. User marked as approved for now."
    )
    mock_telegram_client.kick_participant.assert_not_called()
    mock_telegram_client.delete_messages.assert_not_called()