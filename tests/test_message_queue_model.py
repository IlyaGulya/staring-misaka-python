import pytest
from datetime import datetime, timedelta, UTC
from db import MessageQueue, Base


class TestMessageQueueModel:
    def test_message_queue_creation(self, test_session):
        """Test creating a new MessageQueue entry"""
        queue_item = MessageQueue(
            user_id=12345,
            chat_id=67890,
            message_id=111,
            message_text="Test spam message",
            status='pending'
        )
        
        test_session.add(queue_item)
        test_session.commit()
        
        # Verify the item was created
        assert queue_item.id is not None
        assert queue_item.user_id == 12345
        assert queue_item.chat_id == 67890
        assert queue_item.message_id == 111
        assert queue_item.message_text == "Test spam message"
        assert queue_item.status == 'pending'
        assert queue_item.retry_count == 0
        assert queue_item.max_retries == 5
        assert queue_item.created_at is not None
        assert queue_item.processed_at is None
        assert queue_item.error_message is None
        assert queue_item.spam_result is None

    def test_message_queue_creation_with_defaults(self, test_session):
        """Test creating MessageQueue with default values"""
        queue_item = MessageQueue(
            user_id=123,
            chat_id=456,
            message_id=789,
            message_text="Test message"
        )
        
        test_session.add(queue_item)
        test_session.commit()
        
        # Verify defaults are applied correctly
        assert queue_item.status == 'pending'
        assert queue_item.retry_count == 0
        assert queue_item.max_retries == 5
        assert queue_item.next_retry_at is None
        assert isinstance(queue_item.created_at, datetime)

    def test_message_queue_status_updates(self, test_session, sample_message_queue):
        """Test updating message queue status"""
        # Update to processing
        sample_message_queue.status = 'processing'
        sample_message_queue.retry_count = 1
        test_session.commit()
        
        retrieved = test_session.query(MessageQueue).filter_by(id=sample_message_queue.id).first()
        assert retrieved.status == 'processing'
        assert retrieved.retry_count == 1

        # Update to completed
        sample_message_queue.status = 'completed'
        sample_message_queue.processed_at = datetime.now(UTC)
        sample_message_queue.spam_result = True
        test_session.commit()
        
        retrieved = test_session.query(MessageQueue).filter_by(id=sample_message_queue.id).first()
        assert retrieved.status == 'completed'
        assert retrieved.processed_at is not None
        assert retrieved.spam_result is True

    def test_message_queue_error_handling(self, test_session, sample_message_queue):
        """Test error handling in message queue"""
        error_message = "API rate limit exceeded"
        next_retry = datetime.now(UTC) + timedelta(minutes=5)
        
        sample_message_queue.status = 'failed'
        sample_message_queue.retry_count = 2
        sample_message_queue.error_message = error_message
        sample_message_queue.next_retry_at = next_retry
        test_session.commit()
        
        retrieved = test_session.query(MessageQueue).filter_by(id=sample_message_queue.id).first()
        assert retrieved.status == 'failed'
        assert retrieved.retry_count == 2
        assert retrieved.error_message == error_message
        # Compare timestamps without timezone info since SQLite stores naive datetime
        assert retrieved.next_retry_at.replace(tzinfo=UTC) == next_retry

    def test_message_queue_query_by_status(self, test_session):
        """Test querying messages by status"""
        # Create messages with different statuses
        pending_msg = MessageQueue(
            user_id=1, chat_id=1, message_id=1, 
            message_text="Pending", status='pending'
        )
        processing_msg = MessageQueue(
            user_id=2, chat_id=1, message_id=2, 
            message_text="Processing", status='processing'
        )
        completed_msg = MessageQueue(
            user_id=3, chat_id=1, message_id=3, 
            message_text="Completed", status='completed'
        )
        failed_msg = MessageQueue(
            user_id=4, chat_id=1, message_id=4, 
            message_text="Failed", status='failed'
        )
        
        test_session.add_all([pending_msg, processing_msg, completed_msg, failed_msg])
        test_session.commit()
        
        # Query by status
        pending_count = test_session.query(MessageQueue).filter_by(status='pending').count()
        processing_count = test_session.query(MessageQueue).filter_by(status='processing').count()
        completed_count = test_session.query(MessageQueue).filter_by(status='completed').count()
        failed_count = test_session.query(MessageQueue).filter_by(status='failed').count()
        
        assert pending_count == 1
        assert processing_count == 1
        assert completed_count == 1
        assert failed_count == 1

    def test_message_queue_ready_for_retry(self, test_session):
        """Test querying messages ready for retry"""
        now = datetime.now(UTC)
        past_time = now - timedelta(minutes=10)
        future_time = now + timedelta(minutes=10)
        
        # Message ready for retry (past retry time)
        ready_msg = MessageQueue(
            user_id=1, chat_id=1, message_id=1,
            message_text="Ready", status='failed',
            retry_count=1, next_retry_at=past_time
        )
        
        # Message not ready for retry (future retry time)
        not_ready_msg = MessageQueue(
            user_id=2, chat_id=1, message_id=2,
            message_text="Not ready", status='failed',
            retry_count=1, next_retry_at=future_time
        )
        
        # Message exceeded max retries
        exceeded_msg = MessageQueue(
            user_id=3, chat_id=1, message_id=3,
            message_text="Exceeded", status='failed',
            retry_count=5, max_retries=5, next_retry_at=past_time
        )
        
        test_session.add_all([ready_msg, not_ready_msg, exceeded_msg])
        test_session.commit()
        
        # Query messages ready for processing
        ready_messages = test_session.query(MessageQueue).filter(
            ((MessageQueue.status == 'pending') | 
             ((MessageQueue.status == 'failed') & (MessageQueue.next_retry_at <= now))),
            MessageQueue.retry_count < MessageQueue.max_retries
        ).all()
        
        assert len(ready_messages) == 1
        assert ready_messages[0].user_id == 1

    def test_message_queue_repr(self, test_session, sample_message_queue):
        """Test string representation includes key identifiers"""
        repr_str = repr(sample_message_queue)
        assert "MessageQueue(" in repr_str