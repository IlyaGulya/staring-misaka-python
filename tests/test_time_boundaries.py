import pytest
from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock

from queue_processor import QueueProcessor
from db import MessageQueue
from config import Config


class TestTimeBoundaryEdgeCases:
    """Test time-boundary edge cases for clear_completed_messages"""

    @pytest.fixture
    def queue_processor(self, session_factory, mock_llm, mock_telegram_client, test_config):
        """Create a QueueProcessor instance for testing"""
        return QueueProcessor(session_factory, mock_llm, mock_telegram_client, test_config)
    
    def test_clear_completed_messages_with_old_messages(self, queue_processor, test_session):
        """Test clear_completed_messages clears old messages but preserves recent ones"""
        now = datetime.now(UTC)
        old_time = now - timedelta(hours=25)  # Older than default 24 hour cutoff
        recent_time = now - timedelta(hours=12)  # Within 24 hours
        
        # Create old and recent completed messages
        old_msg = MessageQueue(
            user_id=1001,
            chat_id=67890,
            message_id=2001,
            message_text="Old completed message",
            status='completed',
            processed_at=old_time.replace(tzinfo=None)
        )
        recent_msg = MessageQueue(
            user_id=1002,
            chat_id=67890,
            message_id=2002,
            message_text="Recent completed message",
            status='completed', 
            processed_at=recent_time.replace(tzinfo=None)
        )
        
        test_session.add_all([old_msg, recent_msg])
        test_session.commit()
        
        # Clear with default 24 hour cutoff
        deleted_count = queue_processor.clear_completed_messages()
        
        # Should delete only the old message
        assert deleted_count == 1
        
        # Verify the recent message remains
        remaining_messages = test_session.query(MessageQueue).filter_by(status='completed').all()
        assert len(remaining_messages) == 1
        assert remaining_messages[0].user_id == 1002
    
    @pytest.mark.parametrize("hours_old,cutoff_hours,should_delete", [
        (23, 24, False),  # 23h old message with 24h cutoff - should keep
        (25, 24, True),   # 25h old message with 24h cutoff - should delete
        (1, 2, False),    # 1h old message with 2h cutoff - should keep
        (3, 2, True),     # 3h old message with 2h cutoff - should delete
        (0.5, 1, False),  # 30min old message with 1h cutoff - should keep
        (1.5, 1, True),   # 90min old message with 1h cutoff - should delete
    ])
    def test_clear_completed_messages_boundary_cases(self, queue_processor, test_session, hours_old, cutoff_hours, should_delete):
        """Test clear_completed_messages boundary cases with parametrized times"""
        now = datetime.now(UTC)
        message_time = now - timedelta(hours=hours_old)
        
        msg = MessageQueue(
            user_id=3000,
            chat_id=67890,
            message_id=4000,
            message_text=f"Message from {hours_old}h ago",
            status='completed',
            processed_at=message_time.replace(tzinfo=None)
        )
        test_session.add(msg)
        test_session.commit()
        
        deleted_count = queue_processor.clear_completed_messages(older_than_hours=cutoff_hours)
        
        if should_delete:
            assert deleted_count == 1
            remaining_count = test_session.query(MessageQueue).filter_by(status='completed').count()
            assert remaining_count == 0
        else:
            assert deleted_count == 0
            remaining_count = test_session.query(MessageQueue).filter_by(status='completed').count() 
            assert remaining_count == 1
    
    def test_clear_completed_messages_mixed_statuses(self, queue_processor, test_session):
        """Test that only completed messages are cleared, not other statuses"""
        now = datetime.now(UTC)
        old_time = now - timedelta(hours=25)  # Old enough to be cleared
        
        # Create messages with different statuses, all old enough to be cleared
        statuses = ['completed', 'pending', 'processing', 'failed']
        
        for i, status in enumerate(statuses):
            msg = MessageQueue(
                user_id=5000 + i,
                chat_id=67890,
                message_id=6000 + i,
                message_text=f"Mixed status test message {i}",
                status=status,
                processed_at=old_time.replace(tzinfo=None) if status == 'completed' else None
            )
            test_session.add(msg)
        
        test_session.commit()
        
        deleted_count = queue_processor.clear_completed_messages(older_than_hours=24)
        
        # Should only delete the completed message
        assert deleted_count == 1
        
        # Verify only completed message was deleted
        remaining_messages = test_session.query(MessageQueue).all()
        remaining_statuses = [msg.status for msg in remaining_messages]
        
        assert 'completed' not in remaining_statuses
        assert 'pending' in remaining_statuses
        assert 'processing' in remaining_statuses  
        assert 'failed' in remaining_statuses
    
    def test_clear_completed_messages_custom_timeframe(self, queue_processor, test_session):
        """Test clear_completed_messages with custom timeframe"""
        now = datetime.now(UTC)
        
        # Create messages at different ages
        messages_data = [
            (1, 1001, "1h old"),     # 1 hour old
            (6, 1002, "6h old"),     # 6 hours old  
            (12, 1003, "12h old"),   # 12 hours old
            (25, 1004, "25h old"),   # 25 hours old
        ]
        
        for hours_old, user_id, text in messages_data:
            message_time = now - timedelta(hours=hours_old)
            msg = MessageQueue(
                user_id=user_id,
                chat_id=67890,
                message_id=user_id,
                message_text=text,
                status='completed',
                processed_at=message_time.replace(tzinfo=None)
            )
            test_session.add(msg)
        
        test_session.commit()
        
        # Clear messages older than 8 hours
        deleted_count = queue_processor.clear_completed_messages(older_than_hours=8)
        
        # Should delete 12h and 25h old messages (2 messages)
        assert deleted_count == 2
        
        # Verify 1h and 6h old messages remain
        remaining_messages = test_session.query(MessageQueue).filter_by(status='completed').all()
        remaining_user_ids = [msg.user_id for msg in remaining_messages]
        
        assert 1001 in remaining_user_ids  # 1h old message
        assert 1002 in remaining_user_ids  # 6h old message
        assert 1003 not in remaining_user_ids  # 12h old message (deleted)
        assert 1004 not in remaining_user_ids  # 25h old message (deleted)
    
    def test_clear_completed_messages_no_completed_messages(self, queue_processor, test_session):
        """Test clear_completed_messages when there are no completed messages"""
        now = datetime.now(UTC)
        old_time = now - timedelta(hours=25)
        
        # Create non-completed messages
        msg = MessageQueue(
            user_id=7000,
            chat_id=67890,
            message_id=8000,
            message_text="Pending message",
            status='pending',
            processed_at=old_time.replace(tzinfo=None)
        )
        test_session.add(msg)
        test_session.commit()
        
        deleted_count = queue_processor.clear_completed_messages()
        
        # Should delete nothing
        assert deleted_count == 0
        
        # Original message should remain
        remaining_count = test_session.query(MessageQueue).count()
        assert remaining_count == 1