import pytest
from unittest.mock import MagicMock, patch, AsyncMock
import logging

from llm import Llm, create_llm, SpamCheckResponse
from config import Config


class TestLlmPromptAndExceptionMapping:
    """Test LLM prompt validation and exception mapping"""
    
    @pytest.fixture
    def mock_instructor_client(self):
        """Create a mock instructor client"""
        client = MagicMock()
        client.chat = MagicMock()
        client.chat.completions = MagicMock()
        client.chat.completions.create = MagicMock()  # NOT AsyncMock - instructor is sync
        return client
    
    @pytest.fixture
    def llm_instance(self, mock_instructor_client):
        """Create LLM instance with mock client"""
        return Llm(mock_instructor_client)
    
    @pytest.mark.asyncio
    async def test_is_spam_prompt_contains_message_tags(self, llm_instance, mock_instructor_client):
        """Test that is_spam generates correct prompt with <message> tags"""
        # Configure mock to return valid response
        mock_response = SpamCheckResponse(is_spam=False)
        mock_instructor_client.chat.completions.create.return_value = mock_response
        
        test_message = "Hello world, this is a test message"
        
        # Call is_spam
        result = await llm_instance.is_spam(test_message)
        
        # Verify the method was called
        mock_instructor_client.chat.completions.create.assert_called_once()
        
        # Extract the actual call arguments
        call_args = mock_instructor_client.chat.completions.create.call_args
        messages = call_args.kwargs['messages']
        
        assert len(messages) == 1
        assert messages[0]['role'] == 'user'
        
        content = messages[0]['content']
        
        # Verify prompt contains required elements
        assert "<message>" in content
        assert "</message>" in content
        assert test_message in content
        assert "Mobile dependency injection solutions" in content
        assert "Determine whether the following message is spam" in content
        
        # Verify result
        assert result is False
    
    @pytest.mark.asyncio
    async def test_is_spam_prompt_structure_with_various_messages(self, llm_instance, mock_instructor_client):
        """Test prompt structure with various message types"""
        mock_response = SpamCheckResponse(is_spam=True)
        mock_instructor_client.chat.completions.create.return_value = mock_response
        
        test_cases = [
            "Short message",
            "A very long message that contains multiple sentences and goes on for quite a while to test how the prompt handles longer content without issues",
            "Message with special characters: !@#$%^&*()_+-=[]{}|;':\",./<>?",
            "Message with\nnewlines\nand\ttabs",
            "",  # Empty message
            "🚀 Message with emojis 💯 and unicode characters 中文",
        ]
        
        for test_message in test_cases:
            mock_instructor_client.reset_mock()
            
            result = await llm_instance.is_spam(test_message)
            
            call_args = mock_instructor_client.chat.completions.create.call_args
            content = call_args.kwargs['messages'][0]['content']
            
            # Verify message is properly enclosed in tags
            message_start = content.find("<message>") + len("<message>")
            message_end = content.find("</message>")
            extracted_message = content[message_start:message_end]
            
            assert extracted_message == test_message
            assert result is True  # Mock always returns True
    
    @pytest.mark.asyncio
    async def test_is_spam_api_parameters(self, llm_instance, mock_instructor_client):
        """Test that correct API parameters are passed to Claude"""
        mock_response = SpamCheckResponse(is_spam=False)
        mock_instructor_client.chat.completions.create.return_value = mock_response
        
        await llm_instance.is_spam("test message")
        
        call_args = mock_instructor_client.chat.completions.create.call_args
        
        # Verify required parameters
        assert 'max_tokens' in call_args.kwargs
        assert call_args.kwargs['max_tokens'] == 1024
        
        assert 'messages' in call_args.kwargs
        assert isinstance(call_args.kwargs['messages'], list)
        
        assert 'response_model' in call_args.kwargs
        assert call_args.kwargs['response_model'] == SpamCheckResponse
    
    @pytest.mark.asyncio
    async def test_is_spam_returns_bool_values(self, llm_instance, mock_instructor_client):
        """Test that is_spam returns correct boolean values from response"""
        # Test spam detection
        spam_response = SpamCheckResponse(is_spam=True)
        mock_instructor_client.chat.completions.create.return_value = spam_response
        
        result = await llm_instance.is_spam("spam message")
        assert result is True
        assert isinstance(result, bool)
        
        # Test non-spam detection
        not_spam_response = SpamCheckResponse(is_spam=False)
        mock_instructor_client.chat.completions.create.return_value = not_spam_response
        
        result = await llm_instance.is_spam("legitimate message")
        assert result is False
        assert isinstance(result, bool)
    
    @pytest.mark.asyncio
    async def test_is_spam_exception_handling_unknown_error(self, llm_instance, mock_instructor_client):
        """Test that unknown exceptions are bubbled up as failed status"""
        # Configure mock to raise an unknown exception
        unknown_exception = RuntimeError("Unexpected API error")
        mock_instructor_client.chat.completions.create.side_effect = unknown_exception
        
        # Should re-raise the exception
        with pytest.raises(RuntimeError) as exc_info:
            await llm_instance.is_spam("test message")
        
        assert str(exc_info.value) == "Unexpected API error"
    
    @pytest.mark.asyncio
    async def test_is_spam_exception_handling_various_errors(self, llm_instance, mock_instructor_client):
        """Test various exception types are properly propagated"""
        test_exceptions = [
            ValueError("Invalid input"),
            ConnectionError("Network error"),
            TimeoutError("Request timeout"),
            KeyError("Missing key"),
            AttributeError("Missing attribute"),
        ]
        
        for exception in test_exceptions:
            mock_instructor_client.chat.completions.create.side_effect = exception
            
            with pytest.raises(type(exception)) as exc_info:
                await llm_instance.is_spam("test message")
            
            assert str(exc_info.value) == str(exception)
    
    @pytest.mark.asyncio
    async def test_is_spam_error_logging(self, llm_instance, mock_instructor_client, caplog):
        """Test error logging during exceptions"""
        test_error = RuntimeError("Test API error")
        mock_instructor_client.chat.completions.create.side_effect = test_error
        
        with caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError):
                await llm_instance.is_spam("test message")
        
        # Verify error was logged
        log_messages = [record.message for record in caplog.records]
        assert any("Error during spam check: Test API error" in msg for msg in log_messages)
    
    def test_create_llm_success(self):
        """Test successful LLM creation"""
        config = Config.for_testing()
        
        with patch('llm.instructor.from_provider') as mock_from_provider:
            mock_client = MagicMock()
            mock_from_provider.return_value = mock_client
            
            llm = create_llm(config)
            
            assert isinstance(llm, Llm)
            assert llm.client == mock_client
            
            # Verify correct provider and API key used
            mock_from_provider.assert_called_once_with(
                "anthropic/claude-3-haiku-20240307", 
                api_key=config.anthropic_api_key
            )
    
    def test_create_llm_failure(self):
        """Test LLM creation failure handling"""
        config = Config.for_testing()
        
        with patch('llm.instructor.from_provider') as mock_from_provider:
            mock_from_provider.side_effect = ValueError("Invalid API key")
            
            with pytest.raises(ValueError) as exc_info:
                create_llm(config)
            
            assert str(exc_info.value) == "Invalid API key"
    
    def test_create_llm_error_logging(self, caplog):
        """Test create_llm error logging"""
        config = Config.for_testing()
        
        with patch('llm.instructor.from_provider') as mock_from_provider:
            test_error = RuntimeError("Provider initialization failed")
            mock_from_provider.side_effect = test_error
            
            with caplog.at_level(logging.ERROR):
                with pytest.raises(RuntimeError):
                    create_llm(config)
            
            log_messages = [record.message for record in caplog.records]
            assert any("Error creating LLM instance: Provider initialization failed" in msg for msg in log_messages)
    
    def test_spam_check_response_model_immutable(self):
        """Test that SpamCheckResponse model is immutable"""
        response = SpamCheckResponse(is_spam=True)
        
        # Should not be able to modify after creation
        with pytest.raises(Exception):  # Pydantic ValidationError or similar
            response.is_spam = False
    
    def test_spam_check_response_model_validation(self):
        """Test SpamCheckResponse model validation"""
        # Valid creation
        response = SpamCheckResponse(is_spam=True)
        assert response.is_spam is True
        
        response = SpamCheckResponse(is_spam=False)
        assert response.is_spam is False
        
        # Invalid types should raise validation error
        with pytest.raises(Exception):  # Pydantic ValidationError
            SpamCheckResponse(is_spam="not a boolean")